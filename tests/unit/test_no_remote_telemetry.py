# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    "relative_path",
    [
        "console/electron/main.cjs",
        "console/src/main.tsx",
        "console/src/App.tsx",
        "src/lantai/app/_app.py",
        "src/lantai/app/routers/__init__.py",
        "src/lantai/app/routers/settings.py",
    ],
)
def test_runtime_has_no_remote_telemetry_entry_points(relative_path: str) -> None:
    content = (PROJECT_ROOT / relative_path).read_text("utf-8")

    for forbidden in (
        "/api/telemetry/events",
        "recordDesktopTelemetry",
        "installRendererTelemetry",
        "recordTelemetryError",
        "collect_and_upload_telemetry",
        "telemetry_error_middleware",
        "telemetry_router",
        "get_telemetry_settings",
    ):
        assert forbidden not in content


def test_repository_has_no_remote_telemetry_endpoint() -> None:
    for path in (
        PROJECT_ROOT / "src",
        PROJECT_ROOT / "console" / "electron",
        PROJECT_ROOT / "console" / "src",
    ):
        for source in path.rglob("*"):
            if not source.is_file() or source.suffix not in {
                ".py",
                ".ts",
                ".tsx",
                ".js",
                ".cjs",
            }:
                continue
            content = source.read_text("utf-8", errors="ignore")
            assert "lantaitelemetry-" not in content
            assert "LANTAI_TELEMETRY_ENDPOINT" not in content
            assert "recordTelemetry" not in content
