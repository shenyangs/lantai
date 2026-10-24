# -*- coding: utf-8 -*-
"""RUN-08: a page-local design failure must not rewrite correct pages."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_local_repair_worker.py"
)
TARGET_SLIDE_ID = "run08-slide-07"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    pythonpath = [str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)]
    if existing_pythonpath:
        pythonpath.append(existing_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return environment


def _run_worker(*arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    completed = subprocess.run(
        [sys.executable, str(WORKER_SCRIPT), *arguments],
        cwd=PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    return completed, json.loads(completed.stdout.strip().splitlines()[-1])


def test_only_seventh_slide_is_repaired_and_rechecked_across_processes(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "run08-state"
    _, composed = _run_worker(
        "--mode",
        "compose",
        "--state-root",
        str(state_root),
    )
    _, observed = _run_worker(
        "--mode",
        "observe",
        "--state-root",
        str(state_root),
        "--job-id",
        composed["job_id"],
    )

    assert composed["status"] == "completed"
    assert composed["report_passed"] is True
    assert composed["repair_rounds"] == 1
    assert composed["slide_count"] == 19
    assert composed["compose_pid"] != os.getpid()
    assert observed["observer_pid"] != os.getpid()
    assert observed["observer_pid"] != composed["compose_pid"]
    assert observed["job_dir_count"] == 1
    assert observed["manifest_count"] == 1
    assert observed["status"] == "completed"
    assert observed["final_stage"] == "completed"
    assert observed["round_zero_passed"] is False
    assert observed["round_one_passed"] is True
    assert observed["round_zero_error_slide_ids"] == [TARGET_SLIDE_ID]
    assert observed["changed_deck_slide_ids"] == [TARGET_SLIDE_ID]
    assert observed["unchanged_deck_slide_count"] == 18
    assert observed["final_checkpoint_matches_repair"] is True
    assert observed["changed_pptx_pages"] == ["slide-07"]
    assert observed["unchanged_pptx_page_count"] == 18
    assert observed["second_round_uncached_slide_ids"] == [TARGET_SLIDE_ID]
    assert len(observed["second_round_cached_slide_ids"]) == 18
    assert TARGET_SLIDE_ID not in observed["second_round_cached_slide_ids"]
    assert observed["second_round_deck_cache_hit"] is False
    assert observed["second_round_critic_calls"] == [
        {
            "render_round": "round-1",
            "rendered_sha256": observed["second_round_critic_calls"][0][
                "rendered_sha256"
            ],
            "scope": "slide",
            "slide_id": TARGET_SLIDE_ID,
        },
        {
            "render_round": "round-1",
            "scope": "deck",
            "slide_count": 19,
        },
    ]
    assert observed["critic_call_count"] == 22
    assert observed["designer_calls"] == [
        {
            "allowed_actions": [
                "move_element",
                "resize_element",
                "change_visual",
                "regenerate_image",
                "redraw_diagram",
                "change_chart",
                "recrop_image",
                "replace_layout",
                "redesign_slide",
            ],
            "issue_codes": ["vision.hierarchy"],
            "slide_id": TARGET_SLIDE_ID,
        },
    ]
    assert observed["output_exists"] is True
    assert observed["output_sha256"] == observed["final_candidate_sha256"]
    assert observed["final_pptx_slide_count"] == 19
    assert observed["final_native_shape_count"] >= 38
    assert observed["quality_metric_rounds"] == [0, 1]

    print(json.dumps(observed, ensure_ascii=False, sort_keys=True))
