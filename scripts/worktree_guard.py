#!/usr/bin/env python3
"""Atomic shared-worktree mutation lease and cleanliness gate.

The repository is frequently operated by several agent tasks.  A lease makes
write ownership explicit, while release checks keep stale or uncommitted work
from silently entering a package.  It deliberately never auto-commits.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


def _git_dir(root: Path) -> Path:
    value = subprocess.check_output(
        ["git", "rev-parse", "--git-dir"], cwd=root, text=True
    ).strip()
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def _lease_path(root: Path) -> Path:
    return _git_dir(root) / "lantai-worktree-lease.json"


def _dirty_paths(root: Path) -> list[str]:
    output = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        text=True,
    )
    return [line[3:] for line in output.splitlines() if len(line) >= 4]


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _active(lease: dict[str, Any] | None, now: float) -> bool:
    # The CLI process that creates the lease is intentionally short-lived, so
    # process liveness cannot represent the lifetime of the owning agent turn.
    # Keep ownership active until an explicit release or the bounded expiry.
    return bool(lease and float(lease.get("expires_at", 0)) > now)


def acquire(root: Path, owner: str, task: str, ttl_minutes: int) -> dict[str, Any]:
    path = _lease_path(root)
    now = time.time()
    existing = _read(path)
    if _active(existing, now):
        raise RuntimeError(
            f"worktree is leased by {existing.get('owner')} for {existing.get('task')}"
        )
    if existing:
        dirty = _dirty_paths(root)
        if dirty:
            raise RuntimeError(
                "expired worktree lease still has "
                f"{len(dirty)} uncommitted path(s); clean or commit them before takeover"
            )
        path.unlink(missing_ok=True)
    lease = {
        "schema_version": 1,
        "token": uuid.uuid4().hex,
        "owner": owner,
        "task": task,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": now,
        "expires_at": now + max(1, ttl_minutes) * 60,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(lease, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return lease


def release(root: Path, owner: str, require_clean: bool) -> dict[str, Any]:
    path = _lease_path(root)
    lease = _read(path)
    if not lease:
        raise RuntimeError("no worktree lease exists")
    if str(lease.get("owner")) != owner:
        raise RuntimeError(f"lease belongs to {lease.get('owner')}, not {owner}")
    dirty = _dirty_paths(root)
    if require_clean and dirty:
        raise RuntimeError(f"worktree still has {len(dirty)} uncommitted path(s)")
    path.unlink()
    return {"released": True, "dirty_count": len(dirty)}


def renew(root: Path, owner: str, ttl_minutes: int) -> dict[str, Any]:
    """Extend an active lease without changing its owner or acquisition token."""

    path = _lease_path(root)
    now = time.time()
    lease = _read(path)
    if not lease:
        raise RuntimeError("no worktree lease exists")
    if str(lease.get("owner")) != owner:
        raise RuntimeError(f"lease belongs to {lease.get('owner')}, not {owner}")
    if not _active(lease, now):
        raise RuntimeError("worktree lease has expired and cannot be renewed")
    renewed = {
        **lease,
        "renewed_at": now,
        "expires_at": now + max(1, ttl_minutes) * 60,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(renewed, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return renewed


def check(root: Path, require_clean: bool, require_no_active_lease: bool) -> dict[str, Any]:
    now = time.time()
    lease = _read(_lease_path(root))
    active = _active(lease, now)
    dirty = _dirty_paths(root)
    errors: list[str] = []
    if require_clean and dirty:
        errors.append(f"worktree has {len(dirty)} uncommitted path(s)")
    if require_no_active_lease and active:
        errors.append(f"active mutation lease belongs to {lease.get('owner')}")
    payload = {
        "ok": not errors,
        "dirty_count": len(dirty),
        "dirty_paths": dirty[:50],
        "lease_active": active,
        "lease": lease,
        "errors": errors,
    }
    if errors:
        raise RuntimeError("; ".join(errors))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    sub = parser.add_subparsers(dest="command", required=True)
    acquire_parser = sub.add_parser("acquire")
    acquire_parser.add_argument("--owner", required=True)
    acquire_parser.add_argument("--task", required=True)
    acquire_parser.add_argument("--ttl-minutes", type=int, default=120)
    release_parser = sub.add_parser("release")
    release_parser.add_argument("--owner", required=True)
    release_parser.add_argument("--require-clean", action="store_true")
    renew_parser = sub.add_parser("renew")
    renew_parser.add_argument("--owner", required=True)
    renew_parser.add_argument("--ttl-minutes", type=int, default=120)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--require-clean", action="store_true")
    check_parser.add_argument("--require-no-active-lease", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        if args.command == "acquire":
            payload = acquire(root, args.owner, args.task, args.ttl_minutes)
        elif args.command == "release":
            payload = release(root, args.owner, args.require_clean)
        elif args.command == "renew":
            payload = renew(root, args.owner, args.ttl_minutes)
        else:
            payload = check(
                root, args.require_clean, args.require_no_active_lease
            )
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
