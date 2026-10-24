#!/usr/bin/env python3
"""Bind reusable CI-only console assets to a revision, platform and exact bytes."""

import argparse
import hashlib
import json
from pathlib import Path


def inventory(root):
    if not (root / "index.html").is_file():
        raise ValueError("Console index.html is missing")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink is not an accepted test asset: {path}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["create", "verify"])
    parser.add_argument("--console", type=Path, default=Path("console"))
    parser.add_argument("--revision", required=True)
    parser.add_argument("--platform", required=True)
    args = parser.parse_args()
    manifest = args.console / "test-artifact.json"
    try:
        expected = {
            "schema_version": 1,
            "purpose": "ci-test-only",
            "revision": args.revision,
            "platform": args.platform,
            "files": inventory(args.console / "dist"),
        }
        if args.mode == "create":
            manifest.write_text(
                json.dumps(expected, indent=2) + "\n", encoding="utf-8"
            )
        elif json.loads(manifest.read_text(encoding="utf-8")) != expected:
            raise ValueError(
                "Test artifact revision, platform or file bytes differ"
            )
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"PASS: {args.mode} CI console assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
