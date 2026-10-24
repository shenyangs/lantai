from __future__ import annotations

import importlib.util
import json
import plistlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "audit_release_version_lineage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "audit_release_version_lineage", SCRIPT_PATH
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


def test_detects_public_channel_delivery_gap() -> None:
    module = _load_module()

    report = module.audit_version_lineage(
        source_version="1.0.29",
        installed_version="1.0.10",
        internal_version="1.0.29",
        public_version="0.0.11",
    )

    codes = {finding.code for finding in report.findings}
    assert "public_channel_behind_installed" in codes
    assert "source_not_public" in codes
    assert "installed_behind_internal" in codes
    assert len(report.blockers) == 2


def test_aligned_versions_are_ready() -> None:
    module = _load_module()

    report = module.audit_version_lineage(
        source_version="1.0.29",
        installed_version="1.0.29",
        internal_version="1.0.29",
        public_version="1.0.29",
    )

    assert not report.blockers
    assert report.findings[0].code == "version_lineage_aligned"


def test_next_patch_is_ready_before_publication() -> None:
    module = _load_module()

    report = module.audit_version_lineage(
        phase="prepublish",
        source_version="1.0.29",
        installed_version="1.0.28",
        internal_version="1.0.29",
        public_version="1.0.28",
    )

    assert not report.blockers
    assert report.findings[0].code == "installed_behind_internal"


def test_recovery_release_can_jump_to_a_higher_upgrade_line() -> None:
    module = _load_module()

    report = module.audit_version_lineage(
        phase="prepublish",
        source_version="1.0.29",
        installed_version="1.0.10",
        internal_version="1.0.29",
        public_version="0.0.11",
    )

    assert not report.blockers
    assert {finding.code for finding in report.findings} >= {
        "public_channel_behind_installed",
        "public_version_jump_planned",
        "installed_behind_internal",
    }


def test_prepublish_target_must_upgrade_the_installed_app() -> None:
    module = _load_module()

    report = module.audit_version_lineage(
        phase="prepublish",
        source_version="1.0.10",
        installed_version="1.0.10",
        internal_version="1.0.10",
        public_version="0.0.11",
    )

    assert {finding.code for finding in report.blockers} == {
        "source_will_not_upgrade_installed"
    }


def test_internal_discovery_uses_highest_dmg_or_app_version(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "澜台-1.0.28-arm64-internal.dmg").touch()
    (tmp_path / "澜台-1.0.29-arm64-internal.dmg").touch()
    _write_app_version(tmp_path / "澜台.app", "1.0.27")

    assert module._discover_internal_version(tmp_path) == "1.0.29"


def test_cli_json_and_strict_exit_expose_blockers(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_module()
    package_json = tmp_path / "package.json"
    package_json.write_text(json.dumps({"version": "1.0.29"}), encoding="utf-8")
    installed_app = tmp_path / "Applications" / "澜台.app"
    _write_app_version(installed_app, "1.0.10")
    internal_dir = tmp_path / "Downloads"
    internal_dir.mkdir()
    (internal_dir / "澜台-1.0.29-arm64-internal.dmg").touch()

    exit_code = module.main(
        [
            "--source-package",
            str(package_json),
            "--installed-app",
            str(installed_app),
            "--internal-dir",
            str(internal_dir),
            "--public-version",
            "0.0.11",
            "--format",
            "json",
            "--strict",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["source_version"] == "1.0.29"
    assert {finding["code"] for finding in payload["findings"]} >= {
        "public_channel_behind_installed",
        "source_not_public",
    }
