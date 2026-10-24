#!/usr/bin/env python3
"""Assemble package-bound Agent reliability evidence from audited child runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import sys
from pathlib import Path
from typing import Any


OUTPUT_SCHEMA = "com.lantai.agent-reliability-release-evidence.v2"
SUSTAINED_SCHEMA = "com.lantai.packaged-agent-sustained-run.v1"
FAULT_SCHEMA = "com.lantai.packaged-agent-fault-matrix.v1"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"evidence root must be an object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_identity(app_path: Path) -> dict[str, Any]:
    actual_app = app_path.expanduser().resolve(strict=True)
    info_path = actual_app / "Contents" / "Info.plist"
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    executable = actual_app / "Contents" / "MacOS" / str(
        info.get("CFBundleExecutable") or ""
    )
    if not executable.is_file() or executable.is_symlink():
        raise ValueError(f"official app executable is missing or unsafe: {executable}")
    return {
        "app_path": str(actual_app),
        "distribution": "official",
        "packaged_app_tested": True,
        "executable_sha256": _sha256(executable),
        "bundle_identifier": info.get("CFBundleIdentifier"),
        "short_version": info.get("CFBundleShortVersionString"),
        "bundle_version": info.get("CFBundleVersion"),
    }


def _assert_child_app(payload: dict[str, Any], package: dict[str, Any], path: Path) -> None:
    child = payload.get("app")
    if not isinstance(child, dict):
        raise ValueError(f"child evidence has no packaged app identity: {path}")
    try:
        child_path = Path(str(child.get("path") or "")).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"child evidence app path is unavailable: {path}") from exc
    if child_path != Path(package["app_path"]):
        raise ValueError(f"child evidence targets a different app: {path}")
    digests = child.get("artifact_sha256")
    recorded = str((digests or {}).get("main_executable") or "")
    if recorded.removeprefix("sha256:") != package["executable_sha256"]:
        raise ValueError(f"child evidence executable digest differs: {path}")


def assemble(
    *,
    app_path: Path,
    sustained_paths: list[Path],
    fault_path: Path,
) -> dict[str, Any]:
    if len(sustained_paths) < 2:
        raise ValueError("at least two sustained evidence files are required")
    package = _package_identity(app_path)
    light: list[dict[str, Any]] = []
    standard: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    works: list[dict[str, Any]] = []
    ui: list[dict[str, Any]] = []
    long_runs: list[dict[str, Any]] = []
    child_sources: list[str] = []
    for path in sustained_paths:
        payload = _read_json(path)
        if payload.get("schema") != SUSTAINED_SCHEMA or payload.get("passed") is not True:
            raise ValueError(f"sustained evidence is not a passing {SUSTAINED_SCHEMA}: {path}")
        _assert_child_app(payload, package, path)
        light.extend(payload.get("light_records") or ())
        standard.extend(payload.get("standard_records") or ())
        effects.extend(payload.get("effect_records") or ())
        works.extend(payload.get("works_records") or ())
        ui.extend(payload.get("ui_records") or ())
        long_run = payload.get("long_run")
        if not isinstance(long_run, dict):
            raise ValueError(f"sustained evidence has no long-run record: {path}")
        long_runs.append(long_run)
        child_sources.append(str(path.resolve()))

    fault = _read_json(fault_path)
    if fault.get("schema") != FAULT_SCHEMA or fault.get("passed") is not True:
        raise ValueError(f"fault evidence is not a passing {FAULT_SCHEMA}: {fault_path}")
    _assert_child_app(fault, package, fault_path)
    fault_records = fault.get("records")
    if not isinstance(fault_records, list):
        raise ValueError(f"fault evidence has no detailed records: {fault_path}")
    child_sources.append(str(fault_path.resolve()))

    return {
        "schema": OUTPUT_SCHEMA,
        "package": package,
        "light": {"records": light},
        "standard": {"records": standard},
        "fault_injection": {"records": fault_records},
        "long_runs": {"records": long_runs},
        "effects": {"records": effects},
        "works": {"records": works},
        "ui_consistency": {"records": ui},
        "child_evidence": child_sources,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--sustained-evidence", action="append", required=True)
    parser.add_argument("--fault-evidence", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        payload = assemble(
            app_path=Path(args.app),
            sustained_paths=[Path(item) for item in args.sustained_evidence],
            fault_path=Path(args.fault_evidence),
        )
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)
    except (OSError, ValueError) as exc:
        print(f"Cannot assemble reliability evidence: {exc}", file=sys.stderr)
        return 1
    print(f"Reliability evidence assembled: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
