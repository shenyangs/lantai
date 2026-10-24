from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers.mcp import router as mcp_router
from lantai.app.mcp.catalog import (
    OFFICIAL_MCP_PRESETS_BY_KEY,
    register_official_connector_oauth,
)
from lantai.app.mcp.lifecycle import ConnectorLifecycleError
from lantai.config.config import AgentProfileConfig, MCPConfig, MCPOAuthConfig


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
async def test_catalog_lists_only_verified_official_remote_connectors(monkeypatch):
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
        response = await client.get("/mcp/catalog")

    assert response.status_code == 200
    assert [item["key"] for item in response.json()] == [
        "notion",
        "linear",
        "figma",
    ]
    assert all(item["official"] for item in response.json())
    assert all(item["configured"] is False for item in response.json())


@pytest.mark.asyncio
async def test_installing_catalog_connector_persists_discovered_oauth(monkeypatch):
    agent = _agent()
    saved = []

    async def fake_get_agent(_request):
        return agent

    async def fake_register(preset, *, redirect_uri, **_kwargs):
        assert preset.key == "notion"
        assert redirect_uri == "http://test/api/mcp/oauth/callback"
        return MCPOAuthConfig(
            enabled=True,
            authorization_url="https://mcp.notion.com/authorize",
            token_url="https://mcp.notion.com/token",
            client_id="registered-desktop-client",
            scopes=["default"],
        )

    monkeypatch.setattr(
        "lantai.app.agent_context.get_agent_for_request",
        fake_get_agent,
    )
    monkeypatch.setattr(
        "lantai.app.routers.mcp.register_official_connector_oauth",
        fake_register,
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
        response = await client.post("/mcp/catalog/notion/install")

    assert response.status_code == 201
    assert response.json()["key"] == "notion"
    assert response.json()["oauth"]["client_id"] == "registered-desktop-client"
    assert agent.config.mcp.clients["notion"].url == "https://mcp.notion.com/mcp"
    assert agent.config.mcp.clients["notion"].oauth.enabled is True
    assert len(saved) == 1


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeOAuthClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return _FakeResponse(self.payloads[url])


@pytest.mark.asyncio
async def test_oauth_registration_uses_protected_resource_discovery_and_pkce_client():
    preset = OFFICIAL_MCP_PRESETS_BY_KEY["notion"]
    client = _FakeOAuthClient(
        {
            preset.resource_metadata_url: {
                "authorization_servers": ["https://mcp.notion.com"],
            },
            preset.authorization_metadata_url: {
                "issuer": "https://mcp.notion.com",
                "authorization_endpoint": "https://mcp.notion.com/authorize",
                "token_endpoint": "https://mcp.notion.com/token",
                "registration_endpoint": "https://mcp.notion.com/register",
                "token_endpoint_auth_methods_supported": ["none"],
            },
            "https://mcp.notion.com/register": {
                "client_id": "native-client",
            },
        },
    )

    oauth = await register_official_connector_oauth(
        preset,
        redirect_uri="http://127.0.0.1:20000/api/mcp/oauth/callback",
        http_client=client,
    )

    assert oauth.enabled is True
    assert oauth.client_id == "native-client"
    assert oauth.scopes == ["default"]
    registration = client.requests[-1][2]["json"]
    assert registration["application_type"] == "native"
    assert registration["token_endpoint_auth_method"] == "none"
    assert registration["redirect_uris"] == [
        "http://127.0.0.1:20000/api/mcp/oauth/callback",
    ]


@pytest.mark.asyncio
async def test_oauth_registration_rejects_mismatched_issuer():
    preset = OFFICIAL_MCP_PRESETS_BY_KEY["notion"]
    client = _FakeOAuthClient(
        {
            preset.resource_metadata_url: {
                "authorization_servers": ["https://mcp.notion.com"],
            },
            preset.authorization_metadata_url: {
                "issuer": "https://identity.evil.example",
                "authorization_endpoint": "https://identity.evil.example/authorize",
                "token_endpoint": "https://identity.evil.example/token",
                "registration_endpoint": "https://identity.evil.example/register",
            },
        },
    )

    with pytest.raises(ConnectorLifecycleError) as exc_info:
        await register_official_connector_oauth(
            preset,
            redirect_uri="http://127.0.0.1/callback",
            http_client=client,
        )

    assert exc_info.value.code == "oauth_issuer_mismatch"
