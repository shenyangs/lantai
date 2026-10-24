from __future__ import annotations

from pathlib import Path

import pytest

from lantai.app.coding_task_runtime import (
    CodeTaskRun,
    CodingTaskRuntime,
)
from lantai.runtime.effects import RuntimeEffectJournal
from lantai.runtime.effects import (
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.store import RuntimeRunStore


def _task(workspace: Path, task_id: str = "terminal-1") -> CodeTaskRun:
    return CodeTaskRun(
        task_id=task_id,
        workspace_dir=str(workspace),
        command="pytest -q",
        status="queued",
        started_at="2026-07-31T12:00:00+00:00",
        timeout_seconds=300,
    )


@pytest.mark.asyncio
async def test_terminal_task_uses_runtime_run_and_effect_as_fact_sources(
    tmp_path: Path,
):
    runtime = CodingTaskRuntime(tmp_path, agent_id="default")
    task = await runtime.create(_task(tmp_path))
    running = await runtime.mark_running(task)

    contract = await RuntimeRunStore(tmp_path).load_contract(task.task_id)
    assert contract is not None
    assert contract.status == "running"
    assert running.status == "running"
    effects = RuntimeEffectJournal(tmp_path).list_effects(
        contract.run_id,
        contract.attempt_id,
    )
    assert [effect.status for effect in effects] == ["intent", "running"]

    reloaded = CodingTaskRuntime(tmp_path, agent_id="default").load_all()
    assert reloaded == [running]


@pytest.mark.asyncio
async def test_terminal_task_success_settles_run_and_effect(tmp_path: Path):
    runtime = CodingTaskRuntime(tmp_path, agent_id="default")
    running = await runtime.mark_running(await runtime.create(_task(tmp_path)))

    settled = await runtime.settle(
        running,
        status="completed",
        returncode=0,
    )

    contract = await RuntimeRunStore(tmp_path).load_contract(running.task_id)
    assert contract is not None
    assert contract.status == "succeeded"
    assert settled.status == "completed"
    assert settled.returncode == 0
    effect = RuntimeEffectJournal(tmp_path).latest_effect(
        contract.run_id,
        contract.attempt_id,
        f"terminal:{contract.run_id}:shell",
    )
    assert effect is not None
    assert effect.status == "committed"


@pytest.mark.asyncio
async def test_terminal_restart_recovery_marks_running_effect_ambiguous(
    tmp_path: Path,
):
    runtime = CodingTaskRuntime(tmp_path, agent_id="default")
    running = await runtime.mark_running(await runtime.create(_task(tmp_path)))

    recovered = await CodingTaskRuntime(
        tmp_path,
        agent_id="default",
    ).recover_interrupted()

    assert recovered[0].task_id == running.task_id
    assert recovered[0].status == "stalled"
    assert recovered[0].error == "Backend restarted while the command was active"
    contract = await RuntimeRunStore(tmp_path).load_contract(running.task_id)
    assert contract is not None
    assert contract.status == "stalled"
    effect = RuntimeEffectJournal(tmp_path).latest_effect(
        contract.run_id,
        contract.attempt_id,
        f"terminal:{contract.run_id}:shell",
    )
    assert effect is not None
    assert effect.status == "ambiguous"


@pytest.mark.asyncio
async def test_terminal_restart_recovery_finishes_committed_effect_without_replay(
    tmp_path: Path,
):
    runtime = CodingTaskRuntime(tmp_path, agent_id="default")
    running = await runtime.mark_running(await runtime.create(_task(tmp_path)))
    contract = await RuntimeRunStore(tmp_path).load_contract(running.task_id)
    assert contract is not None
    journal = RuntimeEffectJournal(tmp_path)
    effect = journal.latest_effect(
        contract.run_id,
        contract.attempt_id,
        f"terminal:{contract.run_id}:shell",
    )
    assert effect is not None
    committed = reduce_runtime_effect(
        effect,
        RuntimeEffectReducerAction(type="commit", result_hash="sha256:done"),
    ).effect
    journal.append_effect(committed)

    recovered = await CodingTaskRuntime(
        tmp_path,
        agent_id="default",
    ).recover_interrupted()

    assert recovered[0].status == "completed"
    settled = await RuntimeRunStore(tmp_path).load_contract(running.task_id)
    assert settled is not None
    assert settled.status == "succeeded"
    assert journal.latest_effect(
        contract.run_id,
        contract.attempt_id,
        committed.effect_id,
    ) == committed
