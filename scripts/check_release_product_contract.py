#!/usr/bin/env python3
"""Verify that a built console/App matches the current browser-use product contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_TEXT = ("让浏览器帮我", "浏览器会话")


def validate_dist(dist: Path, *, expected_sha: str, expected_version: str) -> dict[str, object]:
    violations: list[str] = []
    if not dist.is_dir():
        return {"ok": False, "dist": str(dist), "violations": ["console dist is missing"]}

    old_assets = sorted(path.name for path in dist.rglob("browserWorkbench-*.js"))
    if old_assets:
        violations.append(f"legacy browser workbench assets remain: {', '.join(old_assets)}")

    searchable = b"\n".join(
        path.read_bytes()
        for path in sorted(dist.rglob("*"))
        if path.is_file() and path.suffix in {".html", ".js", ".json"}
    )
    for required in REQUIRED_TEXT:
        if required.encode("utf-8") not in searchable:
            violations.append(f"required product marker missing: {required}")

    identity_path = dist / "release-provenance.json"
    identity: dict[str, object] = {}
    if not identity_path.is_file():
        violations.append("release-provenance.json is missing")
    else:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if identity.get("gitSha") != expected_sha:
            violations.append("embedded Git SHA does not match the build source")
        if identity.get("version") != expected_version:
            violations.append("embedded version does not match the package version")

    return {
        "ok": not violations,
        "dist": str(dist.resolve()),
        "requiredMarkers": list(REQUIRED_TEXT),
        "identity": identity,
        "violations": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--console-dist", type=Path)
    source.add_argument("--app", type=Path)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    dist = (
        args.console_dist
        if args.console_dist
        else args.app / "Contents" / "Resources" / "console" / "dist"
    )
    report = validate_dist(
        dist,
        expected_sha=args.expected_sha,
        expected_version=args.expected_version,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
