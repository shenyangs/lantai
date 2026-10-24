# -*- coding: utf-8 -*-
"""Tests for reminder delivery smoke evidence."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "run_reminder_delivery_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_reminder_delivery_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reminder_delivery_smoke_writes_restart_safe_evidence(
    tmp_path: Path,
) -> None:
    module = _load_module()
    output = tmp_path / "reminder-delivery-smoke.json"
    workspace = tmp_path / "workspace"

    written = module.run_reminder_delivery_smoke(
        output=output,
        workspace=workspace,
        release_edition="official",
    )

    assert written == output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == module.SCHEMA
    assert payload["adapter_kind"] == "injected-test"
    assert payload["passed"] is True
    assert set(payload["checks"]) == module.REQUIRED_CHECKS
    assert all(payload["checks"].values())
    assert payload["delivery_count"] == 1
    assert payload["first_tick"]["delivered_count"] == 1
    assert payload["restart_tick"]["deduped_count"] == 1
    assert len(payload["runtime_runs"]) == 1
    assert payload["runtime_runs"][0]["status"] == "succeeded"
    assert payload["runtime_runs"][0]["release_edition"] == "official"


def test_reminder_delivery_smoke_main_prints_output(
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
        ],
    )

    assert result == 0
    assert output.is_file()
    assert str(output) in capsys.readouterr().out
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runtime_runs"][0]["release_edition"] == "mas"
