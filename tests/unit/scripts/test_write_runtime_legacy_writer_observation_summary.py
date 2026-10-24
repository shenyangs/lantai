# -*- coding: utf-8 -*-
"""Tests for legacy Runtime writer observation summary evidence."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from lantai.app.runtime_legacy_writers import (
    LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
    LEGACY_RUNTIME_WRITER_OBSERVATION_SUMMARY_SCHEMA,
    assert_legacy_runtime_writer_allowed,
)


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "write_runtime_legacy_writer_observation_summary.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "write_runtime_legacy_writer_observation_summary",
        SCRIPT_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_write_runtime_legacy_writer_observation_summary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    observation_file = tmp_path / "legacy-writers.jsonl"
    output = tmp_path / "summary.json"
    monkeypatch.setenv(
        LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
        str(observation_file),
    )

    assert_legacy_runtime_writer_allowed(
        "legacy_task_run_snapshot_writer",
        path=tmp_path / "workspace" / "capabilities" / "task-runs.json",
    )
    written = module.write_runtime_legacy_writer_observation_summary(
        input_path=observation_file,
        output=output,
        release_edition="official",
    )

    assert written == output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == LEGACY_RUNTIME_WRITER_OBSERVATION_SUMMARY_SCHEMA
    assert payload["release_edition"] == "official"
    assert payload["event_count"] == 1
    assert payload["fallback_write_count"] == 1
    assert payload["blocked_attempt_count"] == 0
    assert payload["zero_fallback_writes"] is False
    assert payload["counts_by_writer"]["legacy_task_run_snapshot_writer"] == 1


def test_main_can_require_zero_fallback_writes(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_module()
    observation_file = tmp_path / "legacy-writers.jsonl"
    output = tmp_path / "summary.json"
    monkeypatch.setenv(
        LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
        str(observation_file),
    )
    assert_legacy_runtime_writer_allowed(
        "legacy_runtime_event_ledger_writer",
        path=tmp_path / ".lantai" / "experience" / "events.jsonl",
    )

    result = module.main(
        [
            "--input",
            str(observation_file),
            "--output",
            str(output),
            "--release-edition",
            "mas",
            "--require-zero-fallback-writes",
        ]
    )

    assert result == 4
    assert "fallback_write_count=1" in capsys.readouterr().err
    assert output.is_file()


def test_main_passes_when_zero_observations_required_for_empty_file(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_module()
    observation_file = tmp_path / "legacy-writers.jsonl"
    output = tmp_path / "summary.json"
    observation_file.write_text("", encoding="utf-8")

    result = module.main(
        [
            "--input",
            str(observation_file),
            "--output",
            str(output),
            "--release-edition",
            "official",
            "--require-zero-observations",
        ]
    )

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["zero_observations"] is True
    assert str(output) in capsys.readouterr().out
