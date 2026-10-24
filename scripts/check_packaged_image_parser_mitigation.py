#!/usr/bin/env python3
"""Prove the packaged Presentation v3 runtime rejects disabled image parsers.

PptxGenJS 4.0.1 declares image-size 1.x even though its current distribution
does not call the library. Lantai's Presentation v3 compiler fails closed by
disabling every parser named in the published infinite-loop advisories before
PptxGenJS is loaded. This check executes that exact packaged security bootstrap
with the exact packaged Electron and Node dependency runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA = "com.lantai.packaged-image-parser-mitigation.v1"
DISABLED_TYPES = ("icns", "jxl", "jxl-stream", "heif")
ADVISORIES = (
    "GHSA-w3rx-r6r6-pgpr",
    "GHSA-5p2g-fcmc-qvqq",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probes() -> dict[str, str]:
    # Each buffer is just complete enough for image-size's detector to select
    # the named parser. The parser must never receive it after the bootstrap.
    return {
        "icns": "69636e7300000008",
        "jxl": (
            "0000000c4a584c200d0a870a"
            "00000014667479706a786c20000000006a786c20"
        ),
        "jxl-stream": "ff0a00",
        "heif": "00000010667479706865696300000000",
    }


def _package_identity(app: Path) -> tuple[dict[str, Any], Path, Path]:
    app = app.expanduser().resolve(strict=True)
    info_path = app / "Contents" / "Info.plist"
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    executable = app / "Contents" / "MacOS" / str(info["CFBundleExecutable"])
    modules = app / "Contents" / "Resources" / "app.asar.unpacked" / "node_modules"
    if not executable.is_file() or executable.is_symlink():
        raise ValueError(f"packaged executable is missing or unsafe: {executable}")
    image_size_manifest = modules / "image-size" / "package.json"
    if not image_size_manifest.is_file() or image_size_manifest.is_symlink():
        raise ValueError("packaged image-size manifest is missing or unsafe")
    manifest = json.loads(image_size_manifest.read_text(encoding="utf-8"))
    identity = {
        "path": str(app),
        "bundle_identifier": info.get("CFBundleIdentifier"),
        "short_version": info.get("CFBundleShortVersionString"),
        "bundle_version": info.get("CFBundleVersion"),
        "main_executable_sha256": _sha256(executable),
        "image_size_version": manifest.get("version"),
        "image_size_manifest_sha256": _sha256(image_size_manifest),
    }
    return identity, executable, modules


def _run_probe(
    *,
    executable: Path,
    modules: Path,
    bootstrap: Path,
    image_type: str,
    payload_hex: str,
    timeout: float,
) -> dict[str, Any]:
    program = (
        'const imageSize=require("image-size");'
        f'const input=Buffer.from({json.dumps(payload_hex)},"hex");'
        "try{imageSize(input);process.stdout.write('UNEXPECTED_ACCEPT');process.exit(9)}"
        "catch(error){process.stdout.write(String(error&&error.message||error))}"
    )
    env = {
        **os.environ,
        "ELECTRON_RUN_AS_NODE": "1",
        "NODE_PATH": str(modules),
        "LANTAI_PPTXGENJS_MODULE_PATH": str(modules / "pptxgenjs"),
    }
    completed = subprocess.run(
        [str(executable), "--require", str(bootstrap), "--eval", program],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = (completed.stdout or "").strip()
    expected = f"disabled file type: {image_type}"
    return {
        "type": image_type,
        "passed": completed.returncode == 0 and output == expected,
        "exit_code": completed.returncode,
        "stdout": output,
        "stderr_sha256": hashlib.sha256(
            (completed.stderr or "").encode("utf-8")
        ).hexdigest(),
        "expected": expected,
    }


def _packaged_presentation_root(app: Path) -> Path:
    """Return the PyInstaller BUNDLE data root for Presentation v3."""

    return (
        app.expanduser().resolve()
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "Resources"
        / "lantai"
        / "presentation"
    )


def audit(app: Path, *, timeout: float) -> dict[str, Any]:
    identity, executable, modules = _package_identity(app)
    presentation_root = _packaged_presentation_root(app)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    probes = _probes()
    if set(probes) != set(DISABLED_TYPES):
        errors.append("probe set does not exactly cover the disabled parser set")
    bootstrap = presentation_root / "pptxgenjs_security.cjs"
    if not bootstrap.is_file() or bootstrap.is_symlink():
        errors.append("packaged Presentation v3 security bootstrap is missing")
    else:
        for image_type in DISABLED_TYPES:
            try:
                result = _run_probe(
                    executable=executable,
                    modules=modules,
                    bootstrap=bootstrap,
                    image_type=image_type,
                    payload_hex=probes[image_type],
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                result = {
                    "type": image_type,
                    "passed": False,
                    "timed_out": True,
                }
            result.update(
                {
                    "bootstrap": str(bootstrap),
                    "bootstrap_sha256": _sha256(bootstrap),
                }
            )
            records.append(result)
            if result.get("passed") is not True:
                errors.append(f"Presentation v3 did not reject {image_type} before parsing")
    return {
        "schema": SCHEMA,
        "passed": not errors,
        "app": identity,
        "advisories": list(ADVISORIES),
        "mitigation": {
            "boundary": "packaged Presentation v3 compiler security bootstrap",
            "disabled_types": list(DISABLED_TYPES),
            "fail_closed_before_parser_calculate": True,
            "runtime": "exact packaged Electron executable in Node mode",
        },
        "records": records,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.app, timeout=args.timeout)
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(output)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["passed"] else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot audit packaged image parser mitigation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
