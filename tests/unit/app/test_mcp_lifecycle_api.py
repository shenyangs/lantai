# -*- coding: utf-8 -*-
"""MCP lifecycle API user-journey tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers.mcp import router as mcp_router
from lantai.app.auth import _PUBLIC_PATHS
from lantai.config.config import (
    AgentProfileConfig,
    MCPClientConfig,
    MCPConfig,
    MCPOAuthConfig,
)


def _oauth(**updates) -> MCPOAuthConfig:
    data = {
        "enabled": True,
        "authorization_url": "https://identity.example/authorize",
        "token_url": "https://identity.example/token",
        "client_id": "desktop",
    }
    data.update(updates)
    return MCPOAuthConfig(**data)


class _RuntimeClient:
    def __init__(self):
        self.is_connected = True
        self._cached_tools = []

    async def list_tools(self):
        self._cached_tools = [SimpleNamespace(name="read")]
        return self._cached_tools

    async def list_resources(self):
        return [SimpleNamespace(uri="file:///guide.md", name="Guide")]

    async def list_resource_templates(self):
        return []

    async def read_resource(self, uri):
        return SimpleNamespace(contents=[{"uri": uri, "text": "guide"}])

    async def list_prompts(self):
        return [SimpleNamespace(name="review", description="Review input")]

    async def get_prompt(self, name, arguments):
        return SimpleNamespace(description=name, messages=[arguments])


class _Manager:
    def __init__(self):
        self._clients = {}

    async def replace_client(self, key, _config, timeout):
        assert timeout == 20.0
        self._clients[key] = _RuntimeClient()

    async def get_client(self, key):
        return self._clients.get(key)


def _agent() -> SimpleNamespace:
    config = AgentProfileConfig(
        id="default",
        name="Default",
        mcp=MCPConfig(
            clients={
                "docs": MCPClientConfig(
                    name="Docs",
                    transport="streamable_http",
                    url="https://mcp.example",
                    oauth=_oauth(),
                    allowed_tools=["read"],
                ),
            },
        ),
    )
    return SimpleNamespace(
        agent_id="default",
        config=config,
        mcp_manager=_Manager(),
    )


def test_oauth_callback_is_public_but_requires_single_use_state():
    assert "/api/mcp/oauth/callback" in _PUBLIC_PATHS


@pytest.mark.asyncio
async def test_authorize_probe_and_status_journey(monkeypatch):
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
        initial = await client.get("/mcp/docs/lifecycle")
        started = await client.post("/mcp/docs/oauth/start")

        assert initial.json()["status"] == "authorization_required"
        assert started.status_code == 200
        assert "code_challenge_method=S256" in started.json()["authorization_url"]

        agent.config.mcp.clients["docs"].oauth = _oauth(
            access_token="ready",
            expires_at=9_999_999_999,
        )
        probed = await client.post("/mcp/docs/probe")

    assert probed.status_code == 200
    assert probed.json()["status"] == "ready"
    assert probed.json()["tool_count"] == 1
    assert probed.json()["allowed_tools"] == ["read"]


@pytest.mark.asyncio
async def test_oauth_callback_is_single_use_and_persists_tokens(monkeypatch):
    agent = _agent()
    saved: list[AgentProfileConfig] = []

    async def fake_get_agent(_request):
        return agent

    async def fake_exchange(oauth, **_kwargs):
        return oauth.model_copy(
            update={"access_token": "new-access", "refresh_token": "new-refresh"},
        )

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        fake_get_agent,
    )
    monkeypatch.setattr(
        "lantai.config.config.load_agent_config",
        lambda _agent_id: agent.config,
    )
    monkeypatch.setattr(
        "lantai.config.config.save_agent_config",
        lambda _agent_id, config: saved.append(config),
    )
    monkeypatch.setattr("lantai.app.routers.mcp.exchange_oauth_code", fake_exchange)
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
        started = (await client.post("/mcp/docs/oauth/start")).json()
        callback = await client.get(
            "/mcp/oauth/callback",
            params={"state": started["state"], "code": "one-code"},
        )
        replay = await client.get(
            "/mcp/oauth/callback",
            params={"state": started["state"], "code": "one-code"},
        )

    assert callback.status_code == 200
    assert "连接成功" in callback.text
    assert replay.status_code == 400
    assert len(saved) == 1
    assert agent.config.mcp.clients["docs"].oauth.access_token == "new-access"


@pytest.mark.asyncio
async def test_resource_and_prompt_content_remain_explicitly_untrusted(monkeypatch):
    agent = _agent()
    agent.mcp_manager._clients["docs"] = _RuntimeClient()

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
        resource = await client.post(
            "/mcp/docs/resources/read",
            json={"uri": "file:///guide.md"},
        )
        prompt = await client.post(
            "/mcp/docs/prompts/get",
            json={"name": "review", "arguments": {"focus": "safety"}},
        )

    assert resource.status_code == 200
    assert resource.json()["trust"] == "untrusted_connector_content"
    assert prompt.status_code == 200
    assert prompt.json()["trust"] == "untrusted_connector_content"
