# -*- coding: utf-8 -*-
"""Sanitized LOOP-04 export package contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lantai.runtime.sanitizer import sanitize_runtime_payload

from .events import RuntimeEvent
from .loops import LoopContract, LoopEvaluationReport, loop_contract_with_hash
from .runs import RuntimeRunContract


RUNTIME_LOOP_EXPORT_SCHEMA_VERSION = 1
_STRICT_CONFIG = ConfigDict(extra="forbid", frozen=True)


def _utc_datetime(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_payload(value: object) -> str:
    return f"sha256:{hashlib.sha256(_stable_json(value).encode('utf-8')).hexdigest()}"


class LoopExportTraceEvent(BaseModel):
    """Sanitized trace row included in a Loop export package."""

    model_config = _STRICT_CONFIG

    event_id: str
    seq: int = 0
    type: str
    status: str | None = None
    source: str = "system"
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] | None = None
    created_at: str | None = None


class LoopExportRuntimeRunSummary(BaseModel):
    """RuntimeRun control-plane summary embedded in a Loop export."""

    model_config = _STRICT_CONFIG

    run_id: str
    attempt_id: str
    source: str
    status: str
    revision: int
    created_at: datetime
    started_at: datetime | None = None
    settled_at: datetime | None = None
    heartbeat_at: datetime | None = None
    release_edition: str
    current_step: dict[str, Any] | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    stop_reason: dict[str, Any] | None = None


class LoopExportArtifactManifest(BaseModel):
    """Sanitized artifact manifest view embedded in a Loop export."""

    model_config = _STRICT_CONFIG

    schema_version: int = 1
    manifest_hash: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class LoopExportRetentionSummary(BaseModel):
    """Human-readable retention impact summary for exported loop data."""

    model_config = _STRICT_CONFIG

    artifact_count: int = 0
    retention_policies: dict[str, int] = Field(default_factory=dict)
    contract_retention_days: int
    export_requires_approval: bool
    delete_artifacts_on_contract_delete: bool


class LoopExportPackage(BaseModel):
    """Complete sanitized package for a long-running Loop audit/export."""

    model_config = _STRICT_CONFIG

    schema_version: Literal[1] = RUNTIME_LOOP_EXPORT_SCHEMA_VERSION
    exported_at: datetime
    exported_by: str
    export_reason: str
    contract: LoopContract
    runtime_run: LoopExportRuntimeRunSummary
    trace_events: list[LoopExportTraceEvent] = Field(default_factory=list)
    latest_evaluation: LoopEvaluationReport | None = None
    artifact_manifest: LoopExportArtifactManifest
    retention_summary: LoopExportRetentionSummary
    version_info: dict[str, str] = Field(default_factory=dict)


def _event_mapping(event: RuntimeEvent | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(event, RuntimeEvent):
        return event.model_dump(mode="json")
    return dict(event)


def _sanitize_trace_event(
    event: RuntimeEvent | Mapping[str, Any],
) -> LoopExportTraceEvent:
    raw = _event_mapping(event)
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    usage = raw.get("usage")
    usage_payload = dict(usage) if isinstance(usage, dict) else None
    event_id = str(raw.get("id") or raw.get("event_id") or "").strip()
    if not event_id:
        event_id = _sha256_payload(raw)
    return LoopExportTraceEvent(
        event_id=event_id,
        seq=int(raw.get("seq") or 0),
        type=str(raw.get("type") or "runtime.warning"),
        status=(str(raw["status"]) if raw.get("status") is not None else None),
        source=str(raw.get("source") or "system"),
        summary=str(raw.get("summary") or ""),
        payload=sanitize_runtime_payload(payload),
        usage=usage_payload,
        created_at=(
            str(raw["created_at"]) if raw.get("created_at") is not None else None
        ),
    )


def _sanitize_artifact(item: Mapping[str, Any]) -> dict[str, Any]:
    artifact = dict(item)
    metadata = artifact.get("metadata") or {}
    artifact["metadata"] = sanitize_runtime_payload(
        dict(metadata) if isinstance(metadata, dict) else {"value": metadata}
    )
    return sanitize_runtime_payload(artifact)


def _artifact_manifest(
    manifest: Mapping[str, Any] | None,
) -> LoopExportArtifactManifest:
    source = dict(manifest or {})
    raw_artifacts = source.get("artifacts") or []
    artifacts = [
        _sanitize_artifact(item)
        for item in raw_artifacts
        if isinstance(item, Mapping)
    ]
    payload = {
        "schema_version": int(source.get("schema_version") or 1),
        "artifacts": artifacts,
    }
    return LoopExportArtifactManifest(
        schema_version=payload["schema_version"],
        manifest_hash=_sha256_payload(payload),
        artifacts=artifacts,
    )


def _retention_summary(
    contract: LoopContract,
    artifact_manifest: LoopExportArtifactManifest,
) -> LoopExportRetentionSummary:
    policies: dict[str, int] = {}
    for artifact in artifact_manifest.artifacts:
        retention = str(artifact.get("retention") or "task_lifetime")
        policies[retention] = policies.get(retention, 0) + 1
    return LoopExportRetentionSummary(
        artifact_count=len(artifact_manifest.artifacts),
        retention_policies=policies,
        contract_retention_days=contract.artifact_policy.retention_days,
        export_requires_approval=contract.artifact_policy.export_requires_approval,
        delete_artifacts_on_contract_delete=(
            contract.artifact_policy.delete_artifacts_on_contract_delete
        ),
    )


def _runtime_run_summary(run: RuntimeRunContract) -> LoopExportRuntimeRunSummary:
    return LoopExportRuntimeRunSummary(
        run_id=run.run_id,
        attempt_id=run.attempt_id,
        source=run.source,
        status=run.status,
        revision=run.revision,
        created_at=run.created_at,
        started_at=run.started_at,
        settled_at=run.settled_at,
        heartbeat_at=run.heartbeat_at,
        release_edition=run.release_edition,
        current_step=(
            run.current_step.model_dump(mode="json")
            if run.current_step is not None
            else None
        ),
        budget=run.budget.model_dump(mode="json"),
        checkpoint=(
            run.checkpoint.model_dump(mode="json")
            if run.checkpoint is not None
            else None
        ),
        error=run.error.model_dump(mode="json") if run.error is not None else None,
        stop_reason=(
            run.stop_reason.model_dump(mode="json")
            if run.stop_reason is not None
            else None
        ),
    )


def build_loop_export_package(
    *,
    contract: LoopContract,
    run: RuntimeRunContract,
    trace_events: Sequence[RuntimeEvent | Mapping[str, Any]],
    latest_evaluation: LoopEvaluationReport | None,
    artifact_manifest: Mapping[str, Any] | None,
    version_info: Mapping[str, object] | None,
    exported_by: str,
    export_reason: str,
    exported_at: datetime | None = None,
) -> LoopExportPackage:
    """Build a strict, sanitized Loop export package without writing files."""

    hashed_contract = loop_contract_with_hash(contract)
    sanitized_manifest = _artifact_manifest(artifact_manifest)
    version = {
        str(key): str(value)
        for key, value in dict(version_info or {}).items()
    }
    return LoopExportPackage(
        exported_at=_utc_datetime(exported_at),
        exported_by=exported_by,
        export_reason=export_reason,
        contract=hashed_contract,
        runtime_run=_runtime_run_summary(run),
        trace_events=[_sanitize_trace_event(event) for event in trace_events],
        latest_evaluation=latest_evaluation,
        artifact_manifest=sanitized_manifest,
        retention_summary=_retention_summary(hashed_contract, sanitized_manifest),
        version_info=version,
    )
