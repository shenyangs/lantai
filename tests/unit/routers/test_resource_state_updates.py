# -*- coding: utf-8 -*-
"""Versioned desired-state updates for capability switches."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers import mcp as mcp_router
from lantai.app.routers import tools as tools_router
from lantai.config.config import (
    BuiltinToolConfig,
    MCPClientConfig,
    MCPConfig,
)


app = FastAPI()
app.include_router(tools_router.router, prefix="/api")
app.include_router(mcp_router.router, prefix="/api")


async def test_tool_enabled_update_uses_desired_state_and_rejects_stale_revision(
    monkeypatch,
) -> None:
    async def _workspace_for_request(_request):
        return SimpleNamespace(agent_id="agent-1")

    tool = BuiltinToolConfig(
        name="optional_tool",
        enabled=False,
        description="Optional",
    )
    agent_config = SimpleNamespace(
        tools=SimpleNamespace(builtin_tools={"optional_tool": tool}),
    )
    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        _workspace_for_request,
    )
    monkeypatch.setattr(
        "lantai.config.config.load_agent_config",
        lambda _agent_id: agent_config,
    )
    monkeypatch.setattr(
        "lantai.config.config.save_agent_config",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "lantai.plugins.registry.PluginRegistry",
        lambda: SimpleNamespace(get_plugin_id_for_tool=lambda _name: "plugin"),
    )
    monkeypatch.setattr(tools_router, "schedule_agent_reload", lambda *_args: None)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        enabled = await client.patch(
            "/api/tools/optional_tool/enabled",
            json={"enabled": True, "expected_revision": 0},
        )
        stale = await client.patch(
            "/api/tools/optional_tool/enabled",
            json={"enabled": False, "expected_revision": 0},
        )

    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["revision"] == 1
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_revision"] == 1
    assert tool.enabled is True


async def test_mcp_enabled_update_rejects_stale_resource_revision(
    monkeypatch,
) -> None:
    client_config = MCPClientConfig(
        name="Demo",
        command="demo-mcp",
        enabled=False,
    )
    agent = SimpleNamespace(
        agent_id="agent-1",
        config=SimpleNamespace(
            mcp=MCPConfig(clients={"demo": client_config}),
        ),
    )

    async def _agent_for_request(_request):
        return agent

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        _agent_for_request,
    )
    monkeypatch.setattr(
        "lantai.config.config.save_agent_config",
        lambda *_args: None,
    )
    monkeypatch.setattr(mcp_router, "schedule_agent_reload", lambda *_args: None)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        enabled = await client.put(
            "/api/mcp/demo",
            json={"enabled": True, "expected_revision": 0},
        )
        stale = await client.put(
            "/api/mcp/demo",
            json={"enabled": False, "expected_revision": 0},
        )

    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["revision"] == 1
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_revision"] == 1
    assert agent.config.mcp.clients["demo"].enabled is True
