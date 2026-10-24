# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from scripts.backend_bundle_layout import (
    BACKEND_APP_NAME,
    BACKEND_EXECUTABLE_RELATIVE,
    finalize_backend_bundle_layout,
)


def test_finalize_backend_bundle_layout_keeps_compat_launcher(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    collected = dist_dir / "lantai-server"
    collected.mkdir(parents=True)
    (collected / "obsolete-runtime").write_text("old", encoding="utf-8")

    app = dist_dir / BACKEND_APP_NAME
    executable = app / "Contents" / "MacOS" / "lantai-server"
    executable.parent.mkdir(parents=True)
    executable.write_text("binary", encoding="utf-8")

    layout = finalize_backend_bundle_layout(dist_dir)

    assert layout.root == dist_dir / "lantai-server"
    assert layout.app == layout.root / BACKEND_APP_NAME
    assert layout.executable == layout.root / BACKEND_EXECUTABLE_RELATIVE
    assert layout.executable.read_text(encoding="utf-8") == "binary"
    assert not (layout.root / "obsolete-runtime").exists()
    assert layout.launcher.is_file()
    assert layout.launcher.stat().st_mode & 0o111
    assert "Lantai Backend.app/Contents/MacOS/lantai-server" in (
        layout.launcher.read_text(encoding="utf-8")
    )
