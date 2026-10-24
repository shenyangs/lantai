# -*- coding: utf-8 -*-
"""Behavior locks for privacy-safe Legacy execution observations."""

from __future__ import annotations

import json
from pathlib import Path

from lantai.agents.legacy_agent_components import LegacyAgentLoop
from lantai.app.runtime_legacy_execution_observations import (
    LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
    LEGACY_RUNTIME_EXECUTION_OBSERVATION_SCHEMA,
    record_legacy_runtime_execution_observation,
    summarize_legacy_runtime_boundary_observations,
    summarize_legacy_runtime_execution_observations,
)
from lantai.app.runtime_legacy_writers import (
    LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
    assert_legacy_runtime_writer_allowed,
)


def test_observation_is_disabled_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        raising=False,
    )

    recorded = record_legacy_runtime_execution_observation(
        kind="legacy_agent_constructed",
        surface="LegacyLantaiAgent.__init__",
    )

    assert recorded is False
    assert list(tmp_path.iterdir()) == []


def test_observation_records_only_bounded_operational_dimensions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observation_file = tmp_path / "evidence" / "legacy-execution.jsonl"
    monkeypatch.setenv(
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        str(observation_file),
    )

    recorded = record_legacy_runtime_execution_observation(
        kind="legacy_loop_invoked",
        surface="LegacyAgentLoop.reply",
        release="2.1.0",
        entrypoint="runner.run_turn",
        engine="legacy",
        channel="console",
        reason_code="explicit_legacy_engine",
    )

    assert recorded is True
    record = json.loads(observation_file.read_text(encoding="utf-8"))
    assert record["schema"] == LEGACY_RUNTIME_EXECUTION_OBSERVATION_SCHEMA
    assert record["kind"] == "legacy_loop_invoked"
    assert record["surface"] == "LegacyAgentLoop.reply"
    assert record["release"] == "2.1.0"
    assert record["entrypoint"] == "runner.run_turn"
    assert record["engine"] == "legacy"
    assert record["channel"] == "console"
    assert record["reason_code"] == "explicit_legacy_engine"
    assert "prompt" not in record
    assert "memory" not in record
    assert "tool_args" not in record
    assert observation_file.stat().st_mode & 0o777 == 0o600


def test_observation_redacts_arbitrary_text_instead_of_persisting_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observation_file = tmp_path / "legacy-execution.jsonl"
    monkeypatch.setenv(
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        str(observation_file),
    )
    secret = "user said: do not persist this sentence"

    assert record_legacy_runtime_execution_observation(
        kind="legacy_agent_constructed",
        surface="LegacyLantaiAgent.__init__",
        reason_code=secret,
    )

    raw = observation_file.read_text(encoding="utf-8")
    assert secret not in raw
    assert json.loads(raw)["reason_code"] == "redacted"


def test_observation_io_failure_never_changes_product_flow(
    monkeypatch,
    tmp_path: Path,
) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv(
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        str(parent_file / "legacy-execution.jsonl"),
    )

    recorded = record_legacy_runtime_execution_observation(
        kind="legacy_runtime_adapter_selected",
        surface="LegacyRuntimeAdapter.run_turn",
    )

    assert recorded is False


def test_summary_distinguishes_facade_construction_from_execution(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observation_file = tmp_path / "legacy-execution.jsonl"
    monkeypatch.setenv(
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        str(observation_file),
    )

    assert record_legacy_runtime_execution_observation(
        kind="legacy_agent_constructed",
        surface="LegacyLantaiAgent.__init__",
        release="2.1.0",
        entrypoint="runner.run_turn",
        engine="native",
        channel="console",
        reason_code="intelligence_facade",
    )

    summary = summarize_legacy_runtime_execution_observations(
        observation_file,
        release_edition="official",
    )

    assert summary["event_count"] == 1
    assert summary["counts_by_kind"] == {
        "legacy_agent_constructed": 1,
        "legacy_loop_invoked": 0,
        "legacy_runtime_adapter_selected": 0,
    }
    assert summary["zero_legacy_loop_invocations"] is True
    assert summary["zero_legacy_runtime_adapter_selections"] is True
    assert summary["construction_only"] is True
    assert summary["evidence_valid"] is True


def test_summary_rejects_malformed_unknown_and_invalid_records(tmp_path: Path) -> None:
    observation_file = tmp_path / "legacy-execution.jsonl"
    observation_file.write_text(
        "\n".join(
            (
                "not-json",
                json.dumps({"schema": "unknown"}),
                json.dumps(
                    {
                        "schema": LEGACY_RUNTIME_EXECUTION_OBSERVATION_SCHEMA,
                        "kind": "legacy_writer_attempted",
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    summary = summarize_legacy_runtime_execution_observations(
        observation_file,
        release_edition="official",
    )

    assert summary["event_count"] == 0
    assert summary["malformed_count"] == 1
    assert summary["unknown_schema_count"] == 1
    assert summary["invalid_kind_count"] == 1
    assert summary["evidence_valid"] is False
    assert summary["zero_legacy_loop_invocations"] is False
    assert summary["zero_legacy_runtime_adapter_selections"] is False


def test_legacy_loop_execution_surface_is_absent_after_r4() -> None:
    assert "__call__" not in LegacyAgentLoop.__dict__
    assert "reply" not in LegacyAgentLoop.__dict__
    assert "_reasoning" not in LegacyAgentLoop.__dict__
    assert "_acting" not in LegacyAgentLoop.__dict__
    assert "_record_legacy_loop_invocation" not in LegacyAgentLoop.__dict__


def test_boundary_summary_keeps_four_categories_distinct(
    monkeypatch,
    tmp_path: Path,
) -> None:
    execution_file = tmp_path / "legacy-execution.jsonl"
    writer_file = tmp_path / "legacy-writers.jsonl"
    monkeypatch.setenv(
        LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV,
        str(execution_file),
    )
    monkeypatch.setenv(
        LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV,
        str(writer_file),
    )
    assert record_legacy_runtime_execution_observation(
        kind="legacy_agent_constructed",
        surface="LegacyLantaiAgent.__init__",
    )
    assert_legacy_runtime_writer_allowed("legacy_task_run_snapshot_writer")

    summary = summarize_legacy_runtime_boundary_observations(
        execution_file,
        writer_file,
        release_edition="official",
    )

    assert summary["counts_by_kind"] == {
        "legacy_agent_constructed": 1,
        "legacy_loop_invoked": 0,
        "legacy_runtime_adapter_selected": 0,
        "legacy_writer_attempted": 1,
    }
    assert summary["forbidden_activity_count"] == 1
    assert summary["zero_forbidden_legacy_activity"] is False
    assert summary["construction_only"] is False


def test_boundary_summary_accepts_construction_only_evidence(tmp_path: Path) -> None:
    execution_file = tmp_path / "legacy-execution.jsonl"
    writer_file = tmp_path / "legacy-writers.jsonl"
    execution_file.write_text(
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
    writer_file.write_text("", encoding="utf-8")

    summary = summarize_legacy_runtime_boundary_observations(
        execution_file,
        writer_file,
        release_edition="official",
    )

    assert summary["observation_files_exist"] is True
    assert summary["zero_forbidden_legacy_activity"] is True
    assert summary["construction_only"] is True


def test_boundary_summary_does_not_treat_missing_files_as_zero_evidence(
    tmp_path: Path,
) -> None:
    summary = summarize_legacy_runtime_boundary_observations(
        tmp_path / "missing-execution.jsonl",
        tmp_path / "missing-writers.jsonl",
        release_edition="official",
    )

    assert summary["observation_files_exist"] is False
    assert summary["zero_forbidden_legacy_activity"] is False
    assert summary["construction_only"] is False
