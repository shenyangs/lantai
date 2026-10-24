# -*- coding: utf-8 -*-
"""Runtime run export package assembly."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from lantai.runtime import (
    LoopExportPackage,
    RuntimeRunContract,
    build_loop_export_package,
    loop_contract_with_hash,
)
from lantai.runtime.store import RuntimeRunStore

from .capabilities.artifacts import load_artifact_manifest
from .experience import build_runtime_run_events_response


class RuntimeRunExportError(ValueError):
    """Raised when a Runtime run cannot be exported as a Loop package."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _run_artifact_manifest(
    workspace_dir: str | Path,
    *,
    run_id: str,
) -> dict[str, Any]:
    manifest = load_artifact_manifest(workspace_dir)
    artifacts = [
        item
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict) and str(item.get("run_id") or "") == run_id
    ]
    return {
        "schema_version": int(manifest.get("schema_version") or 1),
        "artifacts": artifacts,
    }


async def build_runtime_run_export_package(
    *,
    workspace_dir: str | Path,
    run: RuntimeRunContract,
    exported_by: str,
    export_reason: str,
    version_info: Mapping[str, object] | None = None,
) -> LoopExportPackage:
    """Build a sanitized Loop export package from existing runtime stores."""

    store = RuntimeRunStore(workspace_dir)
    contract_snapshot = await store.load_loop_contract_snapshot(
        run.run_id,
        expected_attempt_id=run.attempt_id,
    )
    if contract_snapshot is None:
        raise RuntimeRunExportError(
            "loop_contract_not_found",
            "Runtime run does not have a persisted Loop contract.",
        )

    evaluation_snapshot = await store.load_latest_loop_evaluation_report(
        run.run_id,
        expected_attempt_id=run.attempt_id,
    )
    contract = loop_contract_with_hash(contract_snapshot.contract)
    latest_evaluation = evaluation_snapshot.report if evaluation_snapshot else None
    if latest_evaluation is not None and (
        latest_evaluation.contract_id != contract.contract_id
        or latest_evaluation.contract_hash != contract.contract_hash
    ):
        raise RuntimeRunExportError(
            "loop_evaluation_contract_mismatch",
            "Latest Loop evaluation does not match the exported contract.",
        )

    events_response = build_runtime_run_events_response(
        [workspace_dir],
        run.run_id,
        after_seq=0,
        attempt_id=run.attempt_id,
    )
    trace_events = [
        event.model_dump(mode="json")
        for event in events_response.events
    ]
    return build_loop_export_package(
        contract=contract,
        run=run,
        trace_events=trace_events,
        latest_evaluation=latest_evaluation,
        artifact_manifest=_run_artifact_manifest(workspace_dir, run_id=run.run_id),
        version_info=version_info,
        exported_by=exported_by,
        export_reason=export_reason,
    )
