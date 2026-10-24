# -*- coding: utf-8 -*-
"""Tests for Runtime migration backup/restore evidence smoke."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "run_runtime_migration_backup_restore_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_runtime_migration_backup_restore_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_migration_backup_restore_smoke_writes_passing_evidence(
    tmp_path: Path,
) -> None:
    module = _load_module()
    output = tmp_path / "runtime-migration-backup-restore-smoke.json"

    written = module.run_runtime_migration_backup_restore_smoke(
        output=output,
        release_edition="official",
    )

    assert written == output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == module.SCHEMA
    assert payload["passed"] is True
    assert payload["source_workspace_side_effect_free"] is True
    assert payload["source_count"] == 5
    assert payload["backup_entry_count"] == payload["source_count"]
    assert payload["restored_source_count"] == payload["source_count"]
    assert payload["source_tree_restored_exactly"] is True
    assert payload["signatures_match"] is True
    assert set(payload["checks"]) == module.REQUIRED_CHECKS
    assert payload["downgrade_readers"]["experience_runtime_event_count"] == 1
    assert payload["downgrade_readers"]["task_run_contract_count"] == 1
    assert payload["downgrade_readers"]["evaluation_report_count"] == 1
    assert payload["downgrade_readers"]["mission_config_read"] is True
    assert payload["downgrade_readers"]["mission_prd_read"] is True
    assert payload["downgrade_readers"]["browser_task_record_count"] == 1


def test_runtime_migration_backup_restore_smoke_main_prints_output(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_module()
    output = tmp_path / "evidence.json"

    result = module.main(
        [
            "--release-edition",
            "mas",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert output.is_file()
    assert str(output) in capsys.readouterr().out
