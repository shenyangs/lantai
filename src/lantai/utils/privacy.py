# -*- coding: utf-8 -*-
"""Local privacy migrations for data no longer retained by Lantai."""
from __future__ import annotations

import shutil
from pathlib import Path


LEGACY_TELEMETRY_DIR = ".telemetry"
LEGACY_TELEMETRY_MARKER = ".telemetry_collected"


def purge_legacy_telemetry_data(working_dir: Path) -> int:
    """Delete obsolete local telemetry queues and preferences.

    Lantai no longer collects or uploads product telemetry.  The cleanup is
    deliberately idempotent so every upgraded installation can discard any
    queue left by older versions before the rest of the backend starts.
    """

    removed = 0
    telemetry_dir = working_dir / LEGACY_TELEMETRY_DIR
    marker = working_dir / LEGACY_TELEMETRY_MARKER

    if telemetry_dir.exists() or telemetry_dir.is_symlink():
        if telemetry_dir.is_dir() and not telemetry_dir.is_symlink():
            shutil.rmtree(telemetry_dir)
        else:
            telemetry_dir.unlink()
        removed += 1

    if marker.exists() or marker.is_symlink():
        marker.unlink()
        removed += 1

    return removed
