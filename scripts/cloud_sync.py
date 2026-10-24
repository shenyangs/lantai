#!/usr/bin/env python3
"""Fast-forward-only delivery of validated development checkpoints.

No staging, commits, resets, stashes, force pushes or implicit merges. Runtime
state and validation receipts live outside the checkout and contain no secrets.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

CHANNEL = "codex/cloud-sync"


class SyncBlocked(RuntimeError):
    pass


def run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo,
        env=env, capture_output=True, text=True, timeout=120,
    )
    if check and result.returncode:
        # Git errors can include credential-bearing URLs. Keep them in neither
        # terminal output nor persistent sync state.
        raise SyncBlocked(f"git {args[0]} failed (exit {result.returncode})")
    return result


def git(repo: Path, *args: str) -> str:
    return run(repo, *args).stdout.strip()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with open(temporary, "x", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def git_path(repo: Path, name: str) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", name))


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def readiness(repo: Path) -> dict:
    dirty = bool(git(repo, "status", "--porcelain=v1", "--untracked-files=normal"))
    lease = git_path(repo, "lantai-worktree-lease.json").exists()
    locks = any(git_path(repo, name).exists() for name in (
        "index.lock", "HEAD.lock", "config.lock", "packed-refs.lock",
        "MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD",
    ))
    return {"dirty": dirty, "lease_present": lease, "git_busy": locks,
            "head": head(repo), "branch": git(repo, "branch", "--show-current")}


def ensure_ready(repo: Path) -> dict:
    status = readiness(repo)
    if status["dirty"]:
        raise SyncBlocked("uncommitted changes preserved; checkout update paused")
    if status["lease_present"]:
        raise SyncBlocked("existing mutation lease preserved; checkout update paused")
    if status["git_busy"]:
        raise SyncBlocked("Git transaction in progress; checkout update paused")
    return status


@contextlib.contextmanager
def mutation(repo: Path):
    ensure_ready(repo)
    owner = "cloud-sync-" + uuid.uuid4().hex
    guard = repo / "scripts/worktree_guard.py"
    if not guard.is_file():
        raise SyncBlocked("worktree guard missing; refusing checkout mutation")
    acquired = subprocess.run(
        [sys.executable, str(guard), "acquire", "--owner", owner,
         "--task", "validated cloud development checkpoint sync"], cwd=repo,
        capture_output=True, text=True, timeout=20,
    )
    if acquired.returncode:
        raise SyncBlocked("mutation lease unavailable")
    try:
        if git(repo, "status", "--porcelain=v1", "--untracked-files=normal"):
            raise SyncBlocked("checkout changed while acquiring lease")
        yield
    finally:
        released = subprocess.run(
            [sys.executable, str(guard), "release", "--owner", owner,
             "--require-clean"], cwd=repo, capture_output=True, text=True, timeout=20,
        )
        if released.returncode:
            raise SyncBlocked("checkout changed during sync; lease retained for recovery")


def check_channel(channel: str) -> None:
    if not re.fullmatch(r"codex/[a-zA-Z0-9][a-zA-Z0-9/_-]*", channel):
        raise SyncBlocked("sync destinations must be dedicated codex branches")


def ancestor(repo: Path, older: str, newer: str) -> bool:
    result = run(repo, "merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in (0, 1):
        raise SyncBlocked("commit ancestry unavailable; no history rewrite attempted")
    return result.returncode == 0


def safe_delta(repo: Path, older: str, newer: str) -> None:
    names = git(repo, "diff", "--name-only", "-z", older, newer).split("\0")
    forbidden_roots = {
        "website", "working", "working.secret", ".lantai", ".lantai.secret",
        ".coordination", "node_modules", ".venv", "release", "artifacts", "dist",
    }
    for name in filter(None, names):
        parts = Path(name).parts
        leaf = parts[-1].lower()
        if (parts[0] in forbidden_roots or "node_modules" in parts or
                parts[:2] in {("console", "dist"), ("desktop", "dist")} or
                (leaf.startswith(".env") and leaf not in {".env.example", ".env.sample"}) or
                leaf.endswith((".pem", ".p12", ".pfx", ".key")) or
                "working.secret" in parts):
            raise SyncBlocked(f"protected or local-only path changed: {name}")


def fetch_channel(repo: Path, remote: str, channel: str) -> str:
    check_channel(channel)
    run(repo, "fetch", "--no-tags", remote,
        f"refs/heads/{channel}:refs/remotes/{remote}/{channel}")
    return git(repo, "rev-parse", f"refs/remotes/{remote}/{channel}")


def pull_locked(repo: Path, remote: str, channel: str) -> dict:
    before = head(repo)
    target = fetch_channel(repo, remote, channel)
    if before == target:
        return {"status": "current", "head": before}
    if ancestor(repo, target, before):
        return {"status": "local_ahead", "head": before, "remote_head": target}
    if not ancestor(repo, before, target):
        raise SyncBlocked("local and cloud histories diverged; both versions preserved")
    safe_delta(repo, before, target)
    run(repo, "merge", "--ff-only", target)
    if head(repo) != target:
        raise SyncBlocked("fast-forward result differs from requested checkpoint")
    return {"status": "updated", "before": before, "head": target}


def pull(repo: Path, remote: str = "origin", channel: str = CHANNEL) -> dict:
    status = ensure_ready(repo)
    if status["branch"] in {"main", "master", ""}:
        raise SyncBlocked("main/release/detached checkout requires its coordinator")
    with mutation(repo):
        return pull_locked(repo, remote, channel)


def validation(repo: Path, state_dir: Path, checks: list[list[str]]) -> dict:
    sha = head(repo)
    if not checks:
        raise SyncBlocked("at least one actual validation command is required")
    validation_dir = state_dir / "validation" / sha
    validation_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence = []
    for i, command in enumerate(checks):
        if not command or not all(isinstance(arg, str) for arg in command):
            raise SyncBlocked("invalid validation command")
        # Only exit status and a digest are persisted: test output may contain
        # model replies, private inputs or configured-provider secrets.
        try:
            result = subprocess.run(command, cwd=repo, capture_output=True,
                                    timeout=900, env=dict(os.environ))
        except subprocess.TimeoutExpired:
            raise SyncBlocked(f"validation {i + 1} timed out; checkpoint not uploaded") from None
        item = {"executable": Path(command[0]).name, "argument_count": len(command) - 1,
                "command_sha256": hashlib.sha256(json.dumps(command).encode()).hexdigest(),
                "exit_code": result.returncode,
                "output_sha256": hashlib.sha256(result.stdout + result.stderr).hexdigest()}
        evidence.append(item)
        atomic_json(validation_dir / f"check-{i}.json", item)
        if result.returncode:
            raise SyncBlocked(f"validation {i + 1} failed; checkpoint not uploaded")
    if head(repo) != sha or git(repo, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise SyncBlocked("validation changed checkout; checkpoint not uploaded")
    receipt = {"head": sha, "checks": evidence, "validated_at": time.time()}
    atomic_json(validation_dir / "receipt.json", receipt)
    return receipt


def publish(repo: Path, state_dir: Path, checks: list[list[str]],
            remote: str = "origin", channel: str = CHANNEL, side: str = "local") -> dict:
    check_channel(channel)
    with mutation(repo):
        target = fetch_channel(repo, remote, channel)
        sha = head(repo)
        if ancestor(repo, sha, target):
            return {"status": "already_received", "head": sha, "remote_head": target}
        safe_delta(repo, git(repo, "merge-base", target, sha), sha)
        receipt = validation(repo, state_dir, checks)
        if not ancestor(repo, target, sha):
            incoming = f"{channel}-incoming/{side}/{sha}"
            run(repo, "push", remote, f"{sha}:refs/heads/{incoming}")
            result = {"status": "diverged_saved", "head": sha, "branch": incoming,
                      "remote_head": target, "validated_at": receipt["validated_at"]}
        else:
            # Normal Git push is the server-side compare/ancestry check. A
            # competing update causes failure; never retry with force.
            run(repo, "push", remote, f"{sha}:refs/heads/{channel}")
            result = {"status": "published", "head": sha, "branch": channel,
                      "validated_at": receipt["validated_at"]}
        atomic_json(state_dir / "last-publish.json", result)
        return result


def promote_main_locked(repo: Path, remote: str, channel: str) -> dict:
    run(repo, "fetch", "--no-tags", remote,
        f"refs/heads/main:refs/remotes/{remote}/main")
    latest = git(repo, "rev-parse", f"refs/remotes/{remote}/main")
    target = fetch_channel(repo, remote, channel)
    if ancestor(repo, latest, target):
        return {"status": "channel_contains_main", "main_head": latest}
    if not ancestor(repo, target, latest):
        return {"status": "main_diverged", "main_head": latest, "remote_head": target}
    safe_delta(repo, target, latest)
    # main is the project's validated integration branch. This copies a ref;
    # it neither integrates cloud changes into main nor publishes an app.
    run(repo, "push", remote, f"{latest}:refs/heads/{channel}")
    return {"status": "main_forwarded", "main_head": latest}


def once(config: dict) -> dict:
    repo = Path(config["repo"]).resolve()
    state_dir = Path(config["state_dir"]).resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # This lock is external to the source checkout and survives no crash as
    # an owned process lifetime lock; concurrent launchctl/manual ticks wait.
    import fcntl
    with open(state_dir / "tick.lock", "a", encoding="utf-8") as lock:
        os.chmod(state_dir / "tick.lock", 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "another_tick_running"}
        status = {"checked_at": time.time(), "repo": str(repo)}
        try:
            ready = ensure_ready(repo)
            if ready["branch"] != config.get("local_branch", CHANNEL):
                raise SyncBlocked("sync mirror branch changed; automatic update paused")
            remote = config.get("remote", "origin")
            channel = config.get("channel", CHANNEL)
            with mutation(repo):
                if config.get("follow_main", True):
                    status["main"] = promote_main_locked(repo, remote, channel)
                status.update(pull_locked(repo, remote, channel))
            sources = []
            for source in config.get("observe_sources", []):
                # Observation is strictly read-only. Shared original checkouts
                # remain under their existing worktree/coordinator ownership.
                observed = readiness(Path(source))
                observed["repo"] = source
                observed["status"] = "preserved" if any(observed[k] for k in
                    ("dirty", "lease_present", "git_busy")) else "clean_source"
                sources.append(observed)
            status["sources"] = sources
        except (SyncBlocked, subprocess.TimeoutExpired, OSError) as exc:
            status.update(status="paused", reason=str(exc))
        previous_path = state_dir / "status.json"
        previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
        atomic_json(previous_path, status)
        # Quiet while unchanged; the status file is always fresh for inspection.
        comparable = lambda value: {k: v for k, v in value.items() if k != "checked_at"}
        if comparable(status) != comparable(previous):
            print(json.dumps(status, ensure_ascii=False))
        return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pull", "publish", "once", "status"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--channel", default=CHANNEL)
    parser.add_argument("--side", choices=("local", "cloud"), default="local")
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local/state/lantai-cloud-sync")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--checks-json", help="JSON array of argv arrays; never evaluated as shell")
    args = parser.parse_args()
    try:
        if args.action == "once":
            if not args.config:
                parser.error("once requires --config")
            result = once(json.loads(args.config.read_text()))
            return 0 if result["status"] != "paused" else 2
        if args.action == "status":
            result = readiness(args.repo)
        elif args.action == "pull":
            result = pull(args.repo, args.remote, args.channel)
        else:
            result = publish(args.repo, args.state_dir,
                             json.loads(args.checks_json or "[]"),
                             args.remote, args.channel, args.side)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (SyncBlocked, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        print(json.dumps({"status": "paused", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
