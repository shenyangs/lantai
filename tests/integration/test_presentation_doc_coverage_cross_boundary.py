# -*- coding: utf-8 -*-
"""SRC-01: real binary DOC coverage and fail-closed conversion boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lantai.app.office_conversion import OfficeConversionUnavailable, find_soffice


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_doc_coverage_worker.py"
)
EXPECTED_PARAGRAPHS = [
    "SRC-01 旧稿事实边界",
    "正文事实：第三季度客户续约率为 92%。",
    "结论：先修复交付可靠性，再扩张作者策略。",
]
EXPECTED_TABLE = [
    ["指标", "本期", "口径"],
    ["续约率", "92%", "已签约客户"],
    ["样本量", "240家", "截至9月30日"],
]
EXPECTED_LINES = [
    EXPECTED_PARAGRAPHS[0],
    EXPECTED_PARAGRAPHS[1],
    *(" | ".join(row) for row in EXPECTED_TABLE),
    EXPECTED_PARAGRAPHS[2],
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
    try:
        find_soffice()
    except OfficeConversionUnavailable:
        pytest.skip("LibreOffice is not installed in this test environment")
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
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_real_legacy_doc_preserves_body_and_table_and_rejects_corruption(
    tmp_path: Path,
) -> None:
    result = _run_worker(tmp_path / "src01")

    assert result["matrix_id"] == "SRC-01"
    assert result["minimum_evidence"] == "cross_boundary"
    child_pids = result["child_pids"]
    assert len(set(child_pids)) == 3
    assert int(result["orchestrator_pid"]) not in child_pids
    assert os.getpid() not in child_pids

    prepare = result["phases"]["prepare"]
    extract = result["phases"]["extract"]
    observe = result["phases"]["observe"]

    assert prepare["fixture_class"] == (
        "synthetic_content_real_binary_word_97_2003"
    )
    assert prepare["ole_signature"] == "d0cf11e0a1b11ae1"
    assert prepare["ole_is_valid"] is True
    assert "WordDocument" in prepare["ole_streams"]
    assert prepare["expected_paragraphs"] == EXPECTED_PARAGRAPHS
    assert prepare["expected_table"] == EXPECTED_TABLE

    assert extract["source_section_count"] == 1
    assert extract["source_refs"] == ["source-src01-real-doc"]
    assert extract["extracted_lines"] == EXPECTED_LINES
    assert all(count == 1 for count in extract["line_occurrences"].values())
    coverage = extract["coverage"]
    assert coverage["method"] == "libreoffice_doc_to_docx_ooxml"
    assert coverage["status"] == "converted_doc_unverified"
    assert coverage["source_sha256"] == prepare["legacy_doc_sha256"].removeprefix(
        "sha256:"
    )
    assert len(coverage["converted_sha256"]) == 64
    assert coverage["extracted_chars"] > 0
    assert coverage["limitations"]

    assert observe["observed_paragraphs"] == EXPECTED_PARAGRAPHS
    assert observe["observed_table"] == EXPECTED_TABLE
    assert observe["legacy_doc_sha256"] == prepare["legacy_doc_sha256"]
    assert observe["brief_source_sha256"] == coverage["source_sha256"]
    assert observe["observed_docx_sha256"] == observe[
        "observed_docx_converter_sha256"
    ]
    assert observe["observed_pdf_sha256"] == observe[
        "observed_pdf_converter_sha256"
    ]
    assert observe["pdf_page_count"] == 1
    assert observe["rendered_page_count"] == 1
    assert len(observe["rendered_page_sha256"]) == 1
    assert observe["corrupt_doc_rejected"] is True
    assert "not a valid OLE compound document" in observe["corrupt_error"]

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
