#!/usr/bin/env python3
"""Fail closed when Lantai release layers do not report one version."""

from __future__ import annotations

import argparse
import json
import math
import plistlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


STRICT_VERSION_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
)
BACKEND_SOURCE_VERSION_PATTERN = re.compile(
    r'^\s*__version__\s*=\s*["\']([^"\']+)["\']\s*$',
    re.MULTILINE,
)
BACKEND_CLI_VERSION_PATTERN = re.compile(r"\bversion\s+([0-9]+\.[0-9]+\.[0-9]+)\b")
BACKEND_DIST_INFO_PATTERN = re.compile(r"^lantai-(.+)\.dist-info$")
METADATA_VERSION_PATTERN = re.compile(r"^Version:\s*(\S+)\s*$", re.MULTILINE)
DEFAULT_BACKEND_TIMEOUT_SECONDS = 60.0


class VersionCoherenceError(ValueError):
    """Raised when two release layers report different versions."""


@dataclass(frozen=True)
class VersionCoherenceResult:
    expected_version: str
    observed_versions: dict[str, str]


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VersionCoherenceError(f"Could not read release metadata {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VersionCoherenceError(f"Release metadata must be a JSON object: {path}")
    return payload


def _strict_version(value: object, *, label: str) -> str:
    version = value.strip() if isinstance(value, str) else ""
    if STRICT_VERSION_PATTERN.fullmatch(version) is None:
        raise VersionCoherenceError(
            f"{label} has invalid version {value!r}; expected strict x.y.z."
        )
    return version


def _source_versions(project_root: Path) -> dict[str, str]:
    package = _read_json(project_root / "console" / "package.json")
    package_lock = _read_json(project_root / "console" / "package-lock.json")
    packages = package_lock.get("packages")
    package_lock_root = packages.get("") if isinstance(packages, dict) else None
    backend_path = project_root / "src" / "lantai" / "__version__.py"
    try:
        backend_source = backend_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VersionCoherenceError(
            f"Could not read Python backend source version from {backend_path}: {exc}"
        ) from exc
    backend_match = BACKEND_SOURCE_VERSION_PATTERN.search(backend_source)

    return {
        "Electron package": _strict_version(
            package.get("version"), label="Electron package"
        ),
        "package-lock root": _strict_version(
            package_lock.get("version"), label="package-lock root"
        ),
        "package-lock package": _strict_version(
            package_lock_root.get("version")
            if isinstance(package_lock_root, dict)
            else None,
            label="package-lock package",
        ),
        "Python backend source": _strict_version(
            backend_match.group(1) if backend_match else None,
            label="Python backend source",
        ),
    }


def _backend_executable_version(
    executable: Path,
    *,
    label: str,
    timeout_seconds: float = DEFAULT_BACKEND_TIMEOUT_SECONDS,
) -> str:
    if not executable.is_file():
        raise VersionCoherenceError(f"{label} executable is missing: {executable}")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VersionCoherenceError(
            f"{label} version timeout must be a positive finite number; "
            f"got {timeout_seconds!r}."
        )
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VersionCoherenceError(
            f"Could not read {label} version from {executable}: {exc}"
        ) from exc
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    match = BACKEND_CLI_VERSION_PATTERN.search(output)
    if completed.returncode != 0 or match is None:
        raise VersionCoherenceError(
            f"{label} --version failed or returned an invalid value: "
            f"exit={completed.returncode}, output={output.strip()!r}."
        )
    return _strict_version(match.group(1), label=label)


def _backend_distribution_versions(executable: Path, *, label: str) -> dict[str, str]:
    """Read the installed Lantai distribution metadata inside a PyInstaller app.

    The executable imports version source directly, so ``--version`` can be current
    while PyInstaller still copies stale ``lantai-*.dist-info`` from the build
    virtualenv. Validate both independent sources and reject mixed packages.
    """

    try:
        backend_app = executable.parents[2]
    except IndexError as exc:
        raise VersionCoherenceError(
            f"{label} executable is not inside an app bundle: {executable}"
        ) from exc
    contents = backend_app / "Contents"
    if backend_app.suffix != ".app" or not contents.is_dir():
        raise VersionCoherenceError(
            f"{label} executable is not inside a PyInstaller app bundle: {executable}"
        )

    observed: dict[str, str] = {}
    dist_dirs: list[tuple[str, Path]] = []
    for root_name in ("Resources", "Frameworks"):
        library_dir = contents / root_name
        if not library_dir.is_dir():
            raise VersionCoherenceError(
                f"{label} distribution metadata root is missing: {library_dir}."
            )
        matches = sorted(library_dir.glob("lantai-*.dist-info"))
        if len(matches) != 1:
            names = ", ".join(path.name for path in matches) or "none"
            raise VersionCoherenceError(
                f"{label} distribution metadata under {library_dir} must contain "
                f"exactly one Lantai dist-info directory; found {names}."
            )
        dist_dirs.append((root_name, matches[0]))

    for root_name, display_dir in dist_dirs:
        directory_match = BACKEND_DIST_INFO_PATTERN.fullmatch(display_dir.name)
        directory_version = _strict_version(
            directory_match.group(1) if directory_match else None,
            label=f"{label} distribution directory {display_dir.name}",
        )
        metadata_path = display_dir / "METADATA"
        try:
            metadata_text = metadata_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise VersionCoherenceError(
                f"Could not read {label} distribution metadata {metadata_path}: {exc}"
            ) from exc
        metadata_match = METADATA_VERSION_PATTERN.search(metadata_text)
        metadata_version = _strict_version(
            metadata_match.group(1) if metadata_match else None,
            label=f"{label} distribution metadata",
        )
        if directory_version != metadata_version:
            raise VersionCoherenceError(
                f"{label} distribution metadata disagrees with its directory: "
                f"directory={directory_version}, METADATA={metadata_version}."
            )
        observed[f"{label} distribution metadata ({root_name})"] = metadata_version
    return observed


def _packaged_app_versions(
    packaged_app: Path,
    *,
    backend_timeout_seconds: float = DEFAULT_BACKEND_TIMEOUT_SECONDS,
) -> dict[str, str]:
    info_plist = packaged_app / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as file:
            info = plistlib.load(file)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise VersionCoherenceError(
            f"Could not read packaged app metadata from {info_plist}: {exc}"
        ) from exc
    if not isinstance(info, dict):
        raise VersionCoherenceError(f"Packaged app metadata is invalid: {info_plist}")
    backend = (
        packaged_app
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    observed = {
        "Packaged app short version": _strict_version(
            info.get("CFBundleShortVersionString"),
            label="Packaged app short version",
        ),
        "Packaged app bundle version": _strict_version(
            info.get("CFBundleVersion"),
            label="Packaged app bundle version",
        ),
        "Packaged backend": _backend_executable_version(
            backend,
            label="Packaged backend",
            timeout_seconds=backend_timeout_seconds,
        ),
    }
    observed.update(
        _backend_distribution_versions(backend, label="Packaged backend")
    )
    return observed


def check_release_version_coherence(
    *,
    project_root: Path,
    expected_version: str | None = None,
    frozen_backend: Path | None = None,
    packaged_app: Path | None = None,
    backend_timeout_seconds: float = DEFAULT_BACKEND_TIMEOUT_SECONDS,
) -> VersionCoherenceResult:
    """Require source, frozen backend and app bundle to report one version."""

    observed = _source_versions(project_root.resolve())
    if frozen_backend is not None:
        resolved_backend = frozen_backend.resolve()
        observed["Frozen backend"] = _backend_executable_version(
            resolved_backend,
            label="Frozen backend",
            timeout_seconds=backend_timeout_seconds,
        )
        observed.update(
            _backend_distribution_versions(
                resolved_backend,
                label="Frozen backend",
            )
        )
    if packaged_app is not None:
        observed.update(
            _packaged_app_versions(
                packaged_app.resolve(),
                backend_timeout_seconds=backend_timeout_seconds,
            )
        )
    expected = _strict_version(
        expected_version or observed["Electron package"],
        label="Expected release",
    )
    mismatches = {
        label: version for label, version in observed.items() if version != expected
    }
    if mismatches:
        details = ", ".join(
            f"{label}={version}" for label, version in sorted(mismatches.items())
        )
        raise VersionCoherenceError(
            f"Release version mismatch: expected={expected}; {details}."
        )
    return VersionCoherenceResult(
        expected_version=expected,
        observed_versions=observed,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--expected-version")
    parser.add_argument("--frozen-backend", type=Path)
    parser.add_argument("--packaged-app", type=Path)
    parser.add_argument(
        "--backend-timeout",
        type=float,
        default=DEFAULT_BACKEND_TIMEOUT_SECONDS,
        help=(
            "Maximum seconds for a frozen backend --version cold start "
            f"(default: {DEFAULT_BACKEND_TIMEOUT_SECONDS:g})."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = check_release_version_coherence(
            project_root=args.project_root,
            expected_version=args.expected_version,
            frozen_backend=args.frozen_backend,
            packaged_app=args.packaged_app,
            backend_timeout_seconds=args.backend_timeout,
        )
    except VersionCoherenceError as exc:
        raise SystemExit(f"Error: {exc}") from exc
    print(
        "Release version coherence gate passed: "
        f"all checked layers report {result.expected_version}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
