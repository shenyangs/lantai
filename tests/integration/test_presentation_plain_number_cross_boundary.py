# -*- coding: utf-8 -*-
"""RUN-02: plain numeric answers never gain PPT semantics without trusted state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_plain_number_worker.py"
)


def _run_worker(tmp_path: Path, scenario: str) -> dict[str, object]:
    workspace = tmp_path / scenario
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["LANTAI_WORKING_DIR"] = str(workspace / "profile")
    environment["LANTAI_SECRET_DIR"] = str(workspace / "profile.secret")
    environment["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (
            str(PROJECT_ROOT / "src"),
            str(PROJECT_ROOT),
            environment.get("PYTHONPATH", ""),
        )
        if part
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(WORKER_SCRIPT),
            "--workspace",
            str(workspace),
            "--scenario",
            scenario,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_plain_number_one_has_no_ppt_semantics_across_process_boundary(
    tmp_path: Path,
) -> None:
    results = [
        _run_worker(tmp_path, scenario)
        for scenario in ("math", "multiple_choice")
    ]

    assert len({int(result["pid"]) for result in results}) == 2
    assert {result["scenario"] for result in results} == {
        "math",
        "multiple_choice",
    }
    assert all(result["current_user_text"] == "1" for result in results)
    assert all(result["previous_run_persisted"] is True for result in results)
    assert all(
        result["previous_deliverable_contract"] is None for result in results
    )
    assert all(
        result["console_pending_context_projected"] is False
        for result in results
    )
    assert all(
        result["runner_pending_context_present"] is False for result in results
    )
    assert all(result["resolver_registered"] is False for result in results)
    assert all(
        result["resolver_exposed_to_model"] is False for result in results
    )
    assert all(
        result["presentation_tool_exposed_to_model"] is True
        for result in results
    )
    assert all(result["first_request_tool_choice"] == "auto" for result in results)
    assert all(result["model_request_count"] == 1 for result in results)
    assert all(result["tool_calls"] == [] for result in results)
    assert all(result["tool_event_types"] == [] for result in results)
    assert all(
        result["presentation_job_root_exists"] is False for result in results
    )
    assert all(result["pptx_paths"] == [] for result in results)
    assert all(result["message_completed_events"] == 1 for result in results)
    assert all(result["terminal_event"] == "turn.completed" for result in results)

    print(
        json.dumps(
            {
                "matrix_id": "RUN-02",
                "minimum_evidence": "cross_boundary",
                "processes": len(results),
                "scenarios": [result["scenario"] for result in results],
                "current_user_text": "1",
                "trusted_pending_contexts": 0,
                "registered_pending_resolvers": 0,
                "forced_tool_choices": 0,
                "presentation_tool_calls": 0,
                "presentation_jobs_created": 0,
                "pptx_files_created": 0,
                "completed_plain_reply_turns": len(results),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
