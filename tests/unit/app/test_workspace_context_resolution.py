from datetime import datetime, timezone
from pathlib import Path

import pytest

from lantai.app.workspace_context import (
    WorkspaceContextError,
    resolve_workspace_context,
)
from lantai.app.workspace_grants import (
    WorkspaceGrantRegistry,
    create_workspace_selection_receipt,
)


def test_external_project_requires_grant_while_state_stays_managed(
    tmp_path: Path,
) -> None:
    agent_home = tmp_path / "managed" / "agents" / "default"
    state_root = agent_home / "topic-workspaces" / "chat-1"
    project_root = tmp_path / "projects" / "external"
    state_root.mkdir(parents=True)
    project_root.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    registry = WorkspaceGrantRegistry(
        tmp_path / "managed" / "workspace-grants.json",
        clock=lambda: now,
    )

    with pytest.raises(WorkspaceContextError, match="grant"):
        resolve_workspace_context(
            agent_home=agent_home,
            agent_id="default",
            channel_meta={
                "workspace_dir": str(project_root),
                "state_workspace_dir": str(state_root),
            },
            grant_registry=registry,
        )

    grant = registry.claim_selection(
        create_workspace_selection_receipt(
            project_root=project_root,
            agent_id="default",
            desktop_token="secret",
            issued_at=now,
            nonce="context-selection-1",
        ),
        desktop_token="secret",
        agent_id="default",
    )
    context = resolve_workspace_context(
        agent_home=agent_home,
        agent_id="default",
        channel_meta={
            "workspace_dir": "/forged/path/is/ignored",
            "state_workspace_dir": str(state_root),
            "workspace_grant_id": grant.id,
        },
        grant_registry=registry,
    )

    assert context.project_root == project_root.resolve()
    assert context.state_root == state_root.resolve()
    assert context.agent_home == agent_home.resolve()
    assert context.workspace_grant_id == grant.id


def test_read_only_workspace_grant_resolves_without_write_authority(
    tmp_path: Path,
) -> None:
    agent_home = tmp_path / "managed" / "agents" / "default"
    state_root = agent_home / "topic-workspaces" / "chat-read-only"
    project_root = tmp_path / "projects" / "reference-only"
    state_root.mkdir(parents=True)
    project_root.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    registry = WorkspaceGrantRegistry(
        tmp_path / "managed" / "workspace-grants.json",
        clock=lambda: now,
    )
    grant = registry.claim_selection(
        create_workspace_selection_receipt(
            project_root=project_root,
            agent_id="default",
            desktop_token="secret",
            issued_at=now,
            nonce="context-selection-read-only",
            access_mode="read_only",
        ),
        desktop_token="secret",
        agent_id="default",
    )

    context = resolve_workspace_context(
        agent_home=agent_home,
        agent_id="default",
        channel_meta={
            "state_workspace_dir": str(state_root),
            "workspace_grant_id": grant.id,
        },
        grant_registry=registry,
    )

    assert context.project_root == project_root.resolve()
    assert context.access_mode == "read_only"
    assert context.workspace_grant_id == grant.id
