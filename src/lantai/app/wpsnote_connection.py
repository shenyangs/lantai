# -*- coding: utf-8 -*-
"""Connection lifecycle for the official WPS Note desktop CLI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ..security.tool_execution_context import build_sandboxed_tool_env


WPSNOTE_REPOSITORY_URL = "https://github.com/wpsnote/wpsnote-skills"
WPSNOTE_DOWNLOAD_URL = "https://ainote.kdocs.cn/wps-notes-hub/"


class WpsNoteConnectionError(RuntimeError):
    """The WPS Note CLI was present but could not be inspected."""


@dataclass(frozen=True)
class WpsNoteConnectionStatus:
    installed: bool
    connected: bool
    desktop_installed: bool = False
    cli_version: str = ""
    tool_count: int = 0
    install_method: str = "wps_note_desktop"
    repository_url: str = WPSNOTE_REPOSITORY_URL
    download_url: str = WPSNOTE_DOWNLOAD_URL
    next_step: str = "install_wps_note"
    launch_requested: bool = False


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
CliFinder = Callable[[], Path | None]
DesktopFinder = Callable[[], Path | None]
DesktopLauncher = Callable[[], None]


def find_wpsnote_desktop() -> Path | None:
    """Find the official desktop app without scanning arbitrary user paths."""

    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/WPS笔记.app"),
                Path("/Applications/WPS Note.app"),
                Path.home() / "Applications" / "WPS笔记.app",
                Path.home() / "Applications" / "WPS Note.app",
            ]
        )
    elif sys.platform == "win32":
        local_app_data = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
        if local_app_data:
            root = Path(local_app_data)
            candidates.extend(
                [
                    root / "Programs" / "WPSNote" / "WPSNote.exe",
                    root / "WPSNote" / "WPSNote.exe",
                ]
            )
    else:
        detected = shutil.which("wpsnote") or shutil.which("wps-note")
        if detected:
            candidates.append(Path(detected))
    return next((path for path in candidates if path.exists()), None)


def launch_wpsnote_desktop() -> None:
    """Open the trusted official desktop app; authorization remains inside WPS."""

    app_path = find_wpsnote_desktop()
    if app_path is None:
        raise WpsNoteConnectionError("WPS Note Desktop is not installed.")
    if sys.platform == "darwin":
        command = ["open", str(app_path)]
    elif sys.platform == "win32":
        command = ["cmd", "/c", "start", "", str(app_path)]
    else:
        command = [str(app_path)]
    try:
        subprocess.Popen(  # noqa: S603 - executable is selected from trusted paths above.
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise WpsNoteConnectionError("WPS Note Desktop could not be opened.") from exc


def find_wpsnote_cli() -> Path | None:
    """Resolve the CLI installed by WPS Note Desktop."""

    candidates: list[Path] = []
    configured = str(os.environ.get("WPSNOTE_CLI_PATH", "") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    detected = shutil.which("wpsnote-cli")
    if detected:
        candidates.append(Path(detected))
    candidates.extend(
        [
            Path.home() / ".local" / "bin" / "wpsnote-cli",
            Path.home() / ".wpsnote-cli" / "bin" / "wpsnote-cli",
        ],
    )
    local_app_data = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "WPSNote" / "cli" / "wpsnote-cli.cmd",
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


class WpsNoteConnectionService:
    """Connect through WPS Note's own account-scoped CLI installer."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner = subprocess.run,
        cli_finder: CliFinder = find_wpsnote_cli,
        desktop_finder: DesktopFinder = find_wpsnote_desktop,
        desktop_launcher: DesktopLauncher = launch_wpsnote_desktop,
    ) -> None:
        self._command_runner = command_runner
        self._cli_finder = cli_finder
        self._desktop_finder = desktop_finder
        self._desktop_launcher = desktop_launcher

    def status(self) -> WpsNoteConnectionStatus:
        executable = self._cli_finder()
        desktop_installed = bool(self._desktop_finder())
        if executable is None:
            return WpsNoteConnectionStatus(
                installed=False,
                desktop_installed=desktop_installed,
                connected=False,
                next_step=(
                    "enable_in_wps_note"
                    if desktop_installed
                    else "install_wps_note"
                ),
            )

        version_result = self._run([str(executable), "--version"], timeout=10)
        version = (
            version_result.stdout.strip() if version_result.returncode == 0 else ""
        )
        status_result = self._run([str(executable), "status"], timeout=15)
        output = "\n".join(
            part.strip()
            for part in (status_result.stdout, status_result.stderr)
            if part and part.strip()
        )
        tool_match = re.search(r"(?P<count>\d+)\s+tools?\b", output, re.IGNORECASE)
        tool_count = int(tool_match.group("count")) if tool_match else 0
        normalized_output = output.lower()
        configuration_missing = any(
            marker in normalized_output
            for marker in (
                "not configured",
                "not connected",
                "wpsnote-cli setup",
                "未配置",
                "未连接",
            )
        )
        return WpsNoteConnectionStatus(
            installed=True,
            desktop_installed=desktop_installed,
            connected=status_result.returncode == 0 and not configuration_missing,
            cli_version=version,
            tool_count=tool_count,
            next_step=(
                "connected"
                if status_result.returncode == 0 and not configuration_missing
                else "enable_in_wps_note"
            ),
        )

    def connect(self) -> WpsNoteConnectionStatus:
        status = self.status()
        if status.connected or not status.desktop_installed:
            return status
        self._desktop_launcher()
        return replace(status, launch_requested=True)

    def _run(
        self,
        command: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        env = build_sandboxed_tool_env()
        configured = os.environ.get("WPSNOTE_CLI_PATH")
        if configured:
            env["WPSNOTE_CLI_PATH"] = configured
        try:
            return self._command_runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise WpsNoteConnectionError("WPS Note CLI status timed out.") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise WpsNoteConnectionError("WPS Note CLI could not run.") from exc
