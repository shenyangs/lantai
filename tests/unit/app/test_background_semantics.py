# -*- coding: utf-8 -*-
"""Behavior locks for foreground-first background semantic work."""
from __future__ import annotations

import asyncio

import pytest

from lantai.app.background_semantics import (
    BackgroundSemanticService,
    BackgroundSemanticJobStore,
    PrimaryModelPreempted,
    foreground_model_turn,
    run_primary_when_idle,
)


@pytest.mark.asyncio
async def test_primary_background_work_is_preempted_by_foreground_turn() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_background() -> str:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    background = asyncio.create_task(
        run_primary_when_idle("agent-1", slow_background),
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)

    async with foreground_model_turn("agent-1"):
        await asyncio.wait_for(cancelled.wait(), timeout=0.5)

    with pytest.raises(PrimaryModelPreempted):
        await background


def test_semantic_job_store_is_idempotent_and_recovers_running_jobs(
    tmp_path,
) -> None:
    store = BackgroundSemanticJobStore(tmp_path / "semantic-jobs.sqlite3")
    first = store.enqueue(
        kind="memory_capture",
        payload={"session_id": "session-1"},
        idempotency_key="memory:session-1:turn-1",
    )
    duplicate = store.enqueue(
        kind="memory_capture",
        payload={"session_id": "session-1"},
        idempotency_key="memory:session-1:turn-1",
    )

    assert duplicate.job_id == first.job_id
    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.status == "running"

    store.close()
    recovered = BackgroundSemanticJobStore(tmp_path / "semantic-jobs.sqlite3")
    pending = recovered.list_pending()

    assert [job.job_id for job in pending] == [first.job_id]
    assert pending[0].status == "pending"


@pytest.mark.asyncio
async def test_semantic_service_recovers_and_completes_persisted_work(
    tmp_path,
) -> None:
    service = BackgroundSemanticService(tmp_path, agent_id="agent-1")
    job = service.store.enqueue(
        kind="test_semantic",
        payload={"value": "persisted"},
        idempotency_key="test:persisted",
    )
    seen: list[str] = []
    service.register_handler(
        "test_semantic",
        lambda payload: _record_payload(payload, seen),
    )

    await service.start()
    completed = await asyncio.wait_for(
        service.wait_for_job(job.job_id),
        timeout=1.0,
    )
    status = service.status()
    await service.stop()

    assert seen == ["persisted"]
    assert completed.result == {"value": "persisted"}
    assert status["completed"] == 1
    assert status["pending"] == 0


@pytest.mark.asyncio
async def test_workspace_reload_keeps_background_semantics_usable(
    tmp_path,
) -> None:
    from lantai.app.workspace import Workspace

    workspace = Workspace("agent-reload", str(tmp_path))
    service = BackgroundSemanticService(
        tmp_path,
        agent_id="agent-reload",
    )
    service.register_handler(
        "reload_probe",
        lambda payload: _record_payload(payload, []),
    )
    workspace._service_manager.services["background_semantics"] = service

    await service.start()
    try:
        reusable = workspace._service_manager.get_reusable_services()
        await workspace._service_manager.stop_all(final=False)

        job = service.submit(
            kind="reload_probe",
            payload={"value": "still-open"},
            idempotency_key="reload:still-open",
        )
        completed = await asyncio.wait_for(
            service.wait_for_job(job.job_id),
            timeout=1.0,
        )

        assert reusable["background_semantics"] is service
        assert completed.result == {"value": "still-open"}
    finally:
        if service._worker is not None:
            await service.stop()


async def _record_payload(payload: dict, target: list[str]) -> dict:
    target.append(str(payload["value"]))
    return {"value": str(payload["value"])}


@pytest.mark.asyncio
async def test_preempted_persistent_job_retries_after_foreground_settles(
    tmp_path,
) -> None:
    service = BackgroundSemanticService(tmp_path, agent_id="agent-retry")
    started = asyncio.Event()
    attempts = 0

    async def handler(_payload: dict) -> None:
        nonlocal attempts

        async def use_primary() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                started.set()
                await asyncio.Future()

        await run_primary_when_idle("agent-retry", use_primary)

    service.register_handler("retry", handler)
    await service.start()
    job = service.submit(
        kind="retry",
        payload={},
        idempotency_key="retry-on-preemption",
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)

    async with foreground_model_turn("agent-retry"):
        await asyncio.sleep(0)

    completed = await asyncio.wait_for(
        service.wait_for_job(job.job_id),
        timeout=1.0,
    )
    await service.stop()

    assert completed.status == "completed"
    assert attempts == 2
    assert completed.attempts == 0
