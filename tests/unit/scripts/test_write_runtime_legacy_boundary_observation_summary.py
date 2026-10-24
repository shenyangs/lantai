# -*- coding: utf-8 -*-
"""CLI behavior locks for four-category Legacy boundary evidence."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from lantai.app.runtime_legacy_execution_observations import (
    LEGACY_RUNTIME_EXECUTION_OBSERVATION_SCHEMA,
)


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "write_runtime_legacy_boundary_observation_summary.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "write_runtime_legacy_boundary_observation_summary",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summary_writer_preserves_construction_only_semantics(tmp_path: Path) -> None:
    module = _load_module()
    execution_input = tmp_path / "execution.jsonl"
    writer_input = tmp_path / "writers.jsonl"
    output = tmp_path / "summary.json"
    execution_input.write_text(
        json.dumps(
            {
                "schema": LEGACY_RUNTIME_EXECUTION_OBSERVATION_SCHEMA,
                "kind": "legacy_agent_constructed",
                "surface": "LegacyLantaiAgent.__init__",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    writer_input.write_text("", encoding="utf-8")

    written = module.write_runtime_legacy_boundary_observation_summary(
        execution_input=execution_input,
        writer_input=writer_input,
        output=output,
        release_edition="official",
    )

    summary = json.loads(written.read_text(encoding="utf-8"))
    assert summary["construction_only"] is True
    assert summary["zero_forbidden_legacy_activity"] is True
    assert summary["counts_by_kind"]["legacy_writer_attempted"] == 0


def test_cli_gate_rejects_legacy_loop_activity(tmp_path: Path) -> None:
    module = _load_module()
    execution_input = tmp_path / "execution.jsonl"
    writer_input = tmp_path / "writers.jsonl"
    output = tmp_path / "summary.json"
    execution_input.write_text(
        json.dumps(
            {
                "schema": LEGACY_RUNTIME_EXECUTION_OBSERVATION_SCHEMA,
                "kind": "legacy_loop_invoked",
                "surface": "LegacyAgentLoop.reply",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    writer_input.write_text("", encoding="utf-8")

    result = module.main(
        [
            "--execution-input",
            str(execution_input),
            "--writer-input",
            str(writer_input),
            "--output",
            str(output),
            "--require-zero-forbidden-activity",
        ]
    )

    assert result == 3
