# -*- coding: utf-8 -*-
"""Shared execution boundaries for local tool subprocesses."""

from __future__ import annotations

import os
import sys
from pathlib import Path


_SAFE_ENV_EXACT = {
    "COLORTERM",
    "HOME",
    "LANG",
    "NO_COLOR",
    "OFFICECLI_PATH",
    "OFFICECLI_SKIP_UPDATE",
    "PATH",
    "PRESENTATION_NODE_EXECUTABLE",
    "PRESENTATION_NODE_MODE",
    "PRESENTATION_NODE_MODULES",
    "TERM",
    "TMP",
    "TMPDIR",
    "TEMP",
}

_SECRET_ENV_MARKERS = (
    "AUTH",
    "BEARER",
    "BRIDGE",
    "COOKIE",
    "CREDENTIAL",
    "DESKTOP",
    "KEY",
    "LANTAI",
    "OPENAI",
    "ANTHROPIC",
    "AZURE",
    "GEMINI",
    "GOOGLE",
    "PASSWORD",
    "PROVIDER",
    "SECRET",
    "TOKEN",
)


def build_sandboxed_tool_env(
    *,
    base_env: dict[str, str] | None = None,
    include_python_dir: bool = True,
) -> dict[str, str]:
    """Build a conservative subprocess environment for agent-run tools."""

    source = dict(base_env or os.environ)
    env: dict[str, str] = {}
    for key, value in source.items():
        normalized = str(key).upper()
        if _is_secret_env_key(normalized):
            continue
        if normalized in _SAFE_ENV_EXACT or normalized.startswith("LC_"):
            env[str(key)] = str(value)

    env["PATH"] = _safe_path(
        source.get("PATH", ""),
        include_python_dir=include_python_dir,
        officecli_path=source.get("OFFICECLI_PATH", ""),
    )
    # This nonce identifies the supervisor that owns the process, and grants
    # no API access. Take it only from the actual parent; configured tool/MCP
    # environments cannot replace it. Desktop authentication remains filtered.
    owner_nonce = os.environ.get("LANTAI_DESKTOP_INSTANCE_NONCE", "")
    if len(owner_nonce) == 64 and all(c in "0123456789abcdef" for c in owner_nonce):
        env["LANTAI_DESKTOP_INSTANCE_NONCE"] = owner_nonce
    return env


def _is_secret_env_key(normalized_key: str) -> bool:
    return any(marker in normalized_key for marker in _SECRET_ENV_MARKERS)


def _safe_path(
    raw_path: str,
    *,
    include_python_dir: bool,
    officecli_path: str = "",
) -> str:
    path_entries: list[str] = []

    def add_path(path: str | Path | None, *, require_exists: bool = True) -> None:
        if not path:
            return
        path_str = str(path)
        if require_exists and not Path(path_str).exists():
            return
        if path_str not in path_entries:
            path_entries.append(path_str)

    if include_python_dir:
        executable_dir = Path(sys.executable).parent
        add_path(executable_dir, require_exists=False)
        # PyInstaller places optional user-facing CLIs beside the frozen
        # backend under ``lantai-server/bin``.  The backend itself can resolve
        # those tools by absolute path, but agent shell commands also need the
        # same directory on PATH so a documented command such as ``officecli``
        # behaves identically in packaged and development runtimes.
        add_path(executable_dir / "bin")
    # The Office capability resolver may have selected a user-configured or
    # bundled executable outside the backend's own directory.  Shell tools
    # must resolve the same binary as the native Office/MCP integration;
    # otherwise the model can successfully call the logical ``officecli``
    # tool and then immediately receive exit 127 for the documented CLI.
    selected_officecli = str(officecli_path or "").strip()
    if selected_officecli:
        add_path(Path(selected_officecli).expanduser().parent)
    home = Path.home()
    for path in (
        home / ".local" / "bin",
        home / ".cargo" / "bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ):
        add_path(path)

    for path in str(raw_path or "").split(os.pathsep):
        add_path(path, require_exists=False)

    return os.pathsep.join(path_entries)
