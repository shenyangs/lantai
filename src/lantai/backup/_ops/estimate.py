# -*- coding: utf-8 -*-
"""Read-only size estimation for backup scope review."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config.utils import load_config
from ...constant import CONFIG_FILE, SECRET_DIR, WORKING_DIR
from ..models import (
    BackupEstimate,
    BackupEstimateComponent,
    CreateBackupRequest,
)
from .scope import resolve_requested_agent_ids


def _path_stats(path: Path) -> tuple[int, int]:
    if path.is_file():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 0, 0
    if not path.is_dir():
        return 0, 0
    root = path.expanduser().resolve()
    files = 0
    total_bytes = 0
    for entry in root.rglob("*"):
        if not entry.is_file():
            continue
        try:
            resolved = entry.resolve()
            resolved.relative_to(root)
            total_bytes += resolved.stat().st_size
            files += 1
        except (OSError, ValueError):
            continue
    return files, total_bytes


def _component(label: str, path: Path) -> BackupEstimateComponent:
    files, total_bytes = _path_stats(path)
    return BackupEstimateComponent(label=label, files=files, bytes=total_bytes)


def estimate_backup(req: CreateBackupRequest) -> BackupEstimate:
    """Estimate uncompressed bytes for the exact requested backup scope."""
    config = load_config()
    components: list[BackupEstimateComponent] = []
    missing_agents: list[str] = []
    if req.scope.include_agents:
        for agent_id in resolve_requested_agent_ids(
            req.scope,
            req.agents,
            config,
        ):
            ref: Any = config.agents.profiles.get(agent_id)
            if ref is None:
                missing_agents.append(agent_id)
                continue
            components.append(
                _component(f"智能体 · {agent_id}", Path(ref.workspace_dir))
            )
    if req.scope.include_global_config:
        components.append(_component("全局设置", WORKING_DIR / CONFIG_FILE))
    if req.scope.include_secrets:
        components.append(_component("密钥与凭据", SECRET_DIR))
    if req.scope.include_skill_pool:
        from ...agents.skills_manager import get_skill_pool_dir

        components.append(_component("技能库", get_skill_pool_dir()))
    return BackupEstimate(
        files=sum(item.files for item in components),
        bytes=sum(item.bytes for item in components),
        components=components,
        missing_agents=missing_agents,
    )
