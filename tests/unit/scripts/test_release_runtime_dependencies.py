# -*- coding: utf-8 -*-
"""Release builds must not depend on packages left behind in an old venv."""

from __future__ import annotations

from pathlib import Path

from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 release environment
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[3]


def test_critical_runtime_packages_are_direct_project_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {
        Requirement(raw).name.lower().replace("_", "-")
        for raw in project["project"]["dependencies"]
    }

    assert {
        "fastapi",
        "orjson",
        "guanlan",
        "mcp",
        "httpx2",
        "httpcore2",
    } <= dependencies


def test_release_gate_checks_the_locked_environment_and_runtime_imports():
    gate = (ROOT / "scripts/check_release_dependency_locks.py").read_text(
        encoding="utf-8",
    )

    assert '"sync", "--locked", "--extra", "dev", "--check"' in gate
    assert "_validate_direct_runtime_dependencies" in gate
    assert "_validate_runtime_imports" in gate
    assert "lantai.app._app" in gate


def test_backend_packager_requires_the_bundled_guanlan_version():
    build_script = (ROOT / "electron/build-python.py").read_text(encoding="utf-8")

    assert 'GUANLAN_MIN_VERSION = Version("0.10.9")' in build_script
