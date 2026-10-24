# -*- coding: utf-8 -*-
"""QA-06: replacing one shared theme font invalidates every visual review."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_shared_font_worker.py"
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


def test_shared_theme_font_change_rechecks_every_page_and_deck(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "qa06"
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
        "replace-font",
        "--state-root",
        str(state_root),
        "--job-id",
        str(seeded["job_id"]),
    )

    expected_slides = [f"qa06-slide-{index:02d}" for index in range(1, 6)]
    expected_scopes = sorted([*expected_slides, "deck"])
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

    assert modified["deck_ir_unchanged"] is True
    assert modified["theme_body_font_before"] == "Noto Sans SC"
    assert modified["theme_body_font_after"] == "Source Han Sans SC"
    assert modified["font_manifest_before"] == ["Noto Sans SC"]
    assert modified["font_manifest_after"] == [
        "Noto Sans SC",
        "Source Han Sans SC",
    ]
    assert modified["changed_slide_xml"] == []
    assert modified["changed_render_indexes"] == []
    assert modified["slide_calls"] == expected_slides
    assert modified["deck_calls"] == 1
    assert modified["cached_slide_ids"] == []
    assert modified["dirty_slide_ids"] == expected_slides
    assert modified["deck_cache_hit"] is False
    assert modified["changed_fingerprint_scopes"] == expected_scopes
    assert modified["changed_checkpoints"] == modified[
        "expected_changed_checkpoints"
    ]
    assert modified["all_evidence_rebound_to_current_render"] is True
    assert all(modified["rebound_matches"].values())
    assert modified["report_passed"] is True
    assert modified["rendered_slide_count"] == 5
    assert modified["visual_evidence_count"] == 6
    assert modified["cost_used_units"] == 12
    assert modified["job_dir_count"] == 1
    assert modified["manifest_count"] == 1

    print(
        json.dumps(
            {
                "matrix_id": "QA-06",
                "minimum_evidence": "cross_boundary",
                "processes": 2,
                "deck_ir_unchanged": modified["deck_ir_unchanged"],
                "changed_render_indexes": modified["changed_render_indexes"],
                "font_before": modified["theme_body_font_before"],
                "font_after": modified["theme_body_font_after"],
                "rechecked_slide_ids": modified["slide_calls"],
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
