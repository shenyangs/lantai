#!/usr/bin/env python3
"""Write the build identity embedded in both console and packaged App."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--canonical-main", action="store_true")
    parser.add_argument("--artifact-only", action="store_true")
    parser.add_argument("--candidate-id")
    args = parser.parse_args()
    import re
    if args.artifact_only:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.candidate_id or "") or args.canonical_main:
            raise SystemExit("A candidate requires its own safe ID and cannot assert official main identity")
    elif args.candidate_id or not args.canonical_main:
        raise SystemExit("Official build identity requires canonical main authority")

    root = args.root.resolve()
    actual_sha = git(root, "rev-parse", "HEAD")
    if actual_sha != args.source_sha:
        raise SystemExit(f"source SHA moved during build: expected {args.source_sha}, got {actual_sha}")
    try:
        branch = git(root, "symbolic-ref", "--short", "HEAD")
    except subprocess.CalledProcessError:
        branch = "DETACHED"
    if args.canonical_main:
        try:
            from scripts.check_release_source_authority import audit_release_source
        except ModuleNotFoundError:
            from check_release_source_authority import audit_release_source
        authority = audit_release_source(root, artifact_only=False)
        if not authority["ok"] or not authority["canonicalMain"]:
            raise SystemExit("Official build identity requires the actual unique main worktree")

    payload = {
        "schemaVersion": 1,
        "version": args.version,
        "gitSha": actual_sha,
        "shortGitSha": actual_sha[:12],
        "sourceBranch": branch,
        "buildTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "canonicalMain": args.canonical_main,
        "artifactOnly": args.artifact_only,
        "candidateId": args.candidate_id,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
