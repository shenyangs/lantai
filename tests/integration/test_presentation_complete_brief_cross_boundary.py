# -*- coding: utf-8 -*-
"""SRC-02: the public tool carries the complete brief across boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_complete_brief_worker.py"
)
TITLE = "Q4取舍"
AUDIENCE = "2026年10月董事会；非技术决策者；会前独立阅读"
FULL_BRIEF = (
    "会议目的：在8分钟内决定是否把Q4资源从功能扩张转向可靠性交付。\n"
    "受众条件：2026年10月董事会，成员为非技术决策者，并会在会前独立阅读。\n"
    "来源边界：只使用已核验的内部交付记录；没有证据时明确留白。\n"
    "主判断：先保住可恢复、可观察的交付，再讨论功能扩张。\n"
    "使用方式：会前阅读形成问题，会上只讨论资源取舍。\n"
    "硬约束：不得编造因果；不得把草稿写成正式通过；保留‘8分钟’与‘会前独立阅读’。"
)
NARRATIVE_ARC = "先给资源取舍结论，再说明边界与不可牺牲项。"
NOTE_MARKERS = [
    "8分钟",
    "非技术决策者",
    "会前独立阅读",
    "不得编造因果",
    "不得把草稿写成正式通过",
]


def _environment(workspace: Path) -> dict[str, str]:
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
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_complete_brief_reaches_design_storyboard_and_notes_across_processes(
    tmp_path: Path,
) -> None:
    result = _run_worker(tmp_path / "src02")

    assert result["matrix_id"] == "SRC-02"
    assert result["minimum_evidence"] == "cross_boundary"
    child_pids = result["child_pids"]
    assert len(set(child_pids)) == 3
    assert int(result["orchestrator_pid"]) not in child_pids
    assert os.getpid() not in child_pids

    prepare = result["phases"]["prepare"]
    invoke = result["phases"]["invoke"]
    observe = result["phases"]["observe"]

    assert prepare["title"] == TITLE
    assert prepare["full_brief"] == FULL_BRIEF
    assert prepare["full_brief"] != prepare["title"]
    assert prepare["audience"] == AUDIENCE
    assert prepare["narrative_arc"] == NARRATIVE_ARC

    assert invoke["response_is_error"] is True
    assert invoke["response_error_code"] == "presentation_generation_failed"
    assert invoke["controlled_stop_observed"] is True
    assert invoke["design_capture"]["request_user_request"] == FULL_BRIEF
    assert invoke["design_capture"]["brief_objective"] == FULL_BRIEF
    assert invoke["design_capture"]["request_audience"] == AUDIENCE
    assert invoke["design_capture"]["brief_audience"] == AUDIENCE
    assert invoke["design_capture"]["narrative_arc"] == NARRATIVE_ARC
    assert invoke["design_capture"]["title"] == TITLE
    assert invoke["design_capture"]["request_user_request"] != TITLE

    assert observe["job_count"] == 1
    assert observe["request_user_request"] == FULL_BRIEF
    assert observe["request_user_request"] != TITLE
    assert observe["request_audience"] == AUDIENCE
    assert observe["brief_objective"] == FULL_BRIEF
    assert observe["brief_audience"] == AUDIENCE
    assert observe["plan_narrative_arc"] == NARRATIVE_ARC
    assert observe["plan_slide_count"] == 2
    assert observe["authored_slide_count"] == 2
    assert observe["intent_ids_match"] is True
    assert observe["job_status"] == "failed"
    assert observe["job_stage"] == "failed"
    assert observe["output_exists"] is False
    assert observe["checkpoint_names"] == [
        "authored-deck-ir.json",
        "brief.json",
        "deck-plan.json",
    ]
    for marker in NOTE_MARKERS:
        assert all(marker in note for note in observe["speaker_notes"])

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
