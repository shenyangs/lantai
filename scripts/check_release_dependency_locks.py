#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate release dependency locks and write a small provenance manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


LOCK_PATHS = ("uv.lock", "console/package-lock.json")
CRITICAL_DIRECT_RUNTIME_DISTRIBUTIONS = (
    "fastapi",
    "orjson",
    "guanlan",
    "mcp",
    "httpx2",
    "httpcore2",
    "pypdfium2",
)
RUNTIME_IMPORT_TARGETS = (
    "fastapi",
    "orjson",
    "guanlan.integrations.mcp_server",
    "mcp",
    "httpx2",
    "httpcore2",
    "pypdfium2",
    "lantai.app._app",
    "lantai.presentation.cli",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _active_ignore_patterns(root: Path) -> set[str]:
    patterns: set[str] = set()
    ignore_path = root / ".gitignore"
    if not ignore_path.exists():
        return patterns
    for raw_line in ignore_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.add(line)
    return patterns


def _git_head(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _fail(message: str) -> None:
    raise SystemExit(message)


def _validate_lockfiles_present(root: Path) -> None:
    ignored = _active_ignore_patterns(root)
    for relative in LOCK_PATHS:
        if relative in ignored:
            _fail(f"{relative} is ignored by .gitignore")
        if not (root / relative).is_file():
            _fail(f"Missing required release lockfile: {relative}")


def _validate_console_lock(root: Path) -> dict[str, Any]:
    package_path = root / "console" / "package.json"
    lock_path = root / "console" / "package-lock.json"
    package = _load_json(package_path)
    lock = _load_json(lock_path)
    lock_root = lock.get("packages", {}).get("")
    if not isinstance(lock_root, dict):
        _fail("console/package-lock.json is missing packages['']")

    for field in ("name", "version"):
        if lock.get(field) != package.get(field):
            _fail(f"package-lock root {field} does not match package.json")
        if lock_root.get(field) != package.get(field):
            _fail(f"package-lock packages[''] {field} does not match package.json")

    for field in ("dependencies", "devDependencies"):
        expected = package.get(field, {})
        actual = lock_root.get(field, {})
        if actual != expected:
            _fail(f"package-lock packages[''] {field} drifted from package.json")

    packages = lock.get("packages", {})
    if "node_modules/electron-builder" not in packages:
        _fail("electron-builder must be present in console/package-lock.json")

    return {
        "name": package.get("name"),
        "version": package.get("version"),
        "lockfileVersion": lock.get("lockfileVersion"),
        "electronBuilder": packages["node_modules/electron-builder"].get(
            "version",
        ),
    }


def _validate_uv_lock(root: Path, *, skip_uv_check: bool) -> bool:
    if skip_uv_check:
        return False
    uv = shutil.which("uv")
    if not uv:
        _fail("uv is required to verify uv.lock for release builds")

    env = os.environ.copy()
    env.setdefault("UV_PYTHON_DOWNLOADS", "never")
    subprocess.run(
        [uv, "lock", "--check"],
        cwd=root,
        env=env,
        check=True,
    )
    # A valid lock file is insufficient when an old virtualenv still contains
    # packages that are no longer declared. Release builds must prove that the
    # active environment is exactly reproducible from the checked-in lock.
    subprocess.run(
        [uv, "sync", "--locked", "--extra", "dev", "--check"],
        cwd=root,
        env=env,
        check=True,
    )
    return True


def _validate_direct_runtime_dependencies() -> dict[str, str]:
    """Require critical imports to be first-class project dependencies.

    Transitive dependencies and packages left behind in a long-lived venv can
    make local builds pass by accident. Reading the installed Lantai metadata
    verifies the release environment reflects explicit project declarations.
    """

    try:
        raw_requirements = importlib.metadata.requires("lantai") or []
    except importlib.metadata.PackageNotFoundError as exc:
        _fail(
            "The active release environment does not contain the Lantai "
            "project. Run `uv sync --locked --extra dev` before building.",
        )
        raise AssertionError("unreachable") from exc

    direct: set[str] = set()
    for raw in raw_requirements:
        try:
            direct.add(canonicalize_name(Requirement(raw).name))
        except InvalidRequirement:
            _fail(f"Invalid installed Lantai requirement metadata: {raw!r}")

    missing = [
        name
        for name in CRITICAL_DIRECT_RUNTIME_DISTRIBUTIONS
        if canonicalize_name(name) not in direct
    ]
    if missing:
        _fail(
            "Critical runtime packages must be direct pyproject dependencies: "
            + ", ".join(missing),
        )

    versions: dict[str, str] = {}
    for name in CRITICAL_DIRECT_RUNTIME_DISTRIBUTIONS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            _fail(
                f"Declared runtime dependency is missing from the active "
                f"release environment: {name}",
            )
    return versions


def _validate_runtime_imports(root: Path) -> None:
    """Import the real application entry path in a fresh Python process."""

    import_script = "\n".join(
        [
            "from importlib import import_module",
            f"targets = {RUNTIME_IMPORT_TARGETS!r}",
            "for target in targets:",
            "    import_module(target)",
        ],
    )
    proc = subprocess.run(
        [sys.executable, "-c", import_script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if proc.returncode == 0:
        return
    detail = (proc.stderr or proc.stdout or "unknown import failure").strip()
    _fail(
        "Release runtime import smoke failed. A required dependency is "
        f"missing or incompatible:\n{detail[-4000:]}",
    )


def _build_manifest(
    root: Path,
    console: dict[str, Any],
    runtime_dependencies: dict[str, str],
    *,
    environment_lock_verified: bool,
) -> dict[str, Any]:
    return {
        "schema": "com.lantai.release-dependency-manifest.v1",
        "source": {
            "root": str(root),
            "gitHead": _git_head(root),
        },
        "locks": [
            {
                "path": relative,
                "sha256": _sha256(root / relative),
                "bytes": (root / relative).stat().st_size,
            }
            for relative in LOCK_PATHS
        ],
        "console": console,
        "runtime": {
            "python": sys.version.split()[0],
            "criticalDirectDependencies": runtime_dependencies,
            "importTargets": list(RUNTIME_IMPORT_TARGETS),
            "environmentMatchesLock": True if environment_lock_verified else None,
            "environmentLockCheck": "passed" if environment_lock_verified else "skipped",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--write-manifest", type=Path)
    parser.add_argument("--skip-uv-check", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    _validate_lockfiles_present(root)
    console = _validate_console_lock(root)
    environment_lock_verified = _validate_uv_lock(root, skip_uv_check=args.skip_uv_check)
    runtime_dependencies = _validate_direct_runtime_dependencies()
    _validate_runtime_imports(root)

    if args.write_manifest:
        manifest_path = args.write_manifest
        if not manifest_path.is_absolute():
            manifest_path = root / manifest_path
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                _build_manifest(
                    root, console, runtime_dependencies,
                    environment_lock_verified=environment_lock_verified,
                ),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Wrote dependency manifest: {manifest_path}")
    else:
        print(
            "Release dependency locks verified."
            if environment_lock_verified
            else "Dependency imports and console lock verified; Python lock/environment check skipped."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
