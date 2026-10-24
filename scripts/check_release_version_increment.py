#!/usr/bin/env python3
"""Enforce monotonic, intentional Lantai release-version transitions."""

from __future__ import annotations

import argparse
import json
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


STRICT_VERSION_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
)


class VersionIncrementError(ValueError):
    """Raised when a new build is not the next patch or series boundary."""


@dataclass(frozen=True)
class VersionIncrementResult:
    target_version: str
    baseline_version: str | None
    expected_version: str | None


def _parse_version(version: str) -> tuple[int, int, int]:
    match = STRICT_VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise VersionIncrementError(
            f"Invalid release version {version!r}; expected strict x.y.z."
        )
    return tuple(int(part) for part in match.groups())


def _read_deployed_version(deployed_app: Path) -> str | None:
    if not deployed_app.exists():
        return None

    info_plist = deployed_app / "Contents" / "Info.plist"
    if not info_plist.is_file():
        raise VersionIncrementError(
            f"Deployed app exists but has no readable Info.plist: {info_plist}"
        )

    try:
        with info_plist.open("rb") as file:
            info = plistlib.load(file)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise VersionIncrementError(
            f"Could not read deployed app version from {info_plist}: {exc}"
        ) from exc

    version = info.get("CFBundleShortVersionString")
    if not isinstance(version, str) or not version:
        raise VersionIncrementError(
            f"Deployed app is missing CFBundleShortVersionString: {info_plist}"
        )
    _parse_version(version)
    return version


def _validate_package_lock_version(
    package_lock: Path,
    *,
    target_version: str,
) -> None:
    try:
        payload = json.loads(package_lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VersionIncrementError(
            f"Could not read package-lock version from {package_lock}: {exc}"
        ) from exc

    root_version = payload.get("version") if isinstance(payload, dict) else None
    packages = payload.get("packages") if isinstance(payload, dict) else None
    package_root = packages.get("") if isinstance(packages, dict) else None
    package_version = (
        package_root.get("version") if isinstance(package_root, dict) else None
    )
    if root_version != target_version or package_version != target_version:
        raise VersionIncrementError(
            "console/package-lock.json versions must match the target version: "
            f"target={target_version}, root={root_version!r}, "
            f"packages['']={package_version!r}."
        )


def check_release_version_increment(
    *,
    target_version: str,
    deployed_app: Path,
    package_lock: Path | None = None,
    allow_same_version_rebuild: bool = False,
) -> VersionIncrementResult:
    """Require the target to be the next patch, minor, or major boundary.

    Patch releases stay sequential. A new minor must advance exactly one minor
    and reset patch to zero; a new major must advance exactly one major and
    reset both lower components. This admits an intentional 1.x -> 2.0.0
    release without allowing skipped release lines, downgrades, or arbitrary
    version jumps.
    """

    _parse_version(target_version)
    if package_lock is not None:
        _validate_package_lock_version(
            package_lock,
            target_version=target_version,
        )
    baseline_version = _read_deployed_version(deployed_app)
    if baseline_version is None:
        return VersionIncrementResult(
            target_version=target_version,
            baseline_version=None,
            expected_version=None,
        )

    major, minor, patch = _parse_version(baseline_version)
    allowed_versions = (
        f"{major}.{minor}.{patch + 1}",
        f"{major}.{minor + 1}.0",
        f"{major + 1}.0.0",
    )
    expected_version = allowed_versions[0]
    if allow_same_version_rebuild and target_version == baseline_version:
        return VersionIncrementResult(
            target_version=target_version,
            baseline_version=baseline_version,
            expected_version=target_version,
        )
    if target_version not in allowed_versions:
        raise VersionIncrementError(
            "A new Lantai build must be the next patch or an adjacent minor/major "
            "series boundary: "
            f"deployed={baseline_version}, target={target_version}, "
            f"allowed={', '.join(allowed_versions)}. Update console/package.json and "
            "console/package-lock.json before building."
        )

    return VersionIncrementResult(
        target_version=target_version,
        baseline_version=baseline_version,
        expected_version=expected_version,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Require a new Lantai build version to be the next patch or adjacent "
            "minor/major series boundary above the deployed Downloads app."
        )
    )
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--deployed-app", required=True, type=Path)
    parser.add_argument("--package-lock", type=Path)
    parser.add_argument(
        "--allow-same-version-rebuild",
        action="store_true",
        help="Allow replacing an explicitly acknowledged local preview build.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = check_release_version_increment(
            target_version=args.target_version,
            deployed_app=args.deployed_app,
            package_lock=args.package_lock,
            allow_same_version_rebuild=args.allow_same_version_rebuild,
        )
    except VersionIncrementError as exc:
        raise SystemExit(f"Error: {exc}") from exc

    if result.baseline_version is None:
        print(
            "Release version increment gate: no deployed app baseline found; "
            f"allowing first build at {result.target_version}."
        )
    else:
        print(
            "Release version increment gate passed: "
            f"{result.baseline_version} -> {result.target_version}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
