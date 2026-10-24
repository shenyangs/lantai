#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate and record release artifact provenance.

SBOM validation uses the existing jsonschema build dependency with offline
vendored schemas. No schema network retrieval is performed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.packaged_runtime_evidence import load_and_validate
    from scripts.check_release_supply_chain import validate_declared_sbom
except ModuleNotFoundError:  # direct script execution from scripts/
    from packaged_runtime_evidence import load_and_validate
    from check_release_supply_chain import validate_declared_sbom


SCHEMA = "com.lantai.release-artifact-manifest.v1"
SUPPLY_CHAIN_SCHEMA = "com.lantai.release-supply-chain-manifest.v1"


def _die(message: str) -> None:
    raise SystemExit(message)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha512_file_base64(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def _file_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        _die(f"Expected release artifact file: {path}")
    return {
        "name": path.name,
        "path": str(path),
        "sha256": _sha256_file(path),
        "sha512": _sha512_file_base64(path),
        "size": path.stat().st_size,
    }


def _load_package_metadata(root: Path) -> dict[str, Any]:
    package_path = root / "console/package.json"
    package = _read_json(package_path)
    version = str(package.get("version") or "").strip()
    if not version:
        _die(f"{package_path} is missing version")
    return {
        "name": package.get("name"),
        "version": version,
        "productName": package.get("build", {}).get("productName"),
    }


def _read_app_info_plist(app_path: Path) -> dict[str, Any]:
    plist_path = app_path / "Contents/Info.plist"
    if not plist_path.is_file():
        _die(f"Missing Info.plist: {plist_path}")
    with plist_path.open("rb") as handle:
        info = plistlib.load(handle)
    if not isinstance(info, dict):
        _die(f"Invalid Info.plist: {plist_path}")
    return info


def _detect_executable_archs(executable: Path) -> list[str]:
    if not executable.is_file():
        _die(f"Missing app executable: {executable}")

    lipo = shutil.which("lipo") or "/usr/bin/lipo"
    if Path(lipo).exists():
        result = subprocess.run(
            [lipo, "-archs", str(executable)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            archs = [part.strip() for part in result.stdout.split() if part.strip()]
            if archs:
                return archs

    result = subprocess.run(
        ["file", str(executable)],
        check=False,
        capture_output=True,
        text=True,
    )
    output = f"{result.stdout}\n{result.stderr}"
    archs: list[str] = []
    if "arm64" in output:
        archs.append("arm64")
    if "x86_64" in output:
        archs.append("x86_64")
    if archs:
        return archs
    _die(f"Could not detect Mach-O architecture for: {executable}")


def _expected_archs(arch: str) -> set[str]:
    if arch == "arm64":
        return {"arm64"}
    if arch == "x64":
        return {"x86_64"}
    if arch == "universal":
        return {"arm64", "x86_64"}
    _die(f"Unsupported release arch: {arch}")


def _validate_app(app_path: Path, version: str, arch: str) -> dict[str, Any]:
    if not app_path.is_dir():
        _die(f"Expected app bundle directory: {app_path}")

    info = _read_app_info_plist(app_path)
    short_version = str(info.get("CFBundleShortVersionString") or "").strip()
    bundle_version = str(info.get("CFBundleVersion") or "").strip()
    if version not in {short_version, bundle_version}:
        _die(
            "App Info.plist version mismatch: "
            f"expected {version}, got short={short_version!r}, bundle={bundle_version!r}",
        )

    executable_name = str(info.get("CFBundleExecutable") or "").strip()
    if not executable_name:
        _die(f"Info.plist is missing CFBundleExecutable: {app_path}")
    executable = app_path / "Contents/MacOS" / executable_name
    actual_archs = set(_detect_executable_archs(executable))
    expected_archs = _expected_archs(arch)
    if actual_archs != expected_archs:
        _die(
            "App executable architecture mismatch: "
            f"expected {sorted(expected_archs)}, got {sorted(actual_archs)}",
        )

    return {
        "name": app_path.name,
        "path": str(app_path),
        "bundleIdentifier": info.get("CFBundleIdentifier"),
        "shortVersion": short_version,
        "bundleVersion": bundle_version,
        "executable": executable_name,
        "archs": sorted(actual_archs),
    }


def _redact_identifier(value: str) -> str:
    value = value.strip()
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _collect_notary_request_ids(notary_json_dir: Path | None) -> list[dict[str, str]]:
    if not notary_json_dir or not notary_json_dir.is_dir():
        return []
    records: list[dict[str, str]] = []
    for path in sorted(notary_json_dir.glob("*.json")):
        try:
            data = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        request_id = str(data.get("id") or data.get("submissionId") or "").strip()
        if not request_id:
            continue
        records.append(
            {
                "label": path.stem,
                "requestIdRedacted": _redact_identifier(request_id),
                "requestIdSHA256": hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
                "status": data.get("status"),
            },
        )
    return records


def _load_dependency_manifest(path: Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    if not path.is_file():
        _die(f"Missing dependency manifest: {path}")
    return _read_json(path)


def _validate_supply_chain_payload(
    manifest: dict[str, Any],
    *,
    source_sha: str,
    version: str,
    arch: str,
    release_dir: Path,
    allow_unreleasable: bool = False,
) -> None:
    if manifest.get("schema") != SUPPLY_CHAIN_SCHEMA:
        _die("Unexpected release supply-chain manifest schema")
    if manifest.get("source", {}).get("sha") != source_sha:
        _die("Supply-chain manifest source SHA mismatch")
    release = manifest.get("release") or {}
    if release.get("version") != version:
        _die("Supply-chain manifest release version mismatch")
    if release.get("arch") != arch:
        _die("Supply-chain manifest release arch mismatch")

    sbom = manifest.get("sbom") or {}
    sbom_path = Path(str(sbom.get("path") or ""))
    if not sbom_path.is_absolute():
        sbom_path = release_dir / sbom_path
    if not sbom_path.is_file():
        _die(f"Supply-chain SBOM path is missing: {sbom_path}")
    if sbom.get("sha256") != _sha256_file(sbom_path):
        _die("Supply-chain SBOM sha256 mismatch")
    validate_declared_sbom(_read_json(sbom_path), sbom)

    binaries = manifest.get("binarySources")
    if not isinstance(binaries, list) or not binaries:
        _die("Supply-chain manifest is missing binarySources")
    for entry in binaries:
        if not isinstance(entry, dict):
            _die("Supply-chain manifest contains an invalid binarySources entry")
        name = entry.get("name") or "<unknown>"
        for field in ("version", "arch", "sha256", "source", "license"):
            if not entry.get(field):
                _die(f"Supply-chain binary entry {name} missing {field}")
        source = entry.get("source")
        if not isinstance(source, dict) or not source.get("type"):
            _die(f"Supply-chain binary entry {name} has invalid source")
        if source.get("type") != "git" and not source.get("url") and not allow_unreleasable:
            _die(f"Supply-chain binary entry {name} missing source URL")
        if entry.get("releasable") is False and not allow_unreleasable:
            _die(f"Supply-chain binary entry {name} is marked unreleasable")
        binary_path = Path(str(entry.get("path") or ""))
        if not binary_path.is_absolute():
            binary_path = release_dir / binary_path
        if binary_path.is_file() and entry.get("sha256") != _sha256_file(binary_path):
            _die(f"Supply-chain binary entry hash mismatch: {binary_path}")


def _load_supply_chain_manifest(
    path: Path | None,
    *,
    source_sha: str,
    version: str,
    arch: str,
    release_dir: Path,
    allow_unreleasable: bool = False,
) -> dict[str, Any]:
    if not path:
        _die("Missing supply-chain manifest")
    if not path.is_file():
        _die(f"Missing supply-chain manifest: {path}")
    manifest = _read_json(path)
    _validate_supply_chain_payload(
        manifest,
        source_sha=source_sha,
        version=version,
        arch=arch,
        release_dir=release_dir,
        allow_unreleasable=allow_unreleasable,
    )
    return manifest


def write_manifest(args: argparse.Namespace) -> None:
    package = _load_package_metadata(args.root)
    if package["version"] != args.version:
        _die(
            "console/package.json version mismatch: "
            f"expected {args.version}, got {package['version']}",
        )

    try:
        from scripts.release_build_identity import load_app_identity, identity_digest
    except ModuleNotFoundError:
        from release_build_identity import load_app_identity, identity_digest
    if not args.app:
        _die("Release artifact evidence requires the actual App build identity")
    build_identity = load_app_identity(args.app, args.source_sha, args.version)
    artifacts: dict[str, Any] = {}
    if args.app:
        artifacts["app"] = _validate_app(args.app, args.version, args.arch)
    if args.zip:
        artifacts["zip"] = _file_metadata(args.zip)
    if args.dmg:
        artifacts["dmg"] = _file_metadata(args.dmg)
    packaged_runtime_evidence = None
    if args.packaged_runtime_evidence:
        if not args.app:
            _die("Packaged Runtime evidence validation requires --app")
        evidence_payload = load_and_validate(
            args.packaged_runtime_evidence,
            expected_source_sha=args.source_sha,
            expected_version=args.version,
            app=args.app,
        )
        packaged_runtime_evidence = {
            "file": _file_metadata(args.packaged_runtime_evidence),
            "report": evidence_payload,
        }
    manifest = {
        "schema": SCHEMA,
        "source": {
            "sha": args.source_sha,
            "dirty": args.source_dirty == "1",
            "tag": args.release_tag or None,
            "sourceBranch": build_identity["sourceBranch"],
            "canonicalMain": build_identity["canonicalMain"],
        },
        "release": {
            "version": args.version,
            "arch": args.arch,
            "productName": args.product_name,
            "directory": str(args.release_dir),
            "artifactOnly": build_identity.get("artifactOnly", not build_identity["canonicalMain"]),
            "candidateId": build_identity.get("candidateId"),
        },
        "buildIdentity": build_identity,
        "buildIdentitySha256": identity_digest(build_identity),
        "package": package,
        "dependencyManifest": _load_dependency_manifest(args.dependency_manifest),
        "supplyChainManifest": _load_supply_chain_manifest(
            args.supply_chain_manifest,
            source_sha=args.source_sha,
            version=args.version,
            arch=args.arch,
            release_dir=args.release_dir,
            allow_unreleasable=args.allow_unreleasable_binaries,
        ),
        "packagedRuntimeCompletenessEvidence": packaged_runtime_evidence,
        "signing": {
            "mode": args.signing_mode,
            "identityClass": args.codesign_identity_class,
        },
        "notarization": {
            "requestIds": _collect_notary_request_ids(args.notary_json_dir),
        },
        "artifacts": artifacts,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_skip_manifest(args: argparse.Namespace) -> None:
    manifest = _read_json(args.manifest)
    if manifest.get("schema") != SCHEMA:
        _die(f"Unexpected artifact manifest schema in {args.manifest}")
    source = manifest.get("source") or {}
    release = manifest.get("release") or {}
    if source.get("sha") != args.source_sha:
        _die(
            "Artifact manifest source SHA mismatch: "
            f"expected {args.source_sha}, got {source.get('sha')}",
        )
    if release.get("version") != args.version:
        _die(
            "Artifact manifest version mismatch: "
            f"expected {args.version}, got {release.get('version')}",
        )
    if release.get("arch") != args.arch:
        _die(
            "Artifact manifest arch mismatch: "
            f"expected {args.arch}, got {release.get('arch')}",
        )

    if release.get("platform") == "win32":
        # Resolve transported files from the admitted directory, never from a
        # former builder's absolute paths. This is metadata/hash validation;
        # public Windows admission separately reruns the OS trust verifier.
        try:
            from scripts.check_release_upload import admit_windows_release
        except ModuleNotFoundError:
            from check_release_upload import admit_windows_release
        try:
            admit_windows_release(args.release_dir, source_sha=args.source_sha,
                                  version=args.version, public=False)
        except (ValueError, OSError) as exc:
            _die(f"Windows artifact admission failed: {exc}")
        return

    release_dir = args.release_dir
    artifacts = manifest.get("artifacts") or {}
    for artifact_name in ("zip", "dmg"):
        artifact = artifacts.get(artifact_name)
        if not artifact:
            continue
        path = Path(artifact.get("path", ""))
        if not path.is_absolute():
            path = release_dir / path
        if not path.exists():
            _die(f"Manifest artifact path is missing: {path}")
        if path.is_file() and artifact.get("sha256") != _sha256_file(path):
            _die(f"Manifest artifact hash mismatch: {path}")
    app = artifacts.get("app")
    app_path = None
    if app:
        path = Path(app.get("path", ""))
        if not path.is_absolute():
            path = release_dir / path
        if not path.is_dir():
            _die(f"Manifest app path is missing: {path}")
        app_path = path

    packaged_runtime_evidence = manifest.get("packagedRuntimeCompletenessEvidence")
    if packaged_runtime_evidence:
        if not isinstance(packaged_runtime_evidence, dict) or app_path is None:
            _die("Artifact manifest contains invalid packaged Runtime evidence")
        evidence_file = packaged_runtime_evidence.get("file") or {}
        evidence_path = Path(str(evidence_file.get("path") or ""))
        if not evidence_path.is_absolute():
            evidence_path = release_dir / evidence_path
        if not evidence_path.is_file():
            _die(f"Packaged Runtime evidence path is missing: {evidence_path}")
        if evidence_file.get("sha256") != _sha256_file(evidence_path):
            _die("Packaged Runtime evidence hash mismatch")
        evidence_payload = load_and_validate(
            evidence_path,
            expected_source_sha=args.source_sha,
            expected_version=args.version,
            app=app_path,
        )
        if packaged_runtime_evidence.get("report") != evidence_payload:
            _die("Packaged Runtime evidence payload differs from the manifest")

    supply_chain = manifest.get("supplyChainManifest")
    if not isinstance(supply_chain, dict):
        _die("Artifact manifest missing supplyChainManifest")
    _validate_supply_chain_payload(
        supply_chain,
        source_sha=args.source_sha,
        version=args.version,
        arch=args.arch,
        release_dir=release_dir,
        allow_unreleasable=args.allow_unreleasable_binaries,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("write-manifest")
    manifest.add_argument("--root", required=True, type=Path)
    manifest.add_argument("--release-dir", required=True, type=Path)
    manifest.add_argument("--output", required=True, type=Path)
    manifest.add_argument("--source-sha", required=True)
    manifest.add_argument("--source-dirty", required=True, choices=("0", "1"))
    manifest.add_argument("--release-tag", default="")
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--arch", required=True, choices=("arm64", "x64", "universal"))
    manifest.add_argument("--product-name", required=True)
    manifest.add_argument("--signing-mode", required=True)
    manifest.add_argument("--codesign-identity-class", required=True)
    manifest.add_argument("--dependency-manifest", type=Path)
    manifest.add_argument("--supply-chain-manifest", required=True, type=Path)
    manifest.add_argument("--packaged-runtime-evidence", type=Path)
    manifest.add_argument("--allow-unreleasable-binaries", action="store_true")
    manifest.add_argument("--notary-json-dir", type=Path)
    manifest.add_argument("--app", type=Path)
    manifest.add_argument("--zip", type=Path)
    manifest.add_argument("--dmg", type=Path)
    manifest.set_defaults(func=write_manifest)

    skip = subparsers.add_parser("validate-skip-manifest")
    skip.add_argument("--manifest", required=True, type=Path)
    skip.add_argument("--release-dir", required=True, type=Path)
    skip.add_argument("--source-sha", required=True)
    skip.add_argument("--version", required=True)
    skip.add_argument("--arch", required=True, choices=("arm64", "x64", "universal"))
    skip.add_argument("--allow-unreleasable-binaries", action="store_true")
    skip.set_defaults(func=validate_skip_manifest)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - defensive release script output
        print(f"Release provenance validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
