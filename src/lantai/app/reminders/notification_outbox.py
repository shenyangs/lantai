"""Durable notification effects; RuntimeRunStore remains execution authority."""
from __future__ import annotations

import asyncio
import os
import secrets
from datetime import timedelta
from typing import Any
from uuid import uuid4

import psutil

from .models import ReminderDeliveryClaim, ReminderItem, ReminderTriggerResult, utc_now
from .repo import ReminderRepository
from .runtime_runs import record_reminder_runtime_run_settled

_OWNERS: dict[str, asyncio.Task | None] = {}
_BIRTH = psutil.Process(os.getpid()).create_time()


def register_owner() -> dict[str, Any]:
    owner = uuid4().hex
    _OWNERS[owner] = asyncio.current_task()
    return {"owner_id": owner, "owner_pid": os.getpid(), "owner_birth": _BIRTH}


def release_owner(owner: str) -> None:
    _OWNERS.pop(owner, None)


def retain_owner(owner: str) -> None:
    # A terminal write failure retains its owner until this worker retries it.
    _OWNERS[owner] = None


def owner_is_alive(claim: ReminderDeliveryClaim) -> bool:
    if claim.owner_pid == os.getpid() and claim.owner_birth == _BIRTH:
        if claim.owner_id not in _OWNERS:
            return False
        task = _OWNERS[claim.owner_id]
        return task is None or not task.done()
    try:
        process = psutil.Process(claim.owner_pid)
        return process.create_time() == claim.owner_birth and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True


async def complete_task(task: asyncio.Task):
    """Observe the actual operation through repeated caller cancellation."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def receipt_identity(claim: ReminderDeliveryClaim) -> dict[str, str]:
    return {"run_id": claim.run_id, "attempt_id": claim.attempt_id,
            "agent_id": claim.agent_id, "reminder_id": claim.reminder.id,
            "receipt_token": claim.receipt_token}


def confirmed_receipt(receipt: object, claim: ReminderDeliveryClaim) -> bool:
    return (isinstance(receipt, dict) and receipt.get("ok") is True
            and receipt.get("shown") is True and receipt.get("status") == "shown"
            and receipt.get("id") == claim.run_id
            and all(receipt.get(key) == value for key, value in receipt_identity(claim).items())
            and receipt.get("caller_id") == claim.caller_id and bool(claim.caller_id))


def outcome_may_have_reached_desktop(claim: ReminderDeliveryClaim) -> bool:
    return claim.status in {"attempting", "unknown"} and not (
        claim.native_ready and not claim.caller_id
    )


class ReminderNotificationOutbox:
    def __init__(self, repo: ReminderRepository, agent_id: str):
        self.repo, self.agent_id = repo, agent_id

    async def get(self, run_id: str) -> ReminderDeliveryClaim:
        claim = (await self.repo.load()).delivery_claims.get(run_id)
        if claim is None or claim.agent_id != self.agent_id:
            raise ValueError("Notification claim not found for this agent")
        return claim

    async def mutate(self, run_id: str, owner_id: str, **changes) -> ReminderDeliveryClaim:
        updated = None

        def update(data):
            nonlocal updated
            claim = data.delivery_claims.get(run_id)
            if claim is None or claim.agent_id != self.agent_id or claim.owner_id != owner_id:
                raise ValueError("Notification owner identity conflict")
            updated = claim.model_copy(update=changes)
            return data.model_copy(update={"delivery_claims": {**data.delivery_claims, run_id: updated}})

        await self.repo.update(update)
        assert updated is not None
        return updated

    async def settle(self, claim: ReminderDeliveryClaim, status: str, error: str = "") -> None:
        # A failed RuntimeRun write cannot release the effect owner or erase its receipt.
        await record_reminder_runtime_run_settled(
            workspace_dir=self.repo.workspace_dir, agent_id=self.agent_id,
            reminder=claim.reminder, run_id=claim.run_id, final_status=status, error_text=error,
        )
        effect_status = ("submitted" if status == "succeeded" else
                         "unknown" if status == "stalled" else status)
        await self.mutate(claim.run_id, claim.owner_id, settled=True, status=effect_status, error=error)

    async def finish_owner(self, owner_id: str) -> None:
        data = await self.repo.load()
        for claim in data.delivery_claims.values():
            if claim.agent_id != self.agent_id or claim.owner_id != owner_id or claim.settled:
                continue
            if confirmed_receipt(claim.receipt, claim):
                await self.settle(claim, "succeeded")
            else:
                unknown = outcome_may_have_reached_desktop(claim)
                await self.settle(claim, "stalled" if unknown else "failed" if claim.status == "failed" else "cancelled",
                                  "Notification outcome is unknown" if unknown else "Delivery stopped before confirmation")

    async def recover(self) -> None:
        data = await self.repo.load()
        for claim in data.delivery_claims.values():
            if claim.agent_id != self.agent_id or claim.settled or owner_is_alive(claim):
                continue
            if confirmed_receipt(claim.receipt, claim):
                await self.settle(claim, "succeeded")
            else:
                unknown = outcome_may_have_reached_desktop(claim)
                await self.settle(claim, "stalled" if unknown else "failed",
                                  "Original notification owner exited before settlement")

    async def claim_native(self, caller_id: str) -> dict | None:
        if not caller_id.strip() or len(caller_id) > 128:
            raise ValueError("A desktop caller identity is required")
        selected = None

        def update(data):
            nonlocal selected
            if data.settings.native_notification_policy != "explicit_only":
                return data
            claims = dict(data.delivery_claims)
            ready_until = utc_now() + timedelta(seconds=5)
            for claim in data.delivery_claims.values():
                if (claim.agent_id != self.agent_id or claim.settled or claim.status != "attempting"
                        or not claim.native_ready or claim.caller_id or not owner_is_alive(claim)):
                    continue
                current = next((item for item in data.reminders if item.id == claim.reminder.id), None)
                if (current is None or current.status in {"done", "dismissed"}
                        or current.due_at != claim.reminder.due_at
                        or current.snooze_until != claim.reminder.snooze_until):
                    claims[claim.run_id] = claim.model_copy(update={
                        "status": "failed", "receipt": {
                            **receipt_identity(claim), "caller_id": "", "id": claim.run_id,
                            "ok": False, "shown": False, "status": "failed",
                            "reason": "reminder_changed_before_submission",
                        },
                    })
                    continue
                updated = claim.model_copy(update={"caller_id": caller_id, "status": "unknown"})
                claims[claim.run_id] = updated
                selected = {**receipt_identity(updated), "caller_id": caller_id,
                            "title": claim.reminder.title, "body": claim.reminder.summary or claim.reminder.reason}
                break
            return data.model_copy(update={"delivery_claims": claims,
                "native_adapter_agent_id": self.agent_id, "native_adapter_ready_until": ready_until})

        await self.repo.update(update)
        return selected

    async def resolve_native(self, receipt: dict) -> None:
        def update(data):
            run_id = receipt.get("run_id")
            claim = data.delivery_claims.get(run_id)
            if (claim is None or claim.agent_id != self.agent_id
                    or not claim.caller_id
                    or receipt.get("caller_id") != claim.caller_id
                    or not all(receipt.get(key) == value for key, value in receipt_identity(claim).items())
                    or not secrets.compare_digest(str(receipt.get("receipt_token", "")), claim.receipt_token)):
                raise ValueError("Notification receipt identity or owner conflict")
            if claim.receipt is not None:
                if claim.receipt == receipt:
                    return data
                raise ValueError("Notification receipt already resolved")
            if claim.settled or claim.status != "unknown":
                raise ValueError("Notification effect is not awaiting this receipt")
            if receipt.get("shown") is True and not confirmed_receipt(receipt, claim):
                raise ValueError("Positive notification receipt was not confirmed for the original request")
            status = "submitted" if confirmed_receipt(receipt, claim) else (
                "unknown" if receipt.get("status") == "unknown" else "failed")
            updated = claim.model_copy(update={"receipt": receipt, "status": status})
            return data.model_copy(update={"delivery_claims": {**data.delivery_claims, run_id: updated}})

        await self.repo.update(update)


class NativeReminderNotifier:
    """Wait for the authenticated desktop adapter's bound native show receipt."""
    def __init__(self, outbox: ReminderNotificationOutbox, timeout_seconds: float = 8):
        self.outbox, self.timeout_seconds = outbox, timeout_seconds

    async def __call__(self, reminder: ReminderItem, trigger: ReminderTriggerResult) -> dict:
        assert trigger.run_id is not None
        claim = await self.outbox.get(trigger.run_id)
        await self.outbox.mutate(claim.run_id, claim.owner_id, native_ready=True)

        async def wait_receipt():
            while True:
                current = await self.outbox.get(claim.run_id)
                if current.receipt is not None:
                    return current.receipt
                await asyncio.sleep(.05)

        try:
            return await asyncio.wait_for(wait_receipt(), self.timeout_seconds)
        except asyncio.TimeoutError:
            current = await self.outbox.get(claim.run_id)
            if current.receipt is not None:
                return current.receipt
            submitted = bool(current.caller_id)
            return {**receipt_identity(current), "caller_id": current.caller_id,
                    "ok": False, "shown": False, "status": "unknown" if submitted else "failed",
                    "reason": "desktop_receipt_timeout" if submitted else "desktop_not_submitted"}
