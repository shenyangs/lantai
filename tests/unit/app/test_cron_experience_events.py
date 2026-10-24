# -*- coding: utf-8 -*-
"""Cron and heartbeat experience event tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.app.crons.manager import CronManager
from lantai.app.crons.models import CronJobRequest, CronJobSpec, DispatchSpec, DispatchTarget, ScheduleSpec
from lantai.app.crons.repo.json_repo import JsonJobRepository
from lantai.app.experience.events import (
    build_runtime_events_response,
    collect_recent_turn_events,
)
from lantai.runtime.store import RuntimeRunStore


def _make_job(job_id: str) -> CronJobSpec:
    return CronJobSpec(
        id=job_id,
        name="日报整理",
        schedule=ScheduleSpec(cron="0 9 * * 1"),
        task_type="agent",
        request=CronJobRequest(input=[]),
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="default", session_id="console:main"),
        ),
    )


@pytest.mark.asyncio
async def test_safe_recovery_mode_pauses_scheduler_without_deleting_jobs(
    tmp_path, monkeypatch
):
    repo = JsonJobRepository(Path(tmp_path) / "jobs.json")
    job = _make_job("job-safe")
    await repo.upsert_job(job)
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=Path(tmp_path)),
        channel_manager=SimpleNamespace(),
        timezone="UTC",
        agent_id="default",
    )
    monkeypatch.setenv("LANTAI_RECOVERY_SAFE_MODE", "1")

    await manager.start()

    assert manager._started is True
    assert manager._scheduler.running is False
    assert [item.id for item in await manager.list_jobs()] == ["job-safe"]
    await manager.stop()
    assert manager._started is False


@pytest.mark.asyncio
async def test_cron_job_records_started_and_settled_events(tmp_path, monkeypatch):
    runner = SimpleNamespace(workspace_dir=Path(tmp_path))
    manager = CronManager(
        repo=JsonJobRepository(Path(tmp_path) / "jobs.json"),
        runner=runner,
        channel_manager=SimpleNamespace(),
        timezone="UTC",
        agent_id="default",
    )
    job = _make_job("job-1")

    async def fake_execute(_job):
        return None

    monkeypatch.setattr(manager._executor, "execute", fake_execute)

    await manager._execute_once(job)

    snapshot = collect_recent_turn_events([tmp_path], limit=10)
    assert snapshot.event_count == 2
    labels = [item.label for item in snapshot.items]
    assert "定时开始" in labels
    assert "定时结束" in labels


@pytest.mark.asyncio
async def test_cron_job_concurrent_singleton_skips_second_invocation(
    tmp_path,
    monkeypatch,
):
    runner = SimpleNamespace(workspace_dir=Path(tmp_path))
    manager = CronManager(
        repo=JsonJobRepository(Path(tmp_path) / "jobs.json"),
        runner=runner,
        channel_manager=SimpleNamespace(),
        timezone="UTC",
        agent_id="default",
    )
    job = _make_job("job-singleton")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    execute_calls = 0

    async def fake_execute(_job):
        nonlocal execute_calls
        execute_calls += 1
        first_started.set()
        await release_first.wait()

    monkeypatch.setattr(manager._executor, "execute", fake_execute)

    first = asyncio.create_task(manager._execute_once(job))
    await first_started.wait()

    await manager._execute_once(job)
    release_first.set()
    await first

    snapshot = collect_recent_turn_events([tmp_path], limit=10)
    settled = [item for item in snapshot.items if item.label == "定时结束"]
    events = build_runtime_events_response(tmp_path, "cron:job-singleton")
    skipped = next(
        item
        for item in events.events
        if item.payload.get("final_status") == "skipped"
    )
    skipped_contract = await RuntimeRunStore(tmp_path).load_contract(
        str(skipped.payload["invocation_id"]),
    )

    assert execute_calls == 1
    assert len(settled) == 2
    assert skipped.payload["error_text"] == "max_concurrency_reached"
    assert skipped_contract is not None
    assert skipped_contract.source == "cron"
    assert skipped_contract.status == "cancelled"
    assert skipped_contract.stop_reason is not None
    assert skipped_contract.stop_reason.code == "cron_job_skipped"


@pytest.mark.asyncio
async def test_cron_job_refreshes_runtime_heartbeat_during_silent_execution(
    tmp_path,
    monkeypatch,
):
    import lantai.app.crons.manager as cron_manager_module

    runner = SimpleNamespace(workspace_dir=Path(tmp_path))
    manager = CronManager(
        repo=JsonJobRepository(Path(tmp_path) / "jobs.json"),
        runner=runner,
        channel_manager=SimpleNamespace(),
        timezone="UTC",
        agent_id="default",
    )
    job = _make_job("job-heartbeat")
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_execute(_job):
        started.set()
        await release.wait()

    monkeypatch.setattr(manager._executor, "execute", fake_execute)
    monkeypatch.setattr(
        cron_manager_module,
        "_CRON_RUNTIME_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )

    task = asyncio.create_task(manager._execute_once(job))
    await started.wait()

    events = build_runtime_events_response(tmp_path, "cron:job-heartbeat").events
    started_event = next(
        event
        for event in events
        if event.type == "tool.started"
        and event.payload.get("job_id") == "job-heartbeat"
        and event.payload.get("invocation_id")
    )
    run_id = str(started_event.payload["invocation_id"])
    store = RuntimeRunStore(tmp_path)
    before = await store.load_contract(run_id)
    assert before is not None
    assert before.status == "running"
    assert before.heartbeat_at is not None

    await asyncio.sleep(0.05)
    during = await store.load_contract(run_id)

    assert during is not None
    assert during.status == "running"
    assert during.revision > before.revision
    assert during.heartbeat_at is not None
    assert during.heartbeat_at > before.heartbeat_at

    release.set()
    await task
    settled = await store.load_contract(run_id)
    assert settled is not None
    assert settled.status == "succeeded"


@pytest.mark.asyncio
async def test_heartbeat_records_started_and_settled_events(tmp_path, monkeypatch):
    import lantai.app.crons.manager as cron_manager_module

    runner = SimpleNamespace(workspace_dir=Path(tmp_path))
    manager = CronManager(
        repo=JsonJobRepository(Path(tmp_path) / "jobs.json"),
        runner=runner,
        channel_manager=SimpleNamespace(),
        timezone="UTC",
        agent_id="default",
    )

    async def fake_run_heartbeat_once(**_kwargs):
        return {
            "executed": True,
            "status": "completed",
            "reason": "ran_without_dispatch",
            "target": "main",
            "query_path": str(Path(tmp_path) / "HEARTBEAT.md"),
        }

    monkeypatch.setattr(
        cron_manager_module,
        "run_heartbeat_once",
        fake_run_heartbeat_once,
    )

    await manager._heartbeat_callback()

    snapshot = collect_recent_turn_events([tmp_path], limit=10)
    assert snapshot.event_count == 2
    labels = [item.label for item in snapshot.items]
    assert "心跳开始" in labels
    assert "心跳结束" in labels
