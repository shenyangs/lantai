from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.app.workspace_grants import (
    WorkspaceGrantRegistry,
    WorkspaceGrantError,
    create_workspace_grants_router,
    create_workspace_selection_receipt,
)


def test_signed_folder_selection_becomes_an_opaque_persistent_grant(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "outside-agent-home" / "demo-project"
    project_root.mkdir(parents=True)
    now = datetime(2026, 7, 27, 9, 30, tzinfo=timezone.utc)
    desktop_token = "desktop-secret"
    receipt = create_workspace_selection_receipt(
        project_root=project_root,
        agent_id="default",
        desktop_token=desktop_token,
        issued_at=now,
        nonce="selection-1",
    )
    registry = WorkspaceGrantRegistry(
        tmp_path / "managed-state" / "workspace-grants.json",
        clock=lambda: now,
    )

    grant = registry.claim_selection(
        receipt,
        desktop_token=desktop_token,
        agent_id="default",
    )
    binding = registry.resolve(
        grant.id,
        agent_id="default",
        operation="write",
    )

    assert grant.id.startswith("wsg_")
    assert project_root.as_posix() not in grant.id
    assert binding.grant_id == grant.id
    assert binding.project_root == project_root.resolve()
    assert binding.access_mode == "read_write"
    assert registry.list(agent_id="default") == [grant]


def test_claim_api_exchanges_a_desktop_receipt_for_a_grant_id(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "projects" / "alpha"
    project_root.mkdir(parents=True)
    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    receipt = create_workspace_selection_receipt(
        project_root=project_root,
        agent_id="agent-a",
        desktop_token="desktop-secret",
        issued_at=now,
        nonce="api-selection-1",
    )
    registry = WorkspaceGrantRegistry(
        tmp_path / "state" / "workspace-grants.json",
        clock=lambda: now,
    )
    app = FastAPI()
    app.include_router(
        create_workspace_grants_router(
            registry=registry,
            desktop_token_provider=lambda: "desktop-secret",
        ),
    )

    response = TestClient(app).post(
        "/workspace-grants/claim",
        headers={"X-Agent-Id": "agent-a"},
        json={"selection_receipt": receipt},
    )

    assert response.status_code == 201
    assert response.json() == {
        "id": response.json()["id"],
        "display_name": "alpha",
        "project_root": str(project_root.resolve()),
        "access_mode": "read_write",
        "trust_state": "trusted",
        "status": "active",
    }
    listed = TestClient(app).get(
        "/workspace-grants",
        headers={"X-Agent-Id": "agent-a"},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [response.json()["id"]]


def test_selection_receipts_reject_tampering_cross_agent_and_replay(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "projects" / "secure"
    project_root.mkdir(parents=True)
    now = datetime(2026, 7, 27, 11, 0, tzinfo=timezone.utc)
    receipt = create_workspace_selection_receipt(
        project_root=project_root,
        agent_id="agent-a",
        desktop_token="secret",
        issued_at=now,
        nonce="security-selection-1",
    )
    registry = WorkspaceGrantRegistry(
        tmp_path / "state" / "workspace-grants.json",
        clock=lambda: now,
    )

    replacement = "0" if receipt[-1] != "0" else "1"
    tampered_receipt = f"{receipt[:-1]}{replacement}"
    with pytest.raises(WorkspaceGrantError) as tampered:
        registry.claim_selection(
            tampered_receipt,
            desktop_token="secret",
            agent_id="agent-a",
        )
    assert tampered.value.code == "workspace_selection_receipt_invalid"

    with pytest.raises(WorkspaceGrantError) as cross_agent:
        registry.claim_selection(
            receipt,
            desktop_token="secret",
            agent_id="agent-b",
        )
    assert cross_agent.value.code == "workspace_grant_agent_mismatch"

    registry.claim_selection(
        receipt,
        desktop_token="secret",
        agent_id="agent-a",
    )
    with pytest.raises(WorkspaceGrantError) as replay:
        registry.claim_selection(
            receipt,
            desktop_token="secret",
            agent_id="agent-a",
        )
    assert replay.value.code == "workspace_selection_receipt_replayed"
