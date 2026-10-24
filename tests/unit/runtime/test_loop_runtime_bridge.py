# -*- coding: utf-8 -*-
"""Behavior locks for bridging Loop stop assessment to RuntimeRun state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lantai.runtime import (
    LoopBudget,
    LoopBudgetUsage,
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopDeterministicGateResult,
    LoopProgressWindow,
    LoopRubric,
    LoopRubricDimension,
    LoopRubricDimensionScore,
    LoopStopConditions,
    RuntimeBudget,
    RuntimeRunFencingError,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunStore,
    RuntimeRunStoreConflictError,
    assess_loop_runtime_run_stop_conditions_from_store,
    assess_loop_runtime_run_stop_conditions,
    evaluate_loop_contract,
    loop_stop_assessment_to_runtime_reason,
    save_runtime_run_loop_stop_assessment,
    settle_runtime_run_from_loop_stop_assessment,
)


NOW = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)


def _owner() -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        workspace_id="workspace-1",
        channel="console",
    )


def _loop_contract() -> LoopContract:
    return LoopContract(
        contract_id="loop-runtime-1",
        objective="Bridge loop stop assessment into runtime control plane",
        scope=["RuntimeRun control-plane assessment"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="bridge",
                title="Runtime bridge",
                acceptance=["Assessment never mutates the run"],
            )
        ],
        acceptance=["Runtime reducer remains the only status transition owner"],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="runtime-tests",
                name="Runtime tests",
                kind="test",
                command="uv run pytest tests/unit/runtime -q",
            )
        ],
        rubric=LoopRubric(
            rubric_id="bridge-rubric",
            dimensions=[
                LoopRubricDimension(
                    dimension_id="correctness",
                    name="Correctness",
                    weight=1,
                )
            ],
        ),
        budgets=LoopBudget(max_steps=5),
        stop_conditions=LoopStopConditions(max_no_progress_iterations=2),
    )


def _run() -> RuntimeRunContract:
    return RuntimeRunContract(
        run_id="runtime-run-1",
        attempt_id="attempt-1",
        source="chat",
        owner=_owner(),
        status="running",
        revision=3,
        lease_owner="worker-1",
        lease_expires_at=NOW - timedelta(seconds=1),
        created_at=NOW - timedelta(minutes=5),
        started_at=NOW - timedelta(minutes=4),
        heartbeat_at=NOW - timedelta(seconds=90),
        budget=RuntimeBudget(deadline_at=NOW - timedelta(seconds=1)),
        release_edition="official",
    )


def _healthy_run() -> RuntimeRunContract:
    return _run().model_copy(
        update={
            "lease_expires_at": NOW + timedelta(minutes=5),
            "heartbeat_at": NOW,
            "budget": RuntimeBudget(deadline_at=NOW + timedelta(minutes=5)),
        }
    )


def test_loop_runtime_bridge_assesses_without_mutating_run() -> None:
    run = _run()

    assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        run,
        usage=LoopBudgetUsage(steps=5),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    codes = [reason.code for reason in assessment.reasons]
    assert assessment.should_stop is True
    assert "step_budget_exhausted" in codes
    assert "lease_expired" in codes
    assert "heartbeat_stale" in codes
    assert "watchdog_deadline_expired" in codes
    assert run.status == "running"
    assert run.revision == 3


def test_loop_stop_assessment_projects_primary_runtime_reason() -> None:
    assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        _run(),
        usage=LoopBudgetUsage(steps=5),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    reason = loop_stop_assessment_to_runtime_reason(assessment)

    assert reason is not None
    assert reason.code == "loop_step_budget_exhausted"
    assert reason.category == "policy"
    assert reason.retryable is False


def test_loop_stop_assessment_without_reasons_has_no_runtime_reason() -> None:
    run = _run().model_copy(
        update={
            "lease_expires_at": NOW + timedelta(minutes=5),
            "heartbeat_at": NOW,
            "budget": RuntimeBudget(deadline_at=NOW + timedelta(minutes=5)),
        }
    )
    assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        run,
        usage=LoopBudgetUsage(steps=1),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    assert assessment.should_stop is False
    assert loop_stop_assessment_to_runtime_reason(assessment) is None


def test_loop_stop_assessment_settles_runtime_run_through_reducer() -> None:
    run = _run()
    assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        run,
        usage=LoopBudgetUsage(steps=5),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    result = settle_runtime_run_from_loop_stop_assessment(
        run,
        assessment,
        expected_attempt_id="attempt-1",
        expected_revision=3,
        lease_owner="worker-1",
        now=NOW,
    )

    assert result is not None
    assert result.before_status == "running"
    assert result.after_status == "stalled"
    assert result.run.revision == 4
    assert result.run.error is not None
    assert result.run.error.code == "loop_step_budget_exhausted"
    assert run.status == "running"
    assert run.revision == 3


def test_watchdog_deadline_parks_run_without_claiming_terminal_outcome() -> None:
    run = _healthy_run().model_copy(
        update={"budget": RuntimeBudget(deadline_at=NOW - timedelta(seconds=1))}
    )
    assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        run,
        usage=LoopBudgetUsage(steps=1),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    result = settle_runtime_run_from_loop_stop_assessment(
        run,
        assessment,
        expected_attempt_id="attempt-1",
        expected_revision=3,
        lease_owner="worker-1",
        now=NOW,
    )

    assert result is not None
    assert [reason.code for reason in assessment.reasons] == [
        "watchdog_deadline_expired"
    ]
    assert result.after_status == "paused"
    assert result.run.wait_reason == "deadline"
    assert result.run.settled_at is None
    assert result.run.error is None


def test_loop_stop_assessment_settle_respects_runtime_fences() -> None:
    assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        _run(),
        usage=LoopBudgetUsage(steps=5),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    try:
        settle_runtime_run_from_loop_stop_assessment(
            _run(),
            assessment,
            expected_revision=2,
            lease_owner="worker-1",
            now=NOW,
        )
    except RuntimeRunFencingError as exc:
        assert "revision" in str(exc)
    else:  # pragma: no cover - keeps the expected exception explicit.
        raise AssertionError("expected RuntimeRunFencingError")


def test_loop_stop_assessment_success_and_human_stop_terminal_mapping() -> None:
    contract = _loop_contract().model_copy(
        update={"stop_conditions": LoopStopConditions(rubric_score_at_least=4.0)}
    )
    passed_report = evaluate_loop_contract(
        contract,
        gate_results=[
            LoopDeterministicGateResult(gate_id="runtime-tests", status="passed")
        ],
        rubric_scores=[
            LoopRubricDimensionScore(dimension_id="correctness", score=4.5)
        ],
    )
    success_run = _healthy_run()
    success_assessment = assess_loop_runtime_run_stop_conditions(
        contract,
        success_run,
        usage=LoopBudgetUsage(steps=1),
        latest_evaluation=passed_report,
        now=NOW,
    )
    success = settle_runtime_run_from_loop_stop_assessment(
        success_run,
        success_assessment,
        expected_revision=3,
        lease_owner="worker-1",
        now=NOW,
    )

    human_run = _healthy_run()
    human_assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        human_run,
        usage=LoopBudgetUsage(steps=1),
        human_stop_requested=True,
        now=NOW,
    )
    cancelled = settle_runtime_run_from_loop_stop_assessment(
        human_run,
        human_assessment,
        expected_revision=3,
        lease_owner="worker-1",
        now=NOW,
    )

    assert success is not None
    assert success.after_status == "succeeded"
    assert success.run.error is None
    assert cancelled is not None
    assert cancelled.after_status == "cancelled"
    assert cancelled.run.stop_reason is not None
    assert cancelled.run.stop_reason.code == "loop_human_stop_requested"


@pytest.mark.asyncio
async def test_loop_stop_assessment_persists_settled_runtime_run(tmp_path) -> None:
    store = RuntimeRunStore(tmp_path)
    run = _run()
    await store.save_contract(run, expected_revision=None)
    assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        run,
        usage=LoopBudgetUsage(steps=5),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    saved = await save_runtime_run_loop_stop_assessment(
        store,
        run,
        assessment,
        expected_attempt_id="attempt-1",
        expected_revision=3,
        lease_owner="worker-1",
        now=NOW,
    )
    loaded = await store.load_contract(run.run_id)

    assert saved is not None
    assert loaded is not None
    assert saved.status == "stalled"
    assert loaded.status == "stalled"
    assert loaded.revision == 4
    assert loaded.error is not None
    assert loaded.error.code == "loop_step_budget_exhausted"
    assert loaded.progress_report is not None
    assert loaded.progress_report.contract_revision == loaded.revision
    assert loaded.progress_report.kind == "terminal"
    assert loaded.progress_report.status == "stalled"
    assert loaded.progress_report.terminal is True


@pytest.mark.asyncio
async def test_watchdog_deadline_persists_nonterminal_progress_snapshot(
    tmp_path,
) -> None:
    store = RuntimeRunStore(tmp_path)
    run = _healthy_run().model_copy(
        update={"budget": RuntimeBudget(deadline_at=NOW - timedelta(seconds=1))}
    )
    await store.save_contract(run, expected_revision=None)
    assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        run,
        usage=LoopBudgetUsage(steps=1),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    saved = await save_runtime_run_loop_stop_assessment(
        store,
        run,
        assessment,
        expected_attempt_id="attempt-1",
        expected_revision=3,
        lease_owner="worker-1",
        now=NOW,
    )
    loaded = await store.load_contract(run.run_id)

    assert saved is not None
    assert loaded is not None
    assert loaded.status == "paused"
    assert loaded.wait_reason == "deadline"
    assert loaded.settled_at is None
    assert loaded.error is None
    assert loaded.revision == 4
    assert loaded.progress_report is not None
    assert loaded.progress_report.contract_revision == loaded.revision
    assert loaded.progress_report.kind == "deadline_snapshot"
    assert loaded.progress_report.status == "paused"
    assert loaded.progress_report.terminal is False
    assert "extend_deadline_and_resume_or_stop" in (
        loaded.progress_report.user_decisions
    )


@pytest.mark.asyncio
async def test_loop_runtime_bridge_reads_persisted_progress_window(tmp_path) -> None:
    store = RuntimeRunStore(tmp_path)
    run = _healthy_run()
    await store.save_contract(run, expected_revision=None)
    await store.save_loop_progress_window(
        run.run_id,
        attempt_id=run.attempt_id,
        progress=LoopProgressWindow(
            consecutive_failures=2,
            consecutive_same_failures=2,
            consecutive_no_progress_iterations=2,
            last_failure_fingerprint="sha256:gate-a",
        ),
        updated_at=NOW,
    )

    recovered_store = RuntimeRunStore(tmp_path)
    assessment = await assess_loop_runtime_run_stop_conditions_from_store(
        recovered_store,
        _loop_contract(),
        run,
        usage=LoopBudgetUsage(steps=1),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    codes = {reason.code for reason in assessment.reasons}
    assert assessment.should_stop is True
    assert assessment.progress_stalled is True
    assert codes == {"no_progress_exhausted"}


@pytest.mark.asyncio
async def test_loop_runtime_bridge_reads_persisted_latest_evaluation(tmp_path) -> None:
    store = RuntimeRunStore(tmp_path)
    run = _healthy_run()
    contract = _loop_contract().model_copy(
        update={"stop_conditions": LoopStopConditions(rubric_score_at_least=4.0)}
    )
    await store.save_contract(run, expected_revision=None)
    report = evaluate_loop_contract(
        contract,
        gate_results=[
            LoopDeterministicGateResult(gate_id="runtime-tests", status="passed")
        ],
        rubric_scores=[
            LoopRubricDimensionScore(dimension_id="correctness", score=4.5)
        ],
        report_id="persisted-pass",
    )
    await store.save_loop_evaluation_report(
        run.run_id,
        attempt_id=run.attempt_id,
        report=report,
        saved_at=NOW,
    )

    recovered_store = RuntimeRunStore(tmp_path)
    assessment = await assess_loop_runtime_run_stop_conditions_from_store(
        recovered_store,
        contract,
        run,
        usage=LoopBudgetUsage(steps=1),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    codes = {reason.code for reason in assessment.reasons}
    assert assessment.should_stop is True
    assert assessment.rubric_target_met is True
    assert codes == {"rubric_target_met"}


@pytest.mark.asyncio
async def test_loop_stop_assessment_store_save_rejects_stale_revision(tmp_path) -> None:
    store = RuntimeRunStore(tmp_path)
    run = _run()
    await store.save_contract(run, expected_revision=None)
    advanced = run.model_copy(
        update={"revision": 4, "heartbeat_at": NOW + timedelta(seconds=1)}
    )
    await store.save_contract(
        advanced,
        expected_revision=3,
        expected_attempt_id="attempt-1",
        lease_owner="worker-1",
    )
    assessment = assess_loop_runtime_run_stop_conditions(
        _loop_contract(),
        run,
        usage=LoopBudgetUsage(steps=5),
        heartbeat_timeout_seconds=60,
        now=NOW,
    )

    with pytest.raises(RuntimeRunStoreConflictError, match="revision"):
        await save_runtime_run_loop_stop_assessment(
            store,
            run,
            assessment,
            expected_attempt_id="attempt-1",
            expected_revision=3,
            lease_owner="worker-1",
            now=NOW,
        )
