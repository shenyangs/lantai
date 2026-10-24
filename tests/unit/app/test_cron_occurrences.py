# -*- coding: utf-8 -*-
"""Authoritative cron occurrence projection tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.crons.api import get_cron_manager, router
from lantai.app.crons.manager import CronManager
from lantai.app.crons.models import (
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    ScheduleSpec,
)
from lantai.app.crons.repo.json_repo import JsonJobRepository


def _make_job(
    job_id: str,
    *,
    cron: str,
    schedule_timezone: str,
    enabled: bool = True,
) -> CronJobSpec:
    return CronJobSpec(
        id=job_id,
        name=job_id,
        enabled=enabled,
        schedule=ScheduleSpec(cron=cron, timezone=schedule_timezone),
        task_type="text",
        text="run",
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(
                user_id="default",
                session_id="console:main",
            ),
        ),
    )


def _make_manager(tmp_path: Path) -> CronManager:
    return CronManager(
        repo=JsonJobRepository(tmp_path / "jobs.json"),
        runner=SimpleNamespace(workspace_dir=tmp_path),
        channel_manager=SimpleNamespace(),
        timezone="UTC",
        agent_id="default",
    )


@pytest.mark.asyncio
async def test_occurrences_use_each_jobs_schedule_timezone(tmp_path: Path):
    manager = _make_manager(tmp_path)
    await manager.create_or_replace_job(
        _make_job(
            "shanghai-daily",
            cron="0 9 * * *",
            schedule_timezone="Asia/Shanghai",
        ),
    )

    result = await manager.list_job_occurrences(
        start_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
        end_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )

    assert result.rule_count == 1
    assert result.occurrence_count == 2
    assert result.complete is True
    assert [item.scheduled_at.astimezone(timezone.utc).isoformat() for item in result.items] == [
        "2026-09-14T01:00:00+00:00",
        "2026-09-15T01:00:00+00:00",
    ]
    assert {item.schedule_timezone for item in result.items} == {"Asia/Shanghai"}


@pytest.mark.asyncio
async def test_occurrence_count_is_not_the_number_of_rendered_blocks(
    tmp_path: Path,
):
    manager = _make_manager(tmp_path)
    await manager.create_or_replace_job(
        _make_job(
            "hourly",
            cron="0 * * * *",
            schedule_timezone="UTC",
        ),
    )
    await manager.create_or_replace_job(
        _make_job(
            "paused-hourly",
            cron="0 * * * *",
            schedule_timezone="UTC",
            enabled=False,
        ),
    )

    result = await manager.list_job_occurrences(
        start_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
        end_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
        max_items=10,
    )

    assert result.rule_count == 1
    assert result.occurrence_count == 168
    assert len(result.items) == 10
    assert result.truncated is True
    assert result.complete is True


@pytest.mark.asyncio
async def test_occurrence_window_requires_aware_bounded_dates(tmp_path: Path):
    manager = _make_manager(tmp_path)

    with pytest.raises(ValueError, match="timezone-aware"):
        await manager.list_job_occurrences(
            start_at=datetime(2026, 9, 14),
            end_at=datetime(2026, 9, 15),
        )

    with pytest.raises(ValueError, match="after start_at"):
        await manager.list_job_occurrences(
            start_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_occurrence_route_is_reachable_before_dynamic_job_route(
    tmp_path: Path,
):
    manager = _make_manager(tmp_path)
    await manager.create_or_replace_job(
        _make_job(
            "hourly",
            cron="0 * * * *",
            schedule_timezone="UTC",
        ),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_cron_manager] = lambda: manager

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/cron/jobs/occurrences",
            params={
                "start_at": "2026-09-14T00:00:00Z",
                "end_at": "2026-09-14T03:00:00Z",
                "max_items": 2,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["rule_count"] == 1
    assert payload["occurrence_count"] == 3
    assert len(payload["items"]) == 2
    assert payload["truncated"] is True
