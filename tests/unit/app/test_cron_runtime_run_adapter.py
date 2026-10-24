# -*- coding: utf-8 -*-
"""Behavior locks for Cron RuntimeRunStore invocation identity."""

from __future__ import annotations

import pytest

from lantai.app.experience.events import (
    record_cron_job_settled,
    record_cron_job_started,
)
from lantai.runtime.store import RuntimeRunStore


@pytest.mark.asyncio
async def test_cron_job_started_generates_unique_runtime_invocations(tmp_path):
    first = await record_cron_job_started(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        job_id="daily",
        job_name="Daily summary",
        task_type="text",
        channel="console",
    )
    second = await record_cron_job_started(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        job_id="daily",
        job_name="Daily summary",
        task_type="text",
        channel="console",
    )

    assert first.run_id
    assert second.run_id
    assert first.run_id != second.run_id
    assert first.payload["invocation_id"] == first.run_id
    assert second.payload["invocation_id"] == second.run_id


@pytest.mark.asyncio
async def test_cron_job_settled_reuses_invocation_and_settles_runtime_run(tmp_path):
    started = await record_cron_job_started(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        job_id="daily",
        job_name="Daily summary",
        task_type="text",
        channel="console",
    )

    settled = await record_cron_job_settled(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        job_id="daily",
        job_name="Daily summary",
        task_type="text",
        channel="console",
        final_status="success",
        run_id=started.run_id,
    )
    contract = await RuntimeRunStore(tmp_path).load_contract(started.run_id)

    assert settled.run_id == started.run_id
    assert settled.payload["invocation_id"] == started.run_id
    assert contract is not None
    assert contract.source == "cron"
    assert contract.status == "succeeded"
    assert contract.revision == 3
