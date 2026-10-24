# -*- coding: utf-8 -*-
"""EDIT-06: PDF derivation stays bound to the requested PPTX revision."""

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


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT
    / "tests"
    / "support"
    / "presentation_pdf_derivation_worker.py"
)


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
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_pdf_derivation_uses_requested_immutable_pptx_version(
    tmp_path: Path,
) -> None:
    result = _run_worker(tmp_path / "edit06")

    assert result["matrix_id"] == "EDIT-06"
    assert result["minimum_evidence"] == "cross_boundary"
    child_pids = result["child_pids"]
    assert len(set(child_pids)) == 4
    assert int(result["orchestrator_pid"]) not in child_pids
    assert os.getpid() not in child_pids

    prepare = result["phases"]["prepare"]
    advance = result["phases"]["advance"]
    derive = result["phases"]["derive"]
    observe = result["phases"]["observe"]

    assert prepare["version"] == 1
    assert prepare["page_count"] == 2
    assert prepare["contains_v1_token"] is True
    assert advance["version"] == 2
    assert advance["parent_work_id"] == prepare["work_id"]
    assert advance["parent_digest"] == advance["expected_parent_digest"]
    assert advance["page_count"] == 3
    assert advance["contains_v2_token"] is True
    assert advance["source_digest"] != prepare["source_digest"]

    assert derive["requested_work_id"] == prepare["work_id"]
    assert derive["current_v2_work_id"] == advance["work_id"]
    assert derive["idempotent_reuse"] is True
    assert derive["derived_work_id"] == derive["repeated_work_id"]
    assert derive["work_count_before"] == 2
    assert derive["work_count_after"] == 3
    assert derive["presentation_work_count_after"] == 2
    assert derive["pdf_work_count_after"] == 1
    assert derive["derived_kind"] == "pdf"
    assert derive["derived_version"] == 1
    assert derive["derived_parent_work_id"] is None
    assert derive["derived_lineage_root_id"] == derive["derived_work_id"]
    relation = derive["relation"]
    assert relation["operation"] == "office_conversion"
    assert relation["target_format"] == "pdf"
    assert relation["source_work_id"] == prepare["work_id"]
    assert relation["source_artifact_id"] == prepare["artifact_id"]
    assert relation["source_version"] == 1
    assert relation["source_digest"] == prepare["source_digest"]
    assert relation["source_digest"] != advance["source_digest"]
    assert relation["output_digest"] == derive["derived_digest"]
    assert relation["converter_output_digest"] == derive["derived_sha256"]
    assert relation["converter"] == "libreoffice"

    assert observe["v1_page_count"] == 2
    assert observe["v2_page_count"] == 3
    assert observe["pdf_page_count"] == 2
    assert observe["rendered_page_count"] == 2
    assert len(observe["rendered_page_sha256"]) == 2
    assert observe["pdf_signature_valid"] is True
    assert observe["pdf_contains_v1_token"] is True
    assert observe["pdf_excludes_v2_token"] is True
    assert observe["verified_rendered_text"] is True
    assert observe["v1_sha256"] == prepare["pptx_sha256"]
    assert observe["v2_sha256"] == advance["pptx_sha256"]
    assert observe["pdf_sha256"] == derive["derived_sha256"]
    assert observe["relation"] == relation
    assert set(observe["presentation_work_ids"]) == {
        prepare["work_id"],
        advance["work_id"],
    }
    assert observe["pdf_work_ids"] == [derive["derived_work_id"]]
    assert observe["works_index_count"] == 3
    assert observe["artifact_manifest_count"] == 3
    assert observe["derived_is_revision"] is False

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
