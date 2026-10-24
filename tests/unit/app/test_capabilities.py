# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.capabilities.api import task_runs_router
from lantai.app.capabilities.artifacts import (
    artifact_gc_apply,
    artifact_gc_after_run_delete,
    artifact_gc_after_workspace_delete,
    artifact_from_path,
    artifact_gc_dry_run,
    artifact_gc_pressure_policy,
    artifact_lifecycle_cascade,
    artifact_manifest_path,
    register_artifact,
)
from lantai.app.capabilities.draft_builder import build_capability_draft
from lantai.app.capabilities.evaluations import build_report, make_finding
from lantai.app.capabilities.executor import CapabilityExecutor
from lantai.app.capabilities.fast_fail import capability_gate_report
from lantai.app.capabilities.models import (
    Capability,
    CapabilityParameter,
    CapabilityRun,
    CapabilityRunRequest,
    CapabilityStep,
    DeliveryTarget,
    EvaluationReport,
    Pipeline,
    PipelineEdge,
    PipelineNode,
    PipelineRun,
    PipelineRunNode,
    PipelineRunRequest,
    TaskRun,
    TaskStep,
)
from lantai.app.capabilities.lifecycle import transition_task_run
from lantai.app.capabilities.pipeline_executor import PipelineExecutor
from lantai.app.capabilities.repo import CapabilityRepository
from lantai.app.capabilities.service import CapabilityService
from lantai.app.capabilities.task_run_materializer import materialize_task_runs
from lantai.app.capabilities.template import render_template
from lantai.app.browser_workbench.models import (
    BrowserTaskRequest,
    BrowserTaskRun,
)
from lantai.app.crons.models import (
    CapabilityCronRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    PipelineCronRequest,
    ScheduleSpec,
)
from lantai.app.experience.events import append_chat_event
from lantai.runtime.store import RuntimeRunStore


def test_render_template_replaces_placeholders():
    assert (
        render_template("写一份 {{ topic }} 周报", {"topic": "产品"}) == "写一份 产品 周报"
    )


def test_render_template_reports_missing_value():
    with pytest.raises(ValueError, match="topic"):
        render_template("写一份 {{ topic }} 周报", {})


def test_render_template_supports_nested_node_paths():
    assert (
        render_template(
            "上游：{{nodes.node_a.output_text}} / {{pipeline.topic}}",
            {
                "nodes": {"node_a": {"output_text": "完成"}},
                "pipeline": {"topic": "周报"},
            },
        )
        == "上游：完成 / 周报"
    )


def test_artifact_from_path_records_integrity_and_runtime_metadata(
    tmp_path: Path,
):
    path = tmp_path / "report.md"
    content = b"# Report\n"
    path.write_bytes(content)

    artifact = artifact_from_path(
        path,
        title="Agent Report",
        producer="capability",
        run_id="run-1",
        attempt_id="attempt-1",
        step_id="step-1",
        effect_id="effect-1",
        classification="user_content",
        retention="task_lifetime",
    )

    assert artifact.title == "Agent Report"
    assert artifact.mime_type == "text/markdown"
    assert artifact.size == len(content)
    assert artifact.sha256 == hashlib.sha256(content).hexdigest()
    assert artifact.producer == "capability"
    assert artifact.run_id == "run-1"
    assert artifact.attempt_id == "attempt-1"
    assert artifact.step_id == "step-1"
    assert artifact.effect_id == "effect-1"
    assert artifact.classification == "user_content"
    assert artifact.retention == "task_lifetime"


def test_artifact_manifest_registers_and_reports_orphan_dry_run(
    tmp_path: Path,
):
    path = tmp_path / "report.md"
    path.write_text("# Report\n", encoding="utf-8")
    artifact = artifact_from_path(
        path,
        title="Agent Report",
        producer="capability",
        run_id="run-1",
        metadata={
            "authorization": "Bearer should-not-persist",
            "nested": {"api_key": "nested-secret"},
        },
    )

    assert "authorization" not in artifact.metadata
    assert artifact.metadata["nested"]["api_key"]["redacted"] is True
    assert artifact.metadata["data_classification"] == "sensitive"

    manifest_path = register_artifact(tmp_path, artifact)
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    report = artifact_gc_dry_run(tmp_path, live_artifacts=[])

    assert manifest_path == artifact_manifest_path(tmp_path)
    assert "should-not-persist" not in manifest_text
    assert "nested-secret" not in manifest_text
    assert (
        manifest["artifacts"][0]["metadata"]["data_classification"]
        == "sensitive"
    )
    assert report["dry_run"] is True
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["artifact_id"] == artifact.id
    assert report["candidates"][0]["path"] == str(path)
    assert path.exists()


def test_artifact_registration_never_replaces_a_corrupt_manifest(
    tmp_path: Path,
):
    path = tmp_path / "report.md"
    path.write_text("# Report\n", encoding="utf-8")
    artifact = artifact_from_path(path, title="Agent Report")
    manifest_path = artifact_manifest_path(tmp_path)
    manifest_path.parent.mkdir(parents=True)
    corrupt_bytes = b'{"schema_version": 1, "artifacts": ['
    manifest_path.write_bytes(corrupt_bytes)

    with pytest.raises(Exception):
        register_artifact(tmp_path, artifact)

    assert manifest_path.read_bytes() == corrupt_bytes


def test_artifact_gc_apply_deletes_safe_orphans_and_updates_manifest(
    tmp_path: Path,
):
    path = tmp_path / "report.md"
    path.write_text("# Report\n", encoding="utf-8")
    artifact = artifact_from_path(
        path,
        title="Agent Report",
        producer="capability",
        run_id="run-1",
    )
    register_artifact(tmp_path, artifact)

    report = artifact_gc_apply(tmp_path, live_artifacts=[])
    manifest = json.loads(artifact_manifest_path(tmp_path).read_text("utf-8"))
    snapshot_path = Path(report["manifest_snapshot_path"])
    snapshot = json.loads(snapshot_path.read_text("utf-8"))

    assert report["dry_run"] is False
    assert snapshot_path.exists()
    assert (
        report["manifest_snapshot_sha256"]
        == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    )
    assert snapshot["kind"] == "artifact_manifest_snapshot"
    assert snapshot["source_manifest_path"] == str(
        artifact_manifest_path(tmp_path),
    )
    assert snapshot["manifest"]["artifacts"][0]["id"] == artifact.id
    assert report["deleted_count"] == 1
    assert report["manifest_removed_count"] == 1
    assert report["skipped_count"] == 0
    assert report["candidates"][0]["deleted"] is True
    assert report["candidates"][0]["manifest_removed"] is True
    assert not path.exists()
    assert manifest["artifacts"] == []


def test_artifact_gc_apply_keeps_live_artifacts(tmp_path: Path):
    path = tmp_path / "live.md"
    path.write_text("# Live\n", encoding="utf-8")
    artifact = artifact_from_path(
        path,
        title="Live Artifact",
        producer="capability",
        run_id="run-1",
    )
    register_artifact(tmp_path, artifact)

    report = artifact_gc_apply(tmp_path, live_artifacts=[artifact])
    manifest = json.loads(artifact_manifest_path(tmp_path).read_text("utf-8"))

    assert report["candidate_count"] == 0
    assert report["manifest_snapshot_path"] is None
    assert report["manifest_snapshot_sha256"] is None
    assert report["deleted_count"] == 0
    assert path.exists()
    assert manifest["artifacts"][0]["id"] == artifact.id


def test_artifact_gc_apply_blocks_unsafe_or_changed_orphans(tmp_path: Path):
    safe_path = tmp_path / "changed.md"
    safe_path.write_text("original\n", encoding="utf-8")
    changed = artifact_from_path(safe_path, title="Changed")
    register_artifact(tmp_path, changed)
    safe_path.write_text("changed\n", encoding="utf-8")

    symlink_target = tmp_path / "target.md"
    symlink_target.write_text("target\n", encoding="utf-8")
    symlink_path = tmp_path / "linked.md"
    symlink_path.symlink_to(symlink_target)
    symlink_artifact = artifact_from_path(symlink_path, title="Linked")
    register_artifact(tmp_path, symlink_artifact)

    outside_path = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside_path.write_text("outside\n", encoding="utf-8")
    manifest_path = artifact_manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["artifacts"].append(
        {
            "id": "outside-artifact",
            "title": "Outside",
            "path": str(outside_path),
            "sha256": hashlib.sha256(b"outside\n").hexdigest(),
        },
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    report = artifact_gc_apply(tmp_path, live_artifacts=[])
    remaining = json.loads(manifest_path.read_text("utf-8"))["artifacts"]
    blocked = {
        item["artifact_id"]: item["blocked_reason"]
        for item in report["candidates"]
    }

    assert report["deleted_count"] == 0
    assert report["manifest_removed_count"] == 0
    assert report["manifest_snapshot_path"] is None
    assert report["manifest_snapshot_sha256"] is None
    assert report["skipped_count"] == 3
    assert safe_path.exists()
    assert symlink_path.exists()
    assert outside_path.exists()
    assert blocked[changed.id] == "artifact_sha256_mismatch"
    assert blocked[symlink_artifact.id] == "artifact_path_is_symlink"
    assert blocked["outside-artifact"] == "path_outside_workspace"
    assert {item["id"] for item in remaining} == {
        changed.id,
        symlink_artifact.id,
        "outside-artifact",
    }


def test_artifact_gc_apply_removes_missing_orphan_manifest_rows(
    tmp_path: Path,
):
    path = tmp_path / "missing.md"
    path.write_text("# Missing\n", encoding="utf-8")
    artifact = artifact_from_path(path, title="Missing")
    register_artifact(tmp_path, artifact)
    path.unlink()

    report = artifact_gc_apply(tmp_path, live_artifacts=[])
    manifest = json.loads(artifact_manifest_path(tmp_path).read_text("utf-8"))
    snapshot_path = Path(report["manifest_snapshot_path"])
    snapshot = json.loads(snapshot_path.read_text("utf-8"))

    assert report["deleted_count"] == 0
    assert report["manifest_removed_count"] == 1
    assert snapshot["manifest"]["artifacts"][0]["id"] == artifact.id
    assert report["candidates"][0]["blocked_reason"] == "artifact_file_missing"
    assert report["candidates"][0]["manifest_removed"] is True
    assert manifest["artifacts"] == []


def test_artifact_gc_apply_respects_retention_windows_and_policies(
    tmp_path: Path,
):
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    expired_path = tmp_path / "expired.md"
    active_path = tmp_path / "active.md"
    forever_path = tmp_path / "forever.md"
    unknown_path = tmp_path / "unknown.md"
    for path in (expired_path, active_path, forever_path, unknown_path):
        path.write_text(path.name, encoding="utf-8")

    expired = artifact_from_path(expired_path, title="Expired", retention="7d")
    active = artifact_from_path(active_path, title="Active", retention="7d")
    forever = artifact_from_path(
        forever_path,
        title="Forever",
        retention="forever",
    )
    unknown = artifact_from_path(
        unknown_path,
        title="Unknown",
        retention="mystery",
    )
    for artifact in (expired, active, forever, unknown):
        register_artifact(tmp_path, artifact)

    manifest_path = artifact_manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    created_at_by_id = {
        expired.id: now - timedelta(days=8),
        active.id: now - timedelta(days=1),
        forever.id: now - timedelta(days=30),
        unknown.id: now - timedelta(days=30),
    }
    for item in manifest["artifacts"]:
        item["created_at"] = (
            created_at_by_id[item["id"]].isoformat().replace("+00:00", "Z")
        )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    dry_run = artifact_gc_dry_run(tmp_path, live_artifacts=[], now=now)
    dry_by_id = {item["artifact_id"]: item for item in dry_run["candidates"]}
    report = artifact_gc_apply(tmp_path, live_artifacts=[], now=now)
    by_id = {item["artifact_id"]: item for item in report["candidates"]}
    remaining = json.loads(manifest_path.read_text("utf-8"))["artifacts"]

    assert dry_by_id[expired.id]["would_delete"] is True
    assert dry_by_id[active.id]["blocked_reason"] == "retention_window_active"
    assert report["deleted_count"] == 1
    assert report["manifest_removed_count"] == 1
    assert report["skipped_count"] == 3
    assert report["manifest_snapshot_path"] is not None
    assert not expired_path.exists()
    assert active_path.exists()
    assert forever_path.exists()
    assert unknown_path.exists()
    assert by_id[expired.id]["retention_eligible"] is True
    assert by_id[active.id]["blocked_reason"] == "retention_window_active"
    assert (
        by_id[forever.id]["blocked_reason"]
        == "retention_policy_preserves_artifact"
    )
    assert by_id[unknown.id]["blocked_reason"] == "retention_policy_unknown"
    assert {item["id"] for item in remaining} == {
        active.id,
        forever.id,
        unknown.id,
    }


def test_artifact_gc_reports_size_and_count_budgets(tmp_path: Path):
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    expired_path = tmp_path / "expired.txt"
    active_path = tmp_path / "active.txt"
    live_path = tmp_path / "live.txt"
    expired_path.write_bytes(b"expired")
    active_path.write_bytes(b"active")
    live_path.write_bytes(b"live")
    expired = artifact_from_path(expired_path, title="Expired", retention="7d")
    active = artifact_from_path(active_path, title="Active", retention="7d")
    live = artifact_from_path(live_path, title="Live")
    for artifact in (expired, active, live):
        register_artifact(tmp_path, artifact)

    manifest_path = artifact_manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    created_at_by_id = {
        expired.id: now - timedelta(days=8),
        active.id: now - timedelta(days=1),
        live.id: now,
    }
    for item in manifest["artifacts"]:
        item["created_at"] = (
            created_at_by_id[item["id"]].isoformat().replace("+00:00", "Z")
        )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    dry_run = artifact_gc_dry_run(
        tmp_path,
        live_artifacts=[live],
        now=now,
        max_artifact_count=2,
        max_artifact_bytes=10,
    )
    report = artifact_gc_apply(
        tmp_path,
        live_artifacts=[live],
        now=now,
        max_artifact_count=2,
        max_artifact_bytes=10,
    )
    remaining = json.loads(manifest_path.read_text("utf-8"))["artifacts"]

    assert dry_run["budget"]["current_artifact_count"] == 3
    assert dry_run["budget"]["current_artifact_bytes"] == 17
    assert dry_run["budget"]["orphan_artifact_count"] == 2
    assert dry_run["budget"]["reclaimable_artifact_count"] == 1
    assert dry_run["budget"]["reclaimable_artifact_bytes"] == 7
    assert dry_run["budget"]["budget_exceeded"] is True
    assert dry_run["budget"]["budget_satisfied_after_gc"] is True
    assert report["budget"]["projected_artifact_count_after_gc"] == 2
    assert report["budget"]["projected_artifact_bytes_after_gc"] == 10
    assert (
        report["budget"]["budget_blocked_by_live_or_retained_artifacts"]
        is False
    )
    assert not expired_path.exists()
    assert active_path.exists()
    assert live_path.exists()
    assert {item["id"] for item in remaining} == {active.id, live.id}


def test_artifact_gc_pressure_policy_schedules_and_applies_safe_gc(
    tmp_path: Path,
):
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    expired_path = tmp_path / "expired.txt"
    live_path = tmp_path / "live.txt"
    expired_path.write_bytes(b"expired")
    live_path.write_bytes(b"live")
    expired = artifact_from_path(expired_path, title="Expired", retention="7d")
    live = artifact_from_path(live_path, title="Live")
    for artifact in (expired, live):
        register_artifact(tmp_path, artifact)

    manifest_path = artifact_manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    for item in manifest["artifacts"]:
        item["created_at"] = (
            (now - timedelta(days=8)).isoformat().replace("+00:00", "Z")
            if item["id"] == expired.id
            else now.isoformat().replace("+00:00", "Z")
        )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    scheduled = artifact_gc_pressure_policy(
        tmp_path,
        live_artifacts=[live],
        mode="schedule",
        now=now,
        max_artifact_count=1,
        max_artifact_bytes=4,
    )
    assert scheduled["decision"] == "schedule_gc"
    assert scheduled["should_schedule_gc"] is True
    assert scheduled["should_apply_gc"] is False
    assert expired_path.exists()

    applied = artifact_gc_pressure_policy(
        tmp_path,
        live_artifacts=[live],
        mode="apply",
        now=now,
        max_artifact_count=1,
        max_artifact_bytes=4,
    )
    remaining = json.loads(manifest_path.read_text("utf-8"))["artifacts"]

    assert applied["decision"] == "gc_applied_budget_satisfied"
    assert applied["should_apply_gc"] is True
    assert applied["apply_report"]["deleted_count"] == 1
    assert not expired_path.exists()
    assert live_path.exists()
    assert {item["id"] for item in remaining} == {live.id}


def test_artifact_gc_pressure_policy_blocks_when_budget_cannot_be_reclaimed(
    tmp_path: Path,
):
    kept_path = tmp_path / "kept.txt"
    live_path = tmp_path / "live.txt"
    kept_path.write_bytes(b"kept")
    live_path.write_bytes(b"live")
    kept = artifact_from_path(kept_path, title="Kept", retention="user_saved")
    live = artifact_from_path(live_path, title="Live")
    for artifact in (kept, live):
        register_artifact(tmp_path, artifact)

    report = artifact_gc_pressure_policy(
        tmp_path,
        live_artifacts=[live],
        mode="block",
        max_artifact_count=1,
        max_artifact_bytes=4,
    )

    assert report["decision"] == "block_new_artifacts"
    assert report["pressure_triggered"] is True
    assert report["should_block_new_artifacts"] is True
    assert report["should_schedule_gc"] is False
    assert kept_path.exists()
    assert live_path.exists()


def test_artifact_run_delete_cascade_only_targets_matching_run(tmp_path: Path):
    run_artifact_path = tmp_path / "run-artifact.txt"
    saved_artifact_path = tmp_path / "saved-artifact.txt"
    other_run_path = tmp_path / "other-run.txt"
    run_artifact_path.write_text("delete", encoding="utf-8")
    saved_artifact_path.write_text("saved", encoding="utf-8")
    other_run_path.write_text("other", encoding="utf-8")
    run_artifact = artifact_from_path(
        run_artifact_path,
        title="Run Artifact",
        run_id="run-delete",
        retention="run_lifetime",
    )
    saved_artifact = artifact_from_path(
        saved_artifact_path,
        title="Saved Artifact",
        run_id="run-delete",
        retention="user_saved",
    )
    other_run_artifact = artifact_from_path(
        other_run_path,
        title="Other Run",
        run_id="run-keep",
        retention="run_lifetime",
    )
    for artifact in (run_artifact, saved_artifact, other_run_artifact):
        register_artifact(tmp_path, artifact)

    dry_run = artifact_gc_after_run_delete(
        tmp_path,
        run_id="run-delete",
        dry_run=True,
    )
    report = artifact_gc_after_run_delete(
        tmp_path,
        run_id="run-delete",
        dry_run=False,
    )
    remaining = json.loads(
        artifact_manifest_path(tmp_path).read_text("utf-8"),
    )["artifacts"]

    assert dry_run["cascade"]["scope"] == "run"
    assert dry_run["cascade"]["matching_manifest_count"] == 2
    assert dry_run["candidate_count"] == 2
    assert report["deleted_count"] == 1
    assert report["manifest_removed_count"] == 1
    assert not run_artifact_path.exists()
    assert saved_artifact_path.exists()
    assert other_run_path.exists()
    assert {item["id"] for item in remaining} == {
        saved_artifact.id,
        other_run_artifact.id,
    }


def test_artifact_workspace_delete_cascade_and_archive_snapshot(
    tmp_path: Path,
):
    transient_path = tmp_path / "transient.txt"
    saved_path = tmp_path / "saved.txt"
    transient_path.write_text("delete", encoding="utf-8")
    saved_path.write_text("saved", encoding="utf-8")
    transient = artifact_from_path(
        transient_path,
        title="Transient",
        retention="task_lifetime",
    )
    saved = artifact_from_path(
        saved_path,
        title="Saved",
        retention="user_saved",
    )
    for artifact in (transient, saved):
        register_artifact(tmp_path, artifact)

    archive = artifact_lifecycle_cascade(
        tmp_path,
        scope="workspace",
        operation="archive",
        dry_run=False,
    )
    report = artifact_gc_after_workspace_delete(tmp_path, dry_run=False)
    remaining = json.loads(
        artifact_manifest_path(tmp_path).read_text("utf-8"),
    )["artifacts"]

    assert archive["manifest_snapshot_path"] is not None
    assert Path(archive["manifest_snapshot_path"]).exists()
    assert archive["cascade"]["operation"] == "archive"
    assert report["cascade"]["scope"] == "workspace"
    assert report["deleted_count"] == 1
    assert not transient_path.exists()
    assert saved_path.exists()
    assert {item["id"] for item in remaining} == {saved.id}


@pytest.mark.asyncio
async def test_capability_repo_round_trips_capability(tmp_path: Path):
    repo = CapabilityRepository(tmp_path)
    capability = Capability(
        name="生成周报",
        enabled=True,
        parameters=[
            CapabilityParameter(name="topic", label="主题", default="项目"),
        ],
        steps=[
            CapabilityStep(
                title="写周报",
                instruction_template="围绕 {{topic}} 写周报",
                required_parameters=["topic"],
            ),
        ],
        delivery_defaults=[DeliveryTarget(type="chat")],
    )

    await repo.upsert_capability(capability)

    loaded = await repo.get_capability(capability.id)
    assert loaded is not None
    assert loaded.name == "生成周报"
    assert loaded.parameters[0].name == "topic"


@pytest.mark.asyncio
async def test_task_run_round_trips_old_and_new_lifecycle_fields(
    tmp_path: Path,
):
    repo = CapabilityRepository(tmp_path)
    root = tmp_path / "capabilities"
    root.mkdir(parents=True)
    (root / "task-runs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "runs": [
                    {
                        "id": "old-run",
                        "agent_id": "default",
                        "source": "chat",
                        "title": "旧任务",
                        "status": "running",
                        "started_at": "2026-05-25T00:00:00+00:00",
                        "steps": [],
                        "artifacts": [],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    old_run = await repo.get_task_run("old-run")

    assert old_run is not None
    assert old_run.last_heartbeat_at is None
    assert old_run.failure_reason is None
    assert old_run.metadata == {}
    assert old_run.evaluation_summary is None

    new_run = TaskRun(
        id="new-run",
        agent_id="default",
        source="capability",
        title="新任务",
        status="blocked",
        failure_reason="risk gate",
    )
    await repo.upsert_task_run(new_run)

    loaded = await repo.get_task_run("new-run")
    assert loaded is not None
    assert loaded.status == "blocked"
    assert loaded.failure_reason == "risk gate"


@pytest.mark.asyncio
async def test_task_run_storage_sanitizes_runtime_metadata(tmp_path: Path):
    repo = CapabilityRepository(tmp_path)
    artifact_path = tmp_path / "artifact.md"
    artifact_path.write_text("# Artifact\n", encoding="utf-8")
    artifact = artifact_from_path(
        artifact_path,
        title="Artifact",
        metadata={"api_key": "artifact-secret"},
    )
    run = TaskRun(
        id="sensitive-run",
        agent_id="default",
        source="capability",
        title="Sensitive Run",
        status="running",
        metadata={
            "channel": "console",
            "authorization": "Bearer task-secret",
        },
        steps=[
            TaskStep(
                kind="tool_call",
                title="Sensitive Step",
                metadata={"nested": {"token": "step-secret"}},
            ),
        ],
        artifacts=[artifact],
    )

    await repo.upsert_task_run(run)

    raw = (tmp_path / "capabilities" / "task-runs.json").read_text("utf-8")
    loaded = await repo.get_task_run("sensitive-run")

    assert "task-secret" not in raw
    assert "step-secret" not in raw
    assert "artifact-secret" not in raw
    assert loaded is not None
    assert loaded.metadata["channel"] == "console"
    assert "authorization" not in loaded.metadata
    assert loaded.metadata["data_classification"] == "sensitive"
    assert loaded.steps[0].metadata["nested"]["token"]["redacted"] is True
    assert loaded.artifacts[0].metadata["data_classification"] == "sensitive"


@pytest.mark.asyncio
async def test_evaluation_report_storage_sanitizes_finding_metadata(
    tmp_path: Path,
):
    repo = CapabilityRepository(tmp_path)
    report = build_report(
        task_run_id="eval-sensitive-run",
        evaluator="fast_fail_gate",
        findings=[
            make_finding(
                code="contains_secret",
                title="Secret metadata",
                detail="metadata should be redacted",
                metadata={
                    "api_key": "eval-secret",
                    "nested": {"password": "eval-password"},
                },
            ),
        ],
        passed_summary="Evaluation passed.",
        blocked_summary="Evaluation blocked.",
    )

    await repo.save_evaluation_report(report)

    raw = next(
        (
            tmp_path / "capabilities" / "evaluations" / "eval-sensitive-run"
        ).glob(
            "*.json",
        ),
    ).read_text("utf-8")
    loaded = await repo.list_evaluation_reports("eval-sensitive-run")

    assert "eval-secret" not in raw
    assert "eval-password" not in raw
    assert loaded[0].findings[0].metadata["data_classification"] == "sensitive"
    assert "api_key" not in loaded[0].findings[0].metadata
    assert (
        loaded[0].findings[0].metadata["nested"]["password"]["redacted"]
        is True
    )


@pytest.mark.asyncio
async def test_capability_replay_event_storage_sanitizes_payload(
    tmp_path: Path,
):
    repo = CapabilityRepository(tmp_path)

    path = await repo.append_replay_event(
        run_id="run-replay-sensitive",
        step_id="step-1",
        event={
            "ts": "2026-07-16T00:00:00+00:00",
            "kind": "tool_result",
            "payload": {
                "summary": "safe detail",
                "authorization": "Bearer replay-secret",
                "nested": {"password": "replay-password"},
            },
        },
    )

    raw = path.read_text("utf-8")
    row = json.loads(raw.splitlines()[0])

    assert "replay-secret" not in raw
    assert "replay-password" not in raw
    assert row["payload"]["summary"] == "safe detail"
    assert row["payload"]["authorization"]["redacted"] is True
    assert row["payload"]["nested"]["password"]["redacted"] is True
    assert row["data_classification"] == "sensitive"


@pytest.mark.asyncio
async def test_capability_and_pipeline_run_storage_sanitizes_parameters(
    tmp_path: Path,
):
    repo = CapabilityRepository(tmp_path)
    artifact_path = tmp_path / "run-artifact.md"
    artifact_path.write_text("# Artifact\n", encoding="utf-8")
    artifact = artifact_from_path(
        artifact_path,
        title="Run Artifact",
        metadata={"refresh_token": "artifact-refresh-secret"},
    )
    capability_run = CapabilityRun(
        id="cap-run-sensitive",
        capability_id="capability-1",
        capability_name="Capability",
        task_run_id="task-run-1",
        parameters={
            "topic": "weekly report",
            "api_key": "capability-secret",
        },
        artifacts=[artifact],
    )
    pipeline_run = PipelineRun(
        id="pipeline-run-sensitive",
        pipeline_id="pipeline-1",
        pipeline_name="Pipeline",
        task_run_id="task-run-2",
        parameters={"access_token": "pipeline-secret"},
        nodes=[
            PipelineRunNode(
                node_id="node-1",
                title="Node",
                capability_id="capability-1",
                artifacts=[artifact],
            ),
        ],
        artifacts=[artifact],
    )

    await repo.save_capability_run(capability_run)
    await repo.save_pipeline_run(pipeline_run)

    capability_raw = (
        tmp_path / "capabilities" / "runs" / "cap-run-sensitive.json"
    ).read_text("utf-8")
    pipeline_raw = (
        tmp_path
        / "capabilities"
        / "pipeline-runs"
        / "pipeline-run-sensitive.json"
    ).read_text("utf-8")
    loaded_capability = await repo.get_capability_run("cap-run-sensitive")
    loaded_pipeline = await repo.get_pipeline_run("pipeline-run-sensitive")

    assert "capability-secret" not in capability_raw
    assert "pipeline-secret" not in pipeline_raw
    assert "artifact-refresh-secret" not in capability_raw
    assert "artifact-refresh-secret" not in pipeline_raw
    assert loaded_capability is not None
    assert loaded_pipeline is not None
    assert "api_key" not in loaded_capability.parameters
    assert loaded_capability.parameters["data_classification"] == "sensitive"
    assert (
        loaded_capability.artifacts[0].metadata["data_classification"]
        == "sensitive"
    )
    assert "access_token" not in loaded_pipeline.parameters
    assert loaded_pipeline.parameters["data_classification"] == "sensitive"
    assert (
        loaded_pipeline.nodes[0].artifacts[0].metadata["data_classification"]
        == "sensitive"
    )


def test_task_run_lifecycle_rejects_illegal_terminal_transitions():
    run = TaskRun(
        agent_id="default",
        source="capability",
        title="状态机",
        status="running",
    )

    transition_task_run(run, "succeeded")

    with pytest.raises(ValueError, match="terminal"):
        transition_task_run(run, "running")

    active_run = TaskRun(
        agent_id="default",
        source="capability",
        title="状态机",
        status="running",
    )
    with pytest.raises(ValueError, match="Illegal"):
        transition_task_run(active_run, "dispatched")


@pytest.mark.asyncio
async def test_task_run_evaluations_api_returns_stable_payloads(
    tmp_path: Path,
    monkeypatch,
):
    repo = CapabilityRepository(tmp_path)
    task_run = TaskRun(
        id="blocked-run",
        agent_id="default",
        source="capability",
        title="被阻断任务",
        status="blocked",
        failure_reason="risk",
    )
    report = build_report(
        task_run_id=task_run.id,
        evaluator="fast_fail_gate",
        findings=[
            make_finding(
                code="risk_approval_required",
                title="风险确认缺失",
                detail="high",
            ),
        ],
        passed_summary="Fast-fail gate passed.",
        blocked_summary=(
            "Fast-fail gate blocked this capability run before execution."
        ),
    )
    await repo.upsert_task_run(task_run)
    await repo.save_evaluation_report(report)

    async def fake_workspace(_request):
        return SimpleNamespace(
            workspace_dir=tmp_path,
            agent_id="default",
            chat_manager=None,
            cron_manager=None,
        )

    monkeypatch.setattr(
        "lantai.app.capabilities.api.get_agent_for_request",
        fake_workspace,
    )
    app = FastAPI()
    app.include_router(task_runs_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        empty_response = await client.get("/task-runs/no-report/evaluations")
        report_response = await client.get(
            f"/task-runs/{task_run.id}/evaluations",
        )
        run_response = await client.get(f"/task-runs/{task_run.id}")

    assert empty_response.status_code == 200
    assert empty_response.json() == []
    assert report_response.status_code == 200
    assert report_response.json()[0]["conclusion"] == "blocked"
    assert run_response.status_code == 200
    assert run_response.json()["failure_reason"] == "risk"


@pytest.mark.asyncio
async def test_evaluation_reports_are_append_only_history(tmp_path: Path):
    repo = CapabilityRepository(tmp_path)
    first = EvaluationReport(
        id="eval-old",
        task_run_id="run-history",
        evaluator="fast_fail_gate",
        evaluator_version="1",
        rubric_id="fast_fail",
        rubric_version="1",
        attempt_id="attempt-1",
        evidence_sha256="sha256:first",
        conclusion="pass",
        summary="old",
        gate_result="pass",
        created_at="2026-07-16T12:00:00+00:00",
    )
    second = EvaluationReport(
        id="eval-new",
        task_run_id="run-history",
        evaluator="rubric_gate",
        evaluator_version="2",
        rubric_id="quality",
        rubric_version="3",
        attempt_id="attempt-2",
        evidence_sha256="sha256:second",
        conclusion="needs_revision",
        summary="new",
        gate_result="needs_revision",
        scores={"design": 0.5, "reliability": 0.8},
        created_at="2026-07-16T13:00:00+00:00",
    )

    await repo.save_evaluation_report(first)
    await repo.save_evaluation_report(second)

    reports = await repo.list_evaluation_reports("run-history")
    history_dir = tmp_path / "capabilities" / "evaluations" / "run-history"

    assert [report.id for report in reports] == ["eval-new", "eval-old"]
    assert reports[0].schema_version == 1
    assert reports[0].attempt_id == "attempt-2"
    assert reports[0].evidence_sha256 == "sha256:second"
    assert reports[0].scores["design"] == 0.5
    assert (history_dir / "eval-old.json").exists()
    assert (history_dir / "eval-new.json").exists()
    assert not (
        tmp_path / "capabilities" / "evaluations" / "run-history.json"
    ).exists()


@pytest.mark.asyncio
async def test_evaluation_history_reader_rejects_report_id_path_mismatch(
    tmp_path: Path,
):
    repo = CapabilityRepository(tmp_path)
    report = EvaluationReport(
        id="eval-original",
        task_run_id="run-history",
        evaluator="fast_fail_gate",
        evaluator_version="1",
        conclusion="pass",
        summary="original",
        created_at="2026-07-16T12:00:00+00:00",
    )
    await repo.save_evaluation_report(report)
    history_path = (
        tmp_path
        / "capabilities"
        / "evaluations"
        / "run-history"
        / "eval-original.json"
    )
    payload = json.loads(history_path.read_text(encoding="utf-8"))
    payload["id"] = "eval-forged"
    history_path.write_text(json.dumps(payload), encoding="utf-8")

    reports = await repo.list_evaluation_reports("run-history")

    assert reports == []


@pytest.mark.asyncio
async def test_evaluation_history_reader_includes_legacy_single_file(
    tmp_path: Path,
):
    repo = CapabilityRepository(tmp_path)
    legacy_path = tmp_path / "capabilities" / "evaluations" / "run-legacy.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "id": "eval-legacy",
                "task_run_id": "run-legacy",
                "evaluator": "fast_fail_gate",
                "conclusion": "blocked",
                "summary": "legacy",
                "created_at": "2026-07-16T12:00:00+00:00",
            },
        ),
        encoding="utf-8",
    )
    current = EvaluationReport(
        id="eval-current",
        task_run_id="run-legacy",
        evaluator="rubric_gate",
        conclusion="pass",
        summary="current",
        created_at="2026-07-16T13:00:00+00:00",
    )

    await repo.save_evaluation_report(current)

    reports = await repo.list_evaluation_reports("run-legacy")

    assert [report.id for report in reports] == ["eval-current", "eval-legacy"]
    assert reports[1].schema_version == 1
    assert legacy_path.exists()


def test_build_capability_draft_never_auto_enables():
    task_run = TaskRun(
        agent_id="default",
        source="chat",
        title="整理招聘线索",
        user_intent="把今天的招聘线索整理成表格",
        status="succeeded",
        steps=[
            TaskStep(
                index=0,
                kind="message",
                title="读取线索",
                status="succeeded",
            ),
        ],
    )

    draft = build_capability_draft(task_run)

    assert draft.capability.enabled is False
    assert draft.capability.source_run_id == task_run.id
    assert draft.capability.parameters[0].name == "task_goal"
    assert draft.warnings


def test_build_capability_draft_suggests_skill_for_finished_chat_turn():
    task_run = TaskRun(
        agent_id="default",
        source="chat",
        title="整理招聘线索",
        user_intent="把今天的招聘线索整理成表格",
        status="succeeded",
        steps=[
            TaskStep(
                index=0,
                kind="message",
                title="用户请求",
                detail="把今天的招聘线索整理成表格",
                status="succeeded",
            ),
            TaskStep(
                index=1,
                kind="message",
                title="最终回复",
                detail="已整理成姓名、渠道、跟进状态三列。",
                status="succeeded",
                metadata={"experience_kind": "chat.final_answer"},
            ),
        ],
    )

    draft = build_capability_draft(task_run)

    assert len(draft.suggested_skills) == 1
    suggested = draft.suggested_skills[0]
    assert suggested.attach_to_step_index == 0
    assert suggested.tags == ["能力沉淀", "chat"]
    assert "Original request" in suggested.content
    assert "Successful response reference" in suggested.content


def test_draft_skips_suggested_skill_for_non_chat_or_unfinished():
    task_run = TaskRun(
        agent_id="default",
        source="image",
        title="生成封面图",
        user_intent="生成封面图",
        status="succeeded",
        steps=[
            TaskStep(
                index=0,
                kind="message",
                title="最终回复",
                detail="图片已生成。",
                status="succeeded",
            ),
        ],
    )
    failed_chat = TaskRun(
        agent_id="default",
        source="chat",
        title="失败聊天",
        user_intent="整理线索",
        status="failed",
        steps=[
            TaskStep(
                index=0,
                kind="error",
                title="运行错误",
                detail="失败",
                status="failed",
            ),
        ],
    )

    assert build_capability_draft(task_run).suggested_skills == []
    assert build_capability_draft(failed_chat).suggested_skills == []


@pytest.mark.asyncio
async def test_capability_executor_records_run_and_workspace_file(
    tmp_path: Path,
):
    repo = CapabilityRepository(tmp_path)
    capability = Capability(
        name="沉淀报告",
        enabled=True,
        parameters=[CapabilityParameter(name="topic", default="Agent")],
        steps=[
            CapabilityStep(
                index=0,
                title="生成内容",
                kind="agent_prompt",
                instruction_template="写 {{topic}}",
                required_parameters=["topic"],
            ),
            CapabilityStep(
                index=1,
                title="保存文件",
                kind="delivery",
                instruction_template="保存结果",
            ),
        ],
        delivery_defaults=[
            DeliveryTarget(
                type="workspace_file",
                path="reports/agent.md",
                title="Agent 报告",
            ),
        ],
    )
    await repo.upsert_capability(capability)

    executor = CapabilityExecutor(
        repo=repo,
        runner=None,
        workspace_dir=tmp_path,
        agent_id="default",
    )
    run = await executor.run(
        capability,
        CapabilityRunRequest(parameters={"topic": "能力系统"}),
    )

    assert run.status == "succeeded"
    assert (tmp_path / "reports" / "agent.md").exists()
    task_run = await repo.get_task_run(run.task_run_id)
    assert task_run is not None
    assert task_run.status == "succeeded"
    assert task_run.artifacts[0].title == "Agent 报告"
    assert task_run.artifacts[0].sha256
    assert task_run.artifacts[0].producer == "capability"
    assert task_run.artifacts[0].run_id == task_run.id
    assert task_run.artifacts[0].classification == "user_content"
    assert task_run.artifacts[0].retention == "task_lifetime"
    manifest = json.loads(
        artifact_manifest_path(tmp_path).read_text(encoding="utf-8"),
    )
    assert manifest["artifacts"][0]["id"] == task_run.artifacts[0].id
    assert task_run.evaluation_summary is not None
    assert task_run.evaluation_summary.conclusion == "pass"
    reports = await repo.list_evaluation_reports(task_run.id)
    assert reports[0].conclusion == "pass"


@pytest.mark.asyncio
async def test_capability_executor_refreshes_heartbeat_during_silent_step(
    tmp_path: Path,
    monkeypatch,
):
    import lantai.app.capabilities.executor as capability_executor_module

    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: False,
    )
    repo = CapabilityRepository(tmp_path)
    capability = Capability(
        name="慢步骤能力",
        enabled=True,
        parameters=[CapabilityParameter(name="topic", default="Agent")],
        steps=[
            CapabilityStep(
                index=0,
                title="生成内容",
                kind="agent_prompt",
                instruction_template="写 {{topic}}",
                required_parameters=["topic"],
                timeout_seconds=5,
            ),
        ],
    )
    await repo.upsert_capability(capability)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowRunner:
        async def stream_query(self, _payload):
            started.set()
            await release.wait()
            yield "step done"

    monkeypatch.setattr(
        capability_executor_module,
        "_CAPABILITY_TASK_RUN_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    executor = CapabilityExecutor(
        repo=repo,
        runner=SlowRunner(),
        workspace_dir=tmp_path,
        agent_id="default",
    )

    task = asyncio.create_task(
        executor.run(
            capability,
            CapabilityRunRequest(parameters={"topic": "能力系统"}),
        ),
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    task_run = next(
        run
        for run in await repo.list_task_runs()
        if run.source == "capability"
    )
    store = RuntimeRunStore(tmp_path)
    before = await store.load_contract(task_run.id)
    assert before is not None
    assert before.status == "running"
    assert before.heartbeat_at is not None

    await asyncio.sleep(0.05)
    during = await store.load_contract(task_run.id)

    release.set()
    run = await asyncio.wait_for(task, timeout=1.0)

    assert during is not None
    assert during.status == "running"
    assert during.revision > before.revision
    assert during.heartbeat_at is not None
    assert during.heartbeat_at > before.heartbeat_at
    assert run.status == "succeeded"


@pytest.mark.asyncio
async def test_gui_step_requires_manual_ack_and_writes_replay(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: False,
    )
    repo = CapabilityRepository(tmp_path)
    capability = Capability(
        name="浏览器操作",
        enabled=True,
        steps=[
            CapabilityStep(
                index=0,
                title="打开页面",
                kind="gui_prompt",
                instruction_template="打开 {{url}}",
                required_parameters=["url"],
                risk_level="high",
                gui_target="browser",
                replay_policy="full",
            ),
        ],
        parameters=[
            CapabilityParameter(name="url", default="https://example.com"),
        ],
    )
    executor = CapabilityExecutor(
        repo=repo,
        runner=None,
        workspace_dir=tmp_path,
        agent_id="default",
    )

    with pytest.raises(PermissionError, match="高风险"):
        await executor.run(capability, CapabilityRunRequest(parameters={}))

    blocked_run = (await repo.list_task_runs())[0]
    assert blocked_run.status == "blocked"
    assert blocked_run.evaluation_summary is not None
    assert blocked_run.evaluation_summary.conclusion == "blocked"
    reports = await repo.list_evaluation_reports(blocked_run.id)
    assert reports[0].findings[0].severity == "critical"

    run = await executor.run(
        capability,
        CapabilityRunRequest(
            parameters={},
            manual_gui_ack=True,
            approved_risks=["high"],
        ),
    )

    task_run = await repo.get_task_run(run.task_run_id)
    assert task_run is not None
    replay_path = task_run.steps[0].metadata["replay_path"]
    assert Path(str(replay_path)).exists()
    assert "可视化界面操作" in (run.output_text or "")


def test_browser_prompt_gate_requires_profile_authorization(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: False,
    )

    class DisabledProfileService:
        def get_profile(self, _profile_id):
            return SimpleNamespace(
                id="github-work",
                agent_control_enabled=False,
                allowed_domains=["github.com"],
            )

    monkeypatch.setattr(
        "lantai.app.browser_workbench.BrowserProfileService",
        DisabledProfileService,
    )
    capability = Capability(
        name="读取 GitHub",
        enabled=True,
        delivery_defaults=[],
        steps=[
            CapabilityStep(
                title="读取 issue",
                kind="browser_prompt",
                instruction_template="读取 issue",
                profile_id="github-work",
                target_domains=["github.com"],
                access="read",
                risk_level="medium",
            ),
        ],
    )

    report = capability_gate_report(
        task_run_id="task-1",
        capability=capability,
        request=CapabilityRunRequest(),
        values={},
        workspace_dir=tmp_path,
        source="capability",
    )

    assert report.conclusion == "blocked"
    assert report.findings[0].code == "browser_profile_agent_control_disabled"


def test_browser_prompt_gate_blocks_domain_and_write_without_ack(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: False,
    )

    class EnabledProfileService:
        def get_profile(self, _profile_id):
            return SimpleNamespace(
                id="github-work",
                agent_control_enabled=True,
                allowed_domains=["github.com"],
            )

    monkeypatch.setattr(
        "lantai.app.browser_workbench.BrowserProfileService",
        EnabledProfileService,
    )
    capability = Capability(
        name="跨站写入",
        enabled=True,
        delivery_defaults=[],
        steps=[
            CapabilityStep(
                title="提交表单",
                kind="browser_prompt",
                instruction_template="提交表单",
                profile_id="github-work",
                target_domains=["api.github.com", "Example.com"],
                access="write",
                risk_level="medium",
            ),
        ],
    )

    report = capability_gate_report(
        task_run_id="task-1",
        capability=capability,
        request=CapabilityRunRequest(),
        values={},
        workspace_dir=tmp_path,
        source="capability",
    )

    codes = {finding.code for finding in report.findings}
    assert "browser_domain_not_allowed" in codes
    assert "browser_ack_required" in codes


class FakeBrowserTaskRuntime:
    def __init__(self) -> None:
        self.requests: list[BrowserTaskRequest] = []
        self.runs: dict[str, BrowserTaskRun] = {}
        self.artifact_payloads: dict[str, dict] = {}

    async def create(self, request: BrowserTaskRequest) -> BrowserTaskRun:
        self.requests.append(request)
        run = BrowserTaskRun(
            run_id=request.run_id,
            profile_id=request.profile_id,
            target_domains=request.target_domains,
            access=request.access,
            mode=request.mode,
            instruction=request.instruction,
            status="queued",
            replay_run_id=request.run_id,
            planned_action_count=3,
            metadata={
                **request.metadata,
                "plan": {
                    "strategy": "lantaicli_adapter",
                    "adapter_id": request.metadata.get("adapter_id", ""),
                    "expected_outputs": ["issues", "summary"],
                },
            },
        )
        self.runs[run.run_id] = run
        return run

    def get_run(self, run_id: str) -> BrowserTaskRun | None:
        return self.runs.get(run_id)

    def artifacts(self, run_id: str) -> dict:
        return self.artifact_payloads.get(
            run_id,
            {
                "run_id": run_id,
                "event_count": 0,
                "screenshots": [],
                "observation_summaries": [],
                "network": [],
                "console": [],
            },
        )


@pytest.mark.asyncio
async def test_browser_prompt_creates_background_browser_task(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: False,
    )

    class EnabledProfileService:
        def get_profile(self, _profile_id):
            return SimpleNamespace(
                id="github-work",
                agent_control_enabled=True,
                allowed_domains=["github.com"],
            )

    monkeypatch.setattr(
        "lantai.app.browser_workbench.BrowserProfileService",
        EnabledProfileService,
    )
    runtime = FakeBrowserTaskRuntime()
    repo = CapabilityRepository(tmp_path)
    capability = Capability(
        name="读取 GitHub",
        enabled=True,
        delivery_defaults=[],
        steps=[
            CapabilityStep(
                title="读取 issue",
                kind="browser_prompt",
                instruction_template="读取 {{repo_url}} 最近 issue",
                required_parameters=["repo_url"],
                profile_id="github-work",
                target_domains=["github.com"],
                access="read",
                risk_level="medium",
                metadata={
                    "adapter_id": "github.issues.list",
                    "max_actions": 6,
                },
            ),
        ],
        parameters=[
            CapabilityParameter(
                name="repo_url",
                default="https://github.com/org/repo",
            ),
        ],
    )
    executor = CapabilityExecutor(
        repo=repo,
        runner=None,
        workspace_dir=tmp_path,
        agent_id="default",
        browser_task_runtime=runtime,
    )

    run = await executor.run(capability, CapabilityRunRequest(parameters={}))

    task_run = await repo.get_task_run(run.task_run_id)
    assert run.status == "succeeded"
    assert task_run is not None
    assert runtime.requests[0].profile_id == "github-work"
    assert runtime.requests[0].target_domains == ["github.com"]
    assert runtime.requests[0].metadata["adapter_id"] == "github.issues.list"
    assert (
        runtime.requests[0].metadata["source"] == "capability.browser_prompt"
    )
    assert runtime.requests[0].max_actions == 6
    assert "browser_task_run_id" in task_run.steps[0].metadata
    assert (
        task_run.steps[0].metadata["browser_task_plan"]["adapter_id"]
        == "github.issues.list"
    )
    replay_path = Path(str(task_run.steps[0].metadata["replay_path"]))
    replay_text = replay_path.read_text("utf-8")
    assert "browser_task_created" in replay_text
    assert "github.issues.list" in (run.output_text or "")


@pytest.mark.asyncio
async def test_browser_prompt_can_wait_for_read_task_completion(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: False,
    )

    class EnabledProfileService:
        def get_profile(self, _profile_id):
            return SimpleNamespace(
                id="github-work",
                agent_control_enabled=True,
                allowed_domains=["github.com"],
            )

    monkeypatch.setattr(
        "lantai.app.browser_workbench.BrowserProfileService",
        EnabledProfileService,
    )
    runtime = FakeBrowserTaskRuntime()
    repo = CapabilityRepository(tmp_path)
    capability = Capability(
        name="读取 GitHub",
        enabled=True,
        delivery_defaults=[],
        steps=[
            CapabilityStep(
                title="读取 issue",
                kind="browser_prompt",
                instruction_template="读取 issue",
                profile_id="github-work",
                target_domains=["github.com"],
                access="read",
                metadata={
                    "adapter_id": "github.issues.list",
                    "wait_for_completion": True,
                    "wait_timeout_seconds": 3,
                    "poll_interval_seconds": 0.05,
                },
            ),
        ],
    )
    original_create = runtime.create

    async def create_completed(request: BrowserTaskRequest) -> BrowserTaskRun:
        run = await original_create(request)
        completed = run.model_copy(
            update={
                "status": "completed",
                "completed_action_count": 3,
                "planned_action_count": 3,
            },
        )
        runtime.runs[run.run_id] = completed
        runtime.artifact_payloads[run.run_id] = {
            "run_id": run.run_id,
            "event_count": 3,
            "screenshots": ["/tmp/shot.png"],
            "observation_summaries": ["Issue list"],
            "network": [],
            "console": [],
        }
        return run

    runtime.create = create_completed  # type: ignore[method-assign]
    executor = CapabilityExecutor(
        repo=repo,
        runner=None,
        workspace_dir=tmp_path,
        agent_id="default",
        browser_task_runtime=runtime,
    )

    run = await executor.run(capability, CapabilityRunRequest(parameters={}))

    task_run = await repo.get_task_run(run.task_run_id)
    assert task_run is not None
    metadata = task_run.steps[0].metadata
    assert metadata["browser_task_status"] == "completed"
    assert metadata["browser_task_wait_result"] == "completed"
    assert metadata["browser_task_completed_action_count"] == 3
    assert metadata["browser_task_artifacts"]["event_count"] == 3
    assert metadata["browser_task_artifacts"]["screenshot_count"] == 1
    assert "3 events, 1 screenshots" in (run.output_text or "")
    replay_text = Path(str(metadata["replay_path"])).read_text("utf-8")
    assert "browser_task_wait_result" in replay_text


class TimeoutRunner:
    async def stream_query(self, payload):
        if payload is None:
            yield ""
        raise asyncio.TimeoutError()


@pytest.mark.asyncio
async def test_capability_executor_marks_step_timeout_as_stalled(
    tmp_path: Path,
):
    repo = CapabilityRepository(tmp_path)
    capability = Capability(
        name="超时能力",
        enabled=True,
        delivery_defaults=[],
        steps=[
            CapabilityStep(
                title="慢步骤",
                instruction_template="慢任务",
                timeout_seconds=1,
            ),
        ],
    )
    executor = CapabilityExecutor(
        repo=repo,
        runner=TimeoutRunner(),
        workspace_dir=tmp_path,
        agent_id="default",
    )

    with pytest.raises(asyncio.TimeoutError):
        await executor.run(capability, CapabilityRunRequest(parameters={}))

    task_run = (await repo.list_task_runs())[0]
    assert task_run.status == "stalled"
    assert task_run.steps[0].status == "stalled"


@pytest.mark.asyncio
async def test_pipeline_runs_layers_and_passes_output_text(tmp_path: Path):
    repo = CapabilityRepository(tmp_path)
    first = Capability(
        name="第一步",
        enabled=True,
        parameters=[CapabilityParameter(name="topic", default="能力")],
        steps=[
            CapabilityStep(
                title="输出",
                instruction_template="写 {{topic}}",
                required_parameters=["topic"],
            ),
        ],
    )
    second = Capability(
        name="第二步",
        enabled=True,
        parameters=[CapabilityParameter(name="input", default="")],
        steps=[
            CapabilityStep(
                title="总结",
                instruction_template="总结 {{input}}",
                required_parameters=["input"],
            ),
        ],
    )
    await repo.upsert_capability(first)
    await repo.upsert_capability(second)
    first_node = PipelineNode(
        id="first",
        title="采集",
        capability_id=first.id,
        parameters={"topic": "{{pipeline.topic}}"},
    )
    second_node = PipelineNode(
        id="second",
        index=1,
        title="汇总",
        capability_id=second.id,
        parameters={"input": "{{nodes.first.output_text}}"},
    )
    pipeline = Pipeline(
        name="串行流程",
        enabled=True,
        parameters=[CapabilityParameter(name="topic", default="能力系统")],
        nodes=[first_node, second_node],
        edges=[PipelineEdge(source="first", target="second")],
        delivery_defaults=[],
    )
    await repo.upsert_pipeline(pipeline)
    executor = PipelineExecutor(
        repo=repo,
        runner=None,
        workspace_dir=tmp_path,
        agent_id="default",
    )

    run = await executor.run(
        pipeline,
        PipelineRunRequest(parameters={"topic": "Pipeline"}),
    )

    assert run.status == "succeeded"
    assert len(run.nodes) == 2
    assert "Pipeline" in (run.nodes[0].output_text or "")
    assert "Pipeline" in (run.nodes[1].output_text or "")
    task_run = await repo.get_task_run(run.task_run_id)
    assert task_run is not None
    assert task_run.source == "pipeline"


@pytest.mark.asyncio
async def test_pipeline_executor_refreshes_parent_heartbeat_during_silent_node(
    tmp_path: Path,
    monkeypatch,
):
    from lantai.app.capabilities import (
        pipeline_executor as pipeline_executor_module,
    )

    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: False,
    )
    repo = CapabilityRepository(tmp_path)
    capability = Capability(
        name="慢节点",
        enabled=True,
        parameters=[CapabilityParameter(name="topic", default="能力")],
        steps=[
            CapabilityStep(
                title="输出",
                instruction_template="写 {{topic}}",
                required_parameters=["topic"],
                timeout_seconds=5,
            ),
        ],
    )
    await repo.upsert_capability(capability)
    pipeline = Pipeline(
        name="慢 Pipeline",
        enabled=True,
        parameters=[CapabilityParameter(name="topic", default="能力系统")],
        nodes=[
            PipelineNode(
                id="slow",
                title="慢节点",
                capability_id=capability.id,
                parameters={"topic": "{{pipeline.topic}}"},
            ),
        ],
        delivery_defaults=[],
    )
    await repo.upsert_pipeline(pipeline)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowRunner:
        async def stream_query(self, _payload):
            started.set()
            await release.wait()
            yield "node done"

    monkeypatch.setattr(
        pipeline_executor_module,
        "_PIPELINE_TASK_RUN_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    executor = PipelineExecutor(
        repo=repo,
        runner=SlowRunner(),
        workspace_dir=tmp_path,
        agent_id="default",
    )

    task = asyncio.create_task(
        executor.run(
            pipeline,
            PipelineRunRequest(parameters={"topic": "Pipeline"}),
        ),
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    task_run = next(
        run for run in await repo.list_task_runs() if run.source == "pipeline"
    )
    store = RuntimeRunStore(tmp_path)
    before = await store.load_contract(task_run.id)
    assert before is not None
    assert before.status == "running"
    assert before.heartbeat_at is not None

    await asyncio.sleep(0.05)
    during = await store.load_contract(task_run.id)

    release.set()
    run = await asyncio.wait_for(task, timeout=1.0)

    assert during is not None
    assert during.status == "running"
    assert during.revision > before.revision
    assert during.heartbeat_at is not None
    assert during.heartbeat_at > before.heartbeat_at
    assert run.status == "succeeded"


@pytest.mark.asyncio
async def test_cron_pipeline_rejects_gui_capability(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "lantai.app.capabilities.policy.is_app_store_distribution",
        lambda: False,
    )
    repo = CapabilityRepository(tmp_path)
    gui_capability = Capability(
        name="GUI",
        enabled=True,
        parameters=[CapabilityParameter(name="task_goal", default="打开页面")],
        steps=[
            CapabilityStep(
                title="打开页面",
                kind="gui_prompt",
                instruction_template="{{task_goal}}",
                risk_level="high",
                gui_target="browser",
            ),
        ],
    )
    await repo.upsert_capability(gui_capability)
    pipeline = Pipeline(
        name="GUI Pipeline",
        enabled=True,
        nodes=[
            PipelineNode(
                title="GUI",
                capability_id=gui_capability.id,
                parameters={"task_goal": "打开页面"},
            ),
        ],
    )
    executor = PipelineExecutor(
        repo=repo,
        runner=None,
        workspace_dir=tmp_path,
        agent_id="default",
    )

    with pytest.raises(PermissionError, match="定时 Pipeline"):
        await executor.run(
            pipeline,
            PipelineRunRequest(parameters={}),
            source="cron",
        )

    task_run = (await repo.list_task_runs())[0]
    assert task_run.status == "blocked"
    reports = await repo.list_evaluation_reports(task_run.id)
    assert reports[0].conclusion == "blocked"


@pytest.mark.asyncio
async def test_pipeline_fast_fails_when_node_capability_missing(
    tmp_path: Path,
):
    repo = CapabilityRepository(tmp_path)
    downstream = Capability(
        name="下游",
        enabled=True,
        steps=[CapabilityStep(title="输出", instruction_template="ok")],
    )
    await repo.upsert_capability(downstream)
    pipeline = Pipeline(
        name="缺失节点",
        enabled=True,
        nodes=[
            PipelineNode(
                id="missing",
                title="缺失",
                capability_id="missing-cap",
            ),
            PipelineNode(
                id="downstream",
                title="下游",
                capability_id=downstream.id,
            ),
        ],
        edges=[PipelineEdge(source="missing", target="downstream")],
    )
    executor = PipelineExecutor(
        repo=repo,
        runner=None,
        workspace_dir=tmp_path,
        agent_id="default",
    )

    with pytest.raises(PermissionError, match="能力不存在"):
        await executor.run(pipeline, PipelineRunRequest(parameters={}))

    task_runs = await repo.list_task_runs()
    assert len(task_runs) == 1
    assert task_runs[0].status == "blocked"
    reports = await repo.list_evaluation_reports(task_runs[0].id)
    assert reports[0].findings[0].code == "pipeline_capability_missing"


@pytest.mark.asyncio
async def test_app_store_policy_rejects_gui_capability(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LANTAI_APP_STORE", "1")
    service = CapabilityService(CapabilityRepository(tmp_path))
    capability = Capability(
        name="GUI",
        steps=[
            CapabilityStep(
                title="打开页面",
                kind="gui_prompt",
                instruction_template="打开页面",
                risk_level="high",
            ),
        ],
    )

    with pytest.raises(PermissionError, match="App Store"):
        await service.create_capability(capability)


@pytest.mark.asyncio
async def test_app_store_gui_run_is_recorded_as_blocked(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LANTAI_APP_STORE", "1")
    repo = CapabilityRepository(tmp_path)
    capability = Capability(
        name="GUI",
        enabled=True,
        delivery_defaults=[],
        steps=[
            CapabilityStep(
                title="打开页面",
                kind="gui_prompt",
                instruction_template="打开页面",
                risk_level="high",
            ),
        ],
    )
    executor = CapabilityExecutor(
        repo=repo,
        runner=None,
        workspace_dir=tmp_path,
        agent_id="default",
    )

    with pytest.raises(PermissionError, match="App Store"):
        await executor.run(capability, CapabilityRunRequest(parameters={}))

    task_run = (await repo.list_task_runs())[0]
    assert task_run.status == "blocked"
    reports = await repo.list_evaluation_reports(task_run.id)
    assert reports[0].findings[0].code == "app_store_gui_blocked"


def test_app_store_cron_capability_reports_background_degradation(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LANTAI_APP_STORE", "1")
    capability = Capability(
        name="Daily digest",
        enabled=True,
        delivery_defaults=[],
        steps=[
            CapabilityStep(
                title="Summarize",
                kind="agent_prompt",
                instruction_template="Summarize {{ topic }}",
            ),
        ],
    )

    report = capability_gate_report(
        task_run_id="task-mas-cron",
        capability=capability,
        request=CapabilityRunRequest(parameters={"topic": "news"}),
        values={"topic": "news"},
        workspace_dir=tmp_path,
        source="cron",
    )

    assert report.conclusion == "blocked"
    assert report.findings[0].code == "mas_background_execution_limited"
    assert (
        report.findings[0].metadata["capability_matrix_mode"]
        == "interactive_only"
    )


@pytest.mark.asyncio
async def test_task_run_materializer_reads_tool_and_artifact_events(
    tmp_path: Path,
):
    run_id = "run-1"
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        run_id=run_id,
        kind="chat.turn.started",
        status="running",
        summary="开始",
        payload={"text_preview": "整理线索"},
    )
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        run_id=run_id,
        item_id="tool-search",
        kind="chat.tool_call.started",
        status="running",
        summary="工具调用",
        payload={
            "tool_call_id": "tool-search",
            "tool_name": "search",
            "tool_input": {"query": "关键词"},
        },
    )
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        run_id=run_id,
        kind="chat.artifact.created",
        status="finished",
        summary="产物",
        payload={"title": "报告", "path": "reports/a.md", "type": "markdown"},
    )
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        run_id=run_id,
        kind="chat.final_answer",
        status="finished",
        summary="完成",
        payload={"text": "完成"},
    )

    runs = materialize_task_runs(tmp_path, agent_id="default")

    assert runs[0].id == "experience:run-1"
    assert runs[0].status == "succeeded"
    assert [step.kind for step in runs[0].steps] == [
        "message",
        "tool_call",
        "artifact",
        "message",
    ]
    assert runs[0].steps[1].tool_name == "search"
    assert runs[0].steps[1].detail == '{"query":"关键词"}'
    assert runs[0].artifacts[0].title == "报告"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_task_status", "event_status", "expected_status"),
    [
        ("failed", "failed", "failed"),
        ("stalled", "stalled", "stalled"),
    ],
)
async def test_task_run_materializer_uses_canonical_settled_status(
    tmp_path: Path,
    final_task_status: str,
    event_status: str,
    expected_status: str,
):
    run_id = f"run-{final_task_status}"
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        run_id=run_id,
        kind="chat.turn.started",
        status="running",
        summary="开始",
        payload={"text_preview": "执行任务"},
    )
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        run_id=run_id,
        kind="chat.turn.settled",
        status=event_status,
        summary="结束",
        payload={
            "final_task_status": final_task_status,
            "pending_turn_cleared": True,
            "active_run_event_count": 2,
        },
    )

    runs = materialize_task_runs(tmp_path, agent_id="default")

    assert runs[0].status == expected_status


@pytest.mark.asyncio
async def test_task_run_materializer_does_not_treat_stop_request_as_terminal(
    tmp_path: Path,
):
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        kind="chat.turn.started",
        status="running",
        summary="开始",
        payload={"text_preview": "执行任务"},
    )
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        kind="chat.turn.stop_requested",
        status="interrupted",
        summary="请求停止",
        payload={"requested_chat_id": "chat-1", "resolved_chat_id": "chat-1"},
    )
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        kind="chat.turn.settled",
        status="finished",
        summary="完成",
        payload={
            "final_task_status": "finished",
            "pending_turn_cleared": True,
            "active_run_event_count": 3,
        },
    )

    runs = materialize_task_runs(tmp_path, agent_id="default")

    assert runs[0].status == "succeeded"


def test_cron_job_spec_accepts_capability_task():
    spec = CronJobSpec(
        name="每日能力",
        schedule=ScheduleSpec(cron="0 9 * * *"),
        task_type="capability",
        capability_request=CapabilityCronRequest(
            capability_id="cap-1",
            parameters={"topic": "日报"},
        ),
        dispatch=DispatchSpec(
            target=DispatchTarget(
                user_id="capability",
                session_id="console:capability",
            ),
        ),
    )

    assert spec.task_type == "capability"
    assert spec.request is None
    assert spec.capability_request is not None


def test_cron_job_spec_accepts_pipeline_task():
    spec = CronJobSpec(
        name="每日 Pipeline",
        schedule=ScheduleSpec(cron="0 9 * * *"),
        task_type="pipeline",
        pipeline_request=PipelineCronRequest(
            pipeline_id="pipe-1",
            parameters={"topic": "日报"},
        ),
        dispatch=DispatchSpec(
            target=DispatchTarget(
                user_id="capability",
                session_id="console:capability",
            ),
        ),
    )

    assert spec.task_type == "pipeline"
    assert spec.request is None
    assert spec.capability_request is None
    assert spec.pipeline_request is not None
