# -*- coding: utf-8 -*-
"""EDIT-05: an approved page split stays synchronized across boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lantai.app.office_conversion import (
    OfficeConversionUnavailable,
    find_soffice,
)
from lantai.presentation.pptx_compiler import NativePptxCompilerUnavailable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_split_mapping_worker.py"
)


def _environment(workspace: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["LANTAI_WORKING_DIR"] = str(workspace / "profile")
    environment["LANTAI_SECRET_DIR"] = str(workspace / "profile.secret")
    environment.setdefault(
        "LANTAI_NODE_MODULES",
        str(PROJECT_ROOT / "console" / "node_modules"),
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
    try:
        find_soffice()
    except OfficeConversionUnavailable:
        pytest.skip("LibreOffice is not installed in this test environment")
    if not (PROJECT_ROOT / "console" / "node_modules" / "pptxgenjs").exists():
        pytest.skip("the repository PPTX runtime is not installed")
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
        timeout=300,
        check=False,
    )
    if "NativePptxCompilerUnavailable" in completed.stderr:
        raise NativePptxCompilerUnavailable(completed.stderr)
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_approved_split_keeps_plan_notes_sources_and_page_count_synchronized(
    tmp_path: Path,
) -> None:
    result = _run_worker(tmp_path / "edit05")

    assert result["matrix_id"] == "EDIT-05"
    assert result["minimum_evidence"] == "cross_boundary"
    child_pids = result["child_pids"]
    assert len(set(child_pids)) == 3
    assert int(result["orchestrator_pid"]) not in child_pids
    assert os.getpid() not in child_pids

    prepare = result["phases"]["prepare"]
    split = result["phases"]["split"]
    observe = result["phases"]["observe"]

    assert prepare["page_count"] == 2
    assert split["unapproved_split_rejected"] is True
    assert "not allowed" in split["rejection"]
    assert split["actions"] == [
        "slide-judgment: split_slide — 用户批准把判断与证据拆成两页"
    ]
    assert split["deck_slide_ids"] == [
        "slide-judgment",
        "slide-evidence",
        "slide-action",
    ]
    assert split["plan_slide_ids"] == [
        "plan-judgment",
        "plan-evidence",
        "plan-action",
    ]
    assert split["intent_ids"] == [
        "intent-judgment",
        "intent-evidence",
        "intent-action",
    ]
    assert split["deck_page_count"] == 3
    assert split["plan_page_count"] == 3
    assert split["compiled_page_count"] == 3
    assert split["editable_object_count"] >= 6

    assert observe["deck_slide_ids"] == split["deck_slide_ids"]
    assert observe["plan_slide_ids"] == split["plan_slide_ids"]
    assert observe["intent_ids"] == split["intent_ids"]
    assert observe["plan_orders"] == [1, 2, 3]
    assert observe["deck_page_count"] == 3
    assert observe["plan_page_count"] == 3
    assert observe["pptx_page_count"] == 3
    assert observe["pdf_page_count"] == 3
    assert observe["rendered_page_count"] == 3
    assert len(observe["rendered_page_sha256"]) == 3
    assert observe["source_refs_after"] == observe["source_refs_before"]
    assert observe["required_facts_after"] == observe["required_facts_before"]
    assert observe["source_fact_ids_after"] == observe["source_fact_ids_before"]
    assert observe["notes_are_distinct"] is True
    assert observe["unchanged_slide_preserved"] is True
    assert observe["has_slide_id_markers"] is True
    assert observe["has_notes_parts"] is True
    assert "转化率达到 95%" in observe["slide_texts"][0]
    assert "样本 240 份" in observe["slide_texts"][1]
    assert "批准下一阶段验证" in observe["slide_texts"][2]
    assert "EDIT05-NOTE-JUDGMENT" in observe["slide_notes"][0]
    assert "EDIT05-NOTE-EVIDENCE" in observe["slide_notes"][1]
    assert "EDIT05-NOTE-ACTION" in observe["slide_notes"][2]
    assert observe["pptx_sha256"] == split["pptx_sha256"]
    assert observe["pdf_sha256"] == observe["pdf_converter_sha256"]

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
