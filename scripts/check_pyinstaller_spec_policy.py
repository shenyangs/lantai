#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resolve PyInstaller signing, entitlement, arch, and UPX policy."""

from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class PyInstallerSpecPolicy:
    distribution: str
    target_arch: str
    codesign_identity: str
    entitlements_file: str
    upx: bool


def _env_value(env: Mapping[str, str], key: str) -> str:
    return str(env.get(key, "")).strip()


def resolve_distribution(project_root: Path, env: Mapping[str, str]) -> str:
    raw = (
        _env_value(env, "LANTAI_PYINSTALLER_DISTRIBUTION")
        or _env_value(env, "LANTAI_DISTRIBUTION_CHANNEL")
        or _env_value(env, "LANTAI_RELEASE_CHANNEL")
        or _env_value(env, "LANTAI_DISTRIBUTION")
    ).lower()
    if raw in {"app_store", "appstore", "mas"}:
        return "mas"
    if raw in {"official", "developer-id", "developer_id", "direct"}:
        return "official"
    if _env_value(env, "LANTAI_APP_STORE").lower() in {"1", "true", "yes"}:
        return "mas"
    normalized_name = project_root.name.lower().replace("_", " ")
    if "app store" in normalized_name:
        return "mas"
    return "official"


def normalize_pyinstaller_arch(
    arch: str,
    *,
    machine: str | None = None,
) -> str:
    raw = (arch or machine or platform.machine() or "").strip().lower()
    mapping = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "universal": "universal2",
        "universal2": "universal2",
    }
    if raw not in mapping:
        raise RuntimeError(f"Unsupported PyInstaller target arch: {arch!r}")
    return mapping[raw]


def _resolve_entitlements_file(
    project_root: Path,
    distribution: str,
    env: Mapping[str, str],
) -> Path:
    explicit = _env_value(env, "LANTAI_PYINSTALLER_ENTITLEMENTS_FILE")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = project_root / path
    elif distribution == "mas":
        path = project_root / "console" / "electron" / "entitlements" / "mas.inherit.plist"
    else:
        path = (
            project_root
            / "console"
            / "electron"
            / "entitlements"
            / "developer-id.inherit.plist"
        )

    if not path.is_file():
        raise RuntimeError(f"Missing PyInstaller entitlements file: {path}")
    return path


def _resolve_codesign_identity(env: Mapping[str, str]) -> str:
    return (
        _env_value(env, "LANTAI_PYINSTALLER_CODESIGN_IDENTITY")
        or _env_value(env, "LANTAI_CODESIGN_IDENTITY")
        or _env_value(env, "CSC_NAME")
        or "-"
    )


def resolve_pyinstaller_policy(
    project_root: Path,
    env: Mapping[str, str] | None = None,
) -> PyInstallerSpecPolicy:
    if env is None:
        env = os.environ
    project_root = Path(project_root).resolve()
    distribution = resolve_distribution(project_root, env)
    target_arch = normalize_pyinstaller_arch(
        _env_value(env, "LANTAI_PYINSTALLER_TARGET_ARCH")
        or _env_value(env, "LANTAI_RELEASE_ARCH"),
    )
    entitlements_file = _resolve_entitlements_file(
        project_root,
        distribution,
        env,
    )
    return PyInstallerSpecPolicy(
        distribution=distribution,
        target_arch=target_arch,
        codesign_identity=_resolve_codesign_identity(env),
        entitlements_file=str(entitlements_file),
        upx=False,
    )


def main() -> int:
    project_root = Path(os.environ.get("LANTAI_PROJECT_ROOT", ".")).resolve()
    policy = resolve_pyinstaller_policy(project_root)
    print(json.dumps(asdict(policy), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
