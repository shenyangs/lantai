# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers.mcp import router as mcp_router
from lantai.config.config import AgentProfileConfig, MCPConfig


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="default",
        config=AgentProfileConfig(
            id="default",
            name="Default",
            mcp=MCPConfig(clients={}),
        ),
        mcp_manager=None,
    )


@pytest.mark.asyncio
async def test_openchatcut_connection_journey_persists_a_reserved_local_mcp(
    monkeypatch,
):
    agent = _agent()
    saved: list[AgentProfileConfig] = []

    async def fake_get_agent(_request):
        return agent

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        fake_get_agent,
    )
    monkeypatch.setattr(
        "lantai.config.config.save_agent_config",
        lambda _agent_id, config: saved.append(config),
    )
    monkeypatch.setattr(
        "lantai.app.routers.mcp.schedule_agent_reload",
        lambda *_args: None,
    )
    app = FastAPI()
    app.include_router(mcp_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        initial = await client.get("/mcp/openchatcut")
        configured = await client.put(
            "/mcp/openchatcut",
            json={
                "endpoint": "http://127.0.0.1:5199/api/external-mcp/mcp",
                "token": "local-openchatcut-token",
                "enabled": True,
            },
        )
        custom_clients = await client.get("/mcp")

    assert initial.status_code == 200
    assert initial.json() == {
        "client_key": "openchatcut",
        "name": "视频工程 · OpenChatCut",
        "description": "让智能体通过本机 MCP 编辑可审阅、可撤销的视频工程。",
        "configured": False,
        "enabled": False,
        "endpoint": "http://127.0.0.1:5199/api/external-mcp/mcp",
        "token_configured": False,
        "connection_status": "not_configured",
        "repository_url": "https://github.com/0xsline/OpenChatCut",
        "docs_url": (
            "https://github.com/0xsline/OpenChatCut/blob/main/README_ZH.md"
        ),
        "license": "AGPL-3.0-or-later",
        "approval_mode": "manual",
    }
    assert configured.status_code == 200
    assert configured.json()["configured"] is True
    assert configured.json()["enabled"] is True
    assert configured.json()["token_configured"] is True
    assert configured.json()["connection_status"] == "connecting"
    stored = agent.config.mcp.clients["openchatcut"]
    assert stored.transport == "streamable_http"
    assert stored.url == "http://127.0.0.1:5199/api/external-mcp/mcp"
    assert stored.headers == {
        "Authorization": "Bearer local-openchatcut-token",
        "X-OpenChatCut-MCP-Client": "lantai",
        "X-OpenChatCut-MCP-Surface": "external",
    }
    assert stored.allow_private_network is False
    assert stored.allowed_tools == []
    assert custom_clients.json() == []
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_openchatcut_connector_rejects_non_loopback_endpoints(
    monkeypatch,
):
    agent = _agent()

    async def fake_get_agent(_request):
        return agent

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        fake_get_agent,
    )
    app = FastAPI()
    app.include_router(mcp_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/mcp/openchatcut",
            json={
                "endpoint": "https://editor.example/api/external-mcp/mcp",
                "token": "must-not-be-saved",
                "enabled": True,
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "OpenChatCut MCP 只允许连接本机地址"
    assert agent.config.mcp.clients == {}


@pytest.mark.asyncio
async def test_openchatcut_reserved_key_cannot_bypass_curated_setup(
    monkeypatch,
):
    agent = _agent()

    async def fake_get_agent(_request):
        return agent

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        fake_get_agent,
    )
    app = FastAPI()
    app.include_router(mcp_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/mcp",
            json={
                "client_key": "openchatcut",
                "client": {
                    "name": "Bypass",
                    "enabled": True,
                    "transport": "streamable_http",
                    "url": "https://editor.example/mcp",
                },
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "MCP client 'openchatcut' must be configured through its connector"
    )
    assert agent.config.mcp.clients == {}


@pytest.mark.asyncio
async def test_openchatcut_update_can_keep_saved_token_when_disabling(
    monkeypatch,
):
    agent = _agent()

    async def fake_get_agent(_request):
        return agent

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        fake_get_agent,
    )
    monkeypatch.setattr(
        "lantai.config.config.save_agent_config",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "lantai.app.routers.mcp.schedule_agent_reload",
        lambda *_args: None,
    )
    app = FastAPI()
    app.include_router(mcp_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.put(
            "/mcp/openchatcut",
            json={"token": "keep-this-token", "enabled": True},
        )
        response = await client.put(
            "/mcp/openchatcut",
            json={"enabled": False},
        )

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["token_configured"] is True
    assert "keep-this-token" not in response.text
    assert (
        agent.config.mcp.clients["openchatcut"].headers["Authorization"]
        == "Bearer keep-this-token"
    )
