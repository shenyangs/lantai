from __future__ import annotations

import pytest

from lantai.app.mcp.manager import MCPClientManager
from lantai.app.mcp.network_security import (
    _PinnedNetworkBackend,
    UnsafeMCPNetworkTarget,
    resolve_mcp_endpoint,
)
from httpcore2._backends.auto import AutoBackend
from lantai.config.config import MCPClientConfig


def _resolve_to(*addresses: str):
    async def resolver(_host: str, _port: int) -> list[str]:
        return list(addresses)

    return resolver


@pytest.mark.asyncio
async def test_public_mcp_endpoint_is_resolved_for_ip_pinning():
    target = await resolve_mcp_endpoint(
        "https://mcp.example/tools",
        resolver=_resolve_to("93.184.216.34"),
    )

    assert target.host == "mcp.example"
    assert target.port == 443
    assert target.pinned_ip == "93.184.216.34"


@pytest.mark.asyncio
async def test_private_mcp_endpoint_requires_explicit_network_grant():
    resolver = _resolve_to("192.168.1.20")

    with pytest.raises(UnsafeMCPNetworkTarget, match="私有网络"):
        await resolve_mcp_endpoint(
            "https://mcp.internal/tools",
            resolver=resolver,
        )

    target = await resolve_mcp_endpoint(
        "http://mcp.internal/tools",
        allow_private_network=True,
        resolver=resolver,
    )
    assert target.pinned_ip == "192.168.1.20"


@pytest.mark.asyncio
async def test_loopback_mcp_remains_available_without_private_network_grant():
    target = await resolve_mcp_endpoint(
        "http://127.0.0.1:9321/mcp",
        resolver=_resolve_to("127.0.0.1"),
    )

    assert target.port == 9321
    assert target.pinned_ip == "127.0.0.1"


def test_mcp_manager_propagates_private_network_grant():
    config = MCPClientConfig(
        name="lan-mcp",
        transport="streamable_http",
        url="http://192.168.1.20:9321/mcp",
        allow_private_network=True,
    )

    client = MCPClientManager._build_client(config)

    assert client.allow_private_network is True
    assert client._lantai_rebuild_info["allow_private_network"] is True


@pytest.mark.asyncio
async def test_pinned_backend_replaces_only_the_validated_destination(monkeypatch):
    calls = []

    async def fake_connect(
        _self,
        host,
        port,
        timeout=None,
        local_address=None,
        socket_options=None,
    ):
        calls.append((host, port, timeout, local_address, socket_options))
        return object()

    monkeypatch.setattr(AutoBackend, "connect_tcp", fake_connect)
    backend = _PinnedNetworkBackend("mcp.example", "93.184.216.34")

    await backend.connect_tcp("mcp.example", 443, timeout=3)
    await backend.connect_tcp("other.example", 443, timeout=4)

    assert calls[0][:3] == ("93.184.216.34", 443, 3)
    assert calls[1][:3] == ("other.example", 443, 4)
