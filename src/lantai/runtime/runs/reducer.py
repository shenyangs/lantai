# -*- coding: utf-8 -*-
"""Pure Runtime Run state reducer.

The reducer owns control-plane status transitions only. It does not write
storage, emit events, or execute tools; callers persist the returned contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    RuntimeAutonomyGrant,
    RuntimeAttemptContract,
    RuntimeAttemptReason,
    RuntimeBudget,
    RuntimeCheckpoint,
    RuntimeCompletionEvidence,
    RuntimeDeliveryStatus,
    RuntimeRunContract,
    RuntimeProgressReport,
    RuntimeRunReason,
    RuntimeRunStatus,
    RuntimeStepRef,
    RuntimeTaskContract,
    RuntimeWaitReason,
    RuntimeWakeIntent,
)


RuntimeRunReducerActionType = Literal[
    "claim",
    "start",
    "heartbeat",
    "wait_approval",
    "pause",
    "resume",
    "request_stop",
    "settle",
    "update_budget",
    "revise_task",
    "set_autonomy_grant",
    "set_wake_intent",
    "clear_wake_intent",
    "set_progress_report",
]

TERMINAL_RUNTIME_RUN_STATUSES: frozenset[RuntimeRunStatus] = frozenset(
    {"succeeded", "failed", "cancelled", "stalled"},
)
ACTIVE_RUNTIME_RUN_STATUSES: frozenset[RuntimeRunStatus] = frozenset(
    {
        "queued",
        "claimed",
        "running",
        "waiting_approval",
        "paused",
        "stop_requested",
    },
)

_ALLOWED_TRANSITIONS: dict[RuntimeRunStatus, frozenset[RuntimeRunStatus]] = {
    "queued": frozenset({"claimed", "cancelled"}),
    "claimed": frozenset({"running", "failed", "cancelled", "stalled"}),
    "running": frozenset(
        {
            "waiting_approval",
            "paused",
            "stop_requested",
            "succeeded",
            "failed",
            "cancelled",
            "stalled",
        },
    ),
    "waiting_approval": frozenset(
        {"running", "paused", "cancelled", "failed", "stalled"},
    ),
    "paused": frozenset({"running", "cancelled", "failed", "stalled"}),
    "stop_requested": frozenset(
        {"running", "cancelled", "failed", "stalled", "succeeded"},
    ),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "stalled": frozenset(),
}

_CANONICAL_STATUS_ALIASES: dict[str, RuntimeRunStatus] = {
    "dispatched": "queued",
    "acked": "claimed",
    "active": "running",
    "blocked": "waiting_approval",
    "completed": "succeeded",
    "complete": "succeeded",
    "finished": "succeeded",
    "success": "succeeded",
    "ok": "succeeded",
    "error": "failed",
    "failure": "failed",
    "rejected": "failed",
    "canceled": "cancelled",
    "interrupted": "cancelled",
    "timeout": "stalled",
    "timed_out": "stalled",
}
_RUNTIME_STATUS_PRIORITY: dict[str, int] = {
    "queued": 0,
    "dispatched": 0,
    "claimed": 10,
    "acked": 10,
    "running": 20,
    "waiting_approval": 30,
    "blocked": 30,
    "paused": 40,
    "stop_requested": 50,
    "succeeded": 60,
    "finished": 60,
    "completed": 60,
    "cancelled": 70,
    "canceled": 70,
    "interrupted": 70,
    "stalled": 80,
    "timed_out": 80,
    "timeout": 80,
    "failed": 90,
    "failure": 90,
    "error": 90,
}


class RuntimeRunTransitionError(ValueError):
    """Raised when a reducer action would violate the run state machine."""


class RuntimeRunFencingError(RuntimeRunTransitionError):
    """Raised when attempt/revision/lease fencing rejects a reducer action."""


class RuntimeRunReducerAction(BaseModel):
    """One control-plane action submitted to the Runtime Run reducer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: RuntimeRunReducerActionType
    status: RuntimeRunStatus | None = None
    expected_attempt_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    lease_owner: str | None = None
    reason: RuntimeRunReason | None = None
    budget: RuntimeBudget | None = None
    checkpoint: RuntimeCheckpoint | None = None
    current_step: RuntimeStepRef | None = None
    delivery_status: RuntimeDeliveryStatus | None = None
    completion_evidence: RuntimeCompletionEvidence | None = None
    task_contract: RuntimeTaskContract | None = None
    autonomy_grant: RuntimeAutonomyGrant | None = None
    wake_intent: RuntimeWakeIntent | None = None
    progress_report: RuntimeProgressReport | None = None
    wait_reason: RuntimeWaitReason | None = None
    next_eligible_at: datetime | None = None
    now: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("expected_attempt_id", "lease_owner")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("now", "next_eligible_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def validate_wait_state(self):
        if self.next_eligible_at is not None and self.wait_reason is None:
            raise ValueError("next_eligible_at requires wait_reason")
        return self


class RuntimeRunReducerResult(BaseModel):
    """Result returned by the pure Runtime Run reducer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: RuntimeRunContract
    before_status: RuntimeRunStatus
    after_status: RuntimeRunStatus
    before_revision: int
    after_revision: int


class RuntimeStallAssessment(BaseModel):
    """Heartbeat assessment that never marks a run terminal on its own."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: RuntimeRunContract
    suspected_stalled: bool
    elapsed_seconds: float | None = None
    timeout_seconds: float


def normalize_runtime_run_status(value: object) -> RuntimeRunStatus | None:
    """Return a canonical RuntimeRunStatus for known current or legacy text."""

    text = str(value or "").strip().lower()
    if not text:
        return None
    text = _CANONICAL_STATUS_ALIASES.get(text, text)
    if text in _ALLOWED_TRANSITIONS:
        return text  # type: ignore[return-value]
    return None


def runtime_run_status_projection_rank(value: object) -> int:
    """Return the shared projection priority rank for status-like text."""

    text = str(value or "").strip().lower()
    if not text:
        return -1
    if text in _RUNTIME_STATUS_PRIORITY:
        return _RUNTIME_STATUS_PRIORITY[text]
    canonical = normalize_runtime_run_status(text)
    return _RUNTIME_STATUS_PRIORITY.get(canonical or "", -1)


def dominant_runtime_run_status(
    statuses: Iterable[object],
    *,
    default: RuntimeRunStatus = "queued",
) -> RuntimeRunStatus:
    """Return the monotonic projection winner for status-like values."""

    winner = normalize_runtime_run_status(default) or "queued"
    winner_rank = runtime_run_status_projection_rank(winner)
    for status in statuses:
        candidate = normalize_runtime_run_status(status)
        if candidate is None:
            continue
        rank = runtime_run_status_projection_rank(status)
        if rank >= winner_rank:
            winner = candidate
            winner_rank = rank
    return winner


def _check_fencing(
    run: RuntimeRunContract,
    action: RuntimeRunReducerAction,
) -> None:
    if action.expected_attempt_id and action.expected_attempt_id != run.attempt_id:
        raise RuntimeRunFencingError(
            f"RuntimeRun attempt fence rejected: {action.expected_attempt_id} != {run.attempt_id}",
        )
    if (
        action.expected_revision is not None
        and action.expected_revision != run.revision
    ):
        raise RuntimeRunFencingError(
            f"RuntimeRun revision fence rejected: {action.expected_revision} != {run.revision}",
        )
    if (
        action.lease_owner
        and run.lease_owner
        and action.lease_owner != run.lease_owner
    ):
        raise RuntimeRunFencingError(
            f"RuntimeRun lease fence rejected: {action.lease_owner} != {run.lease_owner}",
        )


def _target_status(
    run: RuntimeRunContract,
    action: RuntimeRunReducerAction,
) -> RuntimeRunStatus:
    if action.type == "claim":
        return "claimed"
    if action.type in {"start", "resume"}:
        return "running"
    if action.type == "heartbeat":
        return action.status or run.status
    if action.type == "wait_approval":
        return "waiting_approval"
    if action.type == "pause":
        return "paused"
    if action.type == "request_stop":
        return "stop_requested"
    if action.type == "settle":
        if action.status not in TERMINAL_RUNTIME_RUN_STATUSES:
            raise RuntimeRunTransitionError(
                "RuntimeRun settle action requires a terminal status.",
            )
        return action.status
    if action.type == "update_budget":
        if action.budget is None:
            raise RuntimeRunTransitionError(
                "RuntimeRun update_budget action requires a budget.",
            )
        return run.status
    if action.type == "revise_task":
        if action.task_contract is None:
            raise RuntimeRunTransitionError(
                "RuntimeRun revise_task action requires a task contract.",
            )
        return run.status
    if action.type == "set_autonomy_grant":
        if action.autonomy_grant is None:
            raise RuntimeRunTransitionError(
                "RuntimeRun set_autonomy_grant action requires a grant.",
            )
        return run.status
    if action.type == "set_wake_intent":
        if action.wake_intent is None:
            raise RuntimeRunTransitionError(
                "RuntimeRun set_wake_intent action requires a wake intent.",
            )
        return run.status
    if action.type == "clear_wake_intent":
        return run.status
    if action.type == "set_progress_report":
        if action.progress_report is None:
            raise RuntimeRunTransitionError(
                "RuntimeRun set_progress_report action requires a report.",
            )
        return run.status
    raise RuntimeRunTransitionError(f"Unsupported RuntimeRun action: {action.type}")


def _ensure_transition_allowed(
    current: RuntimeRunStatus,
    target: RuntimeRunStatus,
) -> None:
    if current in TERMINAL_RUNTIME_RUN_STATUSES:
        raise RuntimeRunTransitionError(
            f"RuntimeRun terminal status cannot transition: {current} -> {target}",
        )
    if target == current:
        return
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise RuntimeRunTransitionError(
            f"Illegal RuntimeRun status transition: {current} -> {target}",
        )


def reduce_runtime_run(
    run: RuntimeRunContract,
    action: RuntimeRunReducerAction,
) -> RuntimeRunReducerResult:
    """Apply one fenced reducer action to a RuntimeRunContract."""

    _check_fencing(run, action)
    current_status = run.status
    target_status = _target_status(run, action)
    if action.type != "set_progress_report":
        _ensure_transition_allowed(current_status, target_status)

    if action.type == "set_progress_report":
        report = action.progress_report
        assert report is not None
        if report.run_id != run.run_id or report.attempt_id != run.attempt_id:
            raise RuntimeRunTransitionError(
                "Runtime progress report identity does not match the run.",
            )
        if report.contract_revision != run.revision or report.status != run.status:
            raise RuntimeRunTransitionError(
                "Runtime progress report must describe the current contract revision.",
            )

    if action.type == "revise_task":
        current_task = run.task_contract
        revised_task = action.task_contract
        if current_task is None or revised_task is None:
            raise RuntimeRunTransitionError(
                "RuntimeRun revise_task requires an existing task contract.",
            )
        if revised_task.revision != current_task.revision + 1:
            raise RuntimeRunTransitionError(
                "RuntimeRun task revision must append exactly one command.",
            )
        if revised_task.commands[:-1] != current_task.commands:
            raise RuntimeRunTransitionError(
                "RuntimeRun task revision must preserve command history.",
            )
        protected_exclusions = {"revision", "client_turn_id", "commands"}
        if current_task.model_dump(exclude=protected_exclusions) != revised_task.model_dump(
            exclude=protected_exclusions
        ):
            raise RuntimeRunTransitionError(
                "RuntimeRun task revision cannot rewrite the base contract.",
            )

    if action.type == "set_autonomy_grant":
        current_task = run.task_contract
        candidate = action.autonomy_grant
        if current_task is None or candidate is None:
            raise RuntimeRunTransitionError(
                "RuntimeRun autonomy grant requires an existing task contract.",
            )
        if candidate.run_id != run.run_id:
            raise RuntimeRunTransitionError(
                "RuntimeRun autonomy grant run_id does not match the run.",
            )
        if candidate.target_task_revision != current_task.revision:
            raise RuntimeRunTransitionError(
                "RuntimeRun autonomy grant target revision does not match the task.",
            )
        current_grant = current_task.autonomy_grant
        if current_grant is None:
            if candidate.revision != 1 or candidate.revocation_generation != 0:
                raise RuntimeRunTransitionError(
                    "RuntimeRun initial autonomy grant must start at revision 1.",
                )
        elif candidate.grant_id == current_grant.grant_id:
            if candidate.revision != current_grant.revision + 1:
                raise RuntimeRunTransitionError(
                    "RuntimeRun autonomy grant revision must increase by one.",
                )
            if current_grant.revocation_generation > 0:
                raise RuntimeRunTransitionError(
                    "RuntimeRun revoked autonomy grant cannot be revived.",
                )
            if candidate.revocation_generation not in {
                current_grant.revocation_generation,
                current_grant.revocation_generation + 1,
            }:
                raise RuntimeRunTransitionError(
                    "RuntimeRun autonomy revocation generation is not monotonic.",
                )
        else:
            if current_grant.revocation_generation == 0:
                raise RuntimeRunTransitionError(
                    "RuntimeRun active autonomy grant must be revoked before replacement.",
                )
            if candidate.revision != 1 or candidate.revocation_generation != 0:
                raise RuntimeRunTransitionError(
                    "RuntimeRun replacement autonomy grant must start at revision 1.",
                )

    if action.type == "set_wake_intent":
        current_task = run.task_contract
        wake = action.wake_intent
        grant = current_task.autonomy_grant if current_task is not None else None
        if current_task is None or grant is None or wake is None:
            raise RuntimeRunTransitionError(
                "RuntimeRun wake intent requires a current autonomy grant.",
            )
        if wake.run_id != run.run_id or wake.task_revision != current_task.revision:
            raise RuntimeRunTransitionError(
                "RuntimeRun wake intent does not match the current task revision.",
            )
        if (
            wake.grant_id != grant.grant_id
            or wake.grant_revision != grant.revision
            or wake.revocation_generation != grant.revocation_generation
        ):
            raise RuntimeRunTransitionError(
                "RuntimeRun wake intent does not match the current grant revision.",
            )

    if action.wait_reason is not None and target_status not in {
        "paused",
        "waiting_approval",
    }:
        raise RuntimeRunTransitionError(
            "RuntimeRun wait state requires a paused or approval-waiting status.",
        )

    update: dict[str, object] = {
        "status": target_status,
        "revision": run.revision + 1,
    }
    if action.type != "set_progress_report":
        update["heartbeat_at"] = action.now
    if action.type == "claim":
        update["lease_owner"] = action.lease_owner or run.lease_owner
        if action.lease_owner:
            update["lease_expires_at"] = run.lease_expires_at
    if action.type in {"start", "resume"}:
        update["started_at"] = run.started_at or action.now
        if action.lease_owner:
            update["lease_owner"] = action.lease_owner
    if action.type == "request_stop":
        update["stop_reason"] = action.reason
        current_task = run.task_contract
        current_grant = (
            current_task.autonomy_grant if current_task is not None else None
        )
        if current_task is not None and current_grant is not None:
            if current_grant.revocation_generation == 0:
                revocation_reason = "runtime_stop_requested"
                if action.reason is not None:
                    revocation_reason = (
                        action.reason.message.strip() or action.reason.code
                    )
                revoked_grant = RuntimeAutonomyGrant.model_validate(
                    {
                        **current_grant.model_dump(),
                        "revision": current_grant.revision + 1,
                        "revocation_generation": (
                            current_grant.revocation_generation + 1
                        ),
                        "revoked_at": action.now,
                        "revocation_reason": revocation_reason,
                    }
                )
                update["task_contract"] = current_task.model_copy(
                    update={"autonomy_grant": revoked_grant},
                )
    if action.type == "update_budget":
        update["budget"] = action.budget
    if action.type == "revise_task":
        update["task_contract"] = action.task_contract
    if action.type == "set_autonomy_grant":
        assert run.task_contract is not None
        update["task_contract"] = run.task_contract.model_copy(
            update={"autonomy_grant": action.autonomy_grant},
        )
    if action.type == "set_wake_intent":
        update["wake_intent"] = action.wake_intent
        assert action.wake_intent is not None
        update["wait_reason"] = action.wake_intent.wait_reason
        update["next_eligible_at"] = action.wake_intent.next_eligible_at
    if action.type == "clear_wake_intent":
        update["wake_intent"] = None
    if action.type == "set_progress_report":
        update["progress_report"] = action.progress_report
    if action.type in {
        "request_stop",
        "pause",
        "settle",
        "revise_task",
        "set_autonomy_grant",
    }:
        update["wake_intent"] = None
    if action.type == "wait_approval":
        update["wait_reason"] = action.wait_reason or "approval"
        update["next_eligible_at"] = action.next_eligible_at
    if action.type == "pause":
        update["wait_reason"] = action.wait_reason
        update["next_eligible_at"] = action.next_eligible_at
    if action.type == "heartbeat" and (
        action.wait_reason is not None or action.next_eligible_at is not None
    ):
        update["wait_reason"] = action.wait_reason
        update["next_eligible_at"] = action.next_eligible_at
    if action.type in {"start", "resume", "request_stop", "settle"}:
        update["wait_reason"] = None
        update["next_eligible_at"] = None
    if action.type == "heartbeat" and action.current_step is not None:
        update["current_step"] = action.current_step
    if action.type == "heartbeat" and action.checkpoint is not None:
        update["checkpoint"] = action.checkpoint
    if action.type == "settle" and target_status in TERMINAL_RUNTIME_RUN_STATUSES:
        update["settled_at"] = action.now
        if action.delivery_status is not None:
            update["delivery_status"] = action.delivery_status
        if action.completion_evidence is not None:
            update["completion_evidence"] = action.completion_evidence
        if target_status in {"failed", "stalled"}:
            update["error"] = action.reason
        if target_status == "cancelled":
            update["stop_reason"] = action.reason or run.stop_reason

    reduced = run.model_copy(update=update)
    return RuntimeRunReducerResult(
        run=reduced,
        before_status=current_status,
        after_status=reduced.status,
        before_revision=run.revision,
        after_revision=reduced.revision,
    )


def start_runtime_run_attempt(
    run: RuntimeRunContract,
    *,
    attempt_id: str,
    attempt_reason: RuntimeAttemptReason,
    lease_owner: str | None = None,
    now: datetime | None = None,
) -> RuntimeAttemptContract:
    """Create a new attempt snapshot without mutating the previous attempt."""

    new_attempt_id = attempt_id.strip()
    if not new_attempt_id:
        raise RuntimeRunTransitionError("RuntimeRun new attempt_id cannot be empty.")
    if new_attempt_id == run.attempt_id:
        raise RuntimeRunTransitionError(
            "RuntimeRun retry/resume/takeover requires a new attempt_id.",
        )
    created_at = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    task_contract = run.task_contract
    if task_contract is not None:
        task_contract = task_contract.model_copy(
            update={"run_id": run.run_id, "attempt_id": new_attempt_id},
        )
    return RuntimeAttemptContract(
        run_id=run.run_id,
        attempt_id=new_attempt_id,
        source=run.source,
        owner=run.owner,
        status="queued",
        revision=0,
        lease_owner=lease_owner,
        lease_expires_at=None,
        created_at=created_at,
        started_at=None,
        heartbeat_at=None,
        settled_at=None,
        budget=run.budget,
        task_contract=task_contract,
        delivery_status="pending",
        completion_evidence=None,
        progress_report=None,
        checkpoint=run.checkpoint,
        current_step=None,
        error=None,
        stop_reason=None,
        contract_hash=run.contract_hash,
        policy_snapshot_hash=run.policy_snapshot_hash,
        release_edition=run.release_edition,
        attempt_reason=attempt_reason,
        previous_attempt_id=run.attempt_id,
    )


def assess_runtime_run_heartbeat(
    run: RuntimeRunContract,
    *,
    now: datetime | None = None,
    timeout_seconds: float,
) -> RuntimeStallAssessment:
    """Mark stale heartbeats as suspected only; callers must settle explicitly."""

    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    heartbeat_at = run.heartbeat_at or run.started_at or run.created_at
    elapsed_seconds = None
    suspected = False
    if heartbeat_at is not None and run.status in ACTIVE_RUNTIME_RUN_STATUSES:
        elapsed_seconds = max(0.0, (observed_at - heartbeat_at).total_seconds())
        suspected = elapsed_seconds > timeout_seconds
    return RuntimeStallAssessment(
        run=run,
        suspected_stalled=suspected,
        elapsed_seconds=elapsed_seconds,
        timeout_seconds=timeout_seconds,
    )
