# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "read_vibe_preflight_report.py"


def _run_reader(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_read_vibe_preflight_report_summarizes_passed_report(tmp_path: Path) -> None:
    report_path = tmp_path / "passed-report.json"
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "status": "passed",
            "summary": "All 3 checks passed.",
            "failed_categories": [],
            "commands": [],
        },
    )

    result = _run_reader("--report", str(report_path))

    assert result.returncode == 0
    assert "Status: passed" in result.stdout
    assert "Summary: All 3 checks passed." in result.stdout
    assert "Diagnosis: Preflight is clean: All 3 checks passed." in result.stdout


def test_read_vibe_preflight_report_summarizes_failed_report(tmp_path: Path) -> None:
    report_path = tmp_path / "failed-report.json"
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "status": "failed",
            "summary": "1/4 checks failed.",
            "failed_categories": ["frontend_build"],
            "commands": [
                {
                    "name": "Frontend runtime/vibe-coding focused tests",
                    "category": "frontend_runtime",
                    "status": "passed",
                    "exit_code": 0,
                    "command_text": "npm run test:run",
                    "next_action": "No action.",
                    "output_tail": [],
                },
                {
                    "name": "Frontend production build",
                    "category": "frontend_build",
                    "status": "failed",
                    "exit_code": 2,
                    "command_text": "npm run build",
                    "next_action": "先看首个 TypeScript/Vite build error。",
                    "output_tail": [
                        "vite v6.0.0 building for production...",
                        "src/App.tsx(12,7): error TS2322: Type mismatch",
                        "build failed",
                    ],
                },
            ],
        },
    )

    result = _run_reader("--report", str(report_path))

    assert result.returncode == 1
    assert "Failed categories: frontend_build" in result.stdout
    assert "First failed: [frontend_build] Frontend production build (exit 2)" in result.stdout
    assert "Command: npm run build" in result.stdout
    assert "Next action: 先看首个 TypeScript/Vite build error。" in result.stdout
    assert "src/App.tsx(12,7): error TS2322: Type mismatch" in result.stdout
    assert "Frontend production build failed" in result.stdout


def test_read_vibe_preflight_report_outputs_json(tmp_path: Path) -> None:
    report_path = tmp_path / "failed-report.json"
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "status": "failed",
            "summary": "1/1 checks failed.",
            "failed_categories": ["backend_runtime"],
            "commands": [
                {
                    "name": "Backend runtime, preview, continue, and runner tests",
                    "category": "backend_runtime",
                    "status": "failed",
                    "exit_code": 1,
                    "command_text": "python -m pytest tests/unit/app/test_runtime_events.py",
                    "next_action": "先看首个 pytest failure。",
                    "output_tail": ["E   AssertionError: expected event"],
                },
            ],
        },
    )

    result = _run_reader("--report", str(report_path), "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["failed_categories"] == ["backend_runtime"]
    assert payload["first_failed_exit_code"] == 1
    assert payload["key_error_lines"] == ["E   AssertionError: expected event"]


def test_read_vibe_preflight_report_handles_missing_report(tmp_path: Path) -> None:
    report_path = tmp_path / "missing-report.json"

    result = _run_reader("--report", str(report_path))

    assert result.returncode == 2
    assert "Status: missing" in result.stdout
    assert "Report file not found" in result.stdout
    assert "Run python scripts/vibe_coding_preflight.py" in result.stdout


def test_read_vibe_preflight_report_handles_bad_json(tmp_path: Path) -> None:
    report_path = tmp_path / "bad-report.json"
    report_path.write_text("{not valid json", encoding="utf-8")

    result = _run_reader("--report", str(report_path))

    assert result.returncode == 2
    assert "Status: invalid" in result.stdout
    assert "Report JSON is invalid" in result.stdout
    assert "Error:" in result.stdout
