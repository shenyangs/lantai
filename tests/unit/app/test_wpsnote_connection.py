from __future__ import annotations

import subprocess
from pathlib import Path

from lantai.app.wpsnote_connection import WpsNoteConnectionService


def test_wpsnote_status_reflects_real_cli_connection(tmp_path: Path) -> None:
    executable = tmp_path / "wpsnote-cli"
    executable.touch()

    def runner(command: list[str], **_kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "wpsnote-cli 1.2.3\n", "")
        if command[1:] == ["status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "Connected to WPS Note · 24 tools\n",
                "",
            )
        raise AssertionError(command)

    status = WpsNoteConnectionService(
        command_runner=runner,
        cli_finder=lambda: executable,
    ).status()

    assert status.installed is True
    assert status.connected is True
    assert status.cli_version == "wpsnote-cli 1.2.3"
    assert status.tool_count == 24


def test_wpsnote_status_points_to_desktop_install_when_cli_is_missing() -> None:
    status = WpsNoteConnectionService(
        cli_finder=lambda: None,
        desktop_finder=lambda: True,
    ).status()

    assert status.installed is False
    assert status.desktop_installed is True
    assert status.connected is False
    assert status.install_method == "wps_note_desktop"
    assert status.next_step == "enable_in_wps_note"


def test_wpsnote_connect_launches_desktop_and_returns_actionable_status() -> None:
    launches: list[None] = []

    service = WpsNoteConnectionService(
        cli_finder=lambda: None,
        desktop_finder=lambda: True,
        desktop_launcher=lambda: launches.append(None),
    )

    status = service.connect()

    assert launches == [None]
    assert status.desktop_installed is True
    assert status.launch_requested is True
    assert status.next_step == "enable_in_wps_note"


def test_wpsnote_connect_does_not_launch_when_desktop_is_missing() -> None:
    launches: list[None] = []

    status = WpsNoteConnectionService(
        cli_finder=lambda: None,
        desktop_finder=lambda: False,
        desktop_launcher=lambda: launches.append(None),
    ).connect()

    assert launches == []
    assert status.desktop_installed is False
    assert status.next_step == "install_wps_note"
    assert status.download_url.startswith("https://ainote.kdocs.cn/")


def test_wpsnote_legacy_setup_hint_is_not_reported_as_connected(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "wpsnote-cli"
    executable.touch()

    def runner(command: list[str], **_kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "1.0.0\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            "Not configured. Run wpsnote-cli setup.\n",
            "",
        )

    status = WpsNoteConnectionService(
        command_runner=runner,
        cli_finder=lambda: executable,
    ).status()

    assert status.installed is True
    assert status.connected is False
