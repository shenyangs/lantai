#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exercise Runtime migration backup, restore, and downgrade-read evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lantai.agents.mission.state import read_loop_config, read_prd
from lantai.app.browser_workbench.models import BrowserAction, BrowserTaskRun
from lantai.app.browser_workbench.tasks import BrowserTaskStore
from lantai.app.capabilities.legacy_adapter import LegacyTaskRunCompatibilityAdapter
from lantai.app.capabilities.models import EvaluationReport, TaskRunsFile
from lantai.app.experience.legacy_adapter import LegacyExperienceEventAdapter
from lantai.app.experience.schemas import ChatExperienceEvent
from lantai.app.runtime_migration import (
    RUNTIME_MIGRATION_REPORT_SCHEMA_VERSION,
    build_runtime_migration_report,
)
from lantai.runtime.runs import RuntimeReleaseEdition


SCHEMA = "com.lantai.runtime-migration-backup-restore-smoke.v1"
REQUIRED_CHECKS = {
    "fixture_legacy_sources_seeded",
    "dry_run_report_side_effect_free",
    "legacy_sources_backed_up_to_scratch",
    "legacy_sources_restored_from_scratch_backup",
    "restored_report_matches_original_signatures",
    "downgrade_readers_parse_restored_legacy_sources",
}
NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        snapshot[rel] = path.read_bytes() if path.is_file() else None
    return snapshot


def _seed_fixture_workspace(workspace: Path) -> None:
    event_path = workspace / ".lantai" / "experience" / "events.jsonl"
    event_path.parent.mkdir(parents=True)
    event_path.write_text(
        json.dumps(
            {
                "id": "evt-backup-1",
                "seq": 1,
                "ts": NOW.isoformat(),
                "agent_id": "agent-default",
                "chat_id": "chat-backup",
                "session_id": "session-backup",
                "run_id": "run-chat-backup",
                "scope": "chat",
                "kind": "chat.turn.started",
                "type": "turn.started",
                "status": "running",
                "source": "user",
                "summary": "started",
                "payload": {"text_preview": "backup restore smoke"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    _write_json(
        workspace / "capabilities" / "task-runs.json",
        {
            "version": 1,
            "runs": [
                {
                    "id": "task-backup-1",
                    "agent_id": "agent-default",
                    "source": "capability",
                    "chat_id": "chat-backup",
                    "session_id": "session-backup",
                    "title": "Backup restore task",
                    "status": "succeeded",
                    "started_at": NOW.isoformat(),
                    "ended_at": NOW.isoformat(),
                    "metadata": {"workspace_id": "workspace-backup"},
                }
            ],
        },
    )
    _write_json(
        workspace
        / "capabilities"
        / "evaluations"
        / "task-backup-1"
        / "evaluation-backup-1.json",
        {
            "schema_version": 1,
            "id": "evaluation-backup-1",
            "task_run_id": "task-backup-1",
            "conclusion": "pass",
            "summary": "Backup restore evaluation",
            "created_at": NOW.isoformat(),
        },
    )
    _write_json(
        workspace / "missions" / "mission-20260718-120000" / "loop_config.json",
        {
            "session_id": "mission-session",
            "agent_id": "agent-default",
        },
    )
    _write_json(
        workspace / "missions" / "mission-20260718-120000" / "prd.json",
        {
            "project": "Runtime migration backup restore",
            "userStories": [{"title": "Restore legacy state", "passes": True}],
        },
    )
    (workspace / "missions" / "mission-20260718-120000" / "progress.txt").write_text(
        "restorable\n",
        encoding="utf-8",
    )

    run = BrowserTaskRun(
        run_id="browser-backup-1",
        profile_id="profile-backup",
        access="write",
        instruction="Prepare a draft",
        status="completed",
        metadata={
            "workspace_dir": str(workspace),
            "agent_id": "browser-agent",
            "session_id": "browser-session",
        },
    )
    action = BrowserAction(
        action="fill_form",
        profile_id="profile-backup",
        url="https://example.com/draft",
        access="write",
        run_id=run.run_id,
    )
    _write_json(
        workspace / "browser_workbench" / "tasks" / f"{run.run_id}.json",
        {
            "version": 1,
            "run": run.model_dump(mode="json"),
            "events": [],
            "actions": [action.model_dump(mode="json")],
            "next_index": 1,
        },
    )


def _source_relative_path(workspace: Path, source_path: str) -> str:
    return str(Path(source_path).resolve().relative_to(workspace.resolve()))


def _source_signatures(report: Any, workspace: Path) -> list[dict[str, Any]]:
    signatures: list[dict[str, Any]] = []
    for source in report.sources:
        signatures.append(
            {
                "source_type": source.source_type,
                "relative_path": _source_relative_path(workspace, source.path),
                "bytes": source.bytes,
                "content_sha256": source.content_sha256,
                "line_count": source.line_count,
                "record_count": source.record_count,
                "migratable_count": source.migratable_count,
                "skipped_count": source.skipped_count,
                "conflict_count": source.conflict_count,
                "issue_codes": [issue.code for issue in source.issues],
                "projected_runtime_run_ids": source.projected_runtime_run_ids,
                "projected_runtime_event_ids": source.projected_runtime_event_ids,
            }
        )
    return sorted(
        signatures,
        key=lambda item: (str(item["source_type"]), str(item["relative_path"])),
    )


def _copy_source(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _backup_sources(report: Any, workspace: Path, backup_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source in report.sources:
        relative_path = _source_relative_path(workspace, source.path)
        source_path = workspace / relative_path
        backup_path = backup_root / relative_path
        _copy_source(source_path, backup_path)
        entries.append(
            {
                "source_type": source.source_type,
                "relative_path": relative_path,
                "content_sha256": source.content_sha256,
                "bytes": source.bytes,
            }
        )
    return entries


def _restore_sources(entries: list[dict[str, Any]], backup_root: Path, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        relative_path = str(entry["relative_path"])
        _copy_source(backup_root / relative_path, workspace / relative_path)


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            rows.append(json.loads(text))
    return rows


def _downgrade_reader_checks(
    workspace: Path,
    *,
    release_edition: RuntimeReleaseEdition,
) -> dict[str, Any]:
    event_rows = _read_json_lines(workspace / ".lantai" / "experience" / "events.jsonl")
    runtime_events = LegacyExperienceEventAdapter().to_runtime_events(event_rows)

    task_runs_payload = json.loads(
        (workspace / "capabilities" / "task-runs.json").read_text(encoding="utf-8")
    )
    task_runs = TaskRunsFile.model_validate(task_runs_payload)
    contracts = LegacyTaskRunCompatibilityAdapter(
        release_edition=release_edition,
    ).to_runtime_run_contracts(task_runs.runs)

    evaluation_paths = sorted(
        (workspace / "capabilities" / "evaluations").glob("**/*.json")
    )
    evaluations = [
        EvaluationReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in evaluation_paths
    ]

    mission_dir = workspace / "missions" / "mission-20260718-120000"
    loop_config = read_loop_config(mission_dir)
    prd = read_prd(mission_dir)

    browser_records = BrowserTaskStore(workspace).load_all()

    return {
        "experience_runtime_event_count": len(runtime_events),
        "task_run_contract_count": len(contracts),
        "evaluation_report_count": len(evaluations),
        "mission_config_read": loop_config.get("session_id") == "mission-session",
        "mission_prd_read": bool(prd.get("userStories")),
        "browser_task_record_count": len(browser_records),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/release-evidence/runtime-migration-backup-restore-smoke.json"),
        help="JSON evidence file to write.",
    )
    parser.add_argument(
        "--release-edition",
        choices=("official", "mas"),
        default="official",
        help="Release edition used by downgrade projection adapters.",
    )
    return parser


def run_runtime_migration_backup_restore_smoke(
    *,
    output: Path,
    release_edition: RuntimeReleaseEdition,
) -> Path:
    with tempfile.TemporaryDirectory(prefix="lantai-runtime-migration-") as temp_name:
        scratch = Path(temp_name)
        workspace = scratch / "workspace"
        backup_root = scratch / "backup"
        archived_workspace = scratch / "workspace-before-restore"

        _seed_fixture_workspace(workspace)
        before_report_snapshot = _tree_snapshot(workspace)
        original_report = build_runtime_migration_report(
            workspace,
            release_edition=release_edition,
        )
        after_report_snapshot = _tree_snapshot(workspace)
        source_workspace_side_effect_free = before_report_snapshot == after_report_snapshot

        backup_entries = _backup_sources(original_report, workspace, backup_root)
        source_snapshot = _tree_snapshot(workspace)
        workspace.rename(archived_workspace)
        _restore_sources(backup_entries, backup_root, workspace)
        restored_snapshot = _tree_snapshot(workspace)
        restored_report = build_runtime_migration_report(
            workspace,
            release_edition=release_edition,
        )

        original_signatures = _source_signatures(original_report, workspace)
        restored_signatures = _source_signatures(restored_report, workspace)
        downgrade_readers = _downgrade_reader_checks(
            workspace,
            release_edition=release_edition,
        )
        downgrade_readers_passed = (
            downgrade_readers["experience_runtime_event_count"] >= 1
            and downgrade_readers["task_run_contract_count"] >= 1
            and downgrade_readers["evaluation_report_count"] >= 1
            and downgrade_readers["mission_config_read"] is True
            and downgrade_readers["mission_prd_read"] is True
            and downgrade_readers["browser_task_record_count"] >= 1
        )
        checks = []
        if len(original_report.sources) >= 5:
            checks.append("fixture_legacy_sources_seeded")
        if (
            original_report.schema_version == RUNTIME_MIGRATION_REPORT_SCHEMA_VERSION
            and original_report.dry_run is True
            and original_report.side_effect_free is True
            and original_report.side_effects_executed == 0
            and source_workspace_side_effect_free
        ):
            checks.append("dry_run_report_side_effect_free")
        if len(backup_entries) == len(original_report.sources):
            checks.append("legacy_sources_backed_up_to_scratch")
        if source_snapshot == restored_snapshot:
            checks.append("legacy_sources_restored_from_scratch_backup")
        if original_signatures == restored_signatures:
            checks.append("restored_report_matches_original_signatures")
        if downgrade_readers_passed:
            checks.append("downgrade_readers_parse_restored_legacy_sources")

        passed = set(checks) == REQUIRED_CHECKS
        payload = {
            "schema": SCHEMA,
            "passed": passed,
            "release_edition": release_edition,
            "fixture_seeded": True,
            "source_workspace_side_effect_free": source_workspace_side_effect_free,
            "scratch_workspace_persistent": False,
            "source_count": len(original_report.sources),
            "backup_entry_count": len(backup_entries),
            "restored_source_count": len(restored_report.sources),
            "source_tree_restored_exactly": source_snapshot == restored_snapshot,
            "signatures_match": original_signatures == restored_signatures,
            "checks": sorted(checks),
            "required_checks": sorted(REQUIRED_CHECKS),
            "source_signatures": original_signatures,
            "restored_source_signatures": restored_signatures,
            "downgrade_readers": downgrade_readers,
        }

    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = run_runtime_migration_backup_restore_smoke(
            output=args.output,
            release_edition=args.release_edition,
        )
    except Exception as exc:  # noqa: BLE001 - command-line diagnostics
        print(f"Runtime migration backup/restore smoke failed: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
