"""CI efficiency must retain coverage and reject skipped gates."""

from pathlib import Path
import json
import os
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[3]


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def test_console_build_is_once_per_platform_and_consumers_verify_it():
    config = workflow("tests.yml")
    assert config["env"]["LANTAI_PPTXGENJS_MODULE"] == (
        "${{ github.workspace }}/console/node_modules/pptxgenjs"
    )
    jobs = config["jobs"]
    assert jobs["console-build"]["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
    ]
    assert jobs["approval-gate"]["environment"] == "maintainer-approved"
    for name in ("unit-tests", "integrated-tests", "coverage-report"):
        steps = str(jobs[name]["steps"])
        assert "npm run build" not in steps
        assert "console_test_artifact.py verify" in steps
        assert "check_console_static_fresh.py --sync" in steps
        assert "uv sync --frozen --extra dev" in steps
    assert "tests/contract tests/integration" in str(jobs["integrated-tests"])
    summary = jobs["test-summary"]
    assert "coverage-report" in summary["needs"]
    code = summary["steps"][0]["run"].split("\n", 1)[1].rsplit("PY", 1)[0]
    for status in ("success", "failure", "cancelled", "skipped"):
        results = {name: {"result": "success"} for name in summary["needs"]}
        results["coverage-report"]["result"] = status
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            env={**os.environ, "NEEDS_JSON": json.dumps(results)},
        )
        assert (result.returncode == 0) == (status == "success")


def test_fast_workflow_keeps_hard_gates_without_duplicate_full_run():
    text = (ROOT / ".github/workflows/unit-tests.yml").read_text()
    assert "informational" not in text
    assert "pytest tests/unit " not in text
    for gate in (
        "runtime_evaluator_preflight.py",
        "codex_harness_quality.py",
        "test_mcp_lifecycle_recovery.py",
        "tests/unit/agents/memory",
    ):
        assert gate in text


def test_frontend_executes_tests_once_with_blocking_failure_status():
    jobs = workflow("frontend-tests.yml")["jobs"]
    steps = jobs["vitest"]["steps"]
    runs = [s for s in steps if "test:coverage" in s.get("run", "")]
    assert len(runs) == 1
    assert not runs[0].get("continue-on-error")
    assert not any(s.get("run") == "npm run test:run" for s in steps)
