# -*- coding: utf-8 -*-
"""QA-05: changing only slide 3 invalidates only its page review plus deck review."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = PROJECT_ROOT / "tests" / "support" / "presentation_dirty_cache_worker.py"


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


def _run_worker(workspace: Path, *arguments: str) -> dict[str, object]:
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


def test_slide_three_text_change_only_invalidates_related_visual_checks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "qa05"
    state_root = workspace / "state"
    seeded = _run_worker(
        workspace,
        "--mode",
        "seed",
        "--state-root",
        str(state_root),
    )
    modified = _run_worker(
        workspace,
        "--mode",
        "modify",
        "--state-root",
        str(state_root),
        "--job-id",
        str(seeded["job_id"]),
    )

    expected_slides = [f"qa05-slide-{index:02d}" for index in range(1, 6)]
    cached_slides = [slide_id for slide_id in expected_slides if slide_id != "qa05-slide-03"]
    assert int(seeded["seed_pid"]) != os.getpid()
    assert int(modified["modify_pid"]) != os.getpid()
    assert seeded["seed_pid"] != modified["modify_pid"]
    assert seeded["job_id"] == modified["job_id"]
    assert seeded["slide_calls"] == expected_slides
    assert seeded["deck_calls"] == 1
    assert seeded["report_passed"] is True
    assert seeded["rendered_slide_count"] == 5
    assert seeded["visual_evidence_count"] == 6
    assert seeded["cost_used_units"] == 6

    assert modified["changed_ir_slide_ids"] == ["qa05-slide-03"]
    assert modified["changed_slide_xml"] == ["slide-03"]
    assert modified["changed_render_indexes"] == [3]
    assert modified["slide_calls"] == ["qa05-slide-03"]
    assert modified["deck_calls"] == 1
    assert modified["cached_slide_ids"] == cached_slides
    assert modified["dirty_slide_ids"] == ["qa05-slide-03"]
    assert modified["deck_cache_hit"] is False
    assert modified["changed_checkpoints"] == modified[
        "expected_changed_checkpoints"
    ]
    assert modified["all_evidence_rebound_to_current_render"] is True
    assert all(modified["rebound_matches"].values())
    assert modified["report_passed"] is True
    assert modified["rendered_slide_count"] == 5
    assert modified["visual_evidence_count"] == 6
    assert modified["cost_used_units"] == 8
    assert modified["job_dir_count"] == 1
    assert modified["manifest_count"] == 1

    print(
        json.dumps(
            {
                "matrix_id": "QA-05",
                "minimum_evidence": "cross_boundary",
                "processes": 2,
                "changed_ir_slide_ids": modified["changed_ir_slide_ids"],
                "changed_slide_xml": modified["changed_slide_xml"],
                "changed_render_indexes": modified["changed_render_indexes"],
                "rechecked_slide_ids": modified["slide_calls"],
                "cached_slide_count": len(modified["cached_slide_ids"]),
                "deck_rechecked": modified["deck_calls"] == 1,
                "evidence_rebound": modified[
                    "all_evidence_rebound_to_current_render"
                ],
                "cost_units_before": seeded["cost_used_units"],
                "cost_units_after": modified["cost_used_units"],
                "jobs": modified["job_dir_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
