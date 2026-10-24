# -*- coding: utf-8 -*-
"""Connector lifecycle contract tests."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from lantai.app.mcp.lifecycle import (
    _refresh_locks,
    ConnectorLifecycleError,
    OAuthPendingRegistry,
    exchange_oauth_code,
    oauth_access_token_valid,
    oauth_headers,
    prepare_client_oauth,
    refresh_oauth_token,
)
from lantai.config.config import MCPClientConfig, MCPOAuthConfig


def _oauth(**updates) -> MCPOAuthConfig:
    data = {
        "enabled": True,
        "authorization_url": "https://identity.example/authorize",
        "token_url": "https://identity.example/token",
        "client_id": "desktop-client",
        "scopes": ["tools.read", "tools.call"],
    }
    data.update(updates)
    return MCPOAuthConfig(**data)


def test_oauth_start_uses_pkce_and_state_is_single_use():
    registry = OAuthPendingRegistry()
    started = registry.create(
        agent_id="default",
        client_key="docs",
        oauth=_oauth(),
        redirect_uri="http://127.0.0.1:8088/api/mcp/oauth/callback",
        now=100,
    )

    query = parse_qs(urlparse(started.authorization_url).query)
    assert query["state"] == [started.state]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["tools.read tools.call"]
    pending = registry.consume(started.state, now=101)
    assert pending.agent_id == "default"
    assert len(pending.verifier) >= 43
    with pytest.raises(ConnectorLifecycleError, match="不存在"):
        registry.consume(started.state, now=102)


def test_oauth_state_expires():
    registry = OAuthPendingRegistry()
    started = registry.create(
        agent_id="default",
        client_key="docs",
        oauth=_oauth(),
        redirect_uri="http://localhost/callback",
        now=100,
    )
    with pytest.raises(ConnectorLifecycleError):
        registry.consume(started.state, now=started.expires_at + 1)


@pytest.mark.asyncio
async def test_code_exchange_and_refresh_preserve_rotating_refresh_token():
    requests: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs((await request.aread()).decode())
        requests.append({key: values[0] for key, values in body.items()})
        if body["grant_type"] == ["authorization_code"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 7200,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        exchanged = await exchange_oauth_code(
            _oauth(client_secret="secret"),
            code="one-code",
            verifier="one-verifier",
            redirect_uri="http://localhost/callback",
            http_client=client,
            now=1_000,
        )
        refreshed = await refresh_oauth_token(
            exchanged,
            http_client=client,
            now=2_000,
        )

    assert exchanged.access_token == "access-1"
    assert exchanged.expires_at == 4_600
    assert refreshed.access_token == "access-2"
    assert refreshed.refresh_token == "refresh-2"
    assert requests[0]["code_verifier"] == "one-verifier"
    assert requests[1]["refresh_token"] == "refresh-1"


def test_oauth_header_overrides_stale_static_authorization():
    config = MCPClientConfig(
        name="docs",
        transport="streamable_http",
        url="https://mcp.example",
        headers={"Authorization": "Bearer stale", "X-Workspace": "demo"},
        oauth=_oauth(access_token="fresh", expires_at=20_000),
    )

    assert oauth_access_token_valid(config.oauth, now=1_000)
    assert oauth_headers(config) == {
        "Authorization": "Bearer fresh",
        "X-Workspace": "demo",
    }


@pytest.mark.asyncio
async def test_refresh_requires_reauthorization_without_refresh_token():
    with pytest.raises(ConnectorLifecycleError) as error:
        await refresh_oauth_token(_oauth(access_token="expired"))
    assert error.value.code == "authorization_required"


@pytest.mark.asyncio
async def test_owned_oauth_client_uses_mcp_network_boundary(monkeypatch):
    import lantai.app.mcp.lifecycle as lifecycle_module

    observed: list[tuple[str, bool]] = []

    async def resolve(url, *, allow_private_network=False):
        observed.append((url, allow_private_network))
        return object()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "fresh"}

    class Client:
        async def post(self, *_args, **_kwargs):
            return Response()

        async def aclose(self):
            return None

    monkeypatch.setattr(lifecycle_module, "resolve_mcp_endpoint", resolve)
    monkeypatch.setattr(
        lifecycle_module,
        "create_pinned_mcp_http_client",
        lambda *_args, **_kwargs: Client(),
    )

    refreshed = await refresh_oauth_token(
        _oauth(refresh_token="refresh"),
        allow_private_network=True,
    )

    assert refreshed.access_token == "fresh"
    assert observed == [("https://identity.example/token", True)]


@pytest.mark.asyncio
async def test_oauth_refresh_locks_are_released_after_each_connector(
    monkeypatch,
):
    import lantai.config.config as config_module
    import lantai.app.mcp.lifecycle as lifecycle_module

    configs: dict[str, MCPClientConfig] = {}

    def load_agent_config(agent_id):
        key = agent_id.removeprefix("agent-")
        return type(
            "AgentConfig",
            (),
            {"mcp": type("MCP", (), {"clients": {key: configs[key]}})()},
        )()

    async def refreshed(oauth, **_kwargs):
        return oauth.model_copy(
            update={"access_token": "fresh", "expires_at": 50_000},
        )

    monkeypatch.setattr(config_module, "load_agent_config", load_agent_config)
    monkeypatch.setattr(config_module, "save_agent_config", lambda *_args: None)
    monkeypatch.setattr(lifecycle_module, "refresh_oauth_token", refreshed)
    _refresh_locks.clear()

    try:
        for index in range(32):
            key = f"connector-{index}"
            config = MCPClientConfig(
                name=key,
                transport="streamable_http",
                url=f"https://{key}.example/mcp",
                oauth=_oauth(
                    access_token="expired",
                    refresh_token="refresh",
                    expires_at=1,
                ),
            )
            configs[key] = config
            await prepare_client_oauth(
                agent_id=f"agent-{key}",
                client_key=key,
                client_config=config,
            )

        assert _refresh_locks == {}
    finally:
        _refresh_locks.clear()
