# -*- coding: utf-8 -*-
"""QA-01: three reviewed pages cannot verify a 19-page native deck."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_full_page_coverage_worker.py"
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


def _run_worker(
    workspace: Path,
    *arguments: str,
) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(WORKER_SCRIPT), *arguments],
        cwd=PROJECT_ROOT,
        env=_environment(workspace),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_three_reviewed_pages_cannot_verify_nineteen_page_deck(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "qa01"
    state_root = workspace / "state"
    executed = _run_worker(
        workspace,
        "--mode",
        "execute",
        "--state-root",
        str(state_root),
    )
    observed = _run_worker(
        workspace,
        "--mode",
        "observe",
        "--state-root",
        str(state_root),
        "--job-id",
        str(executed["job_id"]),
    )

    assert int(executed["execute_pid"]) != os.getpid()
    assert int(observed["observer_pid"]) != os.getpid()
    assert executed["execute_pid"] != observed["observer_pid"]
    assert executed["slide_count"] == 19
    assert executed["rendered_image_count"] == 3
    assert executed["critic_calls"] == 0
    assert executed["report_passed"] is False
    assert executed["delivery_status"] == "failed"
    assert executed["render_verified"] is False
    assert executed["visual_verified"] is False
    assert executed["render_evidence"] == "failed"
    assert executed["visual_evidence"] == "failed"
    assert "render evidence" in executed["blocking_gaps"]
    assert "visual evidence" in executed["blocking_gaps"]
    assert "vision.render_set_incomplete" in executed["issue_codes"]
    assert executed["actual_delivery_verified"] is False
    assert executed["authoritative_output_exists"] is False

    assert observed["job_dir_count"] == 1
    assert observed["manifest_count"] == 1
    assert observed["job_status"] == "pending"
    assert observed["completed_event_count"] == 0
    assert observed["slide_count"] == 19
    assert observed["rendered_image_count"] == 3
    assert observed["persisted_render_evidence_count"] == 3
    assert observed["actual_delivery_verified"] is False
    assert observed["forged_green_flags_delivery_verified"] is False
    assert observed["candidate_exists"] is True
    assert observed["candidate_pptx_slide_count"] == 19
    assert observed["candidate_native_shape_count"] >= 38
    assert observed["authoritative_output_exists"] is False

    print(
        json.dumps(
            {
                "matrix_id": "QA-01",
                "minimum_evidence": "cross_boundary",
                "slide_count": observed["slide_count"],
                "reviewed_page_count": observed["rendered_image_count"],
                "critic_calls": executed["critic_calls"],
                "actual_delivery_verified": observed[
                    "actual_delivery_verified"
                ],
                "forged_green_flags_delivery_verified": observed[
                    "forged_green_flags_delivery_verified"
                ],
                "authoritative_output_exists": observed[
                    "authoritative_output_exists"
                ],
                "native_shape_count": observed["candidate_native_shape_count"],
                "processes": 2,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
