# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path


def test_dmg_installer_includes_applications_link_and_retina_background() -> None:
    root = Path.cwd()
    package = json.loads((root / "console" / "package.json").read_text(encoding="utf-8"))
    dmg = package["build"]["dmg"]

    assert dmg["background"] == "build/dmg-background-shenxingzhi.png"
    assert (root / "console" / dmg["background"]).is_file()
    assert (
        root / "console" / "build" / "dmg-background-shenxingzhi@2x.png"
    ).is_file()

    contents = dmg["contents"]
    assert len(contents) == 2
    app, applications = contents
    assert app == {"x": 625, "y": 165}
    assert applications == {
        "x": 625,
        "y": 345,
        "type": "link",
        "path": "/Applications",
    }

    window = dmg["window"]
    for item in contents:
        assert 0 < item["x"] < window["width"]
        assert 0 < item["y"] < window["height"]


def test_macos_bundle_does_not_request_removed_voice_permissions() -> None:
    package = json.loads(
        (Path.cwd() / "console" / "package.json").read_text(encoding="utf-8")
    )
    extend_info = package["build"]["mac"]["extendInfo"]

    assert "NSMicrophoneUsageDescription" not in extend_info
    assert "NSSpeechRecognitionUsageDescription" not in extend_info
    assert "澜台" in extend_info["NSAppleEventsUsageDescription"]
    assert "自动化" in extend_info["NSAppleEventsUsageDescription"]


def _function_body(text: str, name: str) -> str:
    marker = f"{name}() {{"
    start = text.index(marker)
    end = text.index("\n}\n\n", start)
    return text[start : end + 3]


def test_stapled_dmg_rebuild_uses_the_final_dmg_builder_layout() -> None:
    script = (Path.cwd() / "build_and_deploy.sh").read_text(encoding="utf-8")
    rebuild = _function_body(script, "rebuild_release_artifacts_from_stapled_app")
    validate = _function_body(script, "validate_release_dmg_installer_layout")

    assert "package_stapled_app_as_dmg" in rebuild
    assert "hdiutil create" not in rebuild
    assert '"$DMG_INSTALLER_LAYOUT_DIR/.DS_Store"' not in rebuild
    assert "validate_release_dmg_installer_layout" in rebuild

    assert '"$mount_dir/Applications"' in validate
    assert 'readlink "$mount_dir/Applications"' in validate
    assert '"$mount_dir/.DS_Store"' in validate
    assert '"$mount_dir/.background"' in validate
    assert '"$mount_dir/.background.tiff"' in validate
    assert "diskutil info" in validate
    assert "backgroundImageAlias" in validate


def test_stapled_dmg_rebuild_regenerates_finder_background_on_the_final_volume() -> None:
    """The final DMG must not inherit a volume-bound background alias.

    Finder stores a background image alias in .DS_Store.  Recreating a DMG by
    copying that file onto another HFS/APFS volume retains the old alias and
    can leave a white installer window even though the TIFF itself is present.
    Re-package the stapled app through electron-builder so dmgbuild creates the
    layout and background reference for the final volume.
    """
    script = (Path.cwd() / "build_and_deploy.sh").read_text(encoding="utf-8")
    rebuild = _function_body(script, "rebuild_release_artifacts_from_stapled_app")
    package = _function_body(script, "package_stapled_app_as_dmg")

    assert "--prepackaged" in package
    assert "--mac dmg" in package
    assert "--config.directories.output" in package
    assert "npm exec --offline -- electron-builder" in package
