# -*- coding: utf-8 -*-
"""Behavior locks for the Runtime migration dry-run reporter."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from lantai.app.runtime_migration import build_runtime_migration_report
from lantai.runtime.runs import RuntimeRunContract, RuntimeRunOwner
from lantai.runtime.store import RuntimeRunStore


NOW = datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc)


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _tree_snapshot(root) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        snapshot[rel] = path.read_bytes() if path.is_file() else None
    return snapshot


def _existing_contract(run_id: str) -> RuntimeRunContract:
    return RuntimeRunContract(
        run_id=run_id,
        attempt_id=f"{run_id}:canonical",
        source="capability",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id="agent-default",
            session_id="console:chat-1",
            workspace_id="workspace-1",
            channel="console",
        ),
        status="running",
        revision=1,
        created_at=NOW,
        started_at=NOW,
        release_edition="official",
    )


@pytest.mark.asyncio
async def test_runtime_migration_report_is_dry_run_and_counts_risky_sources(tmp_path):
    workspace_dir = tmp_path / "workspace"
    event_path = workspace_dir / ".lantai" / "experience" / "events.jsonl"
    event_path.parent.mkdir(parents=True)
    event_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "evt-1",
                        "seq": 1,
                        "ts": NOW.isoformat(),
                        "agent_id": "agent-default",
                        "chat_id": "chat-1",
                        "session_id": "console:chat-1",
                        "run_id": "run-chat-1",
                        "scope": "chat",
                        "kind": "chat.turn.started",
                        "type": "turn.started",
                        "status": "running",
                        "source": "user",
                        "summary": "started",
                        "payload": {"text_preview": "hello"},
                    },
                    ensure_ascii=False,
                ),
                "{not-json",
            ],
        )
        + "\n",
        encoding="utf-8",
    )

    _write_json(
        workspace_dir / "capabilities" / "task-runs.json",
        {
            "runs": [
                {
                    "id": "task-conflict",
                    "agent_id": "agent-default",
                    "source": "capability",
                    "chat_id": "chat-1",
                    "session_id": "console:chat-1",
                    "title": "Conflicting capability",
                    "status": "succeeded",
                    "started_at": NOW.isoformat(),
                    "ended_at": NOW.isoformat(),
                    "metadata": {"workspace_id": "workspace-1"},
                }
            ],
        },
    )
    _write_json(
        workspace_dir / "capabilities" / "evaluations" / "orphan.json",
        {
            "task_run_id": "missing-task",
            "conclusion": "blocked",
            "summary": "No matching TaskRun snapshot",
            "created_at": NOW.isoformat(),
        },
    )
    _write_json(
        workspace_dir / "missions" / "mission-20260716-130000" / "loop_config.json",
        {
            "session_id": "mission-session",
            "agent_id": "agent-default",
        },
    )
    _write_json(
        workspace_dir / "missions" / "mission-20260716-130000" / "prd.json",
        {
            "project": "Runtime migration",
            "userStories": [{"title": "Audit", "passes": True}],
        },
    )
    (workspace_dir / "missions" / "mission-20260716-130000" / "progress.txt").write_text(
        "done\n",
        encoding="utf-8",
    )
    _write_json(
        workspace_dir / "browser_workbench" / "tasks" / "browser-runtime-run-1.json",
        {
            "version": 1,
            "run": {
                "run_id": "browser-runtime-run-1",
                "profile_id": "profile-1",
                "access": "read",
                "instruction": "Audit a page",
                "status": "completed",
                "metadata": {
                    "workspace_dir": str(workspace_dir),
                    "agent_id": "browser-agent",
                    "session_id": "browser-session",
                },
            },
            "events": [],
            "actions": [],
            "next_index": 0,
        },
    )

    store = RuntimeRunStore(workspace_dir)
    await store.save_contract(_existing_contract("task-conflict"), expected_revision=None)

    before = _tree_snapshot(workspace_dir)
    report = build_runtime_migration_report(
        workspace_dir,
        release_edition="official",
    )
    after = _tree_snapshot(workspace_dir)

    assert after == before
    assert report.schema_version == 2
    assert report.dry_run is True
    assert report.side_effect_free is True
    assert report.side_effects_executed == 0
    assert report.legacy_writer_mode == "active"
    assert report.totals.record_count == 5
    assert report.totals.migratable_count == 4
    assert report.totals.skipped_count == 1
    assert report.totals.conflict_count == 2
    assert all(target.endswith(".bak") for target in report.backup_targets)
    assert not (workspace_dir / ".lantai" / "runtime" / "v1" / "migration-backups").exists()

    by_type = {source.source_type: source for source in report.sources}
    experience = by_type["experience_ledger"]
    assert experience.line_count == 2
    assert experience.record_count == 1
    assert experience.migratable_count == 1
    assert experience.skipped_count == 1
    assert experience.content_sha256.startswith("sha256:")
    assert experience.projected_runtime_event_ids == ["evt-1"]

    task_runs = by_type["task_runs"]
    assert task_runs.projected_runtime_run_ids == ["task-conflict"]
    assert task_runs.conflict_count == 1
    assert task_runs.issues[0].code == "canonical_contract_exists"

    evaluations = by_type["evaluation_reports"]
    assert evaluations.record_count == 1
    assert evaluations.conflict_count == 1
    assert evaluations.issues[0].code == "orphan_evaluation"

    mission = by_type["mission_state"]
    assert mission.projected_runtime_run_ids == ["mission-20260716-130000"]
    assert mission.migratable_count == 1

    browser = by_type["browser_task_state"]
    assert browser.projected_runtime_run_ids == ["browser-runtime-run-1"]
    assert browser.migratable_count == 1

    writers = {item.writer_id: item for item in report.writer_inventory}
    assert set(writers) == {
        "legacy_runtime_event_ledger_writer",
        "legacy_task_run_snapshot_writer",
        "legacy_evaluation_report_writer",
        "legacy_mission_state_writer",
        "legacy_browser_task_state_writer",
    }
    assert writers["legacy_runtime_event_ledger_writer"].observed_record_count == 1
    assert writers["legacy_task_run_snapshot_writer"].observed_issue_count == 1
    assert writers["legacy_evaluation_report_writer"].observed_issue_count == 1
    assert writers["legacy_mission_state_writer"].observed_record_count == 1
    assert writers["legacy_browser_task_state_writer"].observed_record_count == 1
    assert all(item.can_stop_writes is False for item in writers.values())
    assert all(
        "two_official_release_cycles_without_fallback_write"
        in item.stop_writes_blockers
        for item in writers.values()
    )


def test_runtime_migration_report_handles_missing_workspace_without_writes(tmp_path):
    workspace_dir = tmp_path / "missing-workspace"

    report = build_runtime_migration_report(workspace_dir)

    assert not workspace_dir.exists()
    assert report.totals.source_count == 0
    assert report.totals.record_count == 0
    assert report.sources == []
    assert len(report.writer_inventory) == 5
