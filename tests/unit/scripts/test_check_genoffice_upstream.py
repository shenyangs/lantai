# -*- coding: utf-8 -*-
"""Tests for the GenOffice release freshness gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/check_genoffice_upstream.py"
SPEC = importlib.util.spec_from_file_location("check_genoffice_upstream", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_parse_source_pin_requires_exact_40_character_commit():
    commit = "8f523289d6c34f940cd691472ee56b2013d148c8"
    assert MODULE.parse_source_pin(f"- 固定提交：`{commit}`") == commit
    assert MODULE.parse_source_pin(f"固定提交仍为 `{commit}`") == commit

    with pytest.raises(ValueError, match="40 位"):
        MODULE.parse_source_pin("- 固定提交：`8f52328`")


def test_compare_pin_distinguishes_current_and_stale():
    old = "4da673d4dfa994bd0b4a9bc43430e4a058a17c61"
    new = "8f523289d6c34f940cd691472ee56b2013d148c8"
    assert MODULE.compare_pin(new, new) == "current"
    assert MODULE.compare_pin(old, new) == "stale"


def test_main_writes_current_report(monkeypatch, tmp_path):
    commit = "8f523289d6c34f940cd691472ee56b2013d148c8"
    source = tmp_path / "SOURCE.md"
    source.write_text(f"- 固定提交：`{commit}`\n", encoding="utf-8")
    report = tmp_path / "report.json"
    monkeypatch.setattr(MODULE, "fetch_upstream_commit", lambda *_args: commit)

    assert MODULE.main(["--source-file", str(source), "--write-report", str(report)]) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "current"
    assert payload["pinned_commit"] == commit
    assert payload["upstream_commit"] == commit


def test_main_stops_release_when_upstream_changed(monkeypatch, tmp_path):
    old = "4da673d4dfa994bd0b4a9bc43430e4a058a17c61"
    new = "8f523289d6c34f940cd691472ee56b2013d148c8"
    source = tmp_path / "SOURCE.md"
    source.write_text(f"- 固定提交：`{old}`\n", encoding="utf-8")
    monkeypatch.setattr(MODULE, "fetch_upstream_commit", lambda *_args: new)

    with pytest.raises(SystemExit, match="发版已停止"):
        MODULE.main(["--source-file", str(source)])


def test_main_can_record_a_stale_upstream_without_blocking_the_build(monkeypatch, tmp_path):
    old = "4da673d4dfa994bd0b4a9bc43430e4a058a17c61"
    new = "8f523289d6c34f940cd691472ee56b2013d148c8"
    source = tmp_path / "SOURCE.md"
    report = tmp_path / "report.json"
    source.write_text(f"- 固定提交：`{old}`\n", encoding="utf-8")
    monkeypatch.setattr(MODULE, "fetch_upstream_commit", lambda *_args: new)

    assert MODULE.main([
        "--source-file", str(source), "--write-report", str(report), "--allow-stale",
    ]) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "stale"


def test_main_reuses_a_recent_stale_report_for_the_configured_interval(monkeypatch, tmp_path):
    old = "4da673d4dfa994bd0b4a9bc43430e4a058a17c61"
    new = "8f523289d6c34f940cd691472ee56b2013d148c8"
    source = tmp_path / "SOURCE.md"
    report = tmp_path / "report.json"
    source.write_text(f"- 固定提交：`{old}`\n", encoding="utf-8")
    report.write_text(json.dumps({
        "schema": "com.lantai.genoffice-upstream-check.v1",
        "repository": MODULE.DEFAULT_REPOSITORY,
        "branch": MODULE.DEFAULT_BRANCH,
        "pinned_commit": old,
        "upstream_commit": new,
        "status": "stale",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "detail": "",
    }), encoding="utf-8")

    def should_not_fetch(*_args):
        raise AssertionError("recent report should avoid an upstream request")

    monkeypatch.setattr(MODULE, "fetch_upstream_commit", should_not_fetch)
    assert MODULE.main([
        "--source-file", str(source), "--write-report", str(report),
        "--check-interval-hours", "72", "--allow-stale",
    ]) == 0


def test_main_refreshes_an_expired_report(monkeypatch, tmp_path):
    old = "4da673d4dfa994bd0b4a9bc43430e4a058a17c61"
    new = "8f523289d6c34f940cd691472ee56b2013d148c8"
    source = tmp_path / "SOURCE.md"
    report = tmp_path / "report.json"
    source.write_text(f"- 固定提交：`{old}`\n", encoding="utf-8")
    report.write_text(json.dumps({
        "schema": "com.lantai.genoffice-upstream-check.v1",
        "repository": MODULE.DEFAULT_REPOSITORY,
        "branch": MODULE.DEFAULT_BRANCH,
        "pinned_commit": old,
        "upstream_commit": new,
        "status": "stale",
        "checked_at": (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat(),
        "detail": "",
    }), encoding="utf-8")
    calls: list[bool] = []

    def fetch(*_args):
        calls.append(True)
        return new

    monkeypatch.setattr(MODULE, "fetch_upstream_commit", fetch)
    assert MODULE.main([
        "--source-file", str(source), "--write-report", str(report),
        "--check-interval-hours", "72", "--allow-stale",
    ]) == 0
    assert calls == [True]


def test_network_failure_is_only_softened_when_explicit(monkeypatch, tmp_path):
    commit = "8f523289d6c34f940cd691472ee56b2013d148c8"
    source = tmp_path / "SOURCE.md"
    source.write_text(f"- 固定提交：`{commit}`\n", encoding="utf-8")

    def fail(*_args):
        raise RuntimeError("offline")

    monkeypatch.setattr(MODULE, "fetch_upstream_commit", fail)
    with pytest.raises(SystemExit, match="offline"):
        MODULE.main(["--source-file", str(source)])
    assert MODULE.main(["--source-file", str(source), "--allow-network-failure"]) == 0
