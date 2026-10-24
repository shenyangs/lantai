#!/usr/bin/env python3
"""Audit whether Lantai source, local builds, installs, and releases line up."""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
INTERNAL_DMG_PATTERN = re.compile(r"^澜台-(?P<version>\d+\.\d+\.\d+)-.*-internal\.dmg$")


class VersionLineageError(ValueError):
    """Raised when version evidence cannot be parsed safely."""


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class VersionLineageReport:
    phase: str
    source_version: str | None
    installed_version: str | None
    internal_version: str | None
    public_version: str | None
    findings: tuple[Finding, ...]

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == "blocker")


def _parse_version(version: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise VersionLineageError(
            f"Invalid version {version!r}; expected strict x.y.z."
        )
    return tuple(int(part) for part in match.groups())


def _read_package_version(package_json: Path) -> str:
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VersionLineageError(
            f"Could not read source version from {package_json}: {exc}"
        ) from exc

    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str):
        raise VersionLineageError(f"Missing version in {package_json}.")
    _parse_version(version)
    return version


def _read_app_version(app_path: Path) -> str | None:
    if not app_path.exists():
        return None

    info_plist = app_path / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as file:
            payload = plistlib.load(file)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise VersionLineageError(
            f"Could not read app version from {info_plist}: {exc}"
        ) from exc

    version = payload.get("CFBundleShortVersionString")
    if not isinstance(version, str):
        raise VersionLineageError(
            f"Missing CFBundleShortVersionString in {info_plist}."
        )
    _parse_version(version)
    return version


def _highest_version(versions: Sequence[str]) -> str | None:
    if not versions:
        return None
    return max(versions, key=_parse_version)


def _discover_internal_version(internal_dir: Path) -> str | None:
    versions: list[str] = []
    if internal_dir.is_dir():
        for path in internal_dir.iterdir():
            match = INTERNAL_DMG_PATTERN.fullmatch(path.name)
            if path.is_file() and match is not None:
                version = match.group("version")
                _parse_version(version)
                versions.append(version)

    downloads_app_version = _read_app_version(internal_dir / "澜台.app")
    if downloads_app_version is not None:
        versions.append(downloads_app_version)
    return _highest_version(versions)


def _discover_public_version(release_repo: str) -> str:
    repo_url = f"https://github.com/{release_repo}.git"
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", repo_url],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VersionLineageError(
            f"Could not inspect public release tags for {release_repo}: {exc}"
        ) from exc

    versions: list[str] = []
    for line in completed.stdout.splitlines():
        _, _, ref = line.partition("\t")
        tag = ref.removeprefix("refs/tags/").removeprefix("v")
        if VERSION_PATTERN.fullmatch(tag):
            versions.append(tag)
    latest = _highest_version(versions)
    if latest is None:
        raise VersionLineageError(
            f"No strict x.y.z release tags found for {release_repo}."
        )
    return latest


def audit_version_lineage(
    *,
    phase: str = "postpublish",
    source_version: str | None,
    installed_version: str | None,
    internal_version: str | None,
    public_version: str | None,
) -> VersionLineageReport:
    """Return actionable findings for one release-version snapshot."""

    if phase not in {"prepublish", "postpublish"}:
        raise VersionLineageError(
            f"Invalid release phase {phase!r}; expected prepublish or postpublish."
        )

    versions = {
        "source": source_version,
        "installed": installed_version,
        "internal": internal_version,
        "public": public_version,
    }
    for version in versions.values():
        if version is not None:
            _parse_version(version)

    findings: list[Finding] = []
    if installed_version is None:
        findings.append(
            Finding(
                "installed_app_missing",
                "warning",
                "No installed Lantai app was found; packaged behavior cannot be compared.",
            )
        )
    if internal_version is None:
        findings.append(
            Finding(
                "internal_build_missing",
                "blocker",
                "No internal app or DMG was found for release verification.",
            )
        )
    if public_version is None:
        findings.append(
            Finding(
                "public_version_unavailable",
                "blocker",
                "The public release version is unavailable, so delivery cannot be proven.",
            )
        )

    if (
        source_version is not None
        and internal_version is not None
        and source_version != internal_version
    ):
        findings.append(
            Finding(
                "source_internal_mismatch",
                "blocker",
                "Source and newest internal build differ: "
                f"source={source_version}, internal={internal_version}.",
            )
        )

    if (
        public_version is not None
        and installed_version is not None
        and _parse_version(public_version) < _parse_version(installed_version)
    ):
        severity = "blocker" if phase == "postpublish" else "warning"
        findings.append(
            Finding(
                "public_channel_behind_installed",
                severity,
                "The current public release is older than this install: "
                f"public={public_version}, installed={installed_version}.",
            )
        )

    if source_version is not None and public_version is not None:
        source_parts = _parse_version(source_version)
        public_parts = _parse_version(public_version)
        if phase == "prepublish":
            expected_source = (
                public_parts[0],
                public_parts[1],
                public_parts[2] + 1,
            )
            if source_parts <= public_parts:
                findings.append(
                    Finding(
                        "source_not_newer_than_public",
                        "blocker",
                        "A pre-publication build must be newer than public: "
                        f"public={public_version}, source={source_version}.",
                    )
                )
            elif source_parts != expected_source:
                expected_text = ".".join(str(part) for part in expected_source)
                findings.append(
                    Finding(
                        "public_version_jump_planned",
                        "warning",
                        "This recovery release skips the next public patch: "
                        f"public={public_version}, source={source_version}, "
                        f"expected={expected_text}.",
                    )
                )
        elif public_parts < source_parts:
            findings.append(
                Finding(
                    "source_not_public",
                    "blocker",
                    "Current source has not reached the public channel: "
                    f"source={source_version}, public={public_version}.",
                )
            )
        elif public_parts > source_parts:
            findings.append(
                Finding(
                    "public_ahead_of_source",
                    "blocker",
                    "Public release is newer than this source checkout: "
                    f"public={public_version}, source={source_version}.",
                )
            )

    if (
        phase == "prepublish"
        and source_version is not None
        and installed_version is not None
        and _parse_version(source_version) <= _parse_version(installed_version)
    ):
        findings.append(
            Finding(
                "source_will_not_upgrade_installed",
                "blocker",
                "The planned release will not upgrade the installed app: "
                f"source={source_version}, installed={installed_version}.",
            )
        )

    if (
        installed_version is not None
        and internal_version is not None
        and _parse_version(installed_version) < _parse_version(internal_version)
    ):
        findings.append(
            Finding(
                "installed_behind_internal",
                "info",
                "The installed app is older than the newest internal build: "
                f"installed={installed_version}, internal={internal_version}.",
            )
        )

    if not findings:
        findings.append(
            Finding(
                "version_lineage_aligned",
                "info",
                "Source, internal build, installed app, and public release are aligned.",
            )
        )

    return VersionLineageReport(
        phase=phase,
        source_version=source_version,
        installed_version=installed_version,
        internal_version=internal_version,
        public_version=public_version,
        findings=tuple(findings),
    )


def _build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Compare Lantai source, installed app, internal build, and public "
            "release versions."
        )
    )
    parser.add_argument(
        "--source-package",
        type=Path,
        default=root / "console" / "package.json",
    )
    parser.add_argument(
        "--installed-app",
        type=Path,
        default=Path("/Applications/澜台.app"),
    )
    parser.add_argument(
        "--internal-dir",
        type=Path,
        default=Path.home() / "Downloads",
    )
    parser.add_argument(
        "--release-repo",
        default="shenyangs/lantai3-releases",
    )
    parser.add_argument(
        "--public-version",
        help="Use an explicit public version instead of querying GitHub tags.",
    )
    parser.add_argument(
        "--phase",
        choices=("prepublish", "postpublish"),
        default="postpublish",
        help=(
            "Use prepublish when source/internal should be one patch above public; "
            "use postpublish when all release versions should match."
        ),
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any delivery blocker is found.",
    )
    return parser


def _print_report(report: VersionLineageReport, output_format: str) -> None:
    if output_format == "json":
        payload = asdict(report)
        payload["ready"] = not report.blockers
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print("Lantai release version lineage")
    print(f"  phase:     {report.phase}")
    print(f"  source:    {report.source_version or 'missing'}")
    print(f"  installed: {report.installed_version or 'missing'}")
    print(f"  internal:  {report.internal_version or 'missing'}")
    print(f"  public:    {report.public_version or 'missing'}")
    for finding in report.findings:
        print(f"[{finding.severity.upper()}] {finding.code}: {finding.message}")
    result = "BLOCKED" if report.blockers else "READY"
    print(f"Result: {result} ({len(report.blockers)} blocker(s))")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        source_version = _read_package_version(args.source_package)
        installed_version = _read_app_version(args.installed_app)
        internal_version = _discover_internal_version(args.internal_dir)
        public_version = args.public_version or _discover_public_version(
            args.release_repo
        )
        report = audit_version_lineage(
            phase=args.phase,
            source_version=source_version,
            installed_version=installed_version,
            internal_version=internal_version,
            public_version=public_version,
        )
    except VersionLineageError as exc:
        raise SystemExit(f"Error: {exc}") from exc

    _print_report(report, args.format)
    return 1 if args.strict and report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
