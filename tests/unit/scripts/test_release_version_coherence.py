from __future__ import annotations

import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "check_release_version_coherence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_release_version_coherence", SCRIPT_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_source_versions(root: Path, *, console: str, backend: str) -> None:
    console_dir = root / "console"
    console_dir.mkdir(parents=True)
    (console_dir / "package.json").write_text(
        json.dumps({"version": console}), encoding="utf-8"
    )
    (console_dir / "package-lock.json").write_text(
        json.dumps(
            {"version": console, "packages": {"": {"version": console}}}
        ),
        encoding="utf-8",
    )
    backend_version = root / "src" / "lantai" / "__version__.py"
    backend_version.parent.mkdir(parents=True)
    backend_version.write_text(
        f'__version__ = "{backend}"\n',
        encoding="utf-8",
    )


def _write_backend_distribution_metadata(app_path: Path, version: str) -> None:
    metadata = (
        app_path
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "Resources"
        / f"lantai-{version}.dist-info"
        / "METADATA"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        f"Metadata-Version: 2.4\nName: lantai\nVersion: {version}\n",
        encoding="utf-8",
    )
    frameworks = metadata.parents[2] / "Frameworks"
    frameworks.mkdir(parents=True, exist_ok=True)
    framework_metadata = frameworks / metadata.parent.name
    if not framework_metadata.exists():
        framework_metadata.symlink_to(
            Path("../Resources") / metadata.parent.name,
            target_is_directory=True,
        )


def _write_backend_executable(app_path: Path, version: str) -> Path:
    backend = (
        app_path
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    backend.parent.mkdir(parents=True, exist_ok=True)
    backend.write_text(
        f'#!/bin/sh\necho "澜台, version {version}"\n', encoding="utf-8"
    )
    backend.chmod(0o755)
    return backend


def test_source_gate_rejects_console_and_backend_version_mismatch(
    tmp_path: Path,
) -> None:
    module = _load_module()
    _write_source_versions(tmp_path, console="1.0.49", backend="1.0.48")

    with pytest.raises(module.VersionCoherenceError, match="Python backend source"):
        module.check_release_version_coherence(project_root=tmp_path)


def test_packaged_app_gate_rejects_stale_frozen_backend(tmp_path: Path) -> None:
    module = _load_module()
    _write_source_versions(tmp_path, console="1.0.49", backend="1.0.49")
    app_path = tmp_path / "release" / "澜台.app"
    info_plist = app_path / "Contents" / "Info.plist"
    info_plist.parent.mkdir(parents=True)
    with info_plist.open("wb") as file:
        plistlib.dump(
            {
                "CFBundleShortVersionString": "1.0.49",
                "CFBundleVersion": "1.0.49",
            },
            file,
        )
    backend = (
        app_path
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    backend.parent.mkdir(parents=True)
    backend.write_text('#!/bin/sh\necho "澜台, version 1.0.48"\n', encoding="utf-8")
    backend.chmod(0o755)
    _write_backend_distribution_metadata(app_path, "1.0.49")

    with pytest.raises(module.VersionCoherenceError, match="Packaged backend"):
        module.check_release_version_coherence(
            project_root=tmp_path,
            packaged_app=app_path,
        )


def test_packaged_app_gate_rejects_stale_backend_distribution_metadata(
    tmp_path: Path,
) -> None:
    module = _load_module()
    _write_source_versions(tmp_path, console="1.0.49", backend="1.0.49")
    app_path = tmp_path / "release" / "澜台.app"
    info_plist = app_path / "Contents" / "Info.plist"
    info_plist.parent.mkdir(parents=True)
    with info_plist.open("wb") as file:
        plistlib.dump(
            {
                "CFBundleShortVersionString": "1.0.49",
                "CFBundleVersion": "1.0.49",
            },
            file,
        )
    backend = (
        app_path
        / "Contents"
        / "Resources"
        / "lantai-server"
        / "Lantai Backend.app"
        / "Contents"
        / "MacOS"
        / "lantai-server"
    )
    backend.parent.mkdir(parents=True)
    backend.write_text('#!/bin/sh\necho "澜台, version 1.0.49"\n', encoding="utf-8")
    backend.chmod(0o755)
    _write_backend_distribution_metadata(app_path, "1.0.48")

    with pytest.raises(
        module.VersionCoherenceError,
        match="backend distribution metadata",
    ):
        module.check_release_version_coherence(
            project_root=tmp_path,
            packaged_app=app_path,
        )


def test_backend_metadata_gate_rejects_missing_frameworks_copy(tmp_path: Path) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    backend = _write_backend_executable(app_path, "1.0.49")
    _write_backend_distribution_metadata(app_path, "1.0.49")
    framework_metadata = (
        backend.parents[1] / "Frameworks" / "lantai-1.0.49.dist-info"
    )
    framework_metadata.unlink()

    with pytest.raises(module.VersionCoherenceError, match="exactly one"):
        module._backend_distribution_versions(backend, label="Packaged backend")


def test_backend_metadata_gate_rejects_duplicate_dist_info(tmp_path: Path) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    backend = _write_backend_executable(app_path, "1.0.49")
    _write_backend_distribution_metadata(app_path, "1.0.49")
    duplicate = (
        backend.parents[1]
        / "Resources"
        / "lantai-1.0.48.dist-info"
        / "METADATA"
    )
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(
        "Metadata-Version: 2.4\nName: lantai\nVersion: 1.0.48\n",
        encoding="utf-8",
    )

    with pytest.raises(module.VersionCoherenceError, match="exactly one"):
        module._backend_distribution_versions(backend, label="Packaged backend")


def test_backend_metadata_gate_rejects_directory_and_metadata_disagreement(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    backend = _write_backend_executable(app_path, "1.0.49")
    _write_backend_distribution_metadata(app_path, "1.0.49")
    metadata = (
        backend.parents[1]
        / "Resources"
        / "lantai-1.0.49.dist-info"
        / "METADATA"
    )
    metadata.write_text(
        "Metadata-Version: 2.4\nName: lantai\nVersion: 1.0.48\n",
        encoding="utf-8",
    )

    with pytest.raises(module.VersionCoherenceError, match="disagrees"):
        module._backend_distribution_versions(backend, label="Packaged backend")


def test_release_build_enforces_source_and_packaged_version_coherence() -> None:
    script = (ROOT / "build_and_deploy.sh").read_text(encoding="utf-8")

    assert "check_release_version_coherence.py" in script
    assert '--expected-version "$VERSION"' in script
    assert '--packaged-app "$release_app"' in script
    assert "refresh_release_python_project_metadata" in script
    assert '--reinstall-package lantai' in script


def test_backend_packager_checks_frozen_cli_and_running_api_version() -> None:
    packager = (ROOT / "electron" / "build-python.py").read_text(encoding="utf-8")

    assert "check_release_version_coherence.py" in packager
    assert '"--frozen-backend"' in packager
    assert '"--expected-version"' in packager
    assert '"--backend-timeout"' in packager
    assert "_require_project_distribution_version()" in packager


def test_backend_version_probe_uses_configured_cold_start_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    executable = tmp_path / "lantai-server"
    executable.write_text("placeholder", encoding="utf-8")
    observed: dict[str, float] = {}

    def fake_run(command, **kwargs):
        assert command == [str(executable), "--version"]
        observed["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="澜台, version 1.0.49\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._backend_executable_version(
        executable,
        label="Frozen backend",
        timeout_seconds=75.0,
    ) == "1.0.49"
    assert observed == {"timeout": 75.0}
