"""Resolve explicit execution and persistence roots for one agent turn."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from lantai.config.context import WorkspaceContext

from .workspace_grants import (
    WorkspaceGrantError,
    WorkspaceGrantRegistry,
    default_workspace_grant_registry,
)


class WorkspaceContextError(ValueError):
    """The requested roots do not satisfy workspace authorization policy."""


def _managed_path(
    raw_path: object,
    *,
    agent_home: Path,
    label: str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return agent_home
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = agent_home / candidate
    resolved = candidate.resolve(strict=False)
    if resolved != agent_home and agent_home not in resolved.parents:
        raise WorkspaceContextError(f"{label} must stay inside the agent home")
    return resolved


def resolve_workspace_context(
    *,
    agent_home: Path | str,
    agent_id: str,
    channel_meta: Mapping[str, object] | None,
    grant_registry: WorkspaceGrantRegistry | None = None,
) -> WorkspaceContext:
    """Build a context without ever treating an external raw path as authority."""

    resolved_agent_home = Path(agent_home).expanduser().resolve(strict=False)
    meta = channel_meta or {}
    state_root = _managed_path(
        meta.get("state_workspace_dir")
        or meta.get("topic_workspace_dir"),
        agent_home=resolved_agent_home,
        label="State workspace",
    )
    grant_id = str(meta.get("workspace_grant_id") or "").strip() or None
    if grant_id:
        try:
            binding = (
                grant_registry or default_workspace_grant_registry
            ).resolve(
                grant_id,
                agent_id=agent_id,
                operation="read",
            )
        except WorkspaceGrantError as error:
            raise WorkspaceContextError(
                f"Workspace grant is invalid: {error}",
            ) from error
        return WorkspaceContext(
            agent_home=resolved_agent_home,
            state_root=state_root,
            project_root=binding.project_root,
            workspace_grant_id=binding.grant_id,
            access_mode=binding.access_mode,
            trust_state=binding.trust_state,
        )

    project_root = _managed_path(
        meta.get("workspace_dir")
        or meta.get("manual_workspace_dir")
        or meta.get("coding_workspace_dir"),
        agent_home=resolved_agent_home,
        label="Project workspace without a grant",
    )
    return WorkspaceContext(
        agent_home=resolved_agent_home,
        state_root=state_root,
        project_root=project_root,
        workspace_grant_id=None,
        access_mode="read_write",
        trust_state="restricted",
    )
