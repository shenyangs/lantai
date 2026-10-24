# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.check_pyinstaller_spec_policy import (
    resolve_pyinstaller_policy,
)


def _write_plist(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict/>
</plist>
""",
        encoding="utf-8",
    )


def test_official_policy_maps_arch_and_uses_explicit_backend_entitlements(
    tmp_path: Path,
) -> None:
    entitlement = (
        tmp_path
        / "console"
        / "electron"
        / "entitlements"
        / "developer-id.inherit.plist"
    )
    _write_plist(entitlement)

    policy = resolve_pyinstaller_policy(
        tmp_path,
        {
            "LANTAI_PYINSTALLER_DISTRIBUTION": "official",
            "LANTAI_PYINSTALLER_TARGET_ARCH": "x64",
            "LANTAI_PYINSTALLER_CODESIGN_IDENTITY": (
                "Developer ID Application: Example (TEAMID)"
            ),
        },
    )

    assert policy.distribution == "official"
    assert policy.target_arch == "x86_64"
    assert (
        policy.codesign_identity
        == "Developer ID Application: Example (TEAMID)"
    )
    assert policy.entitlements_file == str(entitlement)
    assert policy.upx is False


def test_official_backend_entitlements_allow_bundled_python_dylibs() -> None:
    entitlement = Path(
        "console/electron/entitlements/developer-id.inherit.plist",
    )
    text = entitlement.read_text(encoding="utf-8")

    assert "com.apple.security.cs.disable-library-validation" in text


def test_mas_policy_uses_inherit_entitlements_and_explicit_identity(
    tmp_path: Path,
) -> None:
    entitlement = (
        tmp_path
        / "console"
        / "electron"
        / "entitlements"
        / "mas.inherit.plist"
    )
    _write_plist(entitlement)

    policy = resolve_pyinstaller_policy(
        tmp_path,
        {
            "LANTAI_PYINSTALLER_DISTRIBUTION": "mas",
            "LANTAI_PYINSTALLER_TARGET_ARCH": "arm64",
            "LANTAI_PYINSTALLER_CODESIGN_IDENTITY": (
                "Apple Distribution: Example (TEAMID)"
            ),
        },
    )

    assert policy.distribution == "mas"
    assert policy.target_arch == "arm64"
    assert policy.codesign_identity == "Apple Distribution: Example (TEAMID)"
    assert policy.entitlements_file == str(entitlement)
    assert policy.upx is False


def test_missing_required_entitlements_file_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="entitlements file"):
        resolve_pyinstaller_policy(
            tmp_path,
            {
                "LANTAI_PYINSTALLER_DISTRIBUTION": "mas",
                "LANTAI_PYINSTALLER_TARGET_ARCH": "arm64",
                "LANTAI_PYINSTALLER_CODESIGN_IDENTITY": "Apple Distribution",
            },
        )


def test_pyinstaller_spec_no_longer_uses_implicit_none_or_macos_upx() -> None:
    spec_text = Path("lantai-server.spec").read_text(encoding="utf-8")

    assert "resolve_pyinstaller_policy" in spec_text
    assert "target_arch=None" not in spec_text
    assert "codesign_identity=None" not in spec_text
    assert "entitlements_file=None" not in spec_text
    assert "upx=True" not in spec_text


def _literal_assignment(module: ast.Module, name: str):
    matches = [
        node.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        )
        and isinstance(node.value, (ast.List, ast.Tuple))
    ]
    assert len(matches) == 1
    return ast.literal_eval(matches[0])


def _package_loops(module: ast.Module):
    for node in module.body:
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "package"
        ):
            if isinstance(node.iter, (ast.Tuple, ast.List)):
                yield ast.literal_eval(node.iter), node


def _call_statements(loop: ast.For):
    return [ast.dump(node, include_attributes=False) for node in loop.body]


def _expected_call_statements(source: str):
    return [
        ast.dump(node, include_attributes=False)
        for node in ast.parse(source).body
    ]


def test_pyinstaller_spec_bundles_customization_registry_data() -> None:
    spec_text = Path("lantai-server.spec").read_text(encoding="utf-8")
    module = ast.parse(spec_text)

    assert (
        "src/lantai/customization/data",
        "lantai/customization/data",
    ) in _literal_assignment(module, "datas")
    assert Path("src/lantai/customization/data/registry.json").is_file()


def test_docx_keeps_physical_module_directories_for_relative_templates() -> None:
    module = ast.parse(Path('lantai-server.spec').read_text(encoding='utf-8'))
    analysis = next(node for node in ast.walk(module)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'Analysis')
    modes = next((ast.literal_eval(arg.value) for arg in analysis.keywords
                  if arg.arg == 'module_collection_mode'), {})
    # python-docx opens parts/../templates: the intermediate directory must
    # exist even when its code is also imported from the PYZ archive.
    assert modes.get('docx') == 'pyz+py'


def test_pyinstaller_spec_bundles_the_acp_runtime_and_metadata() -> None:
    spec_text = Path("lantai-server.spec").read_text(encoding="utf-8")
    module = ast.parse(spec_text)
    hiddenimports = _literal_assignment(module, "hiddenimports")

    assert "lantai.cli.acp_server_cmd" in hiddenimports
    assert "lantai.integrations.acp_server.server" in hiddenimports
    runtime_loops = [
        loop
        for packages, loop in _package_loops(module)
        if "acp" in packages
        and "guanlan" in packages
        and _call_statements(loop)
        == _expected_call_statements(
            "require_package(package)\ncollect_package(package)",
        )
    ]
    assert len(runtime_loops) == 1
    assert "acp" in ast.literal_eval(runtime_loops[0].iter)
    assert (
        _call_statements(runtime_loops[0])[0]
        == _expected_call_statements("require_package(package)")[0]
    )
    assert (
        _call_statements(runtime_loops[0])[1]
        == _expected_call_statements("collect_package(package)")[0]
    )
    metadata_loops = [
        loop
        for packages, loop in _package_loops(module)
        if tuple(packages) == ("acp", "fastmcp", "guanlan", "mcp")
    ]
    assert len(metadata_loops) == 1
    assert _call_statements(metadata_loops[0]) == _expected_call_statements(
        "add_dist_info(package)",
    )


def test_pyinstaller_backend_is_an_identifiable_calendar_helper_app() -> None:
    spec_text = Path("lantai-server.spec").read_text(encoding="utf-8")
    build_script = Path("electron/build-python.py").read_text(encoding="utf-8")

    assert "BUNDLE(" in spec_text
    assert "Lantai Backend.app" in spec_text
    assert "com.lantai.app.backend" in spec_text
    assert "NSCalendars" not in spec_text
    assert "EventKit" not in spec_text
    assert "'LSUIElement': True" not in spec_text
    assert "'LSBackgroundOnly': False" not in spec_text
    assert "finalize_backend_bundle_layout" in build_script


def test_release_scripts_pass_pyinstaller_policy_env() -> None:
    project_root = Path(__file__).resolve().parents[3]
    main_script = (project_root / "build_and_deploy.sh").read_text(
        encoding="utf-8",
    )

    assert 'LANTAI_PYINSTALLER_DISTRIBUTION="official"' in main_script
    assert 'LANTAI_PYINSTALLER_TARGET_ARCH="$RELEASE_ARCH"' in main_script
    assert "LANTAI_PYINSTALLER_CODESIGN_IDENTITY" in main_script
