#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Finalize the frozen macOS backend as an identifiable helper app."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


BACKEND_ROOT_NAME = "lantai-server"
BACKEND_APP_NAME = "Lantai Backend.app"
BACKEND_EXECUTABLE_RELATIVE = (
    Path(BACKEND_APP_NAME) / "Contents" / "MacOS" / "lantai-server"
)


@dataclass(frozen=True)
class BackendBundleLayout:
    root: Path
    app: Path
    executable: Path
    launcher: Path


def finalize_backend_bundle_layout(dist_dir: Path) -> BackendBundleLayout:
    """Keep one app-bundled runtime plus a compatibility launcher.

    PyInstaller emits both the original COLLECT directory and the BUNDLE. The
    BUNDLE is the runtime we ship because macOS privacy services require a
    stable application identity and Info.plist. The small launcher preserves
    existing developer smoke commands while Electron uses the bundle binary
    directly.
    """

    dist_dir = Path(dist_dir)
    collected_root = dist_dir / BACKEND_ROOT_NAME
    source_app = dist_dir / BACKEND_APP_NAME
    source_executable = source_app / "Contents" / "MacOS" / "lantai-server"
    if not source_executable.is_file():
        raise RuntimeError(
            f"PyInstaller backend helper app is incomplete: {source_executable}"
        )

    if collected_root.exists():
        shutil.rmtree(collected_root)
    collected_root.mkdir(parents=True)

    bundled_app = collected_root / BACKEND_APP_NAME
    shutil.move(str(source_app), str(bundled_app))
    executable = collected_root / BACKEND_EXECUTABLE_RELATIVE

    launcher = collected_root / "lantai-server"
    launcher.write_text(
        "#!/bin/sh\n"
        'backend_root=$(dirname "$0")\n'
        'exec "$backend_root/Lantai Backend.app/Contents/MacOS/lantai-server" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    return BackendBundleLayout(
        root=collected_root,
        app=bundled_app,
        executable=executable,
        launcher=launcher,
    )
