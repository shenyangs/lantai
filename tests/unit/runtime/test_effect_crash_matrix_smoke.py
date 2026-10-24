# -*- coding: utf-8 -*-
"""Process-kill smoke coverage for Runtime Effect replay safety."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/run_effect_crash_matrix_smoke.py"
KILL_POINTS = {
    "before_intent_write",
    "after_intent_fsync_before_execution",
    "after_external_call_before_response",
    "after_external_success_before_commit",
    "after_commit_before_checkpoint",
    "after_checkpoint_before_terminal",
}
DANGEROUS_WINDOWS = {
    "after_external_call_before_response",
    "after_external_success_before_commit",
    "after_commit_before_checkpoint",
    "after_checkpoint_before_terminal",
}


def test_effect_crash_matrix_smoke_kills_processes_and_blocks_unsafe_replay(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workspace",
            str(tmp_path / "effect-crash-smoke"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(completed.stdout)

    assert result["ok"] is True
    by_point = {item["kill_point"]: item for item in result["kill_points"]}
    assert set(by_point) == KILL_POINTS
    assert all(item["child_killed"] for item in by_point.values())

    assert by_point["before_intent_write"]["observed_status"] == "missing"
    assert by_point["after_intent_fsync_before_execution"]["observed_status"] == "intent"

    for kill_point in DANGEROUS_WINDOWS:
        item = by_point[kill_point]
        assert item["external_calls_before_recovery"] == 1
        assert item["external_calls_after_recovery"] == 1
        assert item["replayed_external_call"] is False
        assert item["external_call_must_not_be_replayed"] is True

    after_commit = by_point["after_commit_before_checkpoint"]
    assert after_commit["needs_checkpoint_rebuild"] is True
    assert after_commit["checkpoint_exists"] is False

    after_checkpoint = by_point["after_checkpoint_before_terminal"]
    assert after_checkpoint["needs_terminal_rewrite"] is True
    assert after_checkpoint["checkpoint_exists"] is True
