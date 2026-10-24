from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.app.routers.skills import router


def test_wpsnote_status_reports_the_desktop_installed_cli(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "wpsnote-cli"
    executable.write_text(
        """#!/bin/sh
if [ "$1" = "--version" ]; then
  printf 'wpsnote-cli 1.0.0\\n'
  exit 0
fi
if [ "$1" = "status" ]; then
  printf 'Connected to WPS Note · 24 tools\\n'
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("WPSNOTE_CLI_PATH", str(executable))
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get("/skills/integrations/wps-note/status")

    assert response.status_code == 200
    assert response.json()["installed"] is True
    assert response.json()["connected"] is True
    assert response.json()["tool_count"] == 24
    assert response.json()["install_method"] == "wps_note_desktop"


def test_wpsnote_connect_uses_the_builtin_desktop_flow(monkeypatch) -> None:
    class FakeService:
        def connect(self):
            from lantai.app.wpsnote_connection import WpsNoteConnectionStatus

            return WpsNoteConnectionStatus(
                installed=False,
                desktop_installed=True,
                connected=False,
                launch_requested=True,
                next_step="enable_in_wps_note",
            )

    monkeypatch.setattr(
        "lantai.app.routers.skills.WpsNoteConnectionService",
        FakeService,
    )
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).post("/skills/integrations/wps-note/connect")

    assert response.status_code == 200
    assert response.json()["desktop_installed"] is True
    assert response.json()["launch_requested"] is True
    assert response.json()["next_step"] == "enable_in_wps_note"
