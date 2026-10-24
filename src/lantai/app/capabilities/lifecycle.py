# -*- coding: utf-8 -*-
from __future__ import annotations

from .models import TaskRun, TaskRunStatus, TaskStep, TaskStepStatus, utc_now

ACK_TIMEOUT_SECONDS = 300
EXECUTION_STALLED_SECONDS = 1200
HEARTBEAT_INTERVAL_SECONDS = 60

ACTIVE_TASK_STATUSES = {"dispatched", "acked", "running"}
TERMINAL_TASK_STATUSES = {
    "succeeded",
    "failed",
    "timed_out",
    "stalled",
    "blocked",
    "cancelled",
}

_ALLOWED_TASK_TRANSITIONS: dict[str, set[str]] = {
    "dispatched": {
        "acked",
        "running",
        "timed_out",
        "blocked",
        "cancelled",
        "failed",
    },
    "acked": {"running", "timed_out", "blocked", "cancelled", "failed"},
    "running": {"succeeded", "failed", "timed_out", "stalled", "blocked", "cancelled"},
}

_TERMINAL_STEP_STATUSES = {"succeeded", "failed", "skipped", "blocked", "stalled"}


def transition_task_run(
    run: TaskRun,
    status: TaskRunStatus,
    *,
    reason: str | None = None,
) -> TaskRun:
    if run.status == status:
        if reason:
            run.failure_reason = reason
        return run
    if run.status in TERMINAL_TASK_STATUSES:
        raise ValueError(f"TaskRun terminal status cannot transition: {run.status} -> {status}")
    if status not in _ALLOWED_TASK_TRANSITIONS.get(run.status, set()):
        raise ValueError(f"Illegal TaskRun status transition: {run.status} -> {status}")

    run.status = status
    run.last_heartbeat_at = utc_now()
    if status in TERMINAL_TASK_STATUSES:
        run.ended_at = run.ended_at or run.last_heartbeat_at
    if reason:
        run.failure_reason = reason
    return run


def heartbeat_task_run(run: TaskRun) -> TaskRun:
    if run.status not in TERMINAL_TASK_STATUSES:
        run.last_heartbeat_at = utc_now()
    return run


def transition_task_step(step: TaskStep, status: TaskStepStatus) -> TaskStep:
    if step.status == status:
        return step
    if step.status in _TERMINAL_STEP_STATUSES:
        raise ValueError(f"TaskStep terminal status cannot transition: {step.status} -> {status}")
    step.status = status
    if status in _TERMINAL_STEP_STATUSES:
        step.ended_at = step.ended_at or utc_now()
    return step
