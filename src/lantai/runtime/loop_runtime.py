# -*- coding: utf-8 -*-
"""Adapters between Loop contracts and RuntimeRun control-plane snapshots."""

from __future__ import annotations

from datetime import datetime

from .loops import (
    LoopBudgetUsage,
    LoopContract,
    LoopEvaluationReport,
    LoopLeaseState,
    LoopProgressWindow,
    LoopStopAssessment,
    LoopStopReasonCategory,
    assess_loop_stop_conditions,
)
from .runs import RuntimeRunContract, RuntimeRunReason
from .runs.reducer import (
    RuntimeRunReducerAction,
    RuntimeRunReducerResult,
    reduce_runtime_run,
)
from .reporting import build_runtime_progress_report
from .store import RuntimeRunStore


_RUNTIME_REASON_CATEGORY_BY_LOOP: dict[LoopStopReasonCategory, str] = {
    "budget": "policy",
    "lease": "runtime",
    "watchdog": "runtime",
    "progress": "runtime",
    "rubric": "policy",
    "human": "user",
    "gate": "policy",
}


def assess_loop_runtime_run_stop_conditions(
    contract: LoopContract,
    run: RuntimeRunContract,
    *,
    usage: LoopBudgetUsage,
    heartbeat_timeout_seconds: float | None = None,
    progress: LoopProgressWindow | None = None,
    latest_evaluation: LoopEvaluationReport | None = None,
    human_stop_requested: bool = False,
    now: datetime | None = None,
) -> LoopStopAssessment:
    """Assess a RuntimeRun snapshot without mutating or settling the run."""

    heartbeat_at = run.heartbeat_at or run.started_at or run.created_at
    lease_state = LoopLeaseState(
        lease_owner=run.lease_owner,
        lease_expires_at=run.lease_expires_at,
        last_heartbeat_at=heartbeat_at,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        watchdog_deadline_at=run.budget.deadline_at,
    )
    return assess_loop_stop_conditions(
        contract,
        usage=usage,
        lease_state=lease_state,
        progress=progress,
        latest_evaluation=latest_evaluation,
        human_stop_requested=human_stop_requested,
        now=now,
    )


async def assess_loop_runtime_run_stop_conditions_from_store(
    store: RuntimeRunStore,
    contract: LoopContract,
    run: RuntimeRunContract,
    *,
    usage: LoopBudgetUsage,
    heartbeat_timeout_seconds: float | None = None,
    progress: LoopProgressWindow | None = None,
    latest_evaluation: LoopEvaluationReport | None = None,
    human_stop_requested: bool = False,
    now: datetime | None = None,
) -> LoopStopAssessment:
    """Assess a RuntimeRun with persisted Loop progress when available."""

    stored_progress = progress
    if stored_progress is None:
        stored_progress = await store.load_loop_progress_window(
            run.run_id,
            expected_attempt_id=run.attempt_id,
        )
    stored_evaluation = latest_evaluation
    if stored_evaluation is None:
        evaluation_snapshot = await store.load_latest_loop_evaluation_report(
            run.run_id,
            expected_attempt_id=run.attempt_id,
        )
        if evaluation_snapshot is not None:
            stored_evaluation = evaluation_snapshot.report
    return assess_loop_runtime_run_stop_conditions(
        contract,
        run,
        usage=usage,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        progress=stored_progress,
        latest_evaluation=stored_evaluation,
        human_stop_requested=human_stop_requested,
        now=now,
    )


def loop_stop_assessment_to_runtime_reason(
    assessment: LoopStopAssessment,
) -> RuntimeRunReason | None:
    """Project the primary Loop stop reason into the RuntimeRun reason shape."""

    if not assessment.should_stop or not assessment.reasons:
        return None

    reason = assessment.reasons[0]
    category = _RUNTIME_REASON_CATEGORY_BY_LOOP.get(reason.category, "runtime")
    return RuntimeRunReason(
        code=f"loop_{reason.code}",
        message=reason.message,
        category=category,  # type: ignore[arg-type]
        retryable=reason.retryable,
    )


def _runtime_terminal_status_for_loop_stop(assessment: LoopStopAssessment) -> str:
    primary = assessment.reasons[0]
    if primary.code == "rubric_target_met":
        return "succeeded"
    if primary.code == "human_stop_requested":
        return "cancelled"
    return "stalled"


def settle_runtime_run_from_loop_stop_assessment(
    run: RuntimeRunContract,
    assessment: LoopStopAssessment,
    *,
    expected_attempt_id: str | None = None,
    expected_revision: int | None = None,
    lease_owner: str | None = None,
    now: datetime | None = None,
) -> RuntimeRunReducerResult | None:
    """Apply a Loop stop through the reducer without falsifying finality.

    A watchdog deadline is a bounded-execution checkpoint, not evidence that
    the task has reached a terminal outcome.  It therefore parks the existing
    run for an explicit budget/deadline revision instead of settling it as
    ``stalled``.  All other stop reasons retain their established terminal
    mapping.
    """

    if not assessment.should_stop or not assessment.reasons:
        return None

    primary = assessment.reasons[0]
    if primary.code == "watchdog_deadline_expired":
        return reduce_runtime_run(
            run,
            RuntimeRunReducerAction(
                type="pause",
                expected_attempt_id=expected_attempt_id,
                expected_revision=expected_revision,
                lease_owner=lease_owner,
                reason=loop_stop_assessment_to_runtime_reason(assessment),
                wait_reason="deadline",
                now=now or datetime.now(run.created_at.tzinfo),
            ),
        )
    return reduce_runtime_run(
        run,
        RuntimeRunReducerAction(
            type="settle",
            status=_runtime_terminal_status_for_loop_stop(assessment),  # type: ignore[arg-type]
            expected_attempt_id=expected_attempt_id,
            expected_revision=expected_revision,
            lease_owner=lease_owner,
            reason=loop_stop_assessment_to_runtime_reason(assessment),
            now=now or datetime.now(run.created_at.tzinfo),
        ),
    )


async def save_runtime_run_loop_stop_assessment(
    store: RuntimeRunStore,
    run: RuntimeRunContract,
    assessment: LoopStopAssessment,
    *,
    expected_attempt_id: str | None = None,
    expected_revision: int | None = None,
    lease_owner: str | None = None,
    now: datetime | None = None,
) -> RuntimeRunContract | None:
    """Persist a reducer-applied Loop stop assessment with store fencing."""

    result = settle_runtime_run_from_loop_stop_assessment(
        run,
        assessment,
        expected_attempt_id=expected_attempt_id,
        expected_revision=expected_revision,
        lease_owner=lease_owner,
        now=now,
    )
    if result is None:
        return None

    report_kind = (
        "deadline_snapshot"
        if result.run.wait_reason == "deadline"
        else "terminal"
    )
    report = build_runtime_progress_report(
        result.run,
        workspace_dir=store.workspace_dir,
        kind=report_kind,
        now=now,
    )
    # The report is a projection of the state produced by the stop action, not
    # a second control-plane transition. Persist both in one fenced write so a
    # watchdog settlement consumes exactly one RuntimeRun revision.
    reported = result.run.model_copy(update={"progress_report": report})
    return await store.save_contract(
        reported,
        expected_revision=result.before_revision,
        expected_attempt_id=run.attempt_id,
        lease_owner=lease_owner,
    )
