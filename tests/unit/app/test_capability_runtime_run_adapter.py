# -*- coding: utf-8 -*-
"""Behavior locks for Capability TaskRun RuntimeRunStore facade."""

from __future__ import annotations

import pytest

from lantai.app.capabilities.models import TaskRun
from lantai.app.capabilities.repo import CapabilityRepository
from lantai.runtime.store import RuntimeRunStore


@pytest.mark.asyncio
async def test_capability_task_run_upsert_writes_runtime_run_contract(tmp_path):
    repo = CapabilityRepository(tmp_path)
    task_run = TaskRun(
        id="capability-runtime-run-1",
        agent_id="agent-default",
        source="capability",
        session_id="session-1",
        title="Capability run",
        status="running",
        metadata={"user_id": "user-1"},
    )

    await repo.upsert_task_run(task_run)
    contract = await RuntimeRunStore(tmp_path).load_contract(task_run.id)

    assert contract is not None
    assert contract.run_id == task_run.id
    assert contract.attempt_id == f"{task_run.id}:capability-attempt-1"
    assert contract.source == "capability"
    assert contract.status == "running"
    assert contract.revision == 2
    assert contract.owner.user_id == "user-1"
    assert contract.owner.agent_id == "agent-default"
    assert contract.owner.session_id == "session-1"
    assert contract.owner.workspace_id == str(tmp_path)
    assert contract.contract_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_capability_task_run_upsert_settles_runtime_run_contract(tmp_path):
    repo = CapabilityRepository(tmp_path)
    task_run = TaskRun(
        id="pipeline-runtime-run-1",
        agent_id="agent-default",
        source="pipeline",
        title="Pipeline run",
        status="running",
    )
    await repo.upsert_task_run(task_run)

    succeeded = task_run.model_copy(
        update={
            "status": "succeeded",
            "failure_reason": None,
        }
    )
    await repo.upsert_task_run(succeeded)
    contract = await RuntimeRunStore(tmp_path).load_contract(task_run.id)

    assert contract is not None
    assert contract.source == "pipeline"
    assert contract.status == "succeeded"
    assert contract.revision == 3
    assert contract.settled_at is not None
