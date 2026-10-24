# -*- coding: utf-8 -*-
"""Behavior locks for disabling legacy Runtime writer surfaces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lantai.agents.mission import state as mission_state
from lantai.app.browser_workbench.models import BrowserTaskRun
from lantai.app.browser_workbench.tasks import BrowserTaskStore
from lantai.app.capabilities.models import (
    EvaluationReport,
    TaskRun,
    TaskRunsFile,
)
from lantai.app.capabilities.repo import CapabilityRepository
from lantai.app.experience.events import (
    append_chat_event,
    events_path_for_workspace,
    legacy_events_path_for_workspace,
)
from lantai.app.runtime_legacy_writers import (
    LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
    LEGACY_RUNTIME_WRITER_OBSERVATION_SCHEMA,
    LEGACY_RUNTIME_WRITER_MODE_ENV,
    LegacyRuntimeWriterBlocked,
    assert_legacy_runtime_writer_allowed,
    get_legacy_runtime_writer_mode,
    normalize_legacy_runtime_writer_mode,
    summarize_legacy_runtime_writer_observations,
)
from lantai.app.runtime_migration import build_runtime_migration_report


def test_legacy_runtime_writer_mode_defaults_to_active(monkeypatch):
    monkeypatch.delenv(LEGACY_RUNTIME_WRITER_MODE_ENV, raising=False)
    monkeypatch.delenv(LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV, raising=False)

    assert get_legacy_runtime_writer_mode() == "active"
    assert normalize_legacy_runtime_writer_mode("read-only") == "read_only"
    assert_legacy_runtime_writer_allowed("legacy_task_run_snapshot_writer")


def test_legacy_runtime_writer_guard_rejects_read_only_mode(monkeypatch, tmp_path):
    monkeypatch.setenv(LEGACY_RUNTIME_WRITER_MODE_ENV, "read_only")

    with pytest.raises(LegacyRuntimeWriterBlocked) as exc_info:
        assert_legacy_runtime_writer_allowed(
            "legacy_task_run_snapshot_writer",
            path=tmp_path / "capabilities" / "task-runs.json",
        )

    assert exc_info.value.writer_id == "legacy_task_run_snapshot_writer"
    assert exc_info.value.mode == "read_only"
    assert exc_info.value.path.endswith("task-runs.json")


def test_legacy_runtime_writer_mode_rejects_typos():
    with pytest.raises(ValueError, match=LEGACY_RUNTIME_WRITER_MODE_ENV):
        normalize_legacy_runtime_writer_mode("readonly-ish")


def test_legacy_runtime_writer_observation_records_allowed_without_raw_path(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(LEGACY_RUNTIME_WRITER_MODE_ENV, "active")
    observation_file = tmp_path / "release-evidence" / "legacy-writers.jsonl"
    monkeypatch.setenv(
        LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
        str(observation_file),
    )
    target = tmp_path / "workspace" / "capabilities" / "task-runs.json"

    assert_legacy_runtime_writer_allowed(
        "legacy_task_run_snapshot_writer",
        path=target,
    )

    raw = observation_file.read_text(encoding="utf-8")
    assert str(target) not in raw
    record = json.loads(raw)
    assert record["schema"] == LEGACY_RUNTIME_WRITER_OBSERVATION_SCHEMA
    assert record["writer_id"] == "legacy_task_run_snapshot_writer"
    assert record["mode"] == "active"
    assert record["outcome"] == "allowed"
    assert record["target_path_name"] == "task-runs.json"
    assert record["target_path_sha256"].startswith("sha256:")
    assert record["target_path_present"] is True

    summary = summarize_legacy_runtime_writer_observations(
        observation_file,
        release_edition="official",
    )
    assert summary["event_count"] == 1
    assert summary["fallback_write_count"] == 1
    assert summary["blocked_attempt_count"] == 0
    assert summary["zero_observations"] is False
    assert summary["zero_fallback_writes"] is False


def test_legacy_runtime_writer_observation_records_blocked_read_only_attempt(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(LEGACY_RUNTIME_WRITER_MODE_ENV, "read_only")
    observation_file = tmp_path / "legacy-writers.jsonl"
    monkeypatch.setenv(
        LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
        str(observation_file),
    )

    with pytest.raises(LegacyRuntimeWriterBlocked):
        assert_legacy_runtime_writer_allowed(
            "legacy_browser_task_state_writer",
            path=tmp_path / "browser_workbench" / "tasks" / "task.json",
        )

    record = json.loads(observation_file.read_text(encoding="utf-8"))
    assert record["writer_id"] == "legacy_browser_task_state_writer"
    assert record["mode"] == "read_only"
    assert record["outcome"] == "blocked"

    summary = summarize_legacy_runtime_writer_observations(
        observation_file,
        release_edition="mas",
    )
    assert summary["event_count"] == 1
    assert summary["fallback_write_count"] == 0
    assert summary["blocked_attempt_count"] == 1
    assert summary["zero_observations"] is False
    assert summary["zero_fallback_writes"] is True


def test_legacy_runtime_writer_observation_summary_handles_empty_file(tmp_path: Path):
    observation_file = tmp_path / "legacy-writers.jsonl"
    observation_file.write_text("", encoding="utf-8")

    summary = summarize_legacy_runtime_writer_observations(
        observation_file,
        release_edition="official",
    )

    assert summary["observation_file_exists"] is True
    assert summary["event_count"] == 0
    assert summary["fallback_write_count"] == 0
    assert summary["zero_observations"] is True
    assert summary["zero_fallback_writes"] is True
    assert set(summary["writer_ids_without_observations"]) == {
        "legacy_runtime_event_ledger_writer",
        "legacy_task_run_snapshot_writer",
        "legacy_evaluation_report_writer",
        "legacy_mission_state_writer",
        "legacy_browser_task_state_writer",
    }


@pytest.mark.asyncio
async def test_experience_event_append_uses_canonical_runtime_projection(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(LEGACY_RUNTIME_WRITER_MODE_ENV, "read_only")
    observation_file = tmp_path / "legacy-writers.jsonl"
    monkeypatch.setenv(
        LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
        str(observation_file),
    )
    workspace_dir = tmp_path / "workspace"

    event = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-1",
        session_id="console:test",
        kind="chat.turn.started",
        status="running",
        summary="started",
    )

    assert event.seq == 1
    assert events_path_for_workspace(workspace_dir).is_file()
    assert not legacy_events_path_for_workspace(workspace_dir).exists()
    assert not observation_file.exists()

    summary = summarize_legacy_runtime_writer_observations(
        observation_file,
        release_edition="official",
    )
    assert summary["fallback_write_count"] == 0
    assert summary["zero_fallback_writes"] is True


@pytest.mark.asyncio
async def test_capability_legacy_writers_block_before_creating_dirs(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(LEGACY_RUNTIME_WRITER_MODE_ENV, "read_only")
    workspace_dir = tmp_path / "workspace"
    repo = CapabilityRepository(workspace_dir)
    run = TaskRun(
        id="run-1",
        agent_id="default",
        source="capability",
        title="Legacy writer check",
    )

    with pytest.raises(LegacyRuntimeWriterBlocked) as upsert_error:
        await repo.upsert_task_run(run)
    assert upsert_error.value.writer_id == "legacy_task_run_snapshot_writer"
    assert not (workspace_dir / "capabilities").exists()

    with pytest.raises(LegacyRuntimeWriterBlocked) as save_error:
        await repo.save_task_runs(TaskRunsFile(runs=[run]))
    assert save_error.value.writer_id == "legacy_task_run_snapshot_writer"
    assert not (workspace_dir / "capabilities").exists()

    report = EvaluationReport(
        id="evaluation-1",
        task_run_id="run-1",
        conclusion="blocked",
    )
    with pytest.raises(LegacyRuntimeWriterBlocked) as evaluation_error:
        await repo.save_evaluation_report(report)
    assert evaluation_error.value.writer_id == "legacy_evaluation_report_writer"
    assert not (workspace_dir / "capabilities").exists()


def test_mission_state_writers_block_before_creating_or_writing_files(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(LEGACY_RUNTIME_WRITER_MODE_ENV, "read_only")
    workspace_dir = tmp_path / "workspace"

    with pytest.raises(LegacyRuntimeWriterBlocked) as create_error:
        mission_state.create_loop_dir(workspace_dir)
    assert create_error.value.writer_id == "legacy_mission_state_writer"
    assert not (workspace_dir / "missions").exists()

    loop_dir = workspace_dir / "missions" / "mission-manual"
    loop_dir.mkdir(parents=True)

    writer_calls = [
        lambda: mission_state.write_loop_config(loop_dir, {"session_id": "s1"}),
        lambda: mission_state.write_task_md(loop_dir, "Task"),
        lambda: mission_state.write_prd_json(loop_dir, {"userStories": []}),
        lambda: mission_state.init_progress_txt(loop_dir),
    ]
    for call in writer_calls:
        with pytest.raises(LegacyRuntimeWriterBlocked) as exc_info:
            call()
        assert exc_info.value.writer_id == "legacy_mission_state_writer"

    assert list(loop_dir.iterdir()) == []


def test_browser_task_store_save_uses_canonical_sidecar_without_legacy_writer(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(LEGACY_RUNTIME_WRITER_MODE_ENV, "read_only")
    observation_file = tmp_path / "legacy-writers.jsonl"
    monkeypatch.setenv(
        LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
        str(observation_file),
    )
    store = BrowserTaskStore(tmp_path)

    path = store.save(BrowserTaskRun(run_id="browser-run-1"), [], [], 0)

    assert path.name.startswith("browser-run-1-")
    assert path.parent == tmp_path / ".lantai" / "runtime" / "v1" / "browser_tasks"
    assert path.is_file()
    assert not (tmp_path / "browser_workbench").exists()
    assert not observation_file.exists()

    loaded = store.load_all()
    assert loaded[0][0].run_id == "browser-run-1"

    summary = summarize_legacy_runtime_writer_observations(
        observation_file,
        release_edition="official",
    )
    assert summary["fallback_write_count"] == 0
    assert summary["zero_fallback_writes"] is True


def test_runtime_migration_report_marks_writers_stopped_in_read_only_mode(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv(LEGACY_RUNTIME_WRITER_MODE_ENV, "read_only")

    report = build_runtime_migration_report(tmp_path / "workspace")

    assert report.legacy_writer_mode == "read_only"
    assert len(report.writer_inventory) == 5
    assert all(
        item.migration_status == "legacy_write_stopped_read_compatibility"
        for item in report.writer_inventory
    )
    assert all(item.legacy_write_allowed is False for item in report.writer_inventory)
    assert all(item.can_stop_writes is True for item in report.writer_inventory)
    assert all(item.stop_writes_blockers == [] for item in report.writer_inventory)
