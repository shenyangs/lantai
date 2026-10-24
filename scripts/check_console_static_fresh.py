#!/usr/bin/env python3
"""Check that embedded console assets match the latest console/dist build."""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DIST_DIR = Path("console") / "dist"
DEFAULT_EMBEDDED_DIR = Path("src") / "lantai" / "console"


@dataclass(frozen=True)
class Drift:
    kind: str
    path: Path
    detail: str


def _iter_files(root: Path) -> set[Path]:
    if not root.exists():
        return set()
    return {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and path.name != ".DS_Store"
    }


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def check_console_static_fresh(
    repo_root: Path,
    *,
    dist_dir: Path = DEFAULT_DIST_DIR,
    embedded_dir: Path = DEFAULT_EMBEDDED_DIR,
    sync: bool = False,
) -> list[Drift]:
    """Return drift between console/dist and src/lantai/console.

    When ``sync`` is true, the embedded tree is replaced with console/dist before
    comparing. Release/wheel builds use this to prove the packaged source assets
    are the same bytes that the desktop build just generated.
    """

    repo_root = repo_root.resolve()
    dist_root = (repo_root / dist_dir).resolve()
    embedded_root = (repo_root / embedded_dir).resolve()

    if not dist_root.exists():
        return [
            Drift(
                "missing-dist",
                dist_dir,
                "Run `cd console && npm run build` before checking console assets.",
            )
        ]
    if not (dist_root / "index.html").is_file():
        return [
            Drift(
                "missing-dist-index",
                dist_dir / "index.html",
                "console/dist exists but does not contain index.html.",
            )
        ]

    if sync:
        if embedded_root.exists():
            shutil.rmtree(embedded_root)
        embedded_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dist_root, embedded_root)

    if not embedded_root.exists():
        return [
            Drift(
                "missing-embedded",
                embedded_dir,
                "Embedded console assets are missing.",
            )
        ]

    dist_files = _iter_files(dist_root)
    embedded_files = _iter_files(embedded_root)
    drifts: list[Drift] = []

    for path in sorted(dist_files - embedded_files):
        drifts.append(
            Drift("missing-embedded-file", path, "Present in console/dist only.")
        )
    for path in sorted(embedded_files - dist_files):
        drifts.append(
            Drift("stale-embedded-file", path, "Present in src/lantai/console only.")
        )
    for path in sorted(dist_files & embedded_files):
        if _read_bytes(dist_root / path) != _read_bytes(embedded_root / path):
            drifts.append(Drift("changed-file", path, "File bytes differ."))
    return drifts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify embedded console assets match console/dist.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Project root. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Replace src/lantai/console with console/dist before checking.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    drifts = check_console_static_fresh(args.repo_root, sync=args.sync)
    if not drifts:
        print("[console-static] embedded assets are fresh")
        return 0

    print("[console-static] embedded assets are stale:", file=sys.stderr)
    for drift in drifts[:50]:
        print(
            f"  - {drift.kind}: {drift.path} ({drift.detail})",
            file=sys.stderr,
        )
    if len(drifts) > 50:
        print(f"  ... {len(drifts) - 50} more drift entries", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
