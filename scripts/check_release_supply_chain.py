#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write and validate release SBOM and binary source manifests.

Schema validation uses the existing jsonschema build dependency and vendored
CycloneDX schemas; validation never retrieves schemas from the network.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
from functools import lru_cache
from email.parser import BytesParser
from email.policy import default as metadata_policy
import json
import plistlib
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote


SCHEMA = "com.lantai.release-supply-chain-manifest.v1"
SBOM_SPEC_VERSION = "1.5"
OFFICECLI_PROVENANCE_SCHEMA = "com.lantai.officecli-bundle-provenance.v1"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _file_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        _die(f"Missing release supply-chain file: {path}")
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _codesign_summary(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["codesign", "-dvv", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    summary: dict[str, Any] = {
        "status": "signed" if result.returncode == 0 else "not-signed",
    }
    authorities: list[str] = []
    for line in text.splitlines():
        if line.startswith("Authority="):
            authorities.append(line.partition("=")[2])
        elif line.startswith("TeamIdentifier="):
            summary["teamIdentifier"] = line.partition("=")[2]
        elif line.startswith("Timestamp="):
            summary["timestamp"] = line.partition("=")[2]
        elif line.startswith("Signature="):
            summary["signature"] = line.partition("=")[2]
    if authorities:
        summary["authorities"] = authorities
    return summary


def _load_package_metadata(root: Path, version: str) -> dict[str, Any]:
    package_path = root / "console/package.json"
    package = _read_json(package_path)
    if package.get("version") != version:
        _die(
            "console/package.json version mismatch: "
            f"expected {version}, got {package.get('version')}",
        )
    return {
        "name": package.get("name") or "lantai-console",
        "version": version,
        "productName": package.get("build", {}).get("productName") or "Lantai",
        "license": package.get("license") or "NOASSERTION",
    }


def _lockfile_metadata(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    if not path.is_file():
        _die(f"Missing lockfile for SBOM: {relative}")
    return {
        "path": relative,
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


@lru_cache(maxsize=1)
def _sbom_schemas() -> dict[str, Any]:
    directory = Path(__file__).resolve().parent / "schemas" / "cyclonedx-1.5"
    return {name: _read_json(directory / name) for name in (
        "bom-1.5.schema.json", "spdx.schema.json", "jsf-0.82.schema.json",
    )}


@lru_cache(maxsize=1)
def _spdx_identifiers() -> frozenset[str]:
    return frozenset(_sbom_schemas()["spdx.schema.json"]["enum"])


def _validate_sbom_schema(sbom: dict[str, Any]) -> None:
    # Keep identity diagnostics and stricter release invariants in addition to schema.
    _validate_sbom_references(sbom)
    if sbom.get("specVersion") != SBOM_SPEC_VERSION:
        _die("SBOM specification version mismatch")
    try:
        from jsonschema import Draft7Validator, FormatChecker
        from referencing import Registry, Resource
        from referencing.exceptions import NoSuchResource
    except ImportError:
        _die("SBOM validation requires the project's jsonschema build dependency")

    def refuse_remote(uri: str) -> Any:
        raise NoSuchResource(ref=uri)

    registry = Registry(retrieve=refuse_remote)
    schemas = _sbom_schemas()
    for name, schema in schemas.items():
        resource = Resource.from_contents(schema)
        for uri in (schema.get("$id"), "http://cyclonedx.org/schema/" + name,
                    "https://cyclonedx.org/schema/" + name):
            if uri:
                registry = registry.with_resource(uri, resource)
    validator = Draft7Validator(schemas["bom-1.5.schema.json"], registry=registry,
                                format_checker=FormatChecker())
    error = next(validator.iter_errors(sbom), None)
    if error is not None:
        path = "/".join(str(part) for part in error.absolute_path) or "<root>"
        _die(f"SBOM schema validation failed at {path}: {error.message}")


def validate_declared_sbom(payload: dict[str, Any], declaration: dict[str, Any]) -> None:
    """Validate the SBOM body and its enclosing manifest's format declaration."""
    _validate_sbom_schema(payload)
    # Legacy manifests may omit these declarations, but cannot contradict the body.
    if declaration.get("format", "CycloneDX") != "CycloneDX" or declaration.get(
        "specVersion", SBOM_SPEC_VERSION,
    ) != SBOM_SPEC_VERSION:
        _die("Supply-chain manifest SBOM format or specification version mismatch")


def _license_entry(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, str) or not value.strip():
        return []
    license_text = value.strip()
    if license_text in _spdx_identifiers():
        return [{"license": {"id": license_text}}]
    return [{"license": {"name": license_text}}]


def _integrity_hashes(integrity: object) -> list[dict[str, str]]:
    if not isinstance(integrity, str):
        return []
    hashes: list[dict[str, str]] = []
    for part in integrity.split():
        if "-" not in part:
            continue
        algorithm, _, encoded = part.partition("-")
        try:
            decoded = base64.b64decode(encoded, validate=True).hex()
        except (ValueError, binascii.Error):
            continue
        hashes.append({"alg": algorithm.upper().replace("SHA", "SHA-"), "content": decoded})
    return hashes


def _npm_name_from_package_path(path: str) -> str | None:
    if "node_modules/" not in path:
        return None
    tail = path.rsplit("node_modules/", 1)[1]
    parts = [part for part in tail.split("/") if part]
    if not parts:
        return None
    if parts[0].startswith("@") and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def _purl_npm(name: str, version: str) -> str:
    if name.startswith("@") and "/" in name:
        scope, package = name.split("/", 1)
        return f"pkg:npm/{quote(scope, safe='')}/{quote(package, safe='')}@{quote(version, safe='')}"
    return f"pkg:npm/{quote(name, safe='')}@{quote(version, safe='')}"


def _purl_pypi(name: str, version: str) -> str:
    return f"pkg:pypi/{quote(name, safe='')}@{quote(version, safe='')}"


def _npm_components(root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    lock = _read_json(root / "console/package-lock.json")
    components: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for package_path, package in sorted(lock.get("packages", {}).items()):
        if not package_path:
            continue
        version = str(package.get("version") or "").strip()
        if not version:
            continue
        name = str(package.get("name") or _npm_name_from_package_path(package_path) or "")
        if not name:
            continue
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": f"npm:{name}@{version}#{quote(package_path, safe='')}",
            "name": name,
            "version": version,
            "purl": _purl_npm(name, version),
            "scope": "excluded" if package.get("dev") else "required",
            "properties": [
                {"name": "lantai:ecosystem", "value": "npm"},
                {"name": "lantai:lock:path", "value": package_path},
            ],
        }
        hashes = _integrity_hashes(package.get("integrity"))
        if hashes:
            component["hashes"] = hashes
        licenses = _license_entry(package.get("license"))
        if licenses:
            component["licenses"] = licenses
        resolved = package.get("resolved")
        if isinstance(resolved, str) and resolved:
            component["externalReferences"] = [
                {"type": "distribution", "url": resolved},
            ]
        components.append(component)
        by_name.setdefault(name, package)
    return components, by_name


def _extract_toml_string(block: str, key: str) -> str:
    match = re.search(rf'^{re.escape(key)}\s*=\s*"([^"]*)"', block, re.MULTILINE)
    return match.group(1) if match else ""


def _parse_inline_table(table: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for match in re.finditer(r'([A-Za-z0-9_-]+)\s*=\s*"([^"]*)"', table):
        result[match.group(1)] = match.group(2)
    for match in re.finditer(r"([A-Za-z0-9_-]+)\s*=\s*([0-9]+)", table):
        result[match.group(1)] = int(match.group(2))
    return result


def _uv_artifacts(block: str) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    sdist = re.search(r"^sdist\s*=\s*\{([^}]*)\}", block, re.MULTILINE)
    if sdist:
        artifact = _parse_inline_table(sdist.group(1))
        artifact["kind"] = "sdist"
        artifacts.append(artifact)
    wheels = re.search(r"^wheels\s*=\s*\[(.*?)^\]", block, re.MULTILINE | re.DOTALL)
    if wheels:
        for table in re.finditer(r"\{([^}]*)\}", wheels.group(1)):
            artifact = _parse_inline_table(table.group(1))
            artifact["kind"] = "wheel"
            artifacts.append(artifact)
    return artifacts


def _uv_registry(block: str) -> str:
    source = re.search(r"^source\s*=\s*\{([^}]*)\}", block, re.MULTILINE)
    if not source:
        return ""
    return str(_parse_inline_table(source.group(1)).get("registry") or "")


def _packaged_python_licenses(app: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Read exact packaged metadata without consulting the build host's packages."""
    records: dict[tuple[str, str], dict[str, Any]] = {}
    app = app.resolve()
    for path in sorted(app.rglob("*.dist-info/METADATA")):
        if not path.resolve().is_relative_to(app):
            _die(f"Packaged Python metadata outside app: {path}")
        data = path.read_bytes()
        metadata = BytesParser(policy=metadata_policy).parsebytes(data, headersonly=True)
        name = str(metadata.get("Name") or "").strip()
        version = str(metadata.get("Version") or "").strip()
        if not name or not version:
            continue
        key = (re.sub(r"[-_.]+", "-", name).lower(), version)
        expression = str(metadata.get("License-Expression") or "").strip()
        legacy = str(metadata.get("License") or "").strip()
        if expression:
            licenses = [{"expression": expression}]
        elif legacy and legacy != "UNKNOWN":
            licenses = _license_entry(legacy)
        else:
            # Preserve the classifier verbatim; a broad BSD classifier does
            # not establish any particular SPDX BSD variant.
            licenses = [{"license": {"name": str(value).strip()}}
                        for value in metadata.get_all("Classifier", [])
                        if str(value).startswith("License ::")]
        source = {"path": path.relative_to(app).as_posix(),
                  "sha256": hashlib.sha256(data).hexdigest()}
        previous = records.get(key)
        if previous is not None:
            if previous["licenses"] != licenses:
                _die(f"Conflicting packaged license metadata for {name}@{version}")
            previous["sources"].append(source)
        else:
            records[key] = {"licenses": licenses, "sources": [source]}
    return records


def _python_components(root: Path, *, app: Path | None = None) -> list[dict[str, Any]]:
    packaged = _packaged_python_licenses(app) if app is not None else {}
    text = (root / "uv.lock").read_text(encoding="utf-8")
    components: list[dict[str, Any]] = []
    for match in re.finditer(
        r"(?ms)^\[\[package\]\]\n(.*?)(?=^\[\[package\]\]|\Z)",
        text,
    ):
        block = match.group(1)
        name = _extract_toml_string(block, "name")
        version = _extract_toml_string(block, "version")
        if not name or not version:
            continue
        artifacts = _uv_artifacts(block)
        hashes: list[dict[str, str]] = []
        properties = [
            {"name": "lantai:ecosystem", "value": "python"},
            {"name": "lantai:lock:path", "value": "uv.lock"},
        ]
        registry = _uv_registry(block)
        if registry:
            properties.append({"name": "lantai:uv:registry", "value": registry})
        external_references: list[dict[str, str]] = []
        for artifact in artifacts:
            raw_hash = str(artifact.get("hash") or "")
            if raw_hash.startswith("sha256:"):
                digest = raw_hash.partition(":")[2]
                if HASH_RE.fullmatch(digest) and not hashes:
                    hashes.append({"alg": "SHA-256", "content": digest})
            url = str(artifact.get("url") or "")
            if url:
                external_references.append({"type": "distribution", "url": url})
                properties.append(
                    {
                        "name": f"lantai:uv:{artifact.get('kind', 'artifact')}:hash",
                        "value": raw_hash,
                    },
                )
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": f"pypi:{name}@{version}",
            "name": name,
            "version": version,
            "purl": _purl_pypi(name, version),
            "scope": "required",
            "properties": properties,
        }
        evidence = packaged.get((re.sub(r"[-_.]+", "-", name).lower(), version))
        if evidence is not None:
            if evidence["licenses"]:
                component["licenses"] = evidence["licenses"]
            for index, source in enumerate(evidence["sources"]):
                properties.extend({"name": f"lantai:python:metadata:{index}:{field}",
                                   "value": value} for field, value in source.items())
        if hashes:
            component["hashes"] = hashes
        if external_references:
            component["externalReferences"] = external_references
        components.append(component)
    return components


def _read_app_info(app: Path) -> dict[str, Any]:
    plist_path = app / "Contents/Info.plist"
    if not plist_path.is_file():
        _die(f"Missing app Info.plist for SBOM: {plist_path}")
    with plist_path.open("rb") as handle:
        info = plistlib.load(handle)
    if not isinstance(info, dict):
        _die(f"Invalid app Info.plist: {plist_path}")
    return info


def _electron_binary_url(version: str, arch: str) -> str:
    electron_arch = {"arm64": "arm64", "x64": "x64", "universal": "universal"}[arch]
    return (
        f"https://github.com/electron/electron/releases/download/v{version}/"
        f"electron-v{version}-darwin-{electron_arch}.zip"
    )


def _binary_entry(
    *,
    name: str,
    kind: str,
    path: Path,
    version: str,
    arch: str,
    source: dict[str, Any],
    license_name: str,
    source_sha: str,
    releasable: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = _file_metadata(path)
    entry: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "version": version,
        "arch": arch,
        "path": meta["path"],
        "sha256": meta["sha256"],
        "bytes": meta["bytes"],
        "source": source,
        "license": license_name,
        "buildSourceSha": source_sha,
        "releasable": releasable,
    }
    if extra:
        entry.update(extra)
    return entry


def _load_officecli_binary(
    app: Path,
    arch: str,
    source_sha: str,
    *,
    allow_unreleasable: bool,
) -> dict[str, Any] | None:
    path = app / "Contents/Resources/lantai-server/bin/officecli"
    provenance_path = path.parent / "officecli.provenance.json"
    if not path.is_file():
        if allow_unreleasable:
            return None
        _die(f"Missing bundled OfficeCLI binary for source manifest: {path}")
    if not provenance_path.is_file():
        _die(f"Missing bundled OfficeCLI provenance: {provenance_path}")
    provenance = _read_json(provenance_path)
    if provenance.get("schema") != OFFICECLI_PROVENANCE_SCHEMA:
        _die(f"Unexpected OfficeCLI provenance schema: {provenance_path}")
    bundled_sha = _sha256_file(path)
    source_sha256 = str(provenance.get("sha256") or "")
    if source_sha256 and not HASH_RE.fullmatch(source_sha256):
        _die("OfficeCLI provenance sha256 is invalid")

    missing = [
        field
        for field in (
            "source_url",
            "sha256",
            "version",
            "expected_arch",
            "license",
            "license_file",
            "notice_file",
            "license_sha256",
            "notice_sha256",
        )
        if not str(provenance.get(field) or "").strip()
    ]
    if missing and not allow_unreleasable:
        _die("OfficeCLI binary source manifest is missing " + ", ".join(missing))
    if not provenance.get("releasable") and not allow_unreleasable:
        _die("OfficeCLI binary is marked unreleasable in provenance")

    attribution: dict[str, dict[str, str]] = {}
    for label in ("license", "notice"):
        filename = str(provenance.get(f"{label}_file") or "")
        expected_hash = str(provenance.get(f"{label}_sha256") or "")
        if not filename or not expected_hash:
            continue
        if Path(filename).name != filename or not HASH_RE.fullmatch(expected_hash):
            _die(f"OfficeCLI {label} provenance is invalid")
        attribution_path = path.parent / filename
        if not attribution_path.is_file():
            _die(f"Missing bundled OfficeCLI {label}: {attribution_path}")
        actual_hash = _sha256_file(attribution_path)
        if actual_hash != expected_hash:
            _die(
                f"OfficeCLI {label} hash mismatch: expected {expected_hash}, "
                f"got {actual_hash}",
            )
        attribution[label] = {
            "path": str(attribution_path),
            "sha256": actual_hash,
        }

    source_url = str(provenance.get("source_url") or provenance.get("source_kind") or "")
    license_name = str(provenance.get("license") or "UNRELEASABLE")
    return _binary_entry(
        name="officecli",
        kind="external-tool",
        path=path,
        version=str(provenance.get("expected_version") or provenance.get("version") or ""),
        arch=str(provenance.get("expected_arch") or arch),
        source={
            "type": "upstream-release",
            "url": source_url,
            "sourceKind": provenance.get("source_kind"),
            "sha256": source_sha256,
        },
        license_name=license_name,
        source_sha=source_sha,
        releasable=bool(provenance.get("releasable")),
        extra={
            "sourceSha256": source_sha256,
            "bundleSha256": bundled_sha,
            "signature": _codesign_summary(path),
            "provenanceSignature": {
                "status": provenance.get("signature_status"),
                "teamIdentifier": provenance.get("team_identifier"),
                "spctl": provenance.get("spctl_status"),
            },
            "attribution": attribution,
        },
    )


def _binary_sources(
    *,
    root: Path,
    app: Path,
    package: dict[str, Any],
    npm_by_name: dict[str, dict[str, Any]],
    source_sha: str,
    version: str,
    arch: str,
    allow_unreleasable: bool,
) -> list[dict[str, Any]]:
    info = _read_app_info(app)
    executable_name = str(info.get("CFBundleExecutable") or package["productName"])
    app_version = str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "")
    if version != app_version:
        _die(f"App version mismatch for SBOM: expected {version}, got {app_version}")

    electron = npm_by_name.get("electron")
    if not electron:
        _die("console/package-lock.json is missing electron for binary source manifest")
    electron_version = str(electron.get("version") or "")
    electron_framework = (
        app / "Contents/Frameworks/Electron Framework.framework/Electron Framework"
    )
    app_executable = app / "Contents/MacOS" / executable_name
    backend = (
        app
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    mac_use_helper = (
        app
        / "Contents"
        / "Resources"
        / "mac-use"
        / "Lantai Mac Use Helper.app"
        / "Contents"
        / "MacOS"
        / "lantai-mac-use-helper"
    )

    entries = [
        _binary_entry(
            name="electron-runtime",
            kind="external-framework",
            path=electron_framework,
            version=electron_version,
            arch=arch,
            source={
                "type": "upstream-release",
                "url": _electron_binary_url(electron_version, arch),
                "package": "electron",
                "packageResolved": electron.get("resolved"),
                "packageIntegrity": electron.get("integrity"),
            },
            license_name=str(electron.get("license") or "MIT"),
            source_sha=source_sha,
        ),
        _binary_entry(
            name="electron-app-launcher",
            kind="app-executable",
            path=app_executable,
            version=version,
            arch=arch,
            source={"type": "git", "sha": source_sha},
            license_name=str(package.get("license") or "NOASSERTION"),
            source_sha=source_sha,
        ),
        _binary_entry(
            name="lantai-server",
            kind="pyinstaller-executable",
            path=backend,
            version=version,
            arch=arch,
            source={"type": "git", "sha": source_sha, "lock": "uv.lock"},
            license_name=str(package.get("license") or "NOASSERTION"),
            source_sha=source_sha,
        ),
        _binary_entry(
            name="mac-use-helper",
            kind="native-helper",
            path=mac_use_helper,
            version=version,
            arch="universal",
            source={"type": "git", "sha": source_sha},
            license_name=str(package.get("license") or "NOASSERTION"),
            source_sha=source_sha,
        ),
    ]
    officecli = _load_officecli_binary(
        app,
        arch,
        source_sha,
        allow_unreleasable=allow_unreleasable,
    )
    if officecli is not None:
        entries.append(officecli)
    for entry in entries:
        _validate_binary_entry(entry, root=root, allow_unreleasable=allow_unreleasable)
    return entries


def _validate_binary_entry(
    entry: dict[str, Any],
    *,
    root: Path | None = None,
    allow_unreleasable: bool = False,
) -> None:
    for field in ("name", "version", "arch", "sha256", "source", "license"):
        if not entry.get(field):
            _die(f"Binary source entry {entry.get('name') or '<unknown>'} missing {field}")
    if not HASH_RE.fullmatch(str(entry.get("sha256"))):
        _die(f"Binary source entry {entry.get('name')} has invalid sha256")
    for hash_field in ("sourceSha256", "bundleSha256"):
        hash_value = entry.get(hash_field)
        if hash_value and not HASH_RE.fullmatch(str(hash_value)):
            _die(f"Binary source entry {entry.get('name')} has invalid {hash_field}")
    if entry.get("bundleSha256") and entry.get("bundleSha256") != entry.get("sha256"):
        _die(f"Binary source entry {entry.get('name')} bundleSha256 mismatch")
    source = entry.get("source")
    if not isinstance(source, dict) or not source.get("type"):
        _die(f"Binary source entry {entry.get('name')} has invalid source")
    if source.get("type") != "git" and not source.get("url") and not allow_unreleasable:
        _die(f"Binary source entry {entry.get('name')} is missing source URL")
    if not entry.get("releasable", True) and not allow_unreleasable:
        _die(f"Binary source entry {entry.get('name')} is marked unreleasable")
    path_value = entry.get("path")
    if isinstance(path_value, str) and path_value:
        path = Path(path_value)
        if root and not path.is_absolute():
            path = root / path
        if path.is_file() and _sha256_file(path) != entry.get("sha256"):
            _die(f"Binary source entry {entry.get('name')} hash mismatch: {path}")


def _binary_components(binary_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for entry in binary_sources:
        components.append(
            {
                "type": "file",
                "bom-ref": f"binary:{entry['name']}@{entry['version']}",
                "name": entry["name"],
                "version": entry["version"],
                "hashes": [{"alg": "SHA-256", "content": entry["sha256"]}],
                "licenses": _license_entry(entry["license"]),
                "externalReferences": (
                    [{"type": "distribution", "url": entry["source"]["url"]}]
                    if entry.get("source", {}).get("url")
                    else []
                ),
                "properties": [
                    {"name": "lantai:binary:kind", "value": entry["kind"]},
                    {"name": "lantai:binary:arch", "value": entry["arch"]},
                    {"name": "lantai:binary:path", "value": entry["path"]},
                ],
            },
        )
    return components


def _validate_sbom_references(sbom: dict[str, Any]) -> None:
    if not isinstance(sbom, dict):
        _die("SBOM must be an object")
    metadata = sbom.get("metadata", {})
    if not isinstance(metadata, dict):
        _die("SBOM metadata must be an object")
    seen: set[str] = set()
    application = metadata.get("component")
    components = sbom.get("components", [])
    if not isinstance(components, list):
        _die("SBOM components must be a list")
    for component in ([application] if application is not None else []) + components:
        if not isinstance(component, dict):
            _die("SBOM component must be an object")
        reference = component.get("bom-ref")
        if not isinstance(reference, str) or not reference.strip():
            _die("SBOM component is missing bom-ref")
        if reference in seen:
            _die(f"SBOM contains duplicate bom-ref: {reference}")
        seen.add(reference)


def _cyclonedx_bom(
    *,
    package: dict[str, Any],
    source_sha: str,
    version: str,
    arch: str,
    components: list[dict[str, Any]],
) -> dict[str, Any]:
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"lantai:{source_sha}:{version}:{arch}")
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": SBOM_SPEC_VERSION,
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": dt.datetime.now(dt.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "component": {
                "type": "application",
                "bom-ref": f"application:{package['name']}@{version}",
                "name": package["name"],
                "version": version,
                "licenses": _license_entry(package.get("license")),
                "properties": [
                    {"name": "lantai:source-sha", "value": source_sha},
                    {"name": "lantai:release-arch", "value": arch},
                ],
            },
        },
        "components": sorted(components, key=lambda item: item.get("bom-ref", "")),
    }

    _validate_sbom_schema(sbom)
    return sbom


def _load_dependency_manifest(path: Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    if not path.is_file():
        _die(f"Missing dependency manifest for supply-chain manifest: {path}")
    return _read_json(path)


def write_manifest(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    app = args.app.resolve()
    package = _load_package_metadata(root, args.version)
    npm_components, npm_by_name = _npm_components(root)
    python_components = _python_components(root, app=app)
    binary_sources = _binary_sources(
        root=root,
        app=app,
        package=package,
        npm_by_name=npm_by_name,
        source_sha=args.source_sha,
        version=args.version,
        arch=args.arch,
        allow_unreleasable=args.allow_unreleasable_binaries,
    )
    components = npm_components + python_components + _binary_components(binary_sources)
    sbom = _cyclonedx_bom(
        package=package,
        source_sha=args.source_sha,
        version=args.version,
        arch=args.arch,
        components=components,
    )

    args.sbom_output.parent.mkdir(parents=True, exist_ok=True)
    args.sbom_output.write_text(
        json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema": SCHEMA,
        "source": {"sha": args.source_sha},
        "release": {"version": args.version, "arch": args.arch},
        "package": package,
        "lockfiles": [
            _lockfile_metadata(root, "uv.lock"),
            _lockfile_metadata(root, "console/package-lock.json"),
        ],
        "dependencyManifest": _load_dependency_manifest(args.dependency_manifest),
        "sbom": {
            "format": "CycloneDX",
            "specVersion": SBOM_SPEC_VERSION,
            **_file_metadata(args.sbom_output),
        },
        "componentCounts": {
            "npm": len(npm_components),
            "python": len(python_components),
            "binary": len(binary_sources),
            "total": len(components),
        },
        "binarySources": binary_sources,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validate_manifest_payload(
        manifest,
        source_sha=args.source_sha,
        version=args.version,
        arch=args.arch,
        base_dir=args.release_dir,
        allow_unreleasable=args.allow_unreleasable_binaries,
    )
    print(f"Wrote release supply-chain manifest: {args.output}")
    print(f"Wrote CycloneDX SBOM: {args.sbom_output}")


def validate_manifest_payload(
    manifest: dict[str, Any],
    *,
    source_sha: str,
    version: str,
    arch: str,
    base_dir: Path,
    allow_unreleasable: bool = False,
) -> None:
    if manifest.get("schema") != SCHEMA:
        _die("Unexpected release supply-chain manifest schema")
    if manifest.get("source", {}).get("sha") != source_sha:
        _die("Supply-chain manifest source SHA mismatch")
    release = manifest.get("release") or {}
    if release.get("version") != version:
        _die("Supply-chain manifest version mismatch")
    if release.get("arch") != arch:
        _die("Supply-chain manifest arch mismatch")
    sbom = manifest.get("sbom") or {}
    sbom_path = Path(str(sbom.get("path") or ""))
    if not sbom_path.is_absolute():
        sbom_path = base_dir / sbom_path
    if not sbom_path.is_file():
        _die(f"Supply-chain manifest SBOM path is missing: {sbom_path}")
    if sbom.get("sha256") != _sha256_file(sbom_path):
        _die("Supply-chain manifest SBOM hash mismatch")
    validate_declared_sbom(_read_json(sbom_path), sbom)
    binaries = manifest.get("binarySources")
    if not isinstance(binaries, list) or not binaries:
        _die("Supply-chain manifest has no binarySources")
    for entry in binaries:
        if not isinstance(entry, dict):
            _die("Supply-chain manifest contains invalid binarySources entry")
        _validate_binary_entry(
            entry,
            root=base_dir,
            allow_unreleasable=allow_unreleasable,
        )


def validate_manifest(args: argparse.Namespace) -> None:
    manifest = _read_json(args.manifest)
    validate_manifest_payload(
        manifest,
        source_sha=args.source_sha,
        version=args.version,
        arch=args.arch,
        base_dir=args.release_dir,
        allow_unreleasable=args.allow_unreleasable_binaries,
    )
    print(f"Release supply-chain manifest verified: {args.manifest}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    write = subparsers.add_parser("write-manifest")
    write.add_argument("--root", required=True, type=Path)
    write.add_argument("--release-dir", required=True, type=Path)
    write.add_argument("--output", required=True, type=Path)
    write.add_argument("--sbom-output", required=True, type=Path)
    write.add_argument("--source-sha", required=True)
    write.add_argument("--version", required=True)
    write.add_argument("--arch", required=True, choices=("arm64", "x64", "universal"))
    write.add_argument("--app", required=True, type=Path)
    write.add_argument("--dependency-manifest", type=Path)
    write.add_argument("--allow-unreleasable-binaries", action="store_true")
    write.set_defaults(func=write_manifest)

    validate = subparsers.add_parser("validate-manifest")
    validate.add_argument("--manifest", required=True, type=Path)
    validate.add_argument("--release-dir", required=True, type=Path)
    validate.add_argument("--source-sha", required=True)
    validate.add_argument("--version", required=True)
    validate.add_argument("--arch", required=True, choices=("arm64", "x64", "universal"))
    validate.add_argument("--allow-unreleasable-binaries", action="store_true")
    validate.set_defaults(func=validate_manifest)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - defensive release script output
        print(f"Release supply-chain validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
