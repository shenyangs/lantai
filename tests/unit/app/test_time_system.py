# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lantai.app.reminders.models import ReminderCreateRequest
from lantai.app.reminders.repo import ReminderRepository
from lantai.app.reminders.service import ReminderService
from lantai.app.time_system.models import TimeItem
from lantai.app.time_system.service import TimeSystemService


@pytest.mark.asyncio
async def test_today_timeline_projects_supported_sources_and_sorts_by_local_time(
    tmp_path: Path,
):
    reminder_service = ReminderService(ReminderRepository(tmp_path))
    await reminder_service.create_reminder(
        ReminderCreateRequest(
            title="提交方案",
            due_at=datetime(2026, 7, 27, 9, 30, tzinfo=timezone.utc),
        )
    )

    async def automation_provider() -> list[TimeItem]:
        return [
            TimeItem(
                id="cron:daily",
                kind="automation",
                source="lantai",
                source_label="自动化",
                title="日报",
                start_at=datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc),
                status="scheduled",
            )
        ]

    async def task_provider() -> list[TimeItem]:
        return [
            TimeItem(
                id="task:running",
                kind="task",
                source="lantai",
                source_label="任务",
                title="整理发布证据",
                start_at=datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc),
                status="running",
            ),
            TimeItem(
                id="task:tomorrow",
                kind="task",
                source="lantai",
                source_label="任务",
                title="明日任务",
                start_at=datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc),
                status="pending",
            ),
        ]

    service = TimeSystemService(
        reminder_service=reminder_service,
        timezone_name="Asia/Shanghai",
        automation_provider=automation_provider,
        task_provider=task_provider,
    )

    timeline = await service.today(
        now=datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)
    )

    assert timeline.date == "2026-07-27"
    assert [item.title for item in timeline.items] == [
        "整理发布证据",
        "提交方案",
        "日报",
    ]
    assert timeline.counts == {
        "reminder": 1,
        "task": 1,
        "automation": 1,
    }
    assert all(item.title != "明日任务" for item in timeline.items)


@pytest.mark.asyncio
async def test_today_timeline_falls_back_to_utc_for_an_unknown_timezone(
    tmp_path: Path,
):
    service = TimeSystemService(
        reminder_service=ReminderService(ReminderRepository(tmp_path)),
        timezone_name="Unknown/Timezone",
    )

    timeline = await service.today(
        now=datetime(2026, 7, 27, 23, 30, tzinfo=timezone.utc)
    )

    assert timeline.date == "2026-07-27"
    assert timeline.items == []
    assert timeline.counts == {"reminder": 0, "task": 0, "automation": 0}
