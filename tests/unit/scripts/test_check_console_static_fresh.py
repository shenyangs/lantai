from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "check_console_static_fresh.py"
)
ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_console_static_fresh", SCRIPT_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_detects_stale_embedded_console_assets(tmp_path: Path) -> None:
    module = _load_module()
    _write(tmp_path / "console/dist/index.html", "new")
    _write(tmp_path / "console/dist/assets/app.js", "new-js")
    _write(tmp_path / "src/lantai/console/index.html", "old")
    _write(tmp_path / "src/lantai/console/assets/old.js", "old-js")

    drifts = module.check_console_static_fresh(tmp_path)

    assert {drift.kind for drift in drifts} == {
        "changed-file",
        "missing-embedded-file",
        "stale-embedded-file",
    }


def test_sync_replaces_embedded_console_assets(tmp_path: Path) -> None:
    module = _load_module()
    _write(tmp_path / "console/dist/index.html", "new")
    _write(tmp_path / "console/dist/assets/app.js", "new-js")
    _write(tmp_path / "src/lantai/console/index.html", "old")
    _write(tmp_path / "src/lantai/console/assets/old.js", "old-js")

    drifts = module.check_console_static_fresh(tmp_path, sync=True)

    assert drifts == []
    assert (tmp_path / "src/lantai/console/index.html").read_text() == "new"
    assert not (tmp_path / "src/lantai/console/assets/old.js").exists()


def test_release_scripts_use_console_static_sync_checker() -> None:
    checked_files = [
        ROOT / "scripts/wheel_build.sh",
        ROOT / "scripts/wheel_build.ps1",
        ROOT / "build_and_deploy.sh",
        ROOT / ".github/workflows/tests.yml",
    ]
    optional_publish_workflow = ROOT / ".github/workflows/publish-pypi.yml"
    if optional_publish_workflow.exists():
        checked_files.append(optional_publish_workflow)

    for path in checked_files:
        text = path.read_text(encoding="utf-8")
        assert "check_console_static_fresh.py --sync" in text
        assert "cp -R console/dist/* src/lantai/console/" not in text
        assert "rm -rf src/lantai/console/*" not in text
        assert 'cp -R "$CONSOLE_DIR/dist/"* "$CONSOLE_DEST/"' not in text
        assert "Copy-Item -Path (Join-Path $ConsoleDist \"*\")" not in text


def test_one_click_packagers_always_rebuild_the_wheel() -> None:
    macos = (ROOT / "scripts/pack/build_macos.sh").read_text(encoding="utf-8")
    windows = (ROOT / "scripts/pack/build_win.ps1").read_text(encoding="utf-8")

    assert "bash scripts/wheel_build.sh" in macos
    assert "already has wheel for version" not in macos
    assert 'whls=("${REPO_ROOT}/dist/lantai-${CURRENT_VERSION}-"*.whl)' not in macos

    assert "& $WheelBuildScript" in windows
    assert "already has wheel for version" not in windows
    assert 'lantai-$CurrentVersion-*.whl' not in windows
