# -*- coding: utf-8 -*-
"""Curated official MCP presets with standards-based OAuth registration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from ...config.config import MCPClientConfig, MCPOAuthConfig
from .lifecycle import ConnectorLifecycleError
from .network_security import create_pinned_mcp_http_client, resolve_mcp_endpoint


@dataclass(frozen=True)
class OfficialMCPPreset:
    key: str
    name: str
    description: str
    url: str
    resource_metadata_url: str
    authorization_metadata_url: str
    scopes: tuple[str, ...]
    docs_url: str

    def build_client(self, oauth: MCPOAuthConfig) -> MCPClientConfig:
        return MCPClientConfig(
            name=self.name,
            description=self.description,
            enabled=True,
            transport="streamable_http",
            url=self.url,
            oauth=oauth,
        )


OFFICIAL_MCP_PRESETS: tuple[OfficialMCPPreset, ...] = (
    OfficialMCPPreset(
        key="notion",
        name="Notion",
        description="搜索、读取并更新 Notion 工作区内容。",
        url="https://mcp.notion.com/mcp",
        resource_metadata_url=(
            "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp"
        ),
        authorization_metadata_url=(
            "https://mcp.notion.com/.well-known/oauth-authorization-server"
        ),
        scopes=("default",),
        docs_url="https://developers.notion.com/guides/mcp/get-started-with-mcp",
    ),
    OfficialMCPPreset(
        key="linear",
        name="Linear",
        description="读取和管理 Linear 项目、议题与团队工作流。",
        url="https://mcp.linear.app/mcp",
        resource_metadata_url=(
            "https://mcp.linear.app/.well-known/oauth-protected-resource/mcp"
        ),
        authorization_metadata_url=(
            "https://mcp.linear.app/.well-known/oauth-authorization-server"
        ),
        scopes=("read", "write"),
        docs_url="https://linear.app/docs/mcp",
    ),
    OfficialMCPPreset(
        key="figma",
        name="Figma",
        description="读取设计上下文，并在支持时对 Figma 画布执行写操作。",
        url="https://mcp.figma.com/mcp",
        resource_metadata_url=(
            "https://mcp.figma.com/.well-known/oauth-protected-resource"
        ),
        authorization_metadata_url=(
            "https://api.figma.com/.well-known/oauth-authorization-server"
        ),
        scopes=("mcp:connect",),
        docs_url=(
            "https://help.figma.com/hc/en-us/articles/32132100833559-"
            "Guide-to-the-Figma-MCP-server"
        ),
    ),
)

OFFICIAL_MCP_PRESETS_BY_KEY = {item.key: item for item in OFFICIAL_MCP_PRESETS}


def _require_https_url(value: Any, *, field: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ConnectorLifecycleError(
            "oauth_discovery_invalid",
            f"官方连接器返回了无效的 {field}。",
            status_code=502,
        )
    return url


async def _request_json(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    owned_client = http_client is None
    if http_client is None:
        endpoint = await resolve_mcp_endpoint(url)
        client = create_pinned_mcp_http_client(
            endpoint,
            timeout=httpx.Timeout(20.0),
        )
    else:
        client = http_client
    try:
        response = await client.request(
            method,
            url,
            json=json,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("OAuth metadata must be an object")
        return payload
    except (httpx.HTTPError, ValueError) as exc:
        raise ConnectorLifecycleError(
            "oauth_discovery_failed",
            "官方连接器的 OAuth 元数据或客户端注册暂时不可用。",
            status_code=502,
        ) from exc
    finally:
        if owned_client:
            await client.aclose()


async def register_official_connector_oauth(
    preset: OfficialMCPPreset,
    *,
    redirect_uri: str,
    http_client: httpx.AsyncClient | None = None,
) -> MCPOAuthConfig:
    """Discover endpoints and dynamically register one native desktop client."""

    resource = await _request_json(
        "GET",
        preset.resource_metadata_url,
        http_client=http_client,
    )
    authorization_servers = resource.get("authorization_servers") or []
    if not isinstance(authorization_servers, list) or not authorization_servers:
        raise ConnectorLifecycleError(
            "oauth_authorization_server_missing",
            "官方连接器没有声明 OAuth 授权服务。",
            status_code=502,
        )

    metadata = await _request_json(
        "GET",
        preset.authorization_metadata_url,
        http_client=http_client,
    )
    issuer = _require_https_url(metadata.get("issuer"), field="issuer")
    normalized_servers = {
        str(item).rstrip("/") for item in authorization_servers if item
    }
    if issuer.rstrip("/") not in normalized_servers:
        raise ConnectorLifecycleError(
            "oauth_issuer_mismatch",
            "官方连接器的 OAuth 授权服务声明不一致。",
            status_code=502,
        )

    authorization_url = _require_https_url(
        metadata.get("authorization_endpoint"),
        field="authorization_endpoint",
    )
    token_url = _require_https_url(
        metadata.get("token_endpoint"),
        field="token_endpoint",
    )
    registration_url = _require_https_url(
        metadata.get("registration_endpoint"),
        field="registration_endpoint",
    )
    supported_methods = metadata.get("token_endpoint_auth_methods_supported") or []
    token_auth_method = (
        "none"
        if "none" in supported_methods
        else "client_secret_post"
        if "client_secret_post" in supported_methods
        else "client_secret_basic"
    )
    registration = await _request_json(
        "POST",
        registration_url,
        json={
            "client_name": "Lantai Desktop",
            "application_type": "native",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": token_auth_method,
            "scope": " ".join(preset.scopes),
        },
        http_client=http_client,
    )
    client_id = str(registration.get("client_id") or "").strip()
    if not client_id:
        raise ConnectorLifecycleError(
            "oauth_client_id_missing",
            "官方连接器没有返回 OAuth client_id。",
            status_code=502,
        )
    return MCPOAuthConfig(
        enabled=True,
        authorization_url=authorization_url,
        token_url=token_url,
        client_id=client_id,
        client_secret=str(registration.get("client_secret") or ""),
        scopes=list(preset.scopes),
        redirect_uri=redirect_uri,
    )
