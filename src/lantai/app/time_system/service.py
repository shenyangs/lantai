# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..reminders.service import ReminderService
from .models import TimeItem, TodayTimeline, utc_now


TimeItemProvider = Callable[[], Awaitable[list[TimeItem]]]


async def _empty_provider() -> list[TimeItem]:
    return []


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class TimeSystemService:
    def __init__(
        self,
        *,
        reminder_service: ReminderService,
        timezone_name: str = "Asia/Shanghai",
        automation_provider: TimeItemProvider | None = None,
        task_provider: TimeItemProvider | None = None,
    ):
        self._reminders = reminder_service
        self._timezone_name = timezone_name
        self._automation_provider = automation_provider or _empty_provider
        self._task_provider = task_provider or _empty_provider

    @property
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._timezone_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    async def today(self, *, now: datetime | None = None) -> TodayTimeline:
        generated_at = _as_aware(now or utc_now())
        local_now = generated_at.astimezone(self.timezone)
        day_start = datetime.combine(
            local_now.date(),
            time.min,
            tzinfo=self.timezone,
        )
        day_end = datetime.combine(
            local_now.date(),
            time.max,
            tzinfo=self.timezone,
        )
        items: list[TimeItem] = []

        reminders = await self._reminders.list_reminders()
        for reminder in reminders:
            if reminder.status in {"dismissed", "done"}:
                continue
            due_at = reminder.snooze_until if reminder.status == "snoozed" else reminder.due_at
            if due_at is None:
                continue
            due_at = _as_aware(due_at)
            if due_at > day_end:
                continue
            items.append(
                TimeItem(
                    id=f"reminder:{reminder.id}",
                    kind="reminder",
                    source="lantai",
                    source_label="提醒",
                    title=reminder.title,
                    summary=reminder.summary,
                    start_at=due_at,
                    blocking=False,
                    status=reminder.status,
                    action_path="/reminders",
                    metadata={
                        "priority": reminder.priority,
                        "overdue": due_at < generated_at,
                    },
                )
            )

        for projected in [
            *await self._task_provider(),
            *await self._automation_provider(),
        ]:
            if projected.start_at is None:
                continue
            at = _as_aware(projected.start_at).astimezone(self.timezone)
            if day_start <= at <= day_end:
                items.append(projected)

        def sort_key(item: TimeItem) -> tuple[int, datetime, str]:
            value = _as_aware(item.start_at or day_start).astimezone(self.timezone)
            return (0 if item.all_day else 1, value, item.title)

        items.sort(key=sort_key)
        counts = {
            kind: len([item for item in items if item.kind == kind])
            for kind in ("reminder", "task", "automation")
        }
        return TodayTimeline(
            date=local_now.date().isoformat(),
            timezone=self._timezone_name,
            generated_at=generated_at,
            items=items,
            counts=counts,
        )
