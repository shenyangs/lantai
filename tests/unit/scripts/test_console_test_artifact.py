"""CI assets must match the bytes, revision and platform manifest."""

import subprocess
import sys
import os
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts/console_test_artifact.py"
)


def call(root, mode, revision="abc123", platform="Linux"):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            mode,
            "--console",
            str(root),
            "--revision",
            revision,
            "--platform",
            platform,
        ],
        capture_output=True,
        text=True,
    )


def test_artifact_round_trip_rejects_stale_cross_platform_and_changed_bytes(
    tmp_path,
):
    (tmp_path / "dist").mkdir()
    index = tmp_path / "dist/index.html"
    index.write_text("<html>test</html>")
    assert call(tmp_path, "create").returncode == 0
    assert call(tmp_path, "verify").returncode == 0
    assert call(tmp_path, "verify", revision="stale").returncode != 0
    assert call(tmp_path, "verify", platform="Windows").returncode != 0
    index.write_text("changed")
    assert call(tmp_path, "verify").returncode != 0


def test_missing_extra_and_symlink_assets_fail_closed(tmp_path):
    assert call(tmp_path, "create").returncode != 0
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/index.html").write_text("ok")
    assert call(tmp_path, "create").returncode == 0
    extra = tmp_path / "dist/stale.js"
    extra.write_text("stale")
    assert call(tmp_path, "verify").returncode != 0
    extra.unlink()
    try:
        extra.symlink_to(tmp_path / "dist/index.html")
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows host has not granted symlink privileges")
        raise
    assert call(tmp_path, "create").returncode != 0
