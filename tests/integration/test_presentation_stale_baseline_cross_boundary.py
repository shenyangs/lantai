# -*- coding: utf-8 -*-
"""EDIT-04: a v1-based edit cannot overwrite v2 across process boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT
    / "tests"
    / "support"
    / "presentation_stale_baseline_worker.py"
)


def _environment(workspace: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["LANTAI_WORKING_DIR"] = str(workspace / "profile")
    environment["LANTAI_SECRET_DIR"] = str(workspace / "profile.secret")
    environment["LANTAI_NODE_MODULES"] = str(
        PROJECT_ROOT / "console" / "node_modules"
    )
    environment["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (
            str(PROJECT_ROOT / "src"),
            str(PROJECT_ROOT),
            environment.get("PYTHONPATH", ""),
        )
        if part
    )
    return environment


def _run_worker(workspace: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(WORKER_SCRIPT),
            "--state-root",
            str(workspace / "state"),
        ],
        cwd=PROJECT_ROOT,
        env=_environment(workspace),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_stale_v1_edit_cannot_overwrite_authoritative_v2(tmp_path: Path) -> None:
    result = _run_worker(tmp_path / "edit04")

    assert result["matrix_id"] == "EDIT-04"
    assert result["minimum_evidence"] == "cross_boundary"
    assert result["ledger_count"] == 1
    child_pids = result["child_pids"]
    assert len(set(child_pids)) == 3
    assert int(result["orchestrator_pid"]) not in child_pids
    assert os.getpid() not in child_pids

    prepare = result["phases"]["prepare"]
    advance = result["phases"]["advance"]
    stale = result["phases"]["stale_attempt"]
    assert prepare["history_count"] == 1
    assert prepare["output_title"] == "权威版本 v1"
    assert prepare["stale_candidate_title"] == "基于 v1 准备但被拒绝的修改"
    assert advance["history_count"] == 2
    assert advance["parent_version_id"] == prepare["version_id"]
    assert advance["output_title"] == "权威版本 v2"
    assert advance["output_sha256"] != prepare["output_sha256"]

    assert "requested base version" in stale["base_error"]
    assert "version parent changed" in stale["parent_error"]
    assert stale["authoritative_version_before"] == advance["version_id"]
    assert stale["authoritative_version_after"] == advance["version_id"]
    assert stale["authoritative_sha256_before"] == advance["output_sha256"]
    assert stale["authoritative_sha256_after"] == advance["output_sha256"]
    assert stale["authoritative_bytes_unchanged"] is True
    assert stale["authoritative_title"] == "权威版本 v2"
    assert stale["stale_candidate_sha256"] == prepare["stale_candidate_sha256"]
    assert stale["stale_candidate_title"] == prepare["stale_candidate_title"]
    assert stale["history_count_before"] == 2
    assert stale["history_count_after"] == 2
    assert stale["artifact_bundles_before"] == stale["artifact_bundles_after"]
    assert len(stale["artifact_bundles_after"]) == 2
    assert stale["staging_entries"] == []

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
