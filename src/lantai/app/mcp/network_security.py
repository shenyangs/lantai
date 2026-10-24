# -*- coding: utf-8 -*-
"""Outbound network boundary for remote MCP transports.

Remote MCP endpoints are resolved once, classified, and connected through a
pinned network backend. This closes the DNS-rebinding gap between validating a
hostname and opening its TCP connection while preserving TLS SNI.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable
from urllib.parse import urlparse

import httpx2
from httpcore2._backends.auto import AutoBackend
from httpcore2._backends.base import SOCKET_OPTION, AsyncNetworkStream

AddressResolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeMCPNetworkTarget(ValueError):
    """Raised when a remote MCP URL crosses its configured network grant."""


@dataclass(frozen=True)
class ResolvedMCPEndpoint:
    """Validated endpoint plus the IP used for its actual TCP connection."""

    url: str
    host: str
    port: int
    pinned_ip: str


async def _system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise UnsafeMCPNetworkTarget(
            f"MCP 主机无法解析：{host}",
        ) from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def _is_explicitly_grantable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_private or address.is_loopback or address.is_link_local


async def resolve_mcp_endpoint(
    url: str,
    *,
    allow_private_network: bool = False,
    resolver: AddressResolver | None = None,
) -> ResolvedMCPEndpoint:
    """Validate, resolve, and choose a pinned IP for one MCP endpoint.

    Public endpoints require HTTPS. Loopback endpoints remain available for
    desktop-local MCP servers. Other private/link-local endpoints require the
    connector's explicit ``allow_private_network`` grant.
    """

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeMCPNetworkTarget("MCP 地址只允许 http 或 https")
    if parsed.username or parsed.password:
        raise UnsafeMCPNetworkTarget("MCP 地址不能在 URL 中携带账号或密码")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeMCPNetworkTarget("MCP 地址缺少主机名")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeMCPNetworkTarget("MCP 地址端口无效") from exc

    addresses = await (resolver or _system_resolver)(host, port)
    if not addresses:
        raise UnsafeMCPNetworkTarget(f"MCP 主机无法解析：{host}")

    parsed_addresses = []
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise UnsafeMCPNetworkTarget("MCP 主机返回了无效 IP 地址") from exc
        if address.is_multicast or address.is_unspecified or address.is_reserved:
            raise UnsafeMCPNetworkTarget("MCP 地址解析到了不可路由网络")
        if not address.is_global and not _is_explicitly_grantable(address):
            raise UnsafeMCPNetworkTarget("MCP 地址解析到了不可用网络")
        parsed_addresses.append(address)

    all_loopback = all(address.is_loopback for address in parsed_addresses)
    any_private = any(not address.is_global for address in parsed_addresses)
    if any_private and not all_loopback and not allow_private_network:
        raise UnsafeMCPNetworkTarget(
            "MCP 地址解析到了私有网络；请先明确授予私有网络访问权限",
        )
    if parsed.scheme != "https" and not (
        all_loopback or (any_private and allow_private_network)
    ):
        raise UnsafeMCPNetworkTarget("公网 MCP 地址必须使用 https")

    return ResolvedMCPEndpoint(
        url=url,
        host=host,
        port=port,
        pinned_ip=str(parsed_addresses[0]),
    )


class _PinnedNetworkBackend(AutoBackend):
    """Connect one validated hostname to its already-checked IP address."""

    def __init__(self, target_host: str, pinned_ip: str) -> None:
        super().__init__()
        self._target_host = target_host
        self._pinned_ip = pinned_ip

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        destination = self._pinned_ip if host == self._target_host else host
        return await super().connect_tcp(
            destination,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class PinnedMCPTransport(httpx2.AsyncHTTPTransport):
    """HTTP transport pinned to a validated IP while retaining TLS SNI."""

    def __init__(self, endpoint: ResolvedMCPEndpoint) -> None:
        super().__init__()
        self._pool._network_backend = _PinnedNetworkBackend(
            endpoint.host,
            endpoint.pinned_ip,
        )


def create_pinned_mcp_http_client(
    endpoint: ResolvedMCPEndpoint,
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    """Create a no-redirect HTTP client for a validated MCP endpoint."""

    return httpx2.AsyncClient(
        transport=PinnedMCPTransport(endpoint),
        headers=headers,
        timeout=timeout or httpx2.Timeout(30.0, read=300.0),
        auth=auth,
        follow_redirects=False,
    )
