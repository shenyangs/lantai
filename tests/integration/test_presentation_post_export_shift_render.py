# -*- coding: utf-8 -*-
"""QA-02: final PPTX pixels reject geometry drift introduced after authoring."""

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
    / "presentation_post_export_shift_worker.py"
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


def test_final_render_rejects_post_export_geometry_shift(tmp_path: Path) -> None:
    result = _run_worker(tmp_path / "qa02")

    assert int(result["pid"]) != os.getpid()
    assert result["matrix_id"] == "QA-02"
    assert result["minimum_evidence"] == "render"
    assert result["compile_engine"] == "native_pptx"
    assert result["editable_object_count"] == 3
    assert result["authored_pptx_sha256"] != result["shifted_pptx_sha256"]
    assert result["serialized_shift"]["old_x_emu"] == round(1.25 * 914400)
    assert result["serialized_shift"]["new_x_emu"] == round(8.75 * 914400)

    authored = result["authored"]
    shifted = result["shifted"]
    assert authored["renderer_id"] == shifted["renderer_id"]
    assert authored["renderer_version"] == shifted["renderer_version"]
    assert authored["render_sha256"] != shifted["render_sha256"]
    assert authored["passed"] is True
    assert authored["delivery_status"] == "passed"
    assert authored["render_evidence"] == "passed"
    assert authored["visual_evidence"] == "passed"
    assert authored["visual_verified"] is True
    assert result["authored_verified_delivery"] is True

    assert shifted["passed"] is False
    assert shifted["delivery_status"] == "failed"
    assert shifted["render_evidence"] == "passed"
    assert shifted["visual_evidence"] == "failed"
    assert shifted["visual_verified"] is False
    assert shifted["issue_codes"] == ["vision.composition"]
    assert shifted["blocking_gaps"] == ["visual evidence"]
    assert result["shifted_verified_delivery"] is False

    observations = result["critic_observations"]
    assert len(observations) == 2
    assert observations[0]["shifted"] is False
    assert observations[1]["shifted"] is True
    assert observations[1]["drift_pixels"]["x"] > 500
    assert observations[0]["rendered_sha256"] == authored["render_sha256"][0]
    assert observations[1]["rendered_sha256"] == shifted["render_sha256"][0]
    assert result["cost_used_units"] == 4
    assert result["job_dir_count"] == 1
    assert result["manifest_count"] == 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
