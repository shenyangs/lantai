# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path


def _major(version_spec: str) -> str:
    value = version_spec.strip()
    while value and not value[0].isdigit():
        value = value[1:]
    return value.split(".", 1)[0]


def _load_console_package(root: Path) -> tuple[dict, dict]:
    package_json = json.loads((root / "console" / "package.json").read_text())
    package_lock = json.loads(
        (root / "console" / "package-lock.json").read_text(),
    )
    return package_json, package_lock


def test_react_runtime_and_type_package_majors_match() -> None:
    package_json, package_lock = _load_console_package(Path.cwd())

    runtime_dependencies = {
        **package_json.get("dependencies", {}),
        **package_json.get("devDependencies", {}),
    }
    dev_dependencies = package_json["devDependencies"]
    expected = {
        "@types/react": "react",
        "@types/react-dom": "react-dom",
    }

    for type_package, runtime_package in expected.items():
        runtime_major = _major(runtime_dependencies[runtime_package])
        type_major = _major(dev_dependencies[type_package])
        assert type_major == runtime_major

        lock_runtime = package_lock["packages"][f"node_modules/{runtime_package}"]
        lock_types = package_lock["packages"][f"node_modules/{type_package}"]
        assert _major(lock_runtime["version"]) == runtime_major
        assert _major(lock_types["version"]) == runtime_major
