# -*- coding: utf-8 -*-
"""Behavior locks for LOOP-04 sanitized Loop export packages."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from lantai.runtime import (
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopDeterministicGateResult,
    LoopExportPackage,
    LoopRubric,
    LoopRubricDimension,
    LoopRubricDimensionScore,
    RuntimeRunContract,
    RuntimeRunOwner,
    build_loop_export_package,
    evaluate_loop_contract,
)


NOW = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)


def _contract() -> LoopContract:
    return LoopContract(
        contract_id="loop-export-1",
        objective="Export a long-running loop audit package",
        scope=["Runtime migration export"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="package",
                title="Export package",
                acceptance=["Trace, evaluation, and artifacts are included"],
            ),
        ],
        acceptance=["Package can be reviewed without raw secrets"],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="runtime-tests",
                name="Runtime tests",
                kind="test",
                command="uv run pytest tests/unit/runtime -q",
            ),
        ],
        rubric=LoopRubric(
            rubric_id="export-rubric",
            dimensions=[
                LoopRubricDimension(
                    dimension_id="privacy",
                    name="Privacy",
                    weight=1,
                ),
            ],
        ),
    )


def _run() -> RuntimeRunContract:
    return RuntimeRunContract(
        run_id="run-export-1",
        attempt_id="attempt-export-1",
        source="chat",
        owner=RuntimeRunOwner(
            user_id="user-1",
            agent_id="agent-1",
            session_id="session-1",
            workspace_id="workspace-1",
            channel="console",
        ),
        status="succeeded",
        revision=7,
        created_at=NOW,
        started_at=NOW,
        settled_at=NOW,
        heartbeat_at=NOW,
        release_edition="official",
    )


def _evaluation():
    return evaluate_loop_contract(
        _contract(),
        gate_results=[
            LoopDeterministicGateResult(gate_id="runtime-tests", status="passed")
        ],
        rubric_scores=[
            LoopRubricDimensionScore(
                dimension_id="privacy",
                score=4.5,
                rationale_summary="Sensitive trace fields were removed.",
                evidence_hash="sha256:privacy-evidence",
            ),
        ],
        report_id="export-evaluation-1",
    )


def test_loop_export_package_contains_auditable_sections() -> None:
    package = build_loop_export_package(
        contract=_contract(),
        run=_run(),
        trace_events=[
            {
                "id": "event-1",
                "seq": 1,
                "type": "runtime.warning",
                "source": "system",
                "summary": "warning with sensitive payload",
                "payload": {
                    "summary": "warning",
                    "authorization": "Bearer secret-token",
                    "details": {"cookie": "session-cookie"},
                },
                "created_at": "2026-07-17T18:00:00Z",
            },
        ],
        latest_evaluation=_evaluation(),
        artifact_manifest={
            "schema_version": 1,
            "artifacts": [
                {
                    "id": "artifact-1",
                    "title": "Evidence",
                    "sha256": "abc123",
                    "retention": "30d",
                    "metadata": {
                        "api_key": "sk-secret",
                        "note": "safe metadata",
                    },
                },
            ],
        },
        version_info={
            "app_version": "1.0.10",
            "runtime_schema": "v1",
        },
        exported_by="user-1",
        export_reason="operator handoff",
        exported_at=NOW,
    )
    dumped = package.model_dump(mode="json")
    dumped_text = json.dumps(dumped, ensure_ascii=False, sort_keys=True)

    assert dumped["schema_version"] == 1
    assert dumped["contract"]["contract_id"] == "loop-export-1"
    assert dumped["contract"]["contract_hash"].startswith("sha256:")
    assert dumped["runtime_run"]["run_id"] == "run-export-1"
    assert dumped["runtime_run"]["status"] == "succeeded"
    assert dumped["latest_evaluation"]["report_id"] == "export-evaluation-1"
    assert dumped["artifact_manifest"]["schema_version"] == 1
    assert dumped["artifact_manifest"]["artifacts"][0]["metadata"]["note"] == "safe metadata"
    assert dumped["retention_summary"]["artifact_count"] == 1
    assert dumped["version_info"]["app_version"] == "1.0.10"
    assert "secret-token" not in dumped_text
    assert "session-cookie" not in dumped_text
    assert "sk-secret" not in dumped_text
    assert dumped["trace_events"][0]["payload"]["redactions"]
    assert dumped["artifact_manifest"]["artifacts"][0]["metadata"]["redactions"]


def test_loop_export_package_rejects_unknown_fields() -> None:
    package = build_loop_export_package(
        contract=_contract(),
        run=_run(),
        trace_events=[],
        latest_evaluation=None,
        artifact_manifest={"schema_version": 1, "artifacts": []},
        version_info={"app_version": "1.0.10"},
        exported_by="user-1",
        export_reason="operator handoff",
        exported_at=NOW,
    )
    payload = package.model_dump(mode="json")
    payload["generator_hidden_reasoning"] = "not allowed"

    with pytest.raises(ValidationError):
        LoopExportPackage.model_validate(payload)
