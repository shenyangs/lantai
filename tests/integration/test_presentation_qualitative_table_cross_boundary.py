# -*- coding: utf-8 -*-
"""DSG-02: a qualitative matrix remains one editable native table."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lantai.app.office_conversion import OfficeConversionUnavailable, find_soffice
from lantai.presentation.pptx_compiler import NativePptxCompilerUnavailable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_qualitative_table_worker.py"
)
EXPECTED_HEADERS = ["维度", "截图能确认的", "澜台已确认的", "融合裁决"]
EXPECTED_ROWS = [
    ["问题组织", "输入被展开为讨论题", "合同有目标和验收", "放第一层"],
    ["分工可见", "三个视角直接呈现", "成员有角色与依赖", "具体问题在前"],
    ["交叉讨论", "截图不足以确认", "有第二轮审阅", "保留真执行"],
    ["结论交付", "截屏未显示", "子结果与交付区分", "最终答案直接可读"],
    ["过程可控", "单张截图无法验证", "有停止暂停恢复", "不为换外观破坏保护"],
]


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


def test_qualitative_matrix_stays_one_native_table_across_processes(
    tmp_path: Path,
) -> None:
    result = _run_worker(tmp_path / "dsg02")

    assert result["matrix_id"] == "DSG-02"
    assert result["minimum_evidence"] == "cross_boundary"
    child_pids = result["child_pids"]
    assert len(set(child_pids)) == 3
    assert int(result["orchestrator_pid"]) not in child_pids
    assert os.getpid() not in child_pids

    prepare = result["phases"]["prepare"]
    compose = result["phases"]["compose"]
    observe = result["phases"]["observe"]

    assert prepare["headers"] == EXPECTED_HEADERS
    assert prepare["rows"] == EXPECTED_ROWS
    assert prepare["object_ids"] == ["comparison-table-title", "comparison-table"]
    assert compose["native_table_count"] == 1
    assert compose["repeated_card_ids"] == []
    assert compose["comparison_relationships"] == ["comparison"]
    assert compose["compiled_slide_count"] == 1
    assert compose["editable_object_count"] >= 2
    assert compose["degradations"] == []

    assert observe["deck_page_count"] == 1
    assert observe["pptx_page_count"] == 1
    assert observe["pdf_page_count"] == 1
    assert observe["rendered_page_count"] == 1
    assert observe["native_table_shape_count"] == 1
    assert observe["native_table_shape_name"] == "comparison-table"
    assert observe["serialized_table_count"] == 1
    assert observe["cell_matrix"] == [EXPECTED_HEADERS, *EXPECTED_ROWS]
    assert observe["header_occurrences"] == {
        "截图能确认的": 1,
        "澜台已确认的": 1,
        "融合裁决": 1,
    }
    assert observe["repeated_shape_names"] == []
    assert observe["source_fact_ids_before"] == observe["source_fact_ids_after"]
    assert "DSG02-NOTE" in observe["speaker_notes"]
    assert observe["pptx_sha256"] == compose["pptx_sha256"]
    assert observe["pdf_sha256"] == observe["pdf_converter_sha256"]
    assert len(observe["rendered_page_sha256"]) == 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
