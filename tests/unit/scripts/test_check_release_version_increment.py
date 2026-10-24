from __future__ import annotations

import importlib.util
import json
import plistlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "check_release_version_increment.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_release_version_increment", SCRIPT_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_app_version(app_path: Path, version: str) -> None:
    info_plist = app_path / "Contents" / "Info.plist"
    info_plist.parent.mkdir(parents=True)
    with info_plist.open("wb") as file:
        plistlib.dump({"CFBundleShortVersionString": version}, file)


def test_exact_patch_increment_is_accepted(tmp_path: Path) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _write_app_version(app_path, "1.0.10")

    result = module.check_release_version_increment(
        target_version="1.0.11",
        deployed_app=app_path,
    )

    assert result.baseline_version == "1.0.10"
    assert result.expected_version == "1.0.11"


@pytest.mark.parametrize(
    ("baseline_version", "target_version"),
    [
        ("1.0.10", "1.1.0"),
        ("1.0.10", "2.0.0"),
        ("1.9.7", "2.0.0"),
    ],
)
def test_adjacent_minor_or_major_series_boundary_is_accepted(
    tmp_path: Path,
    baseline_version: str,
    target_version: str,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _write_app_version(app_path, baseline_version)

    result = module.check_release_version_increment(
        target_version=target_version,
        deployed_app=app_path,
    )

    assert result.baseline_version == baseline_version
    assert result.target_version == target_version


@pytest.mark.parametrize(
    "target_version",
    ["1.0.10", "1.0.12", "1.2.0", "1.1.1", "2.1.0", "3.0.0", "0.9.9"],
)
def test_same_skipped_or_invalid_series_change_is_rejected(
    tmp_path: Path,
    target_version: str,
) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _write_app_version(app_path, "1.0.10")

    with pytest.raises(module.VersionIncrementError, match="allowed=1\\.0\\.11"):
        module.check_release_version_increment(
            target_version=target_version,
            deployed_app=app_path,
        )


def test_first_build_without_deployed_baseline_is_accepted(tmp_path: Path) -> None:
    module = _load_module()

    result = module.check_release_version_increment(
        target_version="1.0.10",
        deployed_app=tmp_path / "missing.app",
    )

    assert result.baseline_version is None
    assert result.expected_version is None


def test_explicit_same_version_preview_rebuild_is_accepted(tmp_path: Path) -> None:
    module = _load_module()
    app_path = tmp_path / "澜台.app"
    _write_app_version(app_path, "1.0.43")

    result = module.check_release_version_increment(
        target_version="1.0.43",
        deployed_app=app_path,
        allow_same_version_rebuild=True,
    )

    assert result.baseline_version == "1.0.43"
    assert result.expected_version == "1.0.43"


@pytest.mark.parametrize(
    "lock_payload",
    [
        {"version": "1.0.10", "packages": {"": {"version": "1.0.11"}}},
        {"version": "1.0.11", "packages": {"": {"version": "1.0.10"}}},
    ],
)
def test_package_lock_versions_must_match_target(
    tmp_path: Path,
    lock_payload: dict[str, object],
) -> None:
    module = _load_module()
    package_lock = tmp_path / "package-lock.json"
    package_lock.write_text(json.dumps(lock_payload), encoding="utf-8")

    with pytest.raises(module.VersionIncrementError, match="package-lock"):
        module.check_release_version_increment(
            target_version="1.0.11",
            deployed_app=tmp_path / "missing.app",
            package_lock=package_lock,
        )


def test_matching_package_lock_versions_are_accepted(tmp_path: Path) -> None:
    module = _load_module()
    package_lock = tmp_path / "package-lock.json"
    package_lock.write_text(
        json.dumps(
            {"version": "1.0.11", "packages": {"": {"version": "1.0.11"}}}
        ),
        encoding="utf-8",
    )

    result = module.check_release_version_increment(
        target_version="1.0.11",
        deployed_app=tmp_path / "missing.app",
        package_lock=package_lock,
    )

    assert result.target_version == "1.0.11"


@pytest.mark.parametrize("version", ["1.0", "v1.0.11", "1.0.11-beta", "one"])
def test_non_strict_semver_is_rejected(tmp_path: Path, version: str) -> None:
    module = _load_module()

    with pytest.raises(module.VersionIncrementError, match="x\\.y\\.z"):
        module.check_release_version_increment(
            target_version=version,
            deployed_app=tmp_path / "missing.app",
        )


def test_build_script_enforces_gate_only_for_new_builds() -> None:
    text = (ROOT / "build_and_deploy.sh").read_text(encoding="utf-8")

    assert "check_release_version_increment.py" in text
    assert '--package-lock "$CONSOLE_DIR/package-lock.json"' in text
    assert 'if [[ "$SKIP_BUILD" -eq 1 ]]; then' in text
    assert "Skipping release version increment gate for --skip-build" in text
