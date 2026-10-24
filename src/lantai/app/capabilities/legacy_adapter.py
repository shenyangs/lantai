# -*- coding: utf-8 -*-
"""Read-only compatibility adapter for legacy TaskRun snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lantai.runtime.runs import (
    RuntimeReleaseEdition,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunReason,
    RuntimeRunSource,
    RuntimeRunStatus,
)

from .models import TaskRun


_TASK_RUN_SOURCE_TO_RUNTIME: dict[str, RuntimeRunSource] = {
    "chat": "chat",
    "cron": "cron",
    "image": "capability",
    "capability": "capability",
    "pipeline": "pipeline",
}
_TASK_RUN_STATUS_TO_RUNTIME: dict[str, RuntimeRunStatus] = {
    "dispatched": "queued",
    "acked": "claimed",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "timed_out": "stalled",
    "stalled": "stalled",
    "blocked": "failed",
    "cancelled": "cancelled",
}


class LegacyTaskRunProjectionMismatch(BaseModel):
    """One difference found by the read-only shadow projection comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    field: str
    legacy_value: str
    canonical_value: str


class LegacyTaskRunProjectionReport(BaseModel):
    """Read-only comparison report for old TaskRun and canonical snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    side_effect_free: Literal[True] = True
    side_effects_executed: int = 0
    matched_run_ids: list[str]
    missing_run_ids: list[str]
    extra_run_ids: list[str]
    status_mismatches: list[LegacyTaskRunProjectionMismatch]


def _task_run_from(raw: Mapping[str, object] | TaskRun) -> TaskRun:
    return raw if isinstance(raw, TaskRun) else TaskRun.model_validate(raw)


def _runtime_source_for_task_run(source: str) -> RuntimeRunSource:
    return _TASK_RUN_SOURCE_TO_RUNTIME.get(source, "capability")


def _runtime_status_for_task_run(status: str) -> RuntimeRunStatus:
    return _TASK_RUN_STATUS_TO_RUNTIME.get(status, "running")


def _legacy_error_for_task_run(run: TaskRun) -> RuntimeRunReason | None:
    status = _runtime_status_for_task_run(run.status)
    if status not in {"failed", "stalled"}:
        return None
    return RuntimeRunReason(
        code="legacy_task_run_failure",
        message=run.failure_reason or run.status,
        category="runtime",
        retryable=status == "stalled",
    )


class LegacyTaskRunCompatibilityAdapter:
    """Convert old TaskRun display snapshots into RuntimeRunContract rows."""

    side_effect_free: Literal[True] = True

    def __init__(
        self,
        *,
        release_edition: RuntimeReleaseEdition = "official",
    ) -> None:
        self.release_edition = release_edition

    def to_runtime_run_contract(
        self,
        raw: Mapping[str, object] | TaskRun,
    ) -> RuntimeRunContract:
        run = _task_run_from(raw)
        workspace_id = (
            str(run.metadata.get("workspace_id") or "").strip()
            or str(run.metadata.get("workspace_dir") or "").strip()
            or None
        )
        status = _runtime_status_for_task_run(run.status)
        return RuntimeRunContract(
            run_id=run.id,
            attempt_id=str(
                run.metadata.get("attempt_id") or f"{run.id}:legacy-attempt",
            ),
            source=_runtime_source_for_task_run(run.source),
            owner=RuntimeRunOwner(
                user_id=str(run.metadata.get("user_id") or "default"),
                agent_id=run.agent_id,
                session_id=run.session_id,
                workspace_id=workspace_id,
                channel=str(run.metadata.get("channel") or "task_center"),
            ),
            status=status,
            revision=int(run.metadata.get("revision") or 0),
            lease_owner=None,
            lease_expires_at=None,
            created_at=run.started_at,
            started_at=run.started_at,
            heartbeat_at=run.last_heartbeat_at,
            settled_at=(
                run.ended_at
                if status in {"succeeded", "failed", "cancelled", "stalled"}
                else None
            ),
            error=_legacy_error_for_task_run(run),
            stop_reason=(
                RuntimeRunReason(
                    code="legacy_task_run_cancelled",
                    message=run.failure_reason or "cancelled",
                    category="user",
                )
                if status == "cancelled"
                else None
            ),
            contract_hash=str(run.metadata.get("contract_hash") or ""),
            policy_snapshot_hash=str(run.metadata.get("policy_snapshot_hash") or ""),
            release_edition=self.release_edition,
        )

    def to_runtime_run_contracts(
        self,
        rows: Sequence[Mapping[str, object] | TaskRun],
    ) -> list[RuntimeRunContract]:
        return [self.to_runtime_run_contract(row) for row in rows]


def legacy_task_run_to_runtime_contract(
    raw: Mapping[str, object] | TaskRun,
    *,
    release_edition: RuntimeReleaseEdition = "official",
) -> RuntimeRunContract:
    """Convert one legacy TaskRun snapshot to a RuntimeRunContract."""

    return LegacyTaskRunCompatibilityAdapter(
        release_edition=release_edition,
    ).to_runtime_run_contract(raw)


def compare_legacy_task_run_projection(
    legacy_runs: Sequence[Mapping[str, object] | TaskRun],
    canonical_runs: Sequence[Mapping[str, object] | RuntimeRunContract],
    *,
    release_edition: RuntimeReleaseEdition = "official",
) -> LegacyTaskRunProjectionReport:
    """Compare old TaskRun snapshots to canonical snapshots without side effects."""

    adapter = LegacyTaskRunCompatibilityAdapter(release_edition=release_edition)
    expected = {
        run.run_id: run
        for run in adapter.to_runtime_run_contracts(legacy_runs)
    }
    actual = {
        run.run_id: run
        for run in (
            row
            if isinstance(row, RuntimeRunContract)
            else RuntimeRunContract.model_validate(row)
            for row in canonical_runs
        )
    }
    matched = sorted(set(expected) & set(actual))
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatches = [
        LegacyTaskRunProjectionMismatch(
            run_id=run_id,
            field="status",
            legacy_value=expected[run_id].status,
            canonical_value=actual[run_id].status,
        )
        for run_id in matched
        if expected[run_id].status != actual[run_id].status
    ]
    return LegacyTaskRunProjectionReport(
        matched_run_ids=matched,
        missing_run_ids=missing,
        extra_run_ids=extra,
        status_mismatches=mismatches,
    )
