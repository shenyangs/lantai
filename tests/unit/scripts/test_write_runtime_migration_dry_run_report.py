# -*- coding: utf-8 -*-
"""Tests for the Runtime migration dry-run evidence writer."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from lantai.app.runtime_legacy_writers import LEGACY_RUNTIME_WRITER_MODE_ENV


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "write_runtime_migration_dry_run_report.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "write_runtime_migration_dry_run_report",
        SCRIPT_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        snapshot[rel] = path.read_bytes() if path.is_file() else None
    return snapshot


def test_write_runtime_migration_dry_run_report_is_side_effect_free(
    tmp_path: Path,
) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    event_path = workspace / ".lantai" / "experience" / "events.jsonl"
    event_path.parent.mkdir(parents=True)
    event_path.write_text("", encoding="utf-8")
    output = tmp_path / "release-evidence" / "runtime-migration-dry-run-report.json"

    before = _tree_snapshot(workspace)
    written = module.write_runtime_migration_dry_run_report(
        workspace=workspace,
        output=output,
        release_edition="official",
    )
    after = _tree_snapshot(workspace)

    assert written == output
    assert after == before
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["dry_run"] is True
    assert payload["side_effect_free"] is True
    assert payload["side_effects_executed"] == 0
    assert payload["legacy_writer_mode"] == "active"
    assert {item["writer_id"] for item in payload["writer_inventory"]} == {
        "legacy_runtime_event_ledger_writer",
        "legacy_task_run_snapshot_writer",
        "legacy_evaluation_report_writer",
        "legacy_mission_state_writer",
        "legacy_browser_task_state_writer",
    }


def test_write_runtime_migration_dry_run_report_can_prove_read_only_shutdown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    monkeypatch.setenv(LEGACY_RUNTIME_WRITER_MODE_ENV, "active")
    workspace = tmp_path / "workspace"
    output = tmp_path / "report.json"

    module.write_runtime_migration_dry_run_report(
        workspace=workspace,
        output=output,
        release_edition="official",
        legacy_writer_mode="read_only",
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["legacy_writer_mode"] == "read_only"
    assert {
        item["migration_status"] for item in payload["writer_inventory"]
    } == {"legacy_write_stopped_read_compatibility"}
    assert all(
        item["legacy_write_allowed"] is False
        for item in payload["writer_inventory"]
    )
    assert all(item["can_stop_writes"] is True for item in payload["writer_inventory"])
    assert all(item["stop_writes_blockers"] == [] for item in payload["writer_inventory"])
    assert module.os.environ[LEGACY_RUNTIME_WRITER_MODE_ENV] == "active"


def test_main_prints_written_report_path(tmp_path: Path, capsys) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    output = tmp_path / "report.json"

    result = module.main(
        [
            "--workspace",
            str(workspace),
            "--output",
            str(output),
            "--release-edition",
            "mas",
            "--legacy-writer-mode",
            "read_only",
        ]
    )

    assert result == 0
    assert output.is_file()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["legacy_writer_mode"] == "read_only"
    assert str(output) in capsys.readouterr().out
