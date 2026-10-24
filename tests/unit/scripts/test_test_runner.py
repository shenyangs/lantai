"""Exercise the test CLI against disposable, real pytest repositories."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def sandbox(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(
        ROOT / "scripts/run_tests.py", tmp_path / "scripts/run_tests.py"
    )
    for suite in ("unit", "contract", "integration"):
        directory = tmp_path / "tests" / suite
        directory.mkdir(parents=True)
        (directory / f"test_{suite}.py").write_text(
            f"def test_{suite}():\n    assert True\n", encoding="utf-8"
        )
    return tmp_path


def run(root, *args):
    return subprocess.run(
        [sys.executable, str(root / "scripts/run_tests.py"), *args],
        cwd=root,
        text=True,
        capture_output=True,
    )


def test_default_runs_root_unit_contract_and_integration_once(tmp_path):
    result = run(sandbox(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
    assert "collected" not in result.stderr


@pytest.mark.parametrize("flag", ["--integration", "--integrated"])
def test_integration_aliases_run_real_suite(tmp_path, flag):
    result = run(sandbox(tmp_path), flag)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_failure_and_empty_suite_are_not_success(tmp_path):
    root = sandbox(tmp_path)
    test = root / "tests/unit/test_unit.py"
    test.write_text("def test_fail():\n    assert False\n")
    failed = run(root, "-u")
    assert failed.returncode == 1
    assert "PASS" not in failed.stdout
    test.unlink()
    empty = run(root, "-u")
    assert empty.returncode == 5
    assert "PASS" not in empty.stdout


def test_missing_or_escaping_suite_is_an_error(tmp_path):
    root = sandbox(tmp_path)
    for target in ("missing", "../../scripts"):
        result = run(root, "-u", target)
        assert result.returncode != 0
        assert "PASS" not in result.stdout


def test_dry_run_is_auditable_and_does_not_execute(tmp_path):
    root = sandbox(tmp_path)
    (root / "tests/unit/test_unit.py").write_text(
        "raise RuntimeError('executed')"
    )
    result = run(root, "--dry-run")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["status"] == "planned"
    assert len(plan["commands"]) == 1
    assert plan["commands"][0]["argv"][1:3] == ["-m", "pytest"]
    assert plan["commands"][0]["argv"][3:6] == [
        "tests/unit",
        "tests/contract",
        "tests/integration",
    ]


def test_parallel_is_bounded_and_keeps_unreviewed_tests_serial(tmp_path):
    root = sandbox(tmp_path)
    (root / "tests/unit/runtime").mkdir()
    reviewed = "tests/unit/runtime/test_recovery_policy.py"
    (root / reviewed).write_text("def test_safe():\n    assert True\n")
    result = run(root, "-p", "--workers", "2", "--dry-run")
    assert result.returncode == 0, result.stderr
    commands = json.loads(result.stdout)["commands"]
    assert len(commands) == 2
    parallel, serial = [item["argv"] for item in commands]
    assert parallel[parallel.index("-n") + 1] == "2"
    assert reviewed in parallel
    assert "-n" not in serial
    assert f"--ignore={reviewed}" in serial
    assert run(root, "--workers", "99", "--dry-run").returncode != 0


def test_parallel_only_target_does_not_add_empty_serial_stage(tmp_path):
    root = sandbox(tmp_path)
    folder = root / "tests/unit/runtime"
    folder.mkdir()
    (folder / "test_recovery_policy.py").write_text(
        "def test_safe():\n    assert True\n"
    )
    report = root / "report.json"
    result = run(root, "-u", "runtime", "-p", "--report", str(report))
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert data["status"] == "passed"
    assert len(data["commands"]) == 1
    assert data["commands"][0]["duration_seconds"] > 0


def test_parallel_and_serial_coverage_combine_without_lowering_final_gate(
    tmp_path,
):
    root = sandbox(tmp_path)
    folder = root / "tests/unit/runtime"
    folder.mkdir()
    (folder / "test_recovery_policy.py").write_text(
        "def test_safe():\n    assert True\n"
    )
    commands = json.loads(run(root, "-p", "-c", "--dry-run").stdout)[
        "commands"
    ]
    assert "--cov-fail-under=0" in commands[0]["argv"]
    assert "--cov-append" in commands[1]["argv"]
    assert "--cov-fail-under=0" not in commands[1]["argv"]


def test_real_parallel_coverage_includes_both_stages(tmp_path):
    root = sandbox(tmp_path)
    source = root / "src/lantai"
    source.mkdir(parents=True)
    (source / "sample.py").write_text(
        "def first():\n    return 1\ndef second():\n    return 2\n"
    )
    (root / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
    )
    folder = root / "tests/unit/runtime"
    folder.mkdir()
    (folder / "test_recovery_policy.py").write_text(
        "from src.lantai.sample import first\n"
        "def test_first():\n    assert first() == 1\n"
    )
    (root / "tests/unit/test_unit.py").write_text(
        "from src.lantai.sample import second\n"
        "def test_second():\n    assert second() == 2\n"
    )
    result = run(root, "-p", "-c", "--", "--cov-fail-under=100")
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'line-rate="1"' in (root / "coverage.xml").read_text()
