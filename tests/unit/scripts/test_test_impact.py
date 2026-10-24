"""Exercise affected selection and safe fallbacks with real Git changes."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def repository(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(
        ROOT / "scripts/test_impact.py", tmp_path / "scripts/test_impact.py"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def plan(root, *args):
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/test_impact.py"),
            "--dry-run",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_untracked_python_test_selects_its_neighbours_and_contracts(tmp_path):
    root = repository(tmp_path)
    folder = root / "tests/unit/runtime"
    folder.mkdir(parents=True)
    (folder / "test_new.py").write_text("def test_new(): pass")
    result = plan(root)
    assert result["selection"] == "affected"
    assert result["python"] == [
        "tests/unit/runtime",
        "tests/contract",
        "tests/integration",
    ]
    assert "tests/unit/runtime/test_new.py" in result["changed"]
    assert any(
        "check_foundation_experience_gate.py" in " ".join(c["argv"])
        for c in result["commands"]
    )


def test_unknown_source_falls_back_to_full(tmp_path):
    root = repository(tmp_path)
    (root / "new-config.toml").write_text("enabled = true")
    result = plan(root)
    assert result["selection"] == "full"
    assert result["python"] == ["tests"]
    assert result["frontend"] == "full"


def test_invalid_base_fails_instead_of_reporting_no_changes(tmp_path):
    root = repository(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/test_impact.py"),
            "--base",
            "missing-ref",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_clean_and_docs_only_plans_do_not_claim_full_test_pass(tmp_path):
    root = repository(tmp_path)
    assert plan(root)["selection"] == "no_changes"
    (root / "docs").mkdir()
    (root / "docs/note.md").write_text("notes")
    result = plan(root)
    assert result["status"] == "planned"
    assert result["scope"] == "local-feedback-not-release-evidence"
    assert len(result["commands"]) == 1


def test_staged_rename_and_deleted_test_force_full_selection(tmp_path):
    root = repository(tmp_path)
    folder = root / "tests/unit"
    folder.mkdir(parents=True)
    original = folder / "test_original.py"
    original.write_text("def test_ok(): pass")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "test fixture",
        ],
        cwd=root,
        check=True,
    )
    original.rename(folder / "test_renamed.py")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    result = plan(root)
    assert result["selection"] == "full"
    assert "tests/unit/test_original.py" in result["changed"]
    assert "tests/unit/test_renamed.py" in result["changed"]


def test_reviewed_source_uses_related_without_allowing_empty_success(tmp_path):
    root = repository(tmp_path)
    folder = root / "console/src/utils"
    folder.mkdir(parents=True)
    (folder / "latestRequest.ts").write_text("export const version = 1")
    result = plan(root)
    assert result["selection"] == "affected"
    command = next(
        c["argv"] for c in result["commands"] if "related" in c["argv"]
    )
    assert "src/utils/latestRequest.ts" in command
    assert "--passWithNoTests" not in command
