# -*- coding: utf-8 -*-
"""Runtime run export route behavior locks."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app.capabilities.artifacts import artifact_from_path, register_artifact
from lantai.app.experience.events import append_chat_event, record_runtime_notification
from lantai.app.routers import runtime as runtime_router
from lantai.runtime import (
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopDeterministicGateResult,
    LoopRubric,
    LoopRubricDimension,
    LoopRubricDimensionScore,
    RuntimeRunContract,
    RuntimeRunOwner,
    evaluate_loop_contract,
)
from lantai.runtime.store import RuntimeRunStore


NOW = datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc)


def _loop_contract() -> LoopContract:
    return LoopContract(
        contract_id="loop-export-route",
        objective="Export a real runtime run package",
        scope=["Runtime export route"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="package",
                title="Export package",
                acceptance=["Run evidence can be reviewed after restart"],
            ),
        ],
        acceptance=["Export package contains no raw secrets"],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="runtime-export-tests",
                name="Runtime export tests",
                kind="test",
                command="uv run pytest tests/unit/app/test_runtime_export.py -q",
            ),
        ],
        rubric=LoopRubric(
            rubric_id="export-route-rubric",
            dimensions=[
                LoopRubricDimension(
                    dimension_id="privacy",
                    name="Privacy",
                    weight=1,
                    target_score=4,
                ),
            ],
        ),
    )


def _evaluation(contract: LoopContract):
    return evaluate_loop_contract(
        contract,
        gate_results=[
            LoopDeterministicGateResult(
                gate_id="runtime-export-tests",
                status="passed",
            ),
        ],
        rubric_scores=[
            LoopRubricDimensionScore(
                dimension_id="privacy",
                score=4.5,
                rationale_summary="Trace and artifact metadata were sanitized.",
                evidence_hash="sha256:privacy",
            ),
        ],
        report_id="export-route-eval",
    )


async def _install_workspace(monkeypatch, workspace_dir):
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=workspace_dir,
        chat_manager=SimpleNamespace(list_chats=lambda: []),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)
    return workspace


async def _write_export_fixtures(workspace_dir):
    contract = _loop_contract()
    run = RuntimeRunContract(
        run_id="run-export-route",
        attempt_id="run-export-route:attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id="default",
            session_id="console:export",
            workspace_id=str(workspace_dir),
            channel="console",
        ),
        status="succeeded",
        revision=8,
        created_at=NOW,
        started_at=NOW,
        settled_at=NOW,
        heartbeat_at=NOW,
        release_edition="official",
    )
    store = RuntimeRunStore(workspace_dir)
    await store.save_contract(run, expected_revision=None)
    await store.save_loop_contract(
        run.run_id,
        attempt_id=run.attempt_id,
        contract=contract,
        saved_at=NOW,
    )
    await store.save_loop_evaluation_report(
        run.run_id,
        attempt_id=run.attempt_id,
        report=_evaluation(contract),
        saved_at=NOW,
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-export",
        session_id="console:export",
        run_id=run.run_id,
        kind="chat.system.event",
        type="runtime.warning",
        status="info",
        summary="export warning",
        payload={
            "attempt_id": run.attempt_id,
            "summary": "export trace",
            "authorization": "Bearer route-secret",
            "details": {"cookie": "route-cookie"},
        },
    )
    await record_runtime_notification(
        workspace_dir=workspace_dir,
        agent_id="default",
        target_id="chat-export",
        session_id="console:export",
        run=run,
        notification_kind="final_complete",
        contract_id=contract.contract_id,
        contract_hash=contract.contract_hash,
        trigger_reason="route_export_fixture",
    )
    artifact_path = workspace_dir / "report.md"
    artifact_path.write_text("# Export\n", encoding="utf-8")
    register_artifact(
        workspace_dir,
        artifact_from_path(
            artifact_path,
            title="Export report",
            run_id=run.run_id,
            attempt_id=run.attempt_id,
            retention="30d",
            metadata={"api_key": "sk-route-secret", "note": "safe"},
        ),
    )
    return run, contract


@pytest.mark.asyncio
async def test_runtime_export_requires_explicit_approval(monkeypatch, tmp_path):
    await _install_workspace(monkeypatch, tmp_path)
    run, _contract = await _write_export_fixtures(tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.export_runtime_run(
            SimpleNamespace(),
            run.run_id,
            runtime_router.RuntimeRunExportRequest(
                expected_attempt_id=run.attempt_id,
                expected_revision=run.revision,
                workspace_dir=str(tmp_path),
                reason="operator handoff",
            ),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "runtime_run_export_approval_required"


@pytest.mark.asyncio
async def test_runtime_export_builds_sanitized_package_from_runtime_state(
    monkeypatch,
    tmp_path,
):
    await _install_workspace(monkeypatch, tmp_path)
    run, contract = await _write_export_fixtures(tmp_path)

    result = await runtime_router.export_runtime_run(
        SimpleNamespace(),
        run.run_id,
        runtime_router.RuntimeRunExportRequest(
            approved=True,
            exported_by="user-1",
            reason="operator handoff",
            expected_attempt_id=run.attempt_id,
            expected_revision=run.revision,
            workspace_dir=str(tmp_path),
            version_info={"app_version": "1.0.10"},
        ),
    )
    dumped_text = json.dumps(result, ensure_ascii=False, sort_keys=True)

    assert result["exported"] is True
    assert result["run_id"] == run.run_id
    assert result["workspace_dir"] == str(tmp_path)
    assert result["package"]["contract"]["contract_id"] == contract.contract_id
    assert result["package"]["runtime_run"]["run_id"] == run.run_id
    assert result["package"]["latest_evaluation"]["report_id"] == "export-route-eval"
    assert result["package"]["artifact_manifest"]["artifacts"][0]["title"] == "Export report"
    assert result["package"]["retention_summary"]["artifact_count"] == 1
    assert result["package"]["version_info"]["app_version"] == "1.0.10"
    assert any(
        event["type"] == "runtime.notification"
        and event["payload"]["notification_kind"] == "final_complete"
        for event in result["package"]["trace_events"]
    )
    assert "route-secret" not in dumped_text
    assert "route-cookie" not in dumped_text
    assert "sk-route-secret" not in dumped_text
    assert result["package"]["trace_events"][0]["payload"]["redactions"]
    assert result["package"]["artifact_manifest"]["artifacts"][0]["metadata"]["redactions"]
