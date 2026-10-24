# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "vibe_coding_preflight.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "vibe_coding_preflight",
        SCRIPT_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["vibe_coding_preflight"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run_preflight(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_vibe_coding_preflight_lists_default_backend_and_frontend_checks() -> None:
    result = _run_preflight("--list")

    assert result.returncode == 0
    assert "Vibe coding preflight plan" in result.stdout
    assert "Runtime evaluator golden fixtures" in result.stdout
    assert "category: golden_evaluator" in result.stdout
    assert "Backend runtime, preview, continue, and runner tests" in result.stdout
    assert "Frontend runtime/vibe-coding focused tests" in result.stdout
    assert "test_runtime_events.py" in result.stdout
    assert "runtimeFailureDiagnosis.test.ts" in result.stdout
    assert "npm run build" not in result.stdout


def test_vibe_coding_preflight_lists_full_frontend_build() -> None:
    result = _run_preflight("--frontend-only", "--full", "--list")

    assert result.returncode == 0
    assert "Runtime evaluator golden fixtures" not in result.stdout
    assert "Frontend runtime/vibe-coding focused tests" in result.stdout
    assert "Frontend production build" in result.stdout
    assert "npm run build" in result.stdout


def test_vibe_coding_preflight_lists_backend_only_checks() -> None:
    result = _run_preflight("--backend-only", "--list")

    assert result.returncode == 0
    assert "Runtime evaluator golden fixtures" in result.stdout
    assert "Backend runtime, preview, continue, and runner tests" in result.stdout
    assert "Frontend runtime/vibe-coding focused tests" not in result.stdout


def test_vibe_coding_preflight_writes_stable_report(tmp_path: Path) -> None:
    module = _load_module()
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ended_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    command = module.PreflightCommand(
        name="Synthetic frontend build",
        category="frontend_build",
        cwd=REPO_ROOT / "console",
        argv=["npm", "run", "build"],
        next_action="先看首个 TypeScript/Vite build error。",
    )
    result = module.CommandResult(
        command=command,
        status="failed",
        exit_code=2,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=1000,
        output_tail=["error TS2322: Type mismatch"],
    )
    report = module.build_report(
        results=[result],
        started_at=started_at,
        ended_at=ended_at,
        full=True,
        include_backend=False,
        include_frontend=True,
    )
    report_path = tmp_path / "vibe-coding-report.json"

    module.write_report(report, report_path)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["status"] == "failed"
    assert payload["failed_categories"] == ["frontend_build"]
    assert payload["commands"][0]["next_action"] == "先看首个 TypeScript/Vite build error。"
    assert payload["commands"][0]["output_tail"] == ["error TS2322: Type mismatch"]


def test_vibe_coding_preflight_self_tests_failure_report(tmp_path: Path) -> None:
    report_path = tmp_path / "synthetic-failure-report.json"
    result = _run_preflight(
        "--self-test-failure-report",
        "--report",
        str(report_path),
    )

    assert result.returncode == 1
    assert "Vibe coding preflight failure self-test" in result.stdout
    assert "frontend_build" in result.stdout
    assert "next:" in result.stdout
    assert f"Report written: {report_path}" in result.stdout

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failed_categories"] == ["frontend_build"]
    assert payload["full"] is True
    assert payload["include_backend"] is False
    assert payload["include_frontend"] is True
    assert payload["commands"][0]["exit_code"] == 2
    assert payload["commands"][0]["category"] == "frontend_build"
    assert "TS2322" in payload["commands"][0]["output_tail"][0]
