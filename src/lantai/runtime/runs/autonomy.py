# -*- coding: utf-8 -*-
"""Fail-closed admission policy for task-scoped unattended execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    RuntimeAutoResumeCondition,
    RuntimeAutonomyOperation,
    RuntimeRunContract,
)


@dataclass(frozen=True)
class RuntimeAutonomyDecision:
    allowed: bool
    code: str
    message: str


def _decision(allowed: bool, code: str, message: str) -> RuntimeAutonomyDecision:
    return RuntimeAutonomyDecision(allowed=allowed, code=code, message=message)


def _utc(value: datetime | None) -> datetime:
    observed = value or datetime.now(timezone.utc)
    return observed if observed.tzinfo is not None else observed.replace(tzinfo=timezone.utc)


def evaluate_runtime_autonomy(
    run: RuntimeRunContract,
    *,
    operation: RuntimeAutonomyOperation,
    chat_id: str,
    workspace_id: str,
    now: datetime | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    resume_condition: RuntimeAutoResumeCondition | None = None,
    target_path: str | None = None,
) -> RuntimeAutonomyDecision:
    """Re-evaluate current user authority for one bounded operation.

    A caller must invoke this again at each resume or external action boundary;
    an earlier allow result is not a transferable approval token.
    """

    if run.status in {
        "paused",
        "waiting_approval",
        "stop_requested",
        "succeeded",
        "failed",
        "cancelled",
        "stalled",
    }:
        return _decision(
            False,
            "autonomy_run_status_blocked",
            f"Runtime status {run.status!r} cannot be resumed automatically.",
        )
    task = run.task_contract
    if task is None:
        return _decision(
            False,
            "autonomy_task_contract_missing",
            "The RuntimeRun has no authoritative task contract.",
        )
    grant = task.autonomy_grant
    if grant is None:
        return _decision(
            False,
            "autonomy_grant_missing",
            "Ordinary tasks do not carry unattended execution authority.",
        )
    if grant.revocation_generation > 0 or grant.revoked_at is not None:
        return _decision(
            False,
            "autonomy_grant_revoked",
            "The user revoked unattended execution authority.",
        )
    observed_at = _utc(now)
    if observed_at < grant.starts_at:
        return _decision(
            False,
            "autonomy_grant_not_started",
            "The unattended execution window has not started.",
        )
    if observed_at >= grant.expires_at:
        return _decision(
            False,
            "autonomy_grant_expired",
            "The unattended execution window has expired.",
        )
    if grant.run_id != run.run_id or (task.run_id and task.run_id != run.run_id):
        return _decision(
            False,
            "autonomy_run_mismatch",
            "The grant is bound to a different RuntimeRun.",
        )
    if grant.chat_id != str(chat_id or "").strip():
        return _decision(
            False,
            "autonomy_chat_mismatch",
            "The grant is bound to a different chat.",
        )
    if grant.workspace_id != str(workspace_id or "").strip():
        return _decision(
            False,
            "autonomy_workspace_mismatch",
            "The grant is bound to a different workspace.",
        )
    if grant.target_task_revision != task.revision:
        return _decision(
            False,
            "autonomy_task_revision_mismatch",
            "A later task revision requires a fresh user grant.",
        )
    if operation not in grant.allowed_operations:
        return _decision(
            False,
            "autonomy_operation_not_allowed",
            f"Operation {operation!r} is outside the user grant.",
        )
    if operation == "automatic_resume":
        if resume_condition is None:
            return _decision(
                False,
                "autonomy_resume_condition_required",
                "Automatic resume requires a concrete recovery condition.",
            )
        if resume_condition not in grant.auto_resume_conditions:
            return _decision(
                False,
                "autonomy_resume_condition_not_allowed",
                "The recovery condition is outside the user grant.",
            )
    if operation in {"local_read", "local_write_new", "local_modify_existing"}:
        raw_target = str(target_path or "").strip()
        if not raw_target:
            return _decision(
                False,
                "autonomy_target_path_required",
                "Local operations require the concrete target path.",
            )
        target = Path(raw_target).expanduser().resolve(strict=False)
        allowed = False
        for raw_root in grant.allowed_paths:
            root = Path(raw_root).expanduser().resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError:
                continue
            allowed = True
            break
        if not allowed:
            return _decision(
                False,
                "autonomy_path_not_allowed",
                "The canonical target path is outside the user-approved roots.",
            )
    if operation == "network_read" and not grant.allow_network:
        return _decision(
            False,
            "autonomy_network_disabled",
            "The grant does not allow network access.",
        )
    if operation == "gui" and not grant.allow_gui:
        return _decision(
            False,
            "autonomy_gui_disabled",
            "The grant does not allow GUI control.",
        )
    if operation == "external_delivery" and not grant.allow_external_delivery:
        return _decision(
            False,
            "autonomy_external_delivery_disabled",
            "The grant does not allow external delivery.",
        )
    if operation == "model_call":
        provider = str(provider_id or "").strip()
        if not provider or provider not in grant.allowed_provider_ids:
            return _decision(
                False,
                "autonomy_provider_not_allowed",
                "The model provider is not in the user-approved set.",
            )
        model = str(model_id or "").strip()
        if not model or model not in grant.allowed_model_ids:
            return _decision(
                False,
                "autonomy_model_not_allowed",
                "The model is not in the user-approved set.",
            )
        if grant.budget_currency != "CNY" or grant.max_cost_micro_cny is None:
            return _decision(
                False,
                "autonomy_budget_missing",
                "Billable unattended work requires an exact CNY task budget.",
            )
        if not any(
            price_bound.provider_id == provider
            and price_bound.model_id == model
            and price_bound.upper_bound_proven is True
            for price_bound in grant.model_price_bounds
        ):
            return _decision(
                False,
                "autonomy_model_price_bound_missing",
                "The selected provider/model has no verified dispatch price bound.",
            )
    return _decision(
        True,
        "autonomy_allowed",
        "The current operation remains inside the active user grant.",
    )


def evaluate_runtime_wake(
    run: RuntimeRunContract,
    *,
    chat_id: str,
    workspace_id: str,
    now: datetime | None = None,
    resume_condition: RuntimeAutoResumeCondition | None = None,
) -> RuntimeAutonomyDecision:
    """Validate a persisted wake against current task and grant state."""

    wake = run.wake_intent
    if wake is None:
        return _decision(False, "wake_intent_missing", "No durable wake is pending.")
    task = run.task_contract
    if task is None:
        return _decision(
            False,
            "autonomy_task_contract_missing",
            "The RuntimeRun has no authoritative task contract.",
        )
    grant = task.autonomy_grant
    if grant is None:
        return _decision(
            False,
            "autonomy_grant_missing",
            "The pending wake has no current user grant.",
        )
    if wake.run_id != run.run_id or wake.task_revision != task.revision:
        return _decision(
            False,
            "wake_task_revision_mismatch",
            "The pending wake belongs to an older task version.",
        )
    if (
        wake.grant_id != grant.grant_id
        or wake.grant_revision != grant.revision
        or wake.revocation_generation != grant.revocation_generation
    ):
        return _decision(
            False,
            "wake_grant_revision_mismatch",
            "The pending wake belongs to an older grant version.",
        )
    if _utc(now) < wake.next_eligible_at:
        return _decision(
            False,
            "wake_not_due",
            "The durable retry boundary has not been reached.",
        )
    return evaluate_runtime_autonomy(
        run,
        operation="automatic_resume",
        chat_id=chat_id,
        workspace_id=workspace_id,
        now=now,
        resume_condition=resume_condition,
    )


__all__ = [
    "RuntimeAutonomyDecision",
    "evaluate_runtime_autonomy",
    "evaluate_runtime_wake",
]
