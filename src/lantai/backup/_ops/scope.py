"""Canonical scope resolution shared by backup creation and estimation."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..models import BackupScope


def is_complete_disaster_scope(scope: BackupScope) -> bool:
    """Return whether *scope* promises a complete local disaster backup."""
    return bool(
        scope.include_agents
        and scope.include_global_config
        and scope.include_secrets
        and scope.include_skill_pool
        and not scope.sanitized
    )


def resolve_requested_agent_ids(
    scope: BackupScope,
    requested_agent_ids: Iterable[str],
    config: Any,
) -> list[str]:
    """Resolve agent IDs without allowing a complete scope to omit profiles."""
    if not scope.include_agents:
        return []
    if is_complete_disaster_scope(scope):
        return list(config.agents.profiles)
    return list(requested_agent_ids)
