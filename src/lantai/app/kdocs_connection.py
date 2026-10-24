# -*- coding: utf-8 -*-
"""Secure connection boundary for the bundled KDocs Skill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..security.tool_execution_context import build_sandboxed_tool_env


KDOCS_TOKEN_URL = "https://www.kdocs.cn/latest"


class KdocsConnectionError(RuntimeError):
    """A KDocs CLI install or authentication operation failed."""


@dataclass(frozen=True)
class KdocsConnectionStatus:
    installed: bool
    authenticated: bool
    cli_version: str = ""
    token_url: str = KDOCS_TOKEN_URL


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
CliFinder = Callable[[], Path | None]


def find_kdocs_cli() -> Path | None:
    """Resolve the official CLI without relying on a login-shell PATH."""

    candidates: list[Path] = []
    configured_dir = str(os.environ.get("KDOCS_CLI_DIR", "") or "").strip()
    if configured_dir:
        candidates.append(Path(configured_dir).expanduser() / "kdocs-cli")
        candidates.append(Path(configured_dir).expanduser() / "kdocs-cli.exe")
    detected = shutil.which("kdocs-cli")
    if detected:
        candidates.append(Path(detected))
    candidates.append(Path.home() / ".local" / "bin" / "kdocs-cli")
    local_app_data = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    if local_app_data:
        candidates.append(Path(local_app_data) / "kdocs-cli" / "kdocs-cli.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


class KdocsConnectionService:
    """Install, authenticate, and inspect the official ``kdocs-cli``."""

    def __init__(
        self,
        *,
        skill_dir: Path,
        command_runner: CommandRunner = subprocess.run,
        cli_finder: CliFinder = find_kdocs_cli,
        platform_name: str = sys.platform,
    ) -> None:
        self._skill_dir = Path(skill_dir)
        self._command_runner = command_runner
        self._cli_finder = cli_finder
        self._platform_name = platform_name

    def status(self) -> KdocsConnectionStatus:
        executable = self._cli_finder()
        if executable is None:
            return KdocsConnectionStatus(installed=False, authenticated=False)

        version_result = self._run([str(executable), "version"], timeout=10)
        version = (
            version_result.stdout.strip() if version_result.returncode == 0 else ""
        )
        auth_result = self._run(
            [str(executable), "auth", "status"],
            timeout=15,
        )
        authenticated = False
        if auth_result.returncode == 0:
            try:
                payload = json.loads(auth_result.stdout or "{}")
                authenticated = bool(payload.get("authenticated", False))
            except (json.JSONDecodeError, AttributeError):
                authenticated = False
        if authenticated:
            authenticated = self._can_read_cloud_documents(executable)
        return KdocsConnectionStatus(
            installed=True,
            authenticated=authenticated,
            cli_version=version,
        )

    def connect(self, token: str) -> KdocsConnectionStatus:
        from .kdocs_read_cache import clear_read_cache
        clear_read_cache()
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise KdocsConnectionError("A KDocs Token is required.")
        executable = self._cli_finder()
        if executable is None:
            self._install_cli()
            executable = self._cli_finder()
        if executable is None:
            raise KdocsConnectionError(
                "The official KDocs CLI could not be installed.",
            )

        result = self._run(
            [str(executable), "auth", "set-token", normalized_token],
            timeout=30,
        )
        if result.returncode != 0:
            raise KdocsConnectionError("KDocs rejected the supplied Token.")

        status = self.status()
        if not status.authenticated:
            raise KdocsConnectionError(
                "KDocs cloud document access could not be verified. "
                "Please obtain a new Token and try again.",
            )
        return status

    def disconnect(self) -> KdocsConnectionStatus:
        from .kdocs_read_cache import clear_read_cache
        clear_read_cache()
        executable = self._cli_finder()
        if executable is None:
            return KdocsConnectionStatus(installed=False, authenticated=False)
        result = self._run(
            [str(executable), "auth", "logout"],
            timeout=30,
        )
        if result.returncode != 0:
            raise KdocsConnectionError("KDocs could not remove the saved Token.")
        return self.status()

    def _install_cli(self) -> None:
        if self._platform_name.startswith("win"):
            setup_script = self._skill_dir / "scripts" / "setup.ps1"
            powershell = (
                shutil.which("powershell") or shutil.which("pwsh") or "powershell"
            )
            command = [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(setup_script),
            ]
        else:
            setup_script = self._skill_dir / "scripts" / "setup.sh"
            command = ["/bin/sh", str(setup_script)]
        if not setup_script.is_file():
            raise KdocsConnectionError("The KDocs CLI installer is unavailable.")
        result = self._run(command, timeout=180)
        if result.returncode != 0:
            raise KdocsConnectionError("The official KDocs CLI could not be installed.")

    def _can_read_cloud_documents(self, executable: Path) -> bool:
        """Verify the saved credential against a minimal read-only API call."""

        result = self._run(
            [
                str(executable),
                "drive",
                "list-my-files",
                '{"page_size":1}',
                "--compact",
            ],
            timeout=30,
        )
        if result.returncode != 0:
            raise KdocsConnectionError(
                "KDocs cloud document access could not be verified.",
            )
        try:
            payload = json.loads(result.stdout or "{}")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise KdocsConnectionError(
                "KDocs cloud document access returned an invalid response.",
            ) from exc
        code = payload.get("code")
        if str(code) == "400006":
            return False
        if code is None:
            raise KdocsConnectionError(
                "KDocs cloud document access returned an invalid response.",
            )
        # Some accounts cannot resolve a default drive (400001), but their
        # Token is valid and link-based read_file calls still work. Only the
        # official authentication failure code proves the credential invalid.
        return True

    def _run(
        self,
        command: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        env = build_sandboxed_tool_env()
        for name in ("KDOCS_CLI_CDN", "KDOCS_CLI_DIR", "KDOCS_CLI_VERSION"):
            value = os.environ.get(name)
            if value:
                env[name] = value
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
            raise KdocsConnectionError("The KDocs command timed out.") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise KdocsConnectionError("The KDocs command could not run.") from exc
