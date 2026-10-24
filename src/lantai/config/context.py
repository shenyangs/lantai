# -*- coding: utf-8 -*-
"""Context variable for agent workspace directory.

This module provides a context variable to pass the agent's workspace
directory to tool functions, allowing them to resolve relative paths
correctly in a multi-agent environment.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class WorkspaceContext:
    """Explicit roots for one agent turn.

    ``project_root`` is the tool working directory. ``state_root`` is where
    Lantai-owned runtime data is persisted; it must never be inferred from the
    selected project path.
    """

    agent_home: Path
    state_root: Path
    project_root: Path
    workspace_grant_id: str | None = None
    access_mode: Literal["read_only", "read_write"] = "read_write"
    trust_state: Literal["trusted", "restricted"] = "restricted"


current_workspace_context: ContextVar[WorkspaceContext | None] = ContextVar(
    "current_workspace_context",
    default=None,
)

# Context variable to store the current agent's workspace directory
current_workspace_dir: ContextVar[Path | None] = ContextVar(
    "current_workspace_dir",
    default=None,
)


def get_current_workspace_dir() -> Path | None:
    """Get the current agent's workspace directory from context.

    Returns:
        Path to the current agent's workspace directory, or None if not set.
    """
    context = current_workspace_context.get()
    if context is not None:
        return context.project_root
    return current_workspace_dir.get()


def set_current_workspace_dir(workspace_dir: Path | None) -> None:
    """Set the current agent's workspace directory in context.

    Args:
        workspace_dir: Path to the agent's workspace directory.
    """
    current_workspace_dir.set(workspace_dir)


def get_current_workspace_context() -> WorkspaceContext | None:
    """Return the structured workspace context for the current turn."""

    return current_workspace_context.get()


def get_current_state_root() -> Path | None:
    """Return the application-managed state root for the current turn."""

    context = current_workspace_context.get()
    if context is not None:
        return context.state_root
    return current_workspace_dir.get()


def set_current_workspace_context(context: WorkspaceContext | None) -> None:
    """Set both the structured context and its legacy working-dir facade."""

    current_workspace_context.set(context)
    current_workspace_dir.set(context.project_root if context else None)


# Context variable to store the recent_max_bytes limit
current_recent_max_bytes: ContextVar[int | None] = ContextVar(
    "current_recent_max_bytes",
    default=None,
)


def get_current_recent_max_bytes() -> int | None:
    """Get the current agent's recent_max_bytes limit from context.

    Returns:
        Byte limit for recent tool output truncation, or None if not set.
    """
    return current_recent_max_bytes.get()


def set_current_recent_max_bytes(max_bytes: int | None) -> None:
    """Set the current agent's recent_max_bytes limit in context.

    Args:
        max_bytes: Byte limit for recent tool output truncation.
    """
    current_recent_max_bytes.set(max_bytes)


# Context variable to store the configured shell command timeout
current_shell_command_timeout: ContextVar[float | None] = ContextVar(
    "current_shell_command_timeout",
    default=None,
)


def get_current_shell_command_timeout() -> float | None:
    """Get the configured default timeout for execute_shell_command.

    Returns:
        Timeout in seconds, or None if not configured.
    """
    return current_shell_command_timeout.get()


def set_current_shell_command_timeout(timeout: float | None) -> None:
    """Set the configured default timeout for execute_shell_command.

    Args:
        timeout: Timeout in seconds.
    """
    current_shell_command_timeout.set(timeout)
