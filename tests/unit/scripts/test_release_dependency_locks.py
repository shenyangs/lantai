# -*- coding: utf-8 -*-
"""Release dependency locking and offline build guard tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _release_roots() -> list[Path]:
    roots = [ROOT]
    mas_root = ROOT.parent / "lantai_App Store"
    if ROOT.name != "lantai_App Store" and mas_root.exists():
        roots.append(mas_root)
    return roots


def _active_ignore_patterns(root: Path) -> set[str]:
    patterns: set[str] = set()
    for raw_line in (root / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.add(line)
    return patterns


def test_release_lockfiles_exist_and_are_not_ignored():
    for root in _release_roots():
        ignored = _active_ignore_patterns(root)

        assert "uv.lock" not in ignored
        assert "console/package-lock.json" not in ignored
        assert (root / "uv.lock").is_file()
        assert (root / "console/package-lock.json").is_file()


def test_console_package_lock_matches_package_manifest():
    for root in _release_roots():
        package = json.loads(
            (root / "console/package.json").read_text(encoding="utf-8"),
        )
        lock = json.loads(
            (root / "console/package-lock.json").read_text(encoding="utf-8"),
        )
        root_package = lock.get("packages", {}).get("", {})

        assert lock["name"] == package["name"]
        assert lock["version"] == package["version"]
        assert root_package["version"] == package["version"]
        assert root_package.get("dependencies", {}) == package.get(
            "dependencies",
            {},
        )
        assert root_package.get("devDependencies", {}) == package.get(
            "devDependencies",
            {},
        )
        assert "node_modules/electron-builder" in lock.get("packages", {})


def test_release_scripts_use_npm_ci_and_offline_electron_builder():
    for root in _release_roots():
        script_names = ["build_and_deploy.sh"]
        if root.name == "lantai_App Store":
            script_names.extend(["build_app_store.sh", "build_app_store_dev.sh"])

        for script_name in script_names:
            text = (root / script_name).read_text(encoding="utf-8")
            assert "npm ci" in text
            assert "npx electron-builder" not in text
            assert "npm exec --offline -- electron-builder" in text
            assert "check_release_dependency_locks.py" in text


    # Only the maintained desktop product owns the candidate-build entrypoint.
    # The sibling App Store tree is a frozen historical archive and must not be
    # brought back into the active release contract.
    package = json.loads(
        (ROOT / "console/package.json").read_text(encoding="utf-8"),
    )
    assert (
        package["scripts"]["electron:build"]
        == "node scripts/build-electron-candidate.cjs"
    )

    candidate_builder = (
        ROOT / "console/scripts/build-electron-candidate.cjs"
    ).read_text(encoding="utf-8")
    assert "build_and_deploy.sh" in candidate_builder
    assert '"--artifact-only"' in candidate_builder
    assert "LANTAI_RELEASE_CANDIDATE_ID" in candidate_builder
    assert "electron-builder" not in candidate_builder


def test_console_candidate_builder_routes_through_official_release_pipeline():
    completed = subprocess.run(
        [
            "node",
            str(ROOT / "console/scripts/build-electron-candidate.cjs"),
            "--print-command",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT / "console",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert Path(payload["command"]).resolve() == (
        ROOT / "build_and_deploy.sh"
    ).resolve()
    assert payload["args"] == ["--artifact-only"]
    assert payload["environment"]["LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD"] == "1"
    assert payload["environment"]["LANTAI_ALLOW_DIRTY_RELEASE_BUILD"] == "1"
    assert payload["environment"]["LANTAI_RELEASE_CANDIDATE_ID"]


def test_python_backend_build_uses_uv_lock_without_runtime_install():
    for root in _release_roots():
        text = (root / "electron/build-python.py").read_text(encoding="utf-8")

        assert '"lock"' in text
        assert '"--check"' in text
        assert '"pip",\n                "install"' not in text
        assert '"uv",\n                "pip"' not in text
        assert '"--upgrade"' not in text
        assert '"-e"' not in text


def test_python_backend_build_requires_desktop_lifecycle_smoke():
    for root in _release_roots():
        text = (root / "electron/build-python.py").read_text(encoding="utf-8")

        assert "frozen_backend_console_smoke.py" in text
        assert '"--desktop-auth"' in text
        assert '"--restart-cycle"' in text


def test_release_dependency_manifest_gate_exists():
    for root in _release_roots():
        assert (root / "scripts/check_release_dependency_locks.py").is_file()


def test_release_dependency_gate_imports_dynamic_presentation_dependencies():
    text = (ROOT / "scripts/check_release_dependency_locks.py").read_text(
        encoding="utf-8",
    )

    assert '"pypdfium2"' in text
    assert '"lantai.presentation.cli"' in text


def test_official_release_builder_rejects_concurrent_builds(tmp_path: Path):
    """One build must not delete files used by another frozen-backend smoke."""

    text = (ROOT / "build_and_deploy.sh").read_text(encoding="utf-8")

    assert 'RELEASE_BUILD_LOCK_FILE="$ROOT_DIR/build/.release-build.lock"' in text
    assert "/usr/bin/shlock" in text
    assert "acquire_release_build_lock" in text
    assert "trap release_build_lock EXIT" in text

    isolated_script = tmp_path / "build_and_deploy.sh"
    isolated_script.write_text(text, encoding="utf-8")
    isolated_script.chmod(0o755)
    lock_path = tmp_path / "build" / ".release-build.lock"
    lock_path.parent.mkdir()
    lock_path.write_text(str(os.getpid()), encoding="utf-8")

    completed = subprocess.run(
        ["bash", str(isolated_script), "--skip-build", "--no-launch"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "another Lantai release build is already running" in completed.stderr
