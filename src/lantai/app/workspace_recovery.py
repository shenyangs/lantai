# -*- coding: utf-8 -*-
"""Transactional recovery of desktop workspaces into managed storage."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config.config import AgentProfileConfig
from ..config.utils import load_config, save_config
from ..constant import WORKING_DIR
from .workspace_access import should_defer_desktop_workspace_io

_RECOVERY_LOCK = threading.Lock()
_SAFE_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class WorkspaceRecoveryError(ValueError):
    """Raised when a staged workspace cannot be adopted safely."""


def _lexical_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _write_agent_config(path: Path, config: AgentProfileConfig) -> None:
    temp_path = path.with_name(f"{path.name}.recovery.tmp")
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(
            config.model_dump(exclude_none=True),
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def _invalidate_agent_config_cache(agent_id: str) -> None:
    from ..config.utils import _agent_config_cache, _agent_config_lock

    with _agent_config_lock:
        _agent_config_cache.pop(agent_id, None)


def adopt_staged_desktop_workspace(
    *,
    agent_id: str,
    expected_source_dir: str,
    staged_workspace_dir: str,
    working_dir: str | Path = WORKING_DIR,
) -> dict:
    """Atomically adopt an Electron-staged workspace into managed storage."""

    if not _SAFE_AGENT_ID.fullmatch(agent_id):
        raise WorkspaceRecoveryError("Invalid agent ID for workspace recovery.")

    managed_root = _lexical_path(working_dir)
    import_root = managed_root / "workspace-imports"
    staged = _lexical_path(staged_workspace_dir)
    target = managed_root / "workspaces" / agent_id
    expected_source = _lexical_path(expected_source_dir)

    if staged.parent != import_root or not staged.name.startswith(f"{agent_id}-"):
        raise WorkspaceRecoveryError("Staged workspace is outside the import root.")
    if not staged.is_dir():
        raise WorkspaceRecoveryError("Staged workspace does not exist.")

    agent_config_path = staged / "agent.json"
    try:
        with open(agent_config_path, "r", encoding="utf-8") as file:
            agent_config = AgentProfileConfig.model_validate(json.load(file))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise WorkspaceRecoveryError(
            "Staged workspace has no valid agent.json.",
        ) from exc
    if agent_config.id != agent_id:
        raise WorkspaceRecoveryError("Staged workspace belongs to another agent.")

    with _RECOVERY_LOCK:
        config = load_config()
        agent_ref = config.agents.profiles.get(agent_id)
        if agent_ref is None:
            raise WorkspaceRecoveryError(f"Agent '{agent_id}' is not configured.")
        current_source = _lexical_path(agent_ref.workspace_dir)
        if current_source != expected_source:
            raise WorkspaceRecoveryError(
                "Agent workspace changed while recovery was in progress.",
            )
        if not should_defer_desktop_workspace_io(
            current_source,
            working_dir=managed_root,
        ):
            raise WorkspaceRecoveryError("Agent workspace is already managed.")

        agent_config.workspace_dir = str(target)
        _write_agent_config(agent_config_path, agent_config)

        backup_root = managed_root / "workspace-recovery-backups"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_root / f"{agent_id}-{timestamp}"
        backup_root.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)

        target_was_backed_up = False
        staged_was_moved = False
        try:
            if target.exists():
                suffix = 1
                while backup.exists():
                    backup = backup_root / f"{agent_id}-{timestamp}-{suffix}"
                    suffix += 1
                os.replace(target, backup)
                target_was_backed_up = True
            os.replace(staged, target)
            staged_was_moved = True

            agent_ref.workspace_dir = str(target)
            save_config(config)
        except Exception:
            if staged_was_moved and target.exists():
                os.replace(target, staged)
            if target_was_backed_up and backup.exists():
                os.replace(backup, target)
            raise

        _invalidate_agent_config_cache(agent_id)
        return {
            "success": True,
            "agent_id": agent_id,
            "workspace_dir": str(target),
            "backup_dir": str(backup) if target_was_backed_up else None,
            "source_preserved": True,
        }
