# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from lantai.utils.privacy import purge_legacy_telemetry_data


def test_purge_legacy_telemetry_data_removes_queue_and_marker(
    tmp_path: Path,
) -> None:
    telemetry_dir = tmp_path / ".telemetry"
    telemetry_dir.mkdir()
    (telemetry_dir / "events.jsonl").write_text("queued\n", "utf-8")
    (telemetry_dir / "recent-events.jsonl").write_text("recent\n", "utf-8")
    marker = tmp_path / ".telemetry_collected"
    marker.write_text('{"telemetry_enabled": true}', "utf-8")
    unrelated = tmp_path / "settings.json"
    unrelated.write_text("{}", "utf-8")

    removed = purge_legacy_telemetry_data(tmp_path)

    assert removed == 2
    assert not telemetry_dir.exists()
    assert not marker.exists()
    assert unrelated.read_text("utf-8") == "{}"


def test_purge_legacy_telemetry_data_is_idempotent(tmp_path: Path) -> None:
    assert purge_legacy_telemetry_data(tmp_path) == 0
    assert purge_legacy_telemetry_data(tmp_path) == 0
