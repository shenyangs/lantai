# -*- coding: utf-8 -*-
"""Occurrence decisions for the controlled daily-brief runner.

This module never runs research itself.  It turns an actual scheduler tick into
at most one canonical cycle for the subscription's current local date.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import BriefCycle, BriefSubscription, StrictModel
from .repo import DailyBriefRepository
from .service import DailyBriefService, DailyBriefError


class BriefScheduleDecision(StrictModel):
    subscription_id: str
    due: bool
    reason: str
    local_date: str
    occurrence_id: str
    observed_at: datetime


def decide_subscription_tick(
    subscription: BriefSubscription,
    *,
    now: datetime,
) -> BriefScheduleDecision:
    if now.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    local_now = now.astimezone(ZoneInfo(subscription.timezone))
    local_date = local_now.date()
    occurrence_id = (
        f"brief:{subscription.id}:schedule-{subscription.schedule_revision}:"
        f"{local_date.isoformat()}"
    )
    if subscription.status != "active":
        reason = "subscription_not_active"
        due = False
    elif local_now.timetz().replace(tzinfo=None) < subscription.local_time:
        reason = "before_scheduled_local_time"
        due = False
    elif subscription.last_successful_local_date == local_date:
        reason = "local_date_already_checked"
        due = False
    else:
        reason = "due_now"
        due = True
    return BriefScheduleDecision(
        subscription_id=subscription.id,
        due=due,
        reason=reason,
        local_date=local_date.isoformat(),
        occurrence_id=occurrence_id,
        observed_at=now.astimezone(timezone.utc),
    )


class DailyBriefScheduler:
    """Claims due cycles only when an approved controlled task calls tick()."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self._repo = DailyBriefRepository(workspace_dir)
        self._service = DailyBriefService(workspace_dir)

    async def tick(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[BriefCycle]:
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("scheduler time must be timezone-aware")
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least one")
        store = await self._repo.load()
        if not store.feature_flags.research:
            return []

        claimed: list[BriefCycle] = []
        for subscription in store.subscriptions:
            if limit is not None and len(claimed) >= limit:
                break
            decision = decide_subscription_tick(subscription, now=instant)
            if not decision.due:
                continue
            logical_key = (
                f"subscription:{subscription.id}:{decision.local_date}"
            )
            # A failed/partial/running cycle remains observable and requires an
            # explicit recovery decision; ordinary ticks never create a retry
            # loop or charge again.
            if any(cycle.logical_key == logical_key for cycle in store.cycles):
                continue
            try:
                cycle = await self._service.claim_occurrence(
                    subscription_id=subscription.id,
                    occurrence_id=decision.occurrence_id,
                    local_date=datetime.fromisoformat(decision.local_date).date(),
                )
            except DailyBriefError:
                # Each authority failure belongs to this subscription. Another
                # workspace process may also have won the same occurrence CAS.
                continue
            claimed.append(cycle)
            store = await self._repo.load()
        return claimed
