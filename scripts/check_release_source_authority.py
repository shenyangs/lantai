#!/usr/bin/env python3
"""Refuse canonical deployment from detached or non-main worktrees."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run_git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def parse_worktrees(payload: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in payload.splitlines() + [""]:
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return entries


def audit_release_source(root: Path, *, artifact_only: bool) -> dict[str, object]:
    requested_root = root.resolve()
    repository_root = Path(run_git(requested_root, "rev-parse", "--show-toplevel")).resolve()
    source_sha = run_git(repository_root, "rev-parse", "HEAD")
    try:
        main_sha = run_git(repository_root, "rev-parse", "refs/heads/main")
    except subprocess.CalledProcessError:
        # An isolated Actions tag checkout need not carry the local main ref.
        # This absence can never confer canonical deployment authority.
        main_sha = None
    try:
        source_branch = run_git(repository_root, "symbolic-ref", "--short", "HEAD")
    except subprocess.CalledProcessError:
        source_branch = "DETACHED"

    worktrees = parse_worktrees(run_git(repository_root, "worktree", "list", "--porcelain"))
    main_worktrees = [
        Path(item["worktree"]).resolve()
        for item in worktrees
        if item.get("branch") == "refs/heads/main" and item.get("worktree")
    ]
    violations: list[str] = []
    if not artifact_only and len(main_worktrees) != 1:
        violations.append(f"expected exactly one main worktree, found {len(main_worktrees)}")
    if not artifact_only and main_sha is None:
        violations.append("local refs/heads/main is unavailable")

    canonical_main_root = main_worktrees[0] if len(main_worktrees) == 1 else None
    canonical = (
        canonical_main_root == repository_root
        and source_branch == "main"
        and main_sha is not None
        and source_sha == main_sha
    )
    if not artifact_only and not canonical:
        violations.append(
            "Downloads deployment is only authorized from the unique main worktree at refs/heads/main"
        )

    return {
        "schemaVersion": 1,
        "ok": not violations,
        "mode": "artifact-only" if artifact_only else "canonical-deploy",
        "sourceRoot": str(repository_root),
        "sourceBranch": source_branch,
        "sourceSha": source_sha,
        "mainSha": main_sha,
        "canonicalMainRoot": str(canonical_main_root) if canonical_main_root else None,
        "canonicalMain": canonical,
        "violations": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--artifact-only", action="store_true")
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = audit_release_source(args.root, artifact_only=args.artifact_only)
    if args.expected_source_sha and report["sourceSha"] != args.expected_source_sha:
        report["violations"].append("HEAD differs from the independently expected source SHA")
    if args.require_clean:
        try:
            from scripts.worktree_guard import check
        except ModuleNotFoundError:
            from worktree_guard import check
        try:
            check(args.root, require_clean=True, require_no_active_lease=True)
        except RuntimeError as exc:
            report["violations"].append(str(exc))
    report["ok"] = not report["violations"]
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
