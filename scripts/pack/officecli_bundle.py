#!/usr/bin/env python3
"""Bundle an OfficeCLI executable into a frozen lantai-server directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, NamedTuple

TRUE_VALUES = {"1", "true", "yes", "on"}
OFFICECLI_ENV_KEYS = ("LANTAI_OFFICECLI_BINARY", "OFFICECLI_PATH")
PROVENANCE_FILENAME = "officecli.provenance.json"
LICENSE_BUNDLE_FILENAME = "officecli-LICENSE.txt"
NOTICE_BUNDLE_FILENAME = "officecli-NOTICE.txt"


class OfficeCliValidation(NamedTuple):
    source_kind: str
    source_path: Path
    source_url: str
    sha256: str
    version: str
    expected_version: str
    expected_arch: str
    license: str
    license_path: Path | None
    notice_path: Path | None
    license_sha256: str
    notice_sha256: str
    releasable: bool
    signature_status: str
    team_identifier: str
    spctl_status: str


def env_flag(name: str, env: Mapping[str, str] | None = None) -> bool:
    value = (env or os.environ).get(name, "")
    return value.strip().lower() in TRUE_VALUES


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _current_platform_slug() -> str | None:
    system = sys.platform
    machine = platform.machine().lower()
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "darwin-arm64"
    if system == "darwin" and machine in {"x86_64", "amd64"}:
        return "darwin-x64"
    return None


def _vendored_source(
    env: Mapping[str, str] | None = None,
) -> tuple[Path, str] | None:
    if env_flag("LANTAI_DISABLE_VENDORED_OFFICECLI", env):
        return None

    slug = _current_platform_slug()
    if not slug:
        return None

    path = (
        Path(__file__).resolve().parent
        / "assets"
        / "officecli"
        / slug
        / "officecli"
    )
    return path, f"vendored:{slug}"


def _officecli_assets_root() -> Path:
    return Path(__file__).resolve().parent / "assets" / "officecli"


def _metadata_path_for_source(path: Path, source: str) -> Path | None:
    if source.startswith("vendored:"):
        return path.parent / "METADATA.txt"
    return None


def _attribution_paths_for_source(
    path: Path,
    source: str,
    env: Mapping[str, str],
) -> tuple[Path | None, Path | None]:
    """Resolve the exact upstream license and notice shipped with OfficeCLI."""
    if source.startswith("vendored:"):
        return path.parent / "LICENSE", path.parent / "NOTICE"
    if source in OFFICECLI_ENV_KEYS:
        license_raw = env.get("LANTAI_OFFICECLI_LICENSE_FILE", "").strip()
        notice_raw = env.get("LANTAI_OFFICECLI_NOTICE_FILE", "").strip()
        return (
            Path(license_raw).expanduser() if license_raw else None,
            Path(notice_raw).expanduser() if notice_raw else None,
        )
    return None, None


def _validate_attribution_file(
    path: Path | None,
    *,
    label: str,
    marker: str,
) -> str:
    if path is None or not path.is_file():
        raise RuntimeError(f"OfficeCLI release attribution is missing {label}")
    text = path.read_text(encoding="utf-8")
    if marker not in text:
        raise RuntimeError(
            f"OfficeCLI {label} does not contain expected upstream marker: {marker}",
        )
    return _sha256_file(path)


def _parse_metadata(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"Missing OfficeCLI metadata: {path}")
    metadata: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in raw_line:
            continue
        key, _, value = raw_line.partition(":")
        metadata[key.strip().lower()] = value.strip()
    return metadata


def _metadata_arch(metadata: Mapping[str, str]) -> str:
    platform_text = metadata.get("platform", "")
    if "arm64" in platform_text:
        return "arm64"
    if "x64" in platform_text or "x86_64" in platform_text:
        return "x64"
    return metadata.get("arch", "")


def _external_metadata(env: Mapping[str, str]) -> dict[str, str]:
    return {
        "version": env.get("LANTAI_OFFICECLI_VERSION", "").strip(),
        "sha256": env.get("LANTAI_OFFICECLI_SHA256", "").strip(),
        "arch": env.get("LANTAI_OFFICECLI_ARCH", "").strip(),
        "license": env.get("LANTAI_OFFICECLI_LICENSE", "").strip(),
        "source": env.get("LANTAI_OFFICECLI_SOURCE_URL", "").strip(),
    }


def _candidate_sources(
    env: Mapping[str, str] | None = None,
) -> list[tuple[Path, str]]:
    values = env or os.environ
    candidates: list[tuple[Path, str]] = []
    external_allowed = env_flag("LANTAI_ALLOW_EXTERNAL_OFFICECLI", values)

    for key in OFFICECLI_ENV_KEYS:
        raw = values.get(key, "").strip()
        if raw:
            path = Path(raw).expanduser()
            if _path_is_within(path, _officecli_assets_root()):
                candidates.append((path, f"vendored:{path.parent.name}"))
                continue
            if not external_allowed and not _path_is_within(
                path,
                _officecli_assets_root(),
            ):
                raise PermissionError(
                    "external OfficeCLI paths are disabled for release builds; "
                    "set LANTAI_ALLOW_EXTERNAL_OFFICECLI=1 only for a fixed, "
                    "hash-pinned artifact source.",
                )
            candidates.append((path, key))

    vendored = _vendored_source(values)
    if vendored is not None:
        candidates.append(vendored)

    which = None
    if env_flag("LANTAI_ALLOW_PATH_OFFICECLI_FOR_LOCAL_BUILD", values):
        which = shutil.which("officecli")
    if which:
        candidates.append((Path(which), "PATH"))

    return candidates


def _blocked_path_fallback(env: Mapping[str, str] | None = None) -> Path | None:
    values = env or os.environ
    if env_flag("LANTAI_ALLOW_PATH_OFFICECLI_FOR_LOCAL_BUILD", values):
        return None
    which = shutil.which("officecli")
    return Path(which) if which else None


def resolve_officecli_source(
    *,
    required: bool,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, str] | None:
    missing: list[str] = []

    for candidate, source in _candidate_sources(env):
        if candidate.is_file():
            return candidate.resolve(), source
        missing.append(f"{source}={candidate}")

    blocked_path = _blocked_path_fallback(env)
    if required and blocked_path is not None:
        raise PermissionError(
            "PATH OfficeCLI fallback is disabled for release builds. "
            "Use the vendored binary or set "
            "LANTAI_ALLOW_PATH_OFFICECLI_FOR_LOCAL_BUILD=1 only for a local "
            f"smoke build. PATH={blocked_path}",
        )

    if required:
        detail = f" Checked: {', '.join(missing)}." if missing else ""
        raise FileNotFoundError(
            "OfficeCLI binary not found. Use the vendored OfficeCLI artifact "
            "or set LANTAI_OFFICECLI_BINARY with LANTAI_ALLOW_EXTERNAL_OFFICECLI=1 "
            "and pinned SHA/version/arch/license metadata before building."
            f"{detail}",
        )
    return None


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def smoke_test_officecli(path: Path, timeout: int = 8) -> str:
    env = os.environ.copy()
    env.setdefault("OFFICECLI_SKIP_UPDATE", "1")
    # Current OfficeCLI builds may rewrite their own executable even when the
    # updater is disabled. Never execute the vendored source or a signed app
    # resource in place; a disposable copy keeps provenance and signatures
    # stable while retaining a real executable smoke test.
    with tempfile.TemporaryDirectory(prefix="lantai-officecli-smoke-") as temp_dir:
        smoke_path = Path(temp_dir) / "officecli"
        shutil.copy2(path, smoke_path)
        _make_executable(smoke_path)
        proc = subprocess.run(
            [str(smoke_path), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    output = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part.strip()
    ).strip()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Bundled OfficeCLI smoke test failed with code {proc.returncode}: "
            f"{output}",
        )
    return output.splitlines()[0].strip() if output else "unknown"


def _detect_macho_archs(path: Path) -> set[str]:
    if sys.platform != "darwin":
        return set()

    lipo = shutil.which("lipo") or "/usr/bin/lipo"
    if Path(lipo).exists():
        result = subprocess.run(
            [lipo, "-archs", str(path)],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return set(result.stdout.split())

    result = subprocess.run(
        ["file", str(path)],
        capture_output=True,
        check=False,
        text=True,
    )
    output = f"{result.stdout}\n{result.stderr}"
    archs: set[str] = set()
    if "arm64" in output:
        archs.add("arm64")
    if "x86_64" in output:
        archs.add("x64")
    return archs


def _validate_arch(path: Path, expected_arch: str) -> None:
    if not expected_arch or expected_arch == "local-script" or sys.platform != "darwin":
        return
    archs = _detect_macho_archs(path)
    if not archs:
        raise RuntimeError(f"OfficeCLI architecture could not be verified: {path}")
    expected = "x64" if expected_arch in {"x86_64", "amd64"} else expected_arch
    if expected == "universal":
        required = {"arm64", "x64"}
        if not required.issubset(archs):
            raise RuntimeError(
                f"OfficeCLI architecture mismatch: expected universal, got {sorted(archs)}",
            )
        return
    if archs != {expected}:
        raise RuntimeError(
            f"OfficeCLI architecture mismatch: expected {expected}, got {sorted(archs)}",
        )


def _codesign_summary(path: Path) -> tuple[str, str]:
    if sys.platform != "darwin":
        return "not-applicable", ""
    result = subprocess.run(
        ["codesign", "-dvv", str(path)],
        capture_output=True,
        check=False,
        text=True,
    )
    output = f"{result.stdout}\n{result.stderr}"
    signature = ""
    team_identifier = ""
    for line in output.splitlines():
        if line.startswith("Signature="):
            signature = line.partition("=")[2].strip()
        elif line.startswith("Authority=Developer ID Application:"):
            # Recent codesign output for a valid standalone Mach-O does not
            # include `Signature=`. The Developer ID authority is the
            # portable evidence of a release signature in that form.
            signature = signature or "developer-id"
        elif line.startswith("TeamIdentifier="):
            team_identifier = line.partition("=")[2].strip()
    if result.returncode != 0:
        signature = "invalid"
    return signature or "unknown", team_identifier


def _spctl_status(path: Path) -> str:
    if sys.platform != "darwin":
        return "not-applicable"
    result = subprocess.run(
        ["spctl", "--assess", "--type", "execute", "--verbose=4", str(path)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode == 0:
        return "accepted"
    output = f"{result.stdout}\n{result.stderr}".lower()
    # spctl assesses application bundles, not standalone Developer ID-signed
    # CLI executables. Keep the distinction in provenance instead of treating
    # that category error as a failed signature or notarization check.
    if "does not seem to be an app" in output:
        return "not-applicable"
    return "rejected"


def _metadata_for_source(
    path: Path,
    source: str,
    env: Mapping[str, str],
) -> tuple[dict[str, str], bool]:
    if source.startswith("vendored:"):
        metadata_path = _metadata_path_for_source(path, source)
        if metadata_path is None:
            raise RuntimeError(f"OfficeCLI vendored metadata path unavailable: {path}")
        return _parse_metadata(metadata_path), True

    if source in OFFICECLI_ENV_KEYS:
        metadata = _external_metadata(env)
        missing = [
            key
            for key in ("version", "sha256", "arch", "license", "source")
            if not metadata.get(key)
        ]
        if missing and not env_flag("LANTAI_ALLOW_UNVERIFIED_OFFICECLI", env):
            raise RuntimeError(
                "External OfficeCLI artifacts require pinned metadata: "
                + ", ".join(missing),
            )
        return metadata, not missing

    if source == "PATH":
        return {
            "version": "",
            "sha256": "",
            "arch": "local-script",
            "license": "",
        }, False

    return {}, False


def validate_officecli_source(
    path: Path,
    source: str,
    *,
    env: Mapping[str, str] | None = None,
    smoke: bool = True,
) -> OfficeCliValidation:
    values = env or os.environ
    allow_unverified = env_flag("LANTAI_ALLOW_UNVERIFIED_OFFICECLI", values)
    metadata, metadata_complete = _metadata_for_source(path, source, values)
    sha256 = _sha256_file(path)
    expected_sha = metadata.get("sha256", "").lower()
    if expected_sha and not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise RuntimeError(f"OfficeCLI SHA256 metadata is invalid: {expected_sha!r}")
    if expected_sha and sha256 != expected_sha:
        raise RuntimeError(
            f"OfficeCLI SHA256 mismatch: expected {expected_sha}, got {sha256}",
        )

    expected_version = metadata.get("version", "")
    version = smoke_test_officecli(path) if smoke else expected_version or "unknown"
    if expected_version and expected_version not in version:
        raise RuntimeError(
            f"OfficeCLI version mismatch: expected {expected_version}, got {version}",
        )

    expected_arch = metadata.get("arch", "") or _metadata_arch(metadata)
    _validate_arch(path, expected_arch)

    signature_status, team_identifier = _codesign_summary(path)
    spctl_status = _spctl_status(path)
    releasable = metadata_complete and source != "PATH"
    if allow_unverified:
        releasable = False
    elif sys.platform == "darwin" and releasable:
        if signature_status in {"adhoc", "invalid", "unknown"}:
            raise RuntimeError(
                f"OfficeCLI is not release-signed: Signature={signature_status}",
            )
        if not team_identifier or team_identifier == "not set":
            raise RuntimeError("OfficeCLI release signature has no TeamIdentifier")
        if spctl_status not in {"accepted", "not-applicable"}:
            raise RuntimeError("OfficeCLI failed Gatekeeper/notarization assessment")

    license_name = metadata.get("license", "")
    if releasable and not license_name:
        raise RuntimeError("OfficeCLI release metadata is missing license")
    source_url = metadata.get("source", "")
    if releasable and not source_url:
        raise RuntimeError("OfficeCLI release metadata is missing source")

    license_path, notice_path = _attribution_paths_for_source(path, source, values)
    license_sha256 = ""
    notice_sha256 = ""
    if releasable:
        license_sha256 = _validate_attribution_file(
            license_path,
            label="LICENSE",
            marker="Apache License",
        )
        notice_sha256 = _validate_attribution_file(
            notice_path,
            label="NOTICE",
            marker="Copyright 2026 OfficeCLI",
        )
    else:
        if license_path is not None and license_path.is_file():
            license_sha256 = _sha256_file(license_path)
        if notice_path is not None and notice_path.is_file():
            notice_sha256 = _sha256_file(notice_path)

    return OfficeCliValidation(
        source_kind=source,
        source_path=path,
        source_url=source_url,
        sha256=sha256,
        version=version,
        expected_version=expected_version,
        expected_arch=expected_arch,
        license=license_name,
        license_path=license_path,
        notice_path=notice_path,
        license_sha256=license_sha256,
        notice_sha256=notice_sha256,
        releasable=releasable,
        signature_status=signature_status,
        team_identifier=team_identifier,
        spctl_status=spctl_status,
    )


def _write_provenance(dest: Path, validation: OfficeCliValidation) -> None:
    payload: dict[str, Any] = {
        "schema": "com.lantai.officecli-bundle-provenance.v1",
        "source_kind": validation.source_kind,
        "source_path": str(validation.source_path),
        "source_url": validation.source_url,
        "sha256": validation.sha256,
        "version": validation.version,
        "expected_version": validation.expected_version,
        "expected_arch": validation.expected_arch,
        "license": validation.license,
        "license_file": LICENSE_BUNDLE_FILENAME if validation.license_sha256 else "",
        "notice_file": NOTICE_BUNDLE_FILENAME if validation.notice_sha256 else "",
        "license_sha256": validation.license_sha256,
        "notice_sha256": validation.notice_sha256,
        "releasable": validation.releasable,
        "signature_status": validation.signature_status,
        "team_identifier": validation.team_identifier,
        "spctl_status": validation.spctl_status,
    }
    (dest.parent / PROVENANCE_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def bundle_officecli(
    backend_dist_dir: Path,
    *,
    required: bool,
    env: Mapping[str, str] | None = None,
    smoke: bool = True,
) -> Path | None:
    resolved = resolve_officecli_source(required=required, env=env)
    if resolved is None:
        return None

    source, source_kind = resolved
    validation = validate_officecli_source(
        source,
        source_kind,
        env=env,
        smoke=smoke,
    )
    dest_dir = backend_dist_dir / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "officecli"
    shutil.copyfile(source, dest)
    _make_executable(dest)
    if validation.license_path is not None and validation.license_path.is_file():
        shutil.copyfile(
            validation.license_path,
            dest_dir / LICENSE_BUNDLE_FILENAME,
        )
    if validation.notice_path is not None and validation.notice_path.is_file():
        shutil.copyfile(
            validation.notice_path,
            dest_dir / NOTICE_BUNDLE_FILENAME,
        )
    _write_provenance(dest, validation)

    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy OfficeCLI into a frozen lantai-server/bin directory.",
    )
    parser.add_argument("backend_dist_dir", type=Path)
    parser.add_argument(
        "--required",
        action="store_true",
        help="fail if OfficeCLI cannot be found",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="copy without running officecli --version",
    )
    args = parser.parse_args(argv)

    required = args.required or env_flag("LANTAI_REQUIRE_BUNDLED_OFFICECLI")
    try:
        resolved = resolve_officecli_source(required=required)
        if resolved is None:
            print("OfficeCLI bundle skipped: no officecli binary found.")
            return 0
        source, source_kind = resolved
        dest = bundle_officecli(
            args.backend_dist_dir,
            required=required,
            env={**os.environ, "LANTAI_OFFICECLI_BINARY": str(source)},
            smoke=not args.skip_smoke,
        )
        print(f"Bundled OfficeCLI from {source_kind}: {source} -> {dest}")
        return 0
    except Exception as exc:
        if not required:
            print(
                "OfficeCLI bundle skipped: optional candidate did not pass "
                f"release validation ({exc}).",
            )
            return 0
        print(f"OfficeCLI bundle failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
