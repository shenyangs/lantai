# -*- coding: utf-8 -*-
"""Curated local MCP configuration for the OpenChatCut video workbench."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

from ...config.config import MCPClientConfig

OPENCHATCUT_CLIENT_KEY = "openchatcut"
OPENCHATCUT_MCP_NAME = "视频工程 · OpenChatCut"
OPENCHATCUT_MCP_DESCRIPTION = "让智能体通过本机 MCP 编辑可审阅、可撤销的视频工程。"
OPENCHATCUT_DEFAULT_ENDPOINT = "http://127.0.0.1:5199/api/external-mcp/mcp"
OPENCHATCUT_REPOSITORY_URL = "https://github.com/0xsline/OpenChatCut"
OPENCHATCUT_DOCS_URL = f"{OPENCHATCUT_REPOSITORY_URL}/blob/main/README_ZH.md"
OPENCHATCUT_LICENSE = "AGPL-3.0-or-later"

_OPENCHATCUT_MCP_PATH = "/api/external-mcp/mcp"


def normalize_openchatcut_endpoint(value: str) -> str:
    """Return a canonical loopback OpenChatCut MCP endpoint."""

    endpoint = str(value or "").strip()
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("OpenChatCut MCP 地址无效") from exc

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("OpenChatCut MCP 地址只允许 http 或 https")
    if parsed.username or parsed.password:
        raise ValueError("OpenChatCut MCP 地址不能包含账号或密码")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        except ValueError as exc:
            raise ValueError("OpenChatCut MCP 只允许连接本机地址") from exc
    if parsed.path.rstrip("/") != _OPENCHATCUT_MCP_PATH:
        raise ValueError("OpenChatCut MCP 地址路径必须是 /api/external-mcp/mcp")
    if parsed.query or parsed.fragment:
        raise ValueError("OpenChatCut MCP 地址不能包含查询参数或片段")

    netloc = parsed.hostname or host
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, _OPENCHATCUT_MCP_PATH, "", ""))


def _normalized_token(token: str | None) -> str:
    value = str(token or "").strip()
    if not value:
        raise ValueError("请填写 OpenChatCut MCP Token")
    if "\r" in value or "\n" in value:
        raise ValueError("OpenChatCut MCP Token 格式无效")
    return value


def build_openchatcut_client_config(
    *,
    endpoint: str = OPENCHATCUT_DEFAULT_ENDPOINT,
    token: str,
    enabled: bool = True,
) -> MCPClientConfig:
    """Build the reserved, local-only OpenChatCut connector config."""

    return MCPClientConfig(
        name=OPENCHATCUT_MCP_NAME,
        description=OPENCHATCUT_MCP_DESCRIPTION,
        enabled=enabled,
        transport="streamable_http",
        url=normalize_openchatcut_endpoint(endpoint),
        allow_private_network=False,
        headers={
            "Authorization": f"Bearer {_normalized_token(token)}",
            "X-OpenChatCut-MCP-Client": "lantai",
            "X-OpenChatCut-MCP-Surface": "external",
        },
        allowed_tools=[],
    )


def openchatcut_token(client: MCPClientConfig | None) -> str | None:
    """Return the configured bearer token without exposing it through APIs."""

    if client is None:
        return None
    authorization = str((client.headers or {}).get("Authorization") or "")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return None
    token = authorization[len(prefix) :].strip()
    return token or None


__all__ = [
    "OPENCHATCUT_CLIENT_KEY",
    "OPENCHATCUT_DEFAULT_ENDPOINT",
    "OPENCHATCUT_DOCS_URL",
    "OPENCHATCUT_LICENSE",
    "OPENCHATCUT_MCP_DESCRIPTION",
    "OPENCHATCUT_MCP_NAME",
    "OPENCHATCUT_REPOSITORY_URL",
    "build_openchatcut_client_config",
    "normalize_openchatcut_endpoint",
    "openchatcut_token",
]
