# -*- coding: utf-8 -*-
"""Tool Guard remembered approval management endpoint tests."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers import config as config_router
from lantai.config.config import (
    Config,
    ToolGuardCommandRuleConfig,
    ToolGuardRememberedApprovalConfig,
)
from lantai.config.utils import load_config, save_config

app = FastAPI()
app.include_router(config_router.router, prefix="/api")


def _remembered_rule(
    rule_id: str,
    workspace_dir: str,
) -> ToolGuardRememberedApprovalConfig:
    return ToolGuardRememberedApprovalConfig(
        id=rule_id,
        workspace_dir=workspace_dir,
        tool_name="execute_shell_command",
        permission_profile="workspace",
        matched_rule_ids=["shell.install_dependency"],
        command_head="npm",
        risk_level="medium",
        created_at="2026-05-01T00:00:00+00:00",
        updated_at="2026-05-02T00:00:00+00:00",
    )


def _seed_config(tmp_path, monkeypatch, rules):
    monkeypatch.setattr("lantai.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("lantai.config.config.WORKING_DIR", tmp_path)
    config = Config()
    config.security.tool_guard.remembered_approvals = list(rules)
    save_config(config)


@pytest.mark.asyncio
async def test_list_remembered_approvals_can_filter_workspace(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    _seed_config(
        tmp_path,
        monkeypatch,
        [
            _remembered_rule("approval-a", str(workspace)),
            _remembered_rule("approval-b", str(other_workspace)),
        ],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/config/security/tool-guard/remembered-approvals",
            params={"workspace_dir": str(workspace)},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["remembered_approvals"][0]["id"] == "approval-a"


@pytest.mark.asyncio
async def test_delete_remembered_approval(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_config(
        tmp_path,
        monkeypatch,
        [_remembered_rule("approval-a", str(workspace))],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            "/api/config/security/tool-guard/remembered-approvals/approval-a",
        )

    assert response.status_code == 200
    body = response.json()
    assert body["removed"] is True
    assert body["count"] == 0
    assert load_config().security.tool_guard.remembered_approvals == []


@pytest.mark.asyncio
async def test_clear_remembered_approvals_preserves_other_workspaces(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    _seed_config(
        tmp_path,
        monkeypatch,
        [
            _remembered_rule("approval-a", str(workspace)),
            _remembered_rule("approval-b", str(other_workspace)),
        ],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            "/api/config/security/tool-guard/remembered-approvals",
            params={"workspace_dir": str(workspace)},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["removed"] == 1
    assert body["count"] == 1
    remaining = load_config().security.tool_guard.remembered_approvals
    assert [rule.id for rule in remaining] == ["approval-b"]


@pytest.mark.asyncio
async def test_tool_guard_config_round_trips_permission_mode_and_command_rules(
    tmp_path,
    monkeypatch,
):
    _seed_config(tmp_path, monkeypatch, [])
    payload = Config().security.tool_guard
    payload.permission_mode = "quiet"
    payload.default_workspace_dir = str(tmp_path / "workspace")
    payload.command_rules = [
        ToolGuardCommandRuleConfig(
            id="allow-git-status",
            effect="allow",
            tool_name="execute_shell_command",
            command_pattern=r"^git\s+status\b",
            description="allow git status",
        ),
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/security/tool-guard",
            json=payload.model_dump(mode="json"),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["permission_mode"] == "quiet"
    assert body["default_workspace_dir"] == str(tmp_path / "workspace")
    assert body["command_rules"][0]["id"] == "allow-git-status"
