from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.worktree_guard import acquire, check, release, renew


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    return tmp_path


def test_lease_is_exclusive_and_requires_the_owner(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    lease = acquire(root, "task-a", "edit runtime", 5)
    assert lease["owner"] == "task-a"
    with pytest.raises(RuntimeError, match="leased by task-a"):
        acquire(root, "task-b", "edit UI", 5)
    with pytest.raises(RuntimeError, match="belongs to task-a"):
        release(root, "task-b", False)
    assert release(root, "task-a", True)["released"] is True


def test_release_and_build_check_refuse_uncommitted_work(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    acquire(root, "task-a", "edit runtime", 5)
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="uncommitted"):
        release(root, "task-a", True)
    with pytest.raises(RuntimeError, match="uncommitted"):
        check(root, True, False)
    release(root, "task-a", False)


def test_release_gate_refuses_an_active_mutation_lease(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    acquire(root, "task-a", "edit runtime", 5)
    with pytest.raises(RuntimeError, match="active mutation lease"):
        check(root, True, True)
    release(root, "task-a", True)


def test_active_owner_can_renew_without_changing_lease_identity(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    lease = acquire(root, "task-a", "edit runtime", 5)

    renewed = renew(root, "task-a", 10)

    assert renewed["token"] == lease["token"]
    assert renewed["acquired_at"] == lease["acquired_at"]
    assert renewed["expires_at"] > lease["expires_at"]
    assert renewed["renewed_at"] >= lease["acquired_at"]
    with pytest.raises(RuntimeError, match="belongs to task-a"):
        renew(root, "task-b", 10)
    release(root, "task-a", True)


def test_expired_lease_cannot_hide_uncommitted_takeover(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    lease = acquire(root, "task-a", "edit runtime", 5)
    git_dir = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--git-dir"], cwd=root, text=True
        ).strip()
    )
    lease_path = (
        git_dir if git_dir.is_absolute() else root / git_dir
    ) / "lantai-worktree-lease.json"

    lease["expires_at"] = 0
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="expired worktree lease"):
        acquire(root, "task-b", "take over", 5)

    with pytest.raises(RuntimeError, match="expired"):
        renew(root, "task-a", 5)
