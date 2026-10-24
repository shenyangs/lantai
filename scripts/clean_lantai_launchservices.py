#!/usr/bin/env python3
"""Remove stale Lantai app registrations without resetting LaunchServices globally."""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path


BUNDLE_ID = "com.lantai.app"
DEFAULT_MAX_PASSES = 12
DEFAULT_SETTLE_SECONDS = 0.25
LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def parse_registrations(dump: str, bundle_id: str = BUNDLE_ID) -> list[dict[str, str]]:
    registrations: list[dict[str, str]] = []
    for block in re.split(r"\n-{40,}\n", dump):
        identifier = re.search(r"^identifier:\s+(.+?)\s*$", block, re.MULTILINE)
        if not identifier or identifier.group(1).strip().lower() != bundle_id.lower():
            continue
        path_match = re.search(
            r"^path:\s+(.*?)\s+\(0x[0-9a-f]+\)\s*$", block, re.MULTILINE
        )
        if not path_match:
            continue
        version = re.search(r"^versionString:\s+(.+?)\s*$", block, re.MULTILINE)
        registrations.append(
            {
                "path": path_match.group(1).strip(),
                "version": version.group(1).strip() if version else "",
            }
        )
    return registrations


def run_lsregister(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LSREGISTER), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def dump_registrations() -> list[dict[str, str]]:
    result = run_lsregister("-dump")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "LaunchServices dump failed")
    return parse_registrations(result.stdout)


def same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).expanduser().absolute() == Path(right).expanduser().absolute()


def read_bundle_metadata(app_path: Path) -> dict[str, str] | None:
    plist_path = app_path / "Contents" / "Info.plist"
    if not plist_path.is_file():
        return None
    try:
        with plist_path.open("rb") as handle:
            metadata = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return None
    return {
        "bundle_id": str(metadata.get("CFBundleIdentifier") or ""),
        "version": str(metadata.get("CFBundleShortVersionString") or ""),
    }


def discover_app_bundles(root: Path, max_depth: int = 2) -> list[Path]:
    """Find complete app bundles without descending into a discovered bundle."""
    found: list[Path] = []

    def visit(directory: Path, depth: int) -> None:
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for candidate in entries:
            if not candidate.is_dir():
                continue
            if (candidate / "Contents" / "Info.plist").is_file():
                found.append(candidate.absolute())
                continue
            if depth < max_depth:
                visit(candidate, depth + 1)

    visit(root.expanduser().absolute(), 0)
    return found


def find_stale_app_copies(
    current_app: Path,
    roots: list[Path],
    bundle_id: str = BUNDLE_ID,
) -> list[dict[str, object]]:
    candidates: set[Path] = set()
    for root in roots:
        candidates.update(discover_app_bundles(root))
    copies: list[dict[str, object]] = []
    trash_root = (Path.home() / ".Trash").absolute()
    for candidate in sorted(candidates, key=lambda item: str(item)):
        if same_path(candidate, current_app):
            continue
        metadata = read_bundle_metadata(candidate)
        if not metadata or metadata["bundle_id"] != bundle_id:
            continue
        copies.append(
            {
                "path": str(candidate),
                "version": metadata["version"] or "unknown",
                "in_trash": candidate == trash_root or trash_root in candidate.parents,
            }
        )
    return copies


def remove_validated_app_copy(
    candidate: Path,
    current_app: Path,
    allowed_roots: list[Path],
) -> None:
    candidate = candidate.expanduser().absolute()
    if same_path(candidate, current_app):
        raise RuntimeError("Refusing to delete the current Lantai app")
    if not any(root.expanduser().absolute() in candidate.parents for root in allowed_roots):
        raise RuntimeError(f"Stale app is outside allowed roots: {candidate}")
    metadata = read_bundle_metadata(candidate)
    if not metadata or metadata["bundle_id"] != BUNDLE_ID:
        raise RuntimeError(f"Refusing to delete an unverified app: {candidate}")
    if candidate.is_symlink():
        candidate.unlink()
    else:
        shutil.rmtree(candidate)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-app", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--remove-stale-app-copies",
        action="store_true",
        help="Permanently remove validated com.lantai.app bundles except --current-app.",
    )
    parser.add_argument(
        "--scan-root",
        action="append",
        type=Path,
        help="Root to scan for old app bundles; may be supplied more than once.",
    )
    parser.add_argument("--max-passes", type=int, default=DEFAULT_MAX_PASSES)
    parser.add_argument(
        "--settle-seconds", type=float, default=DEFAULT_SETTLE_SECONDS
    )
    args = parser.parse_args()

    if args.max_passes < 1:
        parser.error("--max-passes must be at least 1")
    if args.settle_seconds < 0:
        parser.error("--settle-seconds must not be negative")

    current_app = args.current_app.expanduser().absolute()
    scan_roots = args.scan_root or [
        Path("/Applications"),
        Path.home() / "Applications",
        Path.home() / "Downloads",
        Path.home() / ".Trash",
        current_app.parent,
    ]
    scan_roots = list(dict.fromkeys(root.expanduser().absolute() for root in scan_roots))
    current_metadata = read_bundle_metadata(current_app)
    if not current_metadata or current_metadata["bundle_id"] != BUNDLE_ID:
        raise RuntimeError(f"Current app is not a verified {BUNDLE_ID} bundle: {current_app}")

    disk_copies_before = find_stale_app_copies(current_app, scan_roots)
    removed_app_copies: list[dict[str, object]] = []
    if args.remove_stale_app_copies and not args.dry_run:
        for item in disk_copies_before:
            remove_validated_app_copy(Path(str(item["path"])), current_app, scan_roots)
            removed_app_copies.append(item)

    before = dump_registrations()
    stale = [item for item in before if not same_path(item["path"], current_app)]
    attempts: list[dict[str, object]] = []
    after = before
    max_passes = 1 if args.dry_run else args.max_passes
    passes_run = 0
    for pass_number in range(1, max_passes + 1):
        passes_run = pass_number
        pass_stale = [
            item for item in after if not same_path(item["path"], current_app)
        ]
        for item in pass_stale:
            if args.dry_run:
                attempts.append(
                    {**item, "pass": pass_number, "returncode": None, "dry_run": True}
                )
                continue
            result = run_lsregister("-u", item["path"])
            attempts.append(
                {
                    **item,
                    "pass": pass_number,
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip(),
                }
            )

        if args.dry_run:
            break

        # LaunchServices updates asynchronously and can briefly republish paths
        # that were present at the start of a large unregister batch. Re-register
        # the intended app, re-dump the database, and make another bounded pass.
        register_result = run_lsregister("-f", str(current_app))
        if register_result.returncode != 0:
            raise RuntimeError(
                register_result.stderr.strip() or "Current app registration failed"
            )
        if args.settle_seconds:
            time.sleep(args.settle_seconds)
        after = dump_registrations()
        if not any(not same_path(item["path"], current_app) for item in after):
            break

    remaining_stale = [item for item in after if not same_path(item["path"], current_app)]
    disk_copies_after = find_stale_app_copies(current_app, scan_roots)
    report = {
        "bundle_id": BUNDLE_ID,
        "current_app": str(current_app),
        "before_count": len(before),
        "stale_count": len(stale),
        "max_passes": args.max_passes,
        "settle_seconds": args.settle_seconds,
        "passes_run": passes_run,
        "attempts": attempts,
        "after_count": len(after),
        "remaining_stale": remaining_stale,
        "disk_copies_before": disk_copies_before,
        "removed_app_copies": removed_app_copies,
        "disk_copies_after": disk_copies_after,
        "remove_stale_app_copies": args.remove_stale_app_copies,
        "dry_run": args.dry_run,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if args.strict and (remaining_stale or disk_copies_after):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
