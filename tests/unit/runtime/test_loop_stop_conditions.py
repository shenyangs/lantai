# -*- coding: utf-8 -*-
"""Behavior locks for LOOP-03 budget, lease, watchdog, and stop assessment."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from lantai.runtime import (
    LoopBudget,
    LoopBudgetUsage,
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopDeterministicGateResult,
    LoopLeaseState,
    LoopProgressWindow,
    LoopRubric,
    LoopRubricDimension,
    LoopRubricDimensionScore,
    LoopStopConditions,
    advance_loop_progress_window,
    assess_loop_stop_conditions,
    build_loop_budget_change_record,
    evaluate_loop_contract,
)


def _contract() -> LoopContract:
    return LoopContract(
        contract_id="loop-stop-1",
        objective="Run remediation until the contract is satisfied",
        scope=["Runtime migration remediation"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="plan",
                title="Verified remediation plan",
                acceptance=["All gates and stop rules are recorded"],
            )
        ],
        acceptance=["Stop only through structured rules"],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="runtime-tests",
                name="Runtime tests",
                kind="test",
                command="uv run pytest tests/unit/runtime -q",
            )
        ],
        rubric=LoopRubric(
            rubric_id="loop-stop-rubric",
            passing_score=4.0,
            dimensions=[
                LoopRubricDimension(
                    dimension_id="correctness",
                    name="Correctness",
                    weight=0.7,
                    target_score=4.0,
                ),
                LoopRubricDimension(
                    dimension_id="evidence",
                    name="Evidence",
                    weight=0.3,
                    target_score=4.0,
                ),
            ],
        ),
        budgets=LoopBudget(
            max_attempts=2,
            max_steps=5,
            max_tool_calls=7,
            max_tokens=1000,
            max_cost_points=3,
            max_wall_seconds=60,
            max_artifact_bytes=4096,
        ),
        stop_conditions=LoopStopConditions(
            rubric_score_at_least=4.0,
            max_consecutive_failures=2,
            max_consecutive_same_failures=2,
            max_no_progress_iterations=3,
        ),
    )


def _passing_scores() -> list[LoopRubricDimensionScore]:
    return [
        LoopRubricDimensionScore(dimension_id="correctness", score=4.5),
        LoopRubricDimensionScore(dimension_id="evidence", score=4.0),
    ]


def test_loop_stop_assessment_stops_on_budget_lease_and_watchdog_limits() -> None:
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    assessment = assess_loop_stop_conditions(
        _contract(),
        usage=LoopBudgetUsage(
            attempts=2,
            steps=5,
            tool_calls=7,
            tokens=1000,
            cost_points=3,
            wall_seconds=60,
            artifact_bytes=4096,
        ),
        lease_state=LoopLeaseState(
            lease_owner="worker-1",
            lease_expires_at=now - timedelta(seconds=1),
            last_heartbeat_at=now - timedelta(seconds=61),
            heartbeat_timeout_seconds=60,
            watchdog_deadline_at=now,
        ),
        now=now,
    )

    codes = {reason.code for reason in assessment.reasons}
    assert assessment.should_stop is True
    assert assessment.budget_exhausted is True
    assert assessment.lease_expired is True
    assert assessment.heartbeat_stale is True
    assert assessment.watchdog_expired is True
    assert {
        "attempt_budget_exhausted",
        "step_budget_exhausted",
        "tool_call_budget_exhausted",
        "token_budget_exhausted",
        "cost_budget_exhausted",
        "wall_time_budget_exhausted",
        "artifact_budget_exhausted",
        "lease_expired",
        "heartbeat_stale",
        "watchdog_deadline_expired",
    }.issubset(codes)


def test_loop_stop_assessment_stops_on_progress_and_human_stop() -> None:
    assessment = assess_loop_stop_conditions(
        _contract(),
        usage=LoopBudgetUsage(),
        progress=LoopProgressWindow(
            consecutive_failures=2,
            consecutive_same_failures=2,
            consecutive_no_progress_iterations=3,
            last_failure_fingerprint="sha256:same-failure",
        ),
        human_stop_requested=True,
    )

    codes = {reason.code for reason in assessment.reasons}
    assert assessment.should_stop is True
    assert assessment.progress_stalled is True
    assert assessment.human_stop_requested is True
    assert "consecutive_failures_exhausted" in codes
    assert "consecutive_same_failures_exhausted" in codes
    assert "no_progress_exhausted" in codes
    assert "human_stop_requested" in codes


def test_loop_progress_window_advances_failures_and_no_progress() -> None:
    first = advance_loop_progress_window(
        LoopProgressWindow(),
        iteration_failed=True,
        made_progress=False,
        failure_fingerprint="sha256:gate-a",
    )
    second = advance_loop_progress_window(
        first,
        iteration_failed=True,
        made_progress=False,
        failure_fingerprint="sha256:gate-a",
    )
    recovered = advance_loop_progress_window(
        second,
        iteration_failed=False,
        made_progress=True,
    )

    assert first.consecutive_failures == 1
    assert first.consecutive_same_failures == 1
    assert first.consecutive_no_progress_iterations == 1
    assert second.consecutive_failures == 2
    assert second.consecutive_same_failures == 2
    assert second.consecutive_no_progress_iterations == 2
    assert recovered == LoopProgressWindow()


def test_loop_stop_assessment_stops_when_rubric_target_is_met() -> None:
    contract = _contract()
    report = evaluate_loop_contract(
        contract,
        gate_results=[
            LoopDeterministicGateResult(gate_id="runtime-tests", status="passed")
        ],
        rubric_scores=_passing_scores(),
    )

    assessment = assess_loop_stop_conditions(
        contract,
        usage=LoopBudgetUsage(steps=1),
        latest_evaluation=report,
    )

    assert assessment.should_stop is True
    assert assessment.rubric_target_met is True
    assert [reason.code for reason in assessment.reasons] == ["rubric_target_met"]


def test_loop_stop_assessment_rejects_evaluation_from_another_contract() -> None:
    contract = _contract()
    report = evaluate_loop_contract(
        contract,
        gate_results=[
            LoopDeterministicGateResult(gate_id="runtime-tests", status="passed")
        ],
        rubric_scores=_passing_scores(),
    ).model_copy(update={"contract_hash": "sha256:stale"})

    with pytest.raises(ValueError, match="contract hash"):
        assess_loop_stop_conditions(
            contract,
            usage=LoopBudgetUsage(),
            latest_evaluation=report,
        )


def test_loop_budget_change_record_hashes_budget_mutation() -> None:
    previous = _contract()
    next_contract = previous.model_copy(
        update={"budgets": previous.budgets.model_copy(update={"max_steps": 10})}
    )

    record = build_loop_budget_change_record(
        previous,
        next_contract,
        changed_by="user",
        reason="User approved more remediation steps.",
    )

    assert record.contract_id == previous.contract_id
    assert record.previous_budget_hash.startswith("sha256:")
    assert record.next_budget_hash.startswith("sha256:")
    assert record.previous_budget_hash != record.next_budget_hash
    assert record.event_hash.startswith("sha256:")

    with pytest.raises(ValueError, match="actual budget change"):
        build_loop_budget_change_record(
            previous,
            previous,
            changed_by="system",
            reason="No-op should not be written.",
        )

    with pytest.raises(ValidationError):
        build_loop_budget_change_record(
            previous,
            next_contract,
            changed_by="prompt",  # type: ignore[arg-type]
            reason="Prompt text must not change budgets by itself.",
        )
