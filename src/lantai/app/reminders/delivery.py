"""Reminder delivery with durable effect ownership and explicit receipts."""
from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .models import ReminderDeliveryResult, ReminderDeliveryTickResult, ReminderItem, ReminderTriggerResult, utc_now
from .notification_outbox import (ReminderNotificationOutbox, complete_task, confirmed_receipt,
                                  receipt_identity, register_owner, release_owner, retain_owner)
from .service import DEFAULT_REMINDER_DEDUPE_WINDOW, ReminderService

ReminderNotifier = Callable[[ReminderItem, ReminderTriggerResult], Awaitable[object] | object]
ReminderTickObserver = Callable[[ReminderDeliveryTickResult], Awaitable[None] | None]
logger = logging.getLogger(__name__)


async def _owned_operation(awaitable):
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await complete_task(task)
        raise


class ReminderDeliveryWorker:
    def __init__(self, service: ReminderService, *, agent_id: str, notifier: ReminderNotifier,
                 dedupe_window: timedelta = DEFAULT_REMINDER_DEDUPE_WINDOW, limit: int = 50,
                 interval_seconds: float = 60.0, now_provider: Callable[[], datetime] = utc_now,
                 tick_observer: ReminderTickObserver | None = None, injected_test_mode: bool = False,
                 native_only: bool = False) -> None:
        if interval_seconds <= 0 or limit < 1:
            raise ValueError("A positive interval and limit are required")
        if notifier is None:
            raise ValueError("notification adapter is required")
        self._service, self._agent_id, self._notifier = service, agent_id, notifier
        self._dedupe_window, self._limit = dedupe_window, limit
        self._interval_seconds, self._now_provider = interval_seconds, now_provider
        self._tick_observer = tick_observer
        self._injected_test_mode, self._native_only = injected_test_mode, native_only
        self._outbox = ReminderNotificationOutbox(service.repository, agent_id)
        self._run_lock = asyncio.Lock()
        self._pending_owner: str | None = None
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _finish_owner(self, owner: str) -> None:
        task = asyncio.create_task(self._outbox.finish_owner(owner))
        try:
            await complete_task(task)
        except BaseException:
            retain_owner(owner)
            self._pending_owner = owner
            raise
        else:
            release_owner(owner)
            if self._pending_owner == owner:
                self._pending_owner = None

    async def run_once(self, *, now: datetime | None = None) -> ReminderDeliveryTickResult:
        async with self._run_lock:
            if self._pending_owner is not None:
                await self._finish_owner(self._pending_owner)
            await _owned_operation(self._outbox.recover())
            checked_at = now or self._now_provider()
            checked_at = (checked_at.replace(tzinfo=timezone.utc) if checked_at.tzinfo is None
                          else checked_at.astimezone(timezone.utc))
            owner = register_owner()
            results: list[ReminderDeliveryResult] = []
            triggers: list[ReminderTriggerResult] = []
            claim_task = asyncio.create_task(self._service.claim_due_reminders(
                agent_id=self._agent_id, now=checked_at, dedupe_window=self._dedupe_window,
                limit=self._limit, delivery_owner=owner, native_only=self._native_only))
            try:
                try:
                    triggers = await asyncio.shield(claim_task)
                except asyncio.CancelledError:
                    # Includes the background file write and every run creation.
                    triggers = await complete_task(claim_task)
                    raise
                reminders = {item.id: item for item in await self._service.list_reminders()}
                for trigger in triggers:
                    status, reason, error = "failed", "notification_delivery_failed", ""
                    if trigger.status == "deduped":
                        status, reason = "deduped", trigger.reason or "dedupe_window_active"
                    elif trigger.run_id is not None:
                        claim = await self._outbox.get(trigger.run_id)
                        reminder = reminders.get(trigger.reminder_id)
                        if reminder is None:
                            reason, error = "missing_claimed_reminder", "Claimed reminder was not readable after claim."
                            await _owned_operation(self._outbox.settle(claim, "failed", error))
                        else:
                            claim = await _owned_operation(self._outbox.mutate(
                                claim.run_id, claim.owner_id, status="attempting", native_ready=self._native_only))
                            try:
                                receipt = self._notifier(reminder, trigger)
                                if inspect.isawaitable(receipt):
                                    receipt = await receipt
                            except Exception as exc:
                                error = str(exc)
                                claim = await _owned_operation(self._outbox.mutate(
                                    claim.run_id, claim.owner_id, status="failed", error=error))
                                await _owned_operation(self._outbox.settle(claim, "failed", error))
                            else:
                                claim = await self._outbox.get(claim.run_id)
                                if self._injected_test_mode and receipt is None:
                                    # Explicit synthetic adapters preserve fixture/smoke behavior only.
                                    claim = await _owned_operation(self._outbox.mutate(
                                        claim.run_id, claim.owner_id, caller_id="injected-test-adapter"))
                                    receipt = {**receipt_identity(claim), "caller_id": claim.caller_id,
                                               "id": claim.run_id, "ok": True, "shown": True, "status": "shown"}
                                if confirmed_receipt(receipt, claim):
                                    claim = await _owned_operation(self._outbox.mutate(
                                        claim.run_id, claim.owner_id, receipt=receipt, status="submitted"))
                                    await _owned_operation(self._outbox.settle(claim, "succeeded"))
                                    status, reason = "delivered", "notification_delivered"
                                else:
                                    unknown = isinstance(receipt, dict) and (
                                        receipt.get("status") == "unknown" or receipt.get("reason") in
                                        {"timeout", "duplicate", "desktop_receipt_timeout"})
                                    error = (str(receipt.get("error") or receipt.get("reason") or "Notification was not confirmed")
                                             if isinstance(receipt, dict) else "Notification adapter returned no confirmation")
                                    claim = await _owned_operation(self._outbox.mutate(
                                        claim.run_id, claim.owner_id, status="unknown" if unknown else "failed", error=error))
                                    await _owned_operation(self._outbox.settle(claim, "stalled" if unknown else "failed", error))
                    results.append(ReminderDeliveryResult(reminder_id=trigger.reminder_id, status=status,
                        run_id=trigger.run_id, reason=reason, error=error, triggered_at=trigger.triggered_at,
                        dedupe_until=trigger.dedupe_until))
            finally:
                # Every claim, including unattempted batch members, keeps its owner through settlement.
                await self._finish_owner(owner["owner_id"])
            tick = ReminderDeliveryTickResult(checked_at=checked_at, agent_id=self._agent_id,
                claimed_count=sum(t.status == "claimed" for t in triggers),
                delivered_count=sum(r.status == "delivered" for r in results),
                deduped_count=sum(r.status == "deduped" for r in results),
                failed_count=sum(r.status == "failed" for r in results), results=results)
            if self._tick_observer is not None:
                observed = self._tick_observer(tick)
                if inspect.isawaitable(observed):
                    await observed
            return tick

    def start(self) -> None:
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name=f"reminder-delivery-worker:{self._agent_id}")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            if self._pending_owner is not None:
                await self._finish_owner(self._pending_owner)
            return
        self._stop_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await complete_task(task)
        if self._pending_owner is not None:
            await self._finish_owner(self._pending_owner)
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("Reminder delivery failed; durable claims remain owned for recovery")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                continue
