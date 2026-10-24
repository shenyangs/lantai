# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "runtime_evaluator_preflight.py"


def _run_preflight(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_runtime_evaluator_preflight_passes_default_fixtures() -> None:
    result = _run_preflight()

    assert result.returncode == 0
    assert "Runtime evaluator golden preflight" in result.stdout
    assert "Preflight passed: all 9 golden fixtures matched." in result.stdout


def test_runtime_evaluator_preflight_outputs_json() -> None:
    result = _run_preflight("--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["suite_id"] == "runtime_evaluator_golden"
    assert payload["total"] == 9
    assert payload["failed"] == 0
    assert payload["summary"]["conclusion"] == "pass"


def test_runtime_evaluator_preflight_fails_on_fixture_drift(tmp_path: Path) -> None:
    fixture_path = tmp_path / "bad_cases.json"
    fixture_path.write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {
                        "id": "bad-build",
                        "title": "Bad build expectation",
                        "kind": "runtime_event",
                        "input": {
                            "event": {
                                "type": "tool.completed",
                                "status": "failed",
                                "summary": "构建失败",
                                "payload": {
                                    "command": "npm run build",
                                    "stderr": "error TS2322: Type mismatch",
                                    "exit_code": 2,
                                },
                            },
                        },
                        "expect": {
                            "conclusion": "pass",
                            "category": "test",
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run_preflight("--fixtures", str(fixture_path))

    assert result.returncode == 1
    assert "[FAIL] bad-build" in result.stdout
    assert "conclusion expected 'pass'" in result.stdout
