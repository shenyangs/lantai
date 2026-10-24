# -*- coding: utf-8 -*-
"""Native-notification outbox with explicit unknown-effect handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from .models import BriefDeliveryReceipt, BriefEdition, BriefSubscription, StrictModel
from .service import BriefConflict, BriefNotFound, DailyBriefService


class NativeBriefNotification(StrictModel):
    receipt_id: str
    edition_id: str
    edition_revision: int
    title: str = "澜台简报"
    body: str = "有一份新的简报可以阅读"
    open_route: str = "/daily-brief"


class DeliveryAdapterResult(StrictModel):
    state: Literal["submitted", "acknowledged", "failed"]
    adapter_message_id: str | None = None
    error: str = ""


class NativeBriefNotificationAdapter(Protocol):
    async def submit(
        self,
        notification: NativeBriefNotification,
    ) -> DeliveryAdapterResult: ...


@dataclass(frozen=True)
class DisabledNativeBriefNotificationAdapter:
    reason: str = "native notification adapter is not configured"


class DeliveryRunSummary(StrictModel):
    submitted: int = 0
    acknowledged: int = 0
    failed: int = 0
    unknown: int = 0
    suppressed: int = 0
    deferred_for_quiet_hours: int = 0


def _inside_quiet_hours(
    subscription: BriefSubscription,
    *,
    now: datetime,
) -> bool:
    start = subscription.quiet_hours_start
    end = subscription.quiet_hours_end
    if start is None or end is None or start == end:
        return False
    local_clock: time = now.astimezone(ZoneInfo(subscription.timezone)).timetz().replace(
        tzinfo=None
    )
    if start < end:
        return start <= local_clock < end
    return local_clock >= start or local_clock < end


class BriefDeliveryWorker:
    """Processes pending native outbox rows without treating noop as success."""

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        adapter: NativeBriefNotificationAdapter
        | DisabledNativeBriefNotificationAdapter
        | None = None,
    ) -> None:
        self._service = DailyBriefService(workspace_dir)
        self._adapter = adapter or DisabledNativeBriefNotificationAdapter()

    async def run_once(self, *, now: datetime | None = None) -> DeliveryRunSummary:
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("delivery time must be timezone-aware")
        pending = await self._service.list_pending_native_deliveries()
        workspace = await self._service.get_workspace_snapshot()
        subscriptions = {item.id: item for item in workspace.subscriptions}
        totals = {
            "submitted": 0,
            "acknowledged": 0,
            "failed": 0,
            "unknown": 0,
            "suppressed": 0,
            "deferred_for_quiet_hours": 0,
        }

        for receipt in pending:
            subscription = subscriptions.get(receipt.subscription_id)
            if subscription and _inside_quiet_hours(subscription, now=instant):
                totals["deferred_for_quiet_hours"] += 1
                continue
            if isinstance(self._adapter, DisabledNativeBriefNotificationAdapter):
                await self._service.suppress_delivery(receipt.id, self._adapter.reason)
                totals["suppressed"] += 1
                continue

            try:
                claimed = await self._service.claim_native_delivery(receipt.id)
            except BriefConflict:
                continue
            if claimed.state == "suppressed":
                totals["suppressed"] += 1
                continue
            # Authority and the exact edition revision were revalidated in the
            # same durable transition that opened the unknown-effect window.
            # Only read the user-visible payload after that commit-time fence.
            try:
                edition = await self._service.get_edition(claimed.edition_id)
            except BriefNotFound:
                # A grant/topic revocation can withdraw the edition after the
                # claim commit. The receipt is already in the conservative
                # unknown window, so never cross the adapter boundary or retry.
                totals["unknown"] += 1
                continue

            notification = self._notification_for(edition, claimed)
            try:
                result = await self._adapter.submit(notification)
            except Exception:
                # claim_native_delivery persisted "unknown" before crossing the
                # adapter boundary.  It is deliberately not auto-retried.
                totals["unknown"] += 1
                continue
            resolved = await self._service.resolve_delivery(
                claimed.id,
                state=result.state,
                adapter_message_id=result.adapter_message_id,
                error=result.error,
            )
            totals[resolved.state] += 1

        return DeliveryRunSummary(**totals)

    @staticmethod
    def _notification_for(
        edition: BriefEdition,
        receipt: BriefDeliveryReceipt,
    ) -> NativeBriefNotification:
        return NativeBriefNotification(
            receipt_id=receipt.id,
            edition_id=edition.id,
            edition_revision=edition.revision,
            body=(
                "本次已检查，没有值得新增的发现"
                if edition.outcome == "no_material_update"
                else "有一份新的简报可以阅读"
            ),
        )


class NativeBriefDeliveryDispatcher:
    """Claims one authorized outbox row for the trusted desktop renderer.

    Claiming durably enters the unknown-effect window before the payload leaves
    the backend. The renderer must later resolve it as submitted or failed;
    opening the exact receipt is a separate read transition.
    """

    def __init__(self, workspace_dir: str | Path) -> None:
        self._service = DailyBriefService(workspace_dir)

    async def claim_next(
        self,
        *,
        now: datetime | None = None,
    ) -> NativeBriefNotification | None:
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("delivery time must be timezone-aware")
        pending = await self._service.list_pending_native_deliveries()
        workspace = await self._service.get_workspace_snapshot()
        subscriptions = {item.id: item for item in workspace.subscriptions}
        for receipt in pending:
            subscription = subscriptions.get(receipt.subscription_id)
            if subscription is None or _inside_quiet_hours(subscription, now=instant):
                continue
            try:
                claimed = await self._service.claim_native_delivery(receipt.id)
            except BriefConflict:
                continue
            if claimed.state != "unknown":
                continue
            try:
                edition = await self._service.get_edition(claimed.edition_id)
            except BriefNotFound:
                # The durable unknown state prevents unsafe automatic retry.
                continue
            return BriefDeliveryWorker._notification_for(edition, claimed)
        return None
