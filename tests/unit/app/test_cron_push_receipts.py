from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lantai.app.crons.executor import CronExecutor
from lantai.app.crons.models import (
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    ScheduleSpec,
)


@pytest.mark.asyncio
async def test_text_cron_attaches_source_metadata_to_delivery():
    channel_manager = SimpleNamespace(send_text=AsyncMock())
    executor = CronExecutor(
        runner=SimpleNamespace(),
        channel_manager=channel_manager,
    )
    job = CronJobSpec(
        id="job-source",
        name="Daily review",
        schedule=ScheduleSpec(cron="0 9 * * *"),
        task_type="text",
        text="Review complete",
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-1", session_id="session-1"),
            meta={"existing": "value"},
        ),
    )

    await executor.execute(job)

    channel_manager.send_text.assert_awaited_once_with(
        channel="console",
        user_id="user-1",
        session_id="session-1",
        text="Review complete",
        meta={
            "existing": "value",
            "push_source_kind": "cron_job",
            "push_source_id": "job-source",
            "push_source_name": "Daily review",
        },
    )
