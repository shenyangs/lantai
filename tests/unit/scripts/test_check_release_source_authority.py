from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_release_source_authority import audit_release_source


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_only_unique_main_worktree_can_deploy(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Release Gate Test")
    (repository / "README.md").write_text("release\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")

    canonical = audit_release_source(repository, artifact_only=False)
    assert canonical["ok"] is True
    assert canonical["canonicalMain"] is True

    detached = tmp_path / "detached"
    _git(repository, "worktree", "add", "--detach", str(detached), "HEAD")
    refused = audit_release_source(detached, artifact_only=False)
    assert refused["ok"] is False
    assert refused["sourceBranch"] == "DETACHED"
    assert "Downloads deployment" in str(refused["violations"])

    candidate = audit_release_source(detached, artifact_only=True)
    assert candidate["ok"] is True
    assert candidate["canonicalMain"] is False
