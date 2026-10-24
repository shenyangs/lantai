# -*- coding: utf-8 -*-
"""API routes for MCP (Model Context Protocol) clients management."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Literal

from fastapi import APIRouter, Body, HTTPException, Path, Request
from fastapi.responses import HTMLResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from ..utils import schedule_agent_reload
from ...config.config import MCPClientConfig, MCPOAuthConfig
from ..mcp.lifecycle import (
    ConnectorLifecycleError,
    classify_connector_error,
    exchange_oauth_code,
    oauth_access_token_valid,
    oauth_pending_registry,
)
from ..mcp.openchatcut import (
    OPENCHATCUT_CLIENT_KEY,
    OPENCHATCUT_DEFAULT_ENDPOINT,
    OPENCHATCUT_DOCS_URL,
    OPENCHATCUT_LICENSE,
    OPENCHATCUT_MCP_DESCRIPTION,
    OPENCHATCUT_MCP_NAME,
    OPENCHATCUT_REPOSITORY_URL,
    build_openchatcut_client_config,
    openchatcut_token,
)
from ..mcp.catalog import (
    OFFICIAL_MCP_PRESETS,
    OFFICIAL_MCP_PRESETS_BY_KEY,
    register_official_connector_oauth,
)
from ..mcp.chrome_devtools import (
    CHROME_DEVTOOLS_ALLOWED_TOOLS,
    CHROME_DEVTOOLS_CLIENT_KEY,
    CHROME_DEVTOOLS_DOCS_URL,
    CHROME_DEVTOOLS_LICENSE,
    CHROME_DEVTOOLS_MCP_DESCRIPTION,
    CHROME_DEVTOOLS_MCP_NAME,
    CHROME_DEVTOOLS_PACKAGE_VERSION,
    CHROME_DEVTOOLS_REPOSITORY_URL,
    build_chrome_devtools_client_config,
    chrome_devtools_allowed_tools,
    resolve_chrome_devtools_runtime,
)
from ..mcp.guanlan import (
    GUANLAN_CLIENT_KEY,
    GUANLAN_LICENSE,
    GUANLAN_MCP_DESCRIPTION,
    GUANLAN_MCP_NAME,
    GUANLAN_REPOSITORY_URL,
    GUANLAN_TOOLS,
    build_guanlan_client_config,
    ensure_guanlan_package,
    get_guanlan_package_state,
)
from ..office_documents import (
    OFFICECLI_CLIENT_KEY,
    OFFICECLI_DOCS_URL,
    OFFICECLI_MCP_DESCRIPTION,
    OFFICECLI_MCP_NAME,
    OFFICECLI_REPOSITORY_URL,
    build_officecli_mcp_client_config,
    get_officecli_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])


class MCPClientInfo(BaseModel):
    """MCP client information for API responses."""

    key: str = Field(..., description="Unique client key identifier")
    name: str = Field(..., description="Client display name")
    description: str = Field(default="", description="Client description")
    enabled: bool = Field(..., description="Whether the client is enabled")
    revision: int = Field(
        default=0,
        ge=0,
        description="Current resource revision for ordered configuration writes",
    )
    transport: Literal["stdio", "streamable_http", "sse"] = Field(
        ...,
        description="MCP transport type",
    )
    url: str = Field(
        default="",
        description="Remote MCP endpoint URL (for HTTP/SSE transports)",
    )
    allow_private_network: bool = Field(
        default=False,
        description="Whether this connector may access private/LAN addresses",
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers for remote transport",
    )
    command: str = Field(
        default="",
        description="Command to launch the MCP server",
    )
    args: List[str] = Field(
        default_factory=list,
        description="Command-line arguments",
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables",
    )
    cwd: str = Field(
        default="",
        description="Working directory for stdio MCP command",
    )
    oauth: Dict[str, Any] = Field(default_factory=dict)
    allowed_tools: List[str] = Field(default_factory=list)


class MCPClientCreateRequest(BaseModel):
    """Request body for creating/updating an MCP client."""

    name: str = Field(..., description="Client display name")
    description: str = Field(default="", description="Client description")
    enabled: bool = Field(
        default=True,
        description="Whether to enable the client",
    )
    transport: Literal["stdio", "streamable_http", "sse"] = Field(
        default="stdio",
        description="MCP transport type",
    )
    url: str = Field(
        default="",
        description="Remote MCP endpoint URL (for HTTP/SSE transports)",
    )
    allow_private_network: bool = Field(
        default=False,
        description="Explicitly grant access to private/LAN addresses",
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers for remote transport",
    )
    command: str = Field(
        default="",
        description="Command to launch the MCP server",
    )
    args: List[str] = Field(
        default_factory=list,
        description="Command-line arguments",
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables",
    )
    cwd: str = Field(
        default="",
        description="Working directory for stdio MCP command",
    )
    oauth: MCPOAuthConfig = Field(default_factory=MCPOAuthConfig)
    allowed_tools: List[str] = Field(default_factory=list)


class MCPClientUpdateRequest(BaseModel):
    """Request body for updating an MCP client (all fields optional)."""

    name: Optional[str] = Field(None, description="Client display name")
    description: Optional[str] = Field(None, description="Client description")
    enabled: Optional[bool] = Field(
        None,
        description="Whether to enable the client",
    )
    expected_revision: Optional[int] = Field(
        None,
        ge=0,
        description="Reject the write when the resource has changed",
    )
    transport: Optional[Literal["stdio", "streamable_http", "sse"]] = Field(
        None,
        description="MCP transport type",
    )
    url: Optional[str] = Field(
        None,
        description="Remote MCP endpoint URL (for HTTP/SSE transports)",
    )
    allow_private_network: Optional[bool] = Field(
        None,
        description="Explicitly grant access to private/LAN addresses",
    )
    headers: Optional[Dict[str, str]] = Field(
        None,
        description="HTTP headers for remote transport",
    )
    command: Optional[str] = Field(
        None,
        description="Command to launch the MCP server",
    )
    args: Optional[List[str]] = Field(
        None,
        description="Command-line arguments",
    )
    env: Optional[Dict[str, str]] = Field(
        None,
        description="Environment variables",
    )
    cwd: Optional[str] = Field(
        None,
        description="Working directory for stdio MCP command",
    )
    oauth: Optional[Dict[str, Any]] = Field(
        None,
        description="OAuth 2.0 Authorization Code + PKCE configuration",
    )
    allowed_tools: Optional[List[str]] = Field(
        None,
        description="Explicit MCP tool allowlist; empty allows all",
    )


class MCPConnectorLifecycleInfo(BaseModel):
    """Stable connector status contract for settings and diagnostics."""

    client_key: str
    status: Literal[
        "disabled",
        "configuration_required",
        "authorization_required",
        "connecting",
        "ready",
        "error",
    ]
    auth_status: Literal[
        "not_required",
        "ready",
        "refreshable",
        "authorization_required",
    ]
    connection_status: Literal[
        "disabled",
        "disconnected",
        "connecting",
        "ready",
        "error",
    ]
    tool_count: int = 0
    allowed_tools: List[str] = Field(default_factory=list)
    last_probe_at: int | None = None
    error_category: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class MCPOAuthStartResponse(BaseModel):
    authorization_url: str
    state: str
    expires_at: int


class OfficialMCPConnectorInfo(BaseModel):
    key: str
    name: str
    description: str
    url: str
    docs_url: str
    official: bool = True
    configured: bool = False


class MCPResourceReadRequest(BaseModel):
    uri: str = Field(..., min_length=1)


class MCPPromptGetRequest(BaseModel):
    name: str = Field(..., min_length=1)
    arguments: Dict[str, str] = Field(default_factory=dict)


_PROBE_RECORDS: dict[tuple[str, str], dict[str, Any]] = {}


def _restore_original_values(
    incoming: Dict[str, str],
    existing: Dict[str, str],
) -> Dict[str, str]:
    """Preserve original values when incoming matches their masked form."""
    restored: Dict[str, str] = {}
    for k, v in incoming.items():
        if k in existing and v == _mask_env_value(existing[k]):
            restored[k] = existing[k]
        else:
            restored[k] = v
    return restored


def _mask_env_value(value: str) -> str:
    """
    Mask environment variable value showing first 2-3 chars and last 4 chars.

    Examples:
        sk-proj-1234567890abcdefghij1234 -> sk-****************************1234
        abc123456789xyz -> ab***********xyz (if no dash)
        my-api-key-value -> my-************lue
        short123 -> ******** (8 chars or less, fully masked)
    """
    if not value:
        return value

    length = len(value)
    if length <= 8:
        # For short values, just mask everything
        return "*" * length

    # Show first 2-3 characters (3 if there's a dash at position 2)
    prefix_len = 3 if length > 2 and value[2] == "-" else 2
    prefix = value[:prefix_len]

    # Show last 4 characters
    suffix = value[-4:]

    # Calculate masked section length (at least 4 asterisks)
    masked_len = max(length - prefix_len - 4, 4)

    return f"{prefix}{'*' * masked_len}{suffix}"


def _build_client_info(key: str, client: MCPClientConfig) -> MCPClientInfo:
    """Build MCPClientInfo from config with masked env values."""
    # Mask environment variable values for security
    masked_env = (
        {k: _mask_env_value(v) for k, v in client.env.items()}
        if client.env
        else {}
    )
    masked_headers = (
        {k: _mask_env_value(v) for k, v in client.headers.items()}
        if client.headers
        else {}
    )
    oauth = client.oauth.model_dump(mode="json")
    for secret_field in ("client_secret", "access_token", "refresh_token"):
        value = oauth.get(secret_field)
        if isinstance(value, str) and value:
            oauth[secret_field] = _mask_env_value(value)

    return MCPClientInfo(
        key=key,
        name=client.name,
        description=client.description,
        enabled=client.enabled,
        revision=client.revision,
        transport=client.transport,
        url=client.url,
        allow_private_network=client.allow_private_network,
        headers=masked_headers,
        command=client.command,
        args=client.args,
        env=masked_env,
        cwd=client.cwd,
        oauth=oauth,
        allowed_tools=list(client.allowed_tools),
    )


async def _build_lifecycle_info(
    agent,
    client_key: str,
) -> MCPConnectorLifecycleInfo:
    """Build a non-probing status snapshot from config and manager state."""

    client_config = agent.config.mcp.clients[client_key]
    record = _PROBE_RECORDS.get((agent.agent_id, client_key), {})
    runtime_client = None
    if agent.mcp_manager is not None:
        runtime_client = await agent.mcp_manager.get_client(client_key)
    connected = bool(
        runtime_client and getattr(runtime_client, "is_connected", False),
    )
    cached_tools = list(getattr(runtime_client, "_cached_tools", None) or [])

    if not client_config.oauth.enabled:
        auth_status = "not_required"
    elif oauth_access_token_valid(client_config.oauth):
        auth_status = "ready"
    elif client_config.oauth.refresh_token:
        auth_status = "refreshable"
    else:
        auth_status = "authorization_required"

    if not client_config.enabled:
        status = "disabled"
        connection_status = "disabled"
    elif record.get("error_code"):
        status = "error"
        connection_status = "error"
    elif auth_status == "authorization_required":
        status = "authorization_required"
        connection_status = "disconnected"
    elif connected:
        status = "ready"
        connection_status = "ready"
    else:
        status = "connecting"
        connection_status = "connecting"

    return MCPConnectorLifecycleInfo(
        client_key=client_key,
        status=status,
        auth_status=auth_status,
        connection_status=connection_status,
        tool_count=len(cached_tools),
        allowed_tools=list(client_config.allowed_tools),
        last_probe_at=record.get("last_probe_at"),
        error_category=record.get("error_category"),
        error_code=record.get("error_code"),
        error_message=record.get("error_message"),
    )


class MCPToolInfo(BaseModel):
    """MCP tool information returned from a connected server."""

    name: str = Field(..., description="Tool name")
    description: str = Field(default="", description="Tool description")
    input_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for the tool's input parameters",
    )


class GuanlanToolInfo(BaseModel):
    """Static tool summary for the built-in Guanlan module."""

    name: str
    description: str = ""


class GuanlanMCPInfo(BaseModel):
    """Built-in Guanlan MCP module information."""

    client_key: str = GUANLAN_CLIENT_KEY
    name: str = GUANLAN_MCP_NAME
    description: str = GUANLAN_MCP_DESCRIPTION
    enabled: bool = False
    configured: bool = False
    capability_status: Literal[
        "disabled",
        "starting",
        "ready",
        "degraded",
        "error",
    ] = "starting"
    connected: bool = False
    runtime_attempt_count: int = 0
    runtime_error_code: Optional[str] = None
    runtime_message: str = ""
    runtime_updated_at: Optional[float] = None
    package_available: bool = False
    package_version: Optional[str] = None
    bundled_version: Optional[str] = None
    runtime_source: Literal["bundled", "user"] = "bundled"
    package_status: Literal["idle", "checking", "updating", "ready", "error"]
    package_message: str = ""
    package_update_supported: bool = False
    package_updated: bool = False
    package_previous_version: Optional[str] = None
    package_restart_required: bool = False
    package_candidate_version: Optional[str] = None
    package_metadata_source: Literal[
        "pypi",
        "github_tags",
        "unavailable",
    ] = "pypi"
    package_metadata_degraded: bool = False
    package_update_available: bool = False
    repository_url: str = GUANLAN_REPOSITORY_URL
    license: str = GUANLAN_LICENSE
    tools: List[GuanlanToolInfo] = Field(
        default_factory=lambda: [
            GuanlanToolInfo(**tool) for tool in GUANLAN_TOOLS
        ],
    )


class GuanlanToggleRequest(BaseModel):
    """Request body for the built-in Guanlan toggle."""

    enabled: bool = Field(..., description="Whether Guanlan MCP is enabled")


class OfficeCliToolInfo(BaseModel):
    """Static tool summary for the built-in OfficeCLI module."""

    name: str
    description: str = ""


class OfficeCliMCPInfo(BaseModel):
    """Built-in OfficeCLI MCP module information."""

    client_key: str = OFFICECLI_CLIENT_KEY
    name: str = OFFICECLI_MCP_NAME
    description: str = OFFICECLI_MCP_DESCRIPTION
    enabled: bool = False
    configured: bool = False
    available: bool = False
    executable_path: Optional[str] = None
    executable_source: str = "missing"
    version: Optional[str] = None
    package_message: str = ""
    repository_url: str = OFFICECLI_REPOSITORY_URL
    docs_url: str = OFFICECLI_DOCS_URL
    tools: List[OfficeCliToolInfo] = Field(
        default_factory=lambda: [
            OfficeCliToolInfo(
                name="officecli",
                description=(
                    "统一 Office 文档工具：create/view/get/query/set/add/"
                    "remove/move/validate/batch/merge/help"
                ),
            ),
            OfficeCliToolInfo(
                name="load_skill",
                description="按需加载 OfficeCLI 内置 docx/xlsx/pptx 场景技能",
            ),
        ],
    )


class OfficeCliToggleRequest(BaseModel):
    """Request body for the built-in OfficeCLI toggle."""

    enabled: bool = Field(..., description="Whether OfficeCLI MCP is enabled")


class ChromeDevToolsMCPInfo(BaseModel):
    """Bundled Chrome DevTools MCP module information."""

    client_key: str = CHROME_DEVTOOLS_CLIENT_KEY
    name: str = CHROME_DEVTOOLS_MCP_NAME
    description: str = CHROME_DEVTOOLS_MCP_DESCRIPTION
    enabled: bool = False
    configured: bool = False
    available: bool = False
    capability_status: Literal[
        "disabled",
        "starting",
        "ready",
        "degraded",
        "error",
    ] = "disabled"
    connected: bool = False
    runtime_attempt_count: int = 0
    runtime_error_code: Optional[str] = None
    runtime_message: str = ""
    runtime_updated_at: Optional[float] = None
    package_version: str = CHROME_DEVTOOLS_PACKAGE_VERSION
    runtime_source: Literal["bundled", "source", "missing"] = "missing"
    repository_url: str = CHROME_DEVTOOLS_REPOSITORY_URL
    docs_url: str = CHROME_DEVTOOLS_DOCS_URL
    license: str = CHROME_DEVTOOLS_LICENSE
    profile_mode: Literal["persistent_dedicated"] = "persistent_dedicated"
    attaches_existing_chrome: bool = False
    usage_statistics_enabled: bool = False
    update_checks_enabled: bool = False
    performance_crux_enabled: bool = True
    network_headers_redacted: bool = False
    unrestricted_paths_enabled: bool = True
    filesystem_access: Literal["host_authorized"] = "host_authorized"
    screencast_enabled: bool = False
    allowed_tools: List[str] = Field(
        default_factory=lambda: list(CHROME_DEVTOOLS_ALLOWED_TOOLS),
    )


class ChromeDevToolsToggleRequest(BaseModel):
    """Request body for the bundled Chrome DevTools MCP toggle."""

    enabled: bool = Field(
        ...,
        description="Whether Chrome DevTools MCP is enabled",
    )


class OpenChatCutMCPInfo(BaseModel):
    """Curated local OpenChatCut MCP connector information."""

    client_key: str = OPENCHATCUT_CLIENT_KEY
    name: str = OPENCHATCUT_MCP_NAME
    description: str = OPENCHATCUT_MCP_DESCRIPTION
    configured: bool = False
    enabled: bool = False
    endpoint: str = OPENCHATCUT_DEFAULT_ENDPOINT
    token_configured: bool = False
    connection_status: Literal[
        "not_configured",
        "disabled",
        "connecting",
        "ready",
        "error",
    ] = "not_configured"
    repository_url: str = OPENCHATCUT_REPOSITORY_URL
    docs_url: str = OPENCHATCUT_DOCS_URL
    license: str = OPENCHATCUT_LICENSE
    approval_mode: Literal["manual"] = "manual"


class OpenChatCutConfigureRequest(BaseModel):
    """Configure or toggle the local OpenChatCut MCP connector."""

    endpoint: Optional[str] = None
    token: Optional[str] = None
    enabled: bool = True


async def _build_guanlan_info(agent) -> GuanlanMCPInfo:
    """Build the built-in Guanlan module response for an agent."""

    mcp_config = agent.config.mcp
    client = None
    if mcp_config is not None:
        client = (mcp_config.clients or {}).get(GUANLAN_CLIENT_KEY)

    config_enabled = bool(client.enabled) if client is not None else True
    state = get_guanlan_package_state()
    runtime_state = None
    runtime_client = None
    manager = getattr(agent, "mcp_manager", None)
    if manager is not None:
        get_runtime_state = getattr(manager, "get_runtime_state", None)
        if callable(get_runtime_state):
            runtime_state = await get_runtime_state(GUANLAN_CLIENT_KEY)
        runtime_client = await manager.get_client(GUANLAN_CLIENT_KEY)

    connected = bool(
        runtime_client and getattr(runtime_client, "is_connected", False),
    )
    if not config_enabled:
        capability_status = "disabled"
    elif connected:
        capability_status = "ready"
    elif runtime_client is not None:
        # A published client can disconnect after a successful initialize.
        # The manager's last transition may still say ``ready`` while its
        # lifecycle task is already reconnecting, so live transport truth wins.
        capability_status = "degraded"
    elif runtime_state is not None:
        capability_status = (
            "error"
            if runtime_state.status == "ready"
            else runtime_state.status
        )
    elif not state.version:
        capability_status = "error"
    else:
        capability_status = "starting"

    runtime_messages = {
        "disabled": "观澜已关闭，可在澜台设置中重新启用。",
        "starting": "观澜系统能力正在启动。",
        "ready": "观澜系统能力已连接并可调用。",
        "degraded": "观澜当前降级运行，澜台会保留基础 Web 能力。",
        "error": "观澜系统能力暂时不可用，澜台会自动使用基础 Web 能力。",
    }

    return GuanlanMCPInfo(
        enabled=config_enabled,
        configured=client is not None,
        capability_status=capability_status,
        connected=connected,
        runtime_attempt_count=(
            max(
                runtime_state.attempt_count
                if runtime_state is not None
                else 0,
                int(getattr(runtime_client, "reconnect_attempt", 0) or 0),
            )
        ),
        runtime_error_code=(
            runtime_state.error_code if runtime_state is not None else None
        ),
        runtime_message=runtime_messages[capability_status],
        runtime_updated_at=(
            runtime_state.updated_at if runtime_state is not None else None
        ),
        package_available=bool(state.version),
        package_version=state.version,
        bundled_version=state.bundled_version,
        runtime_source=state.runtime_source,
        package_status=state.status,
        package_message=state.message,
        package_update_supported=False,
    )


def _build_officecli_info(agent) -> OfficeCliMCPInfo:
    """Build the built-in OfficeCLI module response for an agent."""

    mcp_config = agent.config.mcp
    client = None
    if mcp_config is not None:
        client = (mcp_config.clients or {}).get(OFFICECLI_CLIENT_KEY)

    status = get_officecli_status()
    return OfficeCliMCPInfo(
        enabled=bool(client.enabled) if client is not None else True,
        configured=client is not None,
        available=status.available,
        executable_path=status.path or None,
        executable_source=status.source,
        version=status.version,
        package_message=status.message or status.error,
    )


async def _build_chrome_devtools_info(agent) -> ChromeDevToolsMCPInfo:
    """Return package presence and live transport truth for the module."""

    mcp_config = agent.config.mcp
    client = None
    if mcp_config is not None:
        client = (mcp_config.clients or {}).get(CHROME_DEVTOOLS_CLIENT_KEY)
    enabled = bool(client.enabled) if client is not None else False
    runtime = resolve_chrome_devtools_runtime()

    runtime_state = None
    runtime_client = None
    manager = getattr(agent, "mcp_manager", None)
    if manager is not None:
        get_runtime_state = getattr(manager, "get_runtime_state", None)
        if callable(get_runtime_state):
            runtime_state = await get_runtime_state(CHROME_DEVTOOLS_CLIENT_KEY)
        runtime_client = await manager.get_client(CHROME_DEVTOOLS_CLIENT_KEY)
    connected = bool(
        runtime_client and getattr(runtime_client, "is_connected", False),
    )

    if not enabled:
        status = "disabled"
        message = "Chrome DevTools 已内置；开启后按需启动完整调试环境。"
    elif not runtime.available:
        status = "error"
        message = runtime.message
    elif connected:
        status = "ready"
        message = "Chrome DevTools MCP 已连接，浏览器会在首次调用时启动。"
    elif runtime_state is not None and runtime_state.status in {
        "error",
        "degraded",
    }:
        status = runtime_state.status
        message = (
            "Chrome DevTools MCP 暂时不可用；普通 BrowserUse 仍可继续使用。"
        )
    else:
        status = "starting"
        message = "Chrome DevTools MCP 正在启动。"

    return ChromeDevToolsMCPInfo(
        enabled=enabled,
        configured=client is not None,
        available=runtime.available,
        capability_status=status,
        connected=connected,
        runtime_attempt_count=(
            runtime_state.attempt_count if runtime_state is not None else 0
        ),
        runtime_error_code=(
            runtime_state.error_code if runtime_state is not None else None
        ),
        runtime_message=message,
        runtime_updated_at=(
            runtime_state.updated_at if runtime_state is not None else None
        ),
        package_version=(runtime.package_version or CHROME_DEVTOOLS_PACKAGE_VERSION),
        runtime_source=runtime.source,
        screencast_enabled=bool(runtime.ffmpeg_path),
        allowed_tools=list(chrome_devtools_allowed_tools(runtime)),
    )


async def _build_openchatcut_info(agent) -> OpenChatCutMCPInfo:
    """Build a safe status snapshot without exposing the bearer token."""

    mcp_config = agent.config.mcp
    client = None
    if mcp_config is not None:
        client = (mcp_config.clients or {}).get(OPENCHATCUT_CLIENT_KEY)
    if client is None:
        return OpenChatCutMCPInfo()

    connected = False
    manager = getattr(agent, "mcp_manager", None)
    if manager is not None:
        runtime_client = await manager.get_client(OPENCHATCUT_CLIENT_KEY)
        connected = bool(
            runtime_client and getattr(runtime_client, "is_connected", False),
        )
    record = _PROBE_RECORDS.get((agent.agent_id, OPENCHATCUT_CLIENT_KEY), {})
    if not client.enabled:
        connection_status = "disabled"
    elif record.get("error_code"):
        connection_status = "error"
    elif connected:
        connection_status = "ready"
    else:
        connection_status = "connecting"

    return OpenChatCutMCPInfo(
        configured=True,
        enabled=client.enabled,
        endpoint=client.url or OPENCHATCUT_DEFAULT_ENDPOINT,
        token_configured=openchatcut_token(client) is not None,
        connection_status=connection_status,
    )


@router.get(
    "/{client_key}/tools",
    response_model=List[MCPToolInfo],
    summary="List tools from a connected MCP server",
)
async def list_mcp_tools(
    request: Request,
    client_key: str = Path(...),
) -> List[MCPToolInfo]:
    """Query a running MCP server for its available tools.

    Returns 503 if the client is not yet connected, empty list if
    disabled, or 502 if the MCP server query fails.
    """
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)

    mcp_config = agent.config.mcp
    if mcp_config is None or client_key not in (mcp_config.clients or {}):
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")

    client_config = mcp_config.clients[client_key]
    if not client_config.enabled:
        return []

    mcp_manager = agent.mcp_manager
    if mcp_manager is None:
        raise HTTPException(
            503,
            detail="MCP manager is not ready yet, please try again later",
        )

    client = await mcp_manager.get_client(client_key)
    if client is None or not getattr(client, "is_connected", False):
        raise HTTPException(
            503,
            detail="MCP server is still connecting, please try again later",
        )

    try:
        tools = await client.list_tools()
    except Exception as e:
        logger.warning(
            f"Failed to list tools for MCP client '{client_key}': {e}",
        )
        raise HTTPException(
            502,
            detail=f"Failed to query tools from MCP server: {e}",
        ) from e

    return [
        MCPToolInfo(
            name=t.name,
            description=getattr(t, "description", "") or "",
            input_schema=(
                getattr(t, "input_schema", None)
                or getattr(t, "inputSchema", {})
                or {}
            ),
        )
        for t in tools
    ]


async def _connected_mcp_client(request: Request, client_key: str):
    """Resolve one live Connector for protocol capability endpoints."""
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    config = agent.config.mcp
    if config is None or client_key not in (config.clients or {}):
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")
    if not config.clients[client_key].enabled:
        raise HTTPException(409, detail="MCP connector is disabled")
    manager = agent.mcp_manager
    client = (
        await manager.get_client(client_key) if manager is not None else None
    )
    if client is None or not getattr(client, "is_connected", False):
        raise HTTPException(503, detail="MCP server is still connecting")
    return client


@router.get(
    "/{client_key}/resources",
    summary="List MCP resources and templates",
)
async def list_mcp_resources(
    request: Request,
    client_key: str = Path(...),
) -> Dict[str, Any]:
    client = await _connected_mcp_client(request, client_key)
    try:
        resources, templates = await asyncio.gather(
            client.list_resources(),
            client.list_resource_templates(),
        )
    except Exception as exc:
        raise HTTPException(
            502,
            detail=f"Failed to query MCP resources: {exc}",
        ) from exc
    return jsonable_encoder(
        {"resources": resources, "resource_templates": templates},
    )


@router.get("/{client_key}/prompts", summary="List MCP prompts")
async def list_mcp_prompts(
    request: Request,
    client_key: str = Path(...),
) -> List[Any]:
    client = await _connected_mcp_client(request, client_key)
    try:
        return jsonable_encoder(await client.list_prompts())
    except Exception as exc:
        raise HTTPException(
            502,
            detail=f"Failed to query MCP prompts: {exc}",
        ) from exc


@router.post("/{client_key}/resources/read", summary="Read one MCP resource")
async def read_mcp_resource(
    request: Request,
    payload: MCPResourceReadRequest,
    client_key: str = Path(...),
) -> Dict[str, Any]:
    client = await _connected_mcp_client(request, client_key)
    try:
        result = await client.read_resource(payload.uri)
    except Exception as exc:
        raise HTTPException(
            502,
            detail=f"Failed to read MCP resource: {exc}",
        ) from exc
    return {
        "trust": "untrusted_connector_content",
        "result": jsonable_encoder(result),
    }


@router.post("/{client_key}/prompts/get", summary="Render one MCP prompt")
async def get_mcp_prompt(
    request: Request,
    payload: MCPPromptGetRequest,
    client_key: str = Path(...),
) -> Dict[str, Any]:
    client = await _connected_mcp_client(request, client_key)
    try:
        result = await client.get_prompt(payload.name, payload.arguments)
    except Exception as exc:
        raise HTTPException(
            502,
            detail=f"Failed to render MCP prompt: {exc}",
        ) from exc
    return {
        "trust": "untrusted_connector_content",
        "result": jsonable_encoder(result),
    }


@router.get(
    "",
    response_model=List[MCPClientInfo],
    summary="List all MCP clients",
)
async def list_mcp_clients(request: Request) -> List[MCPClientInfo]:
    """Get list of all configured MCP clients."""
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    mcp_config = agent.config.mcp
    if mcp_config is None or not mcp_config.clients:
        return []

    return [
        _build_client_info(key, client)
        for key, client in mcp_config.clients.items()
        if key
        not in {
            CHROME_DEVTOOLS_CLIENT_KEY,
            GUANLAN_CLIENT_KEY,
            OFFICECLI_CLIENT_KEY,
            OPENCHATCUT_CLIENT_KEY,
        }
    ]


@router.get(
    "/catalog",
    response_model=List[OfficialMCPConnectorInfo],
    summary="List verified official MCP connectors",
)
async def list_official_mcp_catalog(
    request: Request,
) -> List[OfficialMCPConnectorInfo]:
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    configured_keys = (
        set(agent.config.mcp.clients) if agent.config.mcp else set()
    )
    return [
        OfficialMCPConnectorInfo(
            key=preset.key,
            name=preset.name,
            description=preset.description,
            url=preset.url,
            docs_url=preset.docs_url,
            configured=preset.key in configured_keys,
        )
        for preset in OFFICIAL_MCP_PRESETS
    ]


@router.post(
    "/catalog/{catalog_key}/install",
    response_model=MCPClientInfo,
    status_code=201,
    summary="Install and register one official MCP connector",
)
async def install_official_mcp_connector(
    request: Request,
    catalog_key: str = Path(...),
) -> MCPClientInfo:
    from ..agent_context import get_agent_for_request
    from ...config.config import MCPConfig, save_agent_config

    preset = OFFICIAL_MCP_PRESETS_BY_KEY.get(catalog_key)
    if preset is None:
        raise HTTPException(404, detail="Unknown official connector")
    agent = await get_agent_for_request(request)
    agent_config = agent.config
    if agent_config.mcp is None:
        agent_config.mcp = MCPConfig(clients={})
    if catalog_key in agent_config.mcp.clients:
        return _build_client_info(
            catalog_key,
            agent_config.mcp.clients[catalog_key],
        )

    redirect_uri = (
        str(request.base_url).rstrip("/") + "/api/mcp/oauth/callback"
    )
    try:
        oauth = await register_official_connector_oauth(
            preset,
            redirect_uri=redirect_uri,
        )
    except ConnectorLifecycleError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    client_config = preset.build_client(oauth)
    agent_config.mcp.clients[catalog_key] = client_config
    save_agent_config(agent.agent_id, agent_config)
    schedule_agent_reload(request, agent.agent_id)
    return _build_client_info(catalog_key, client_config)


@router.get(
    "/guanlan",
    response_model=GuanlanMCPInfo,
    summary="Get built-in Guanlan MCP module status",
)
async def get_guanlan_mcp(request: Request) -> GuanlanMCPInfo:
    """Get status for the reserved Guanlan MCP module."""
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    return await _build_guanlan_info(agent)


@router.put(
    "/guanlan",
    response_model=GuanlanMCPInfo,
    summary="Enable or disable built-in Guanlan MCP",
)
async def set_guanlan_mcp(
    request: Request,
    updates: GuanlanToggleRequest = Body(...),
) -> GuanlanMCPInfo:
    """Enable or disable the reserved Guanlan MCP client for this agent."""
    from ..agent_context import get_agent_for_request
    from ...config.config import MCPConfig, save_agent_config

    agent = await get_agent_for_request(request)
    agent_config = agent.config

    if updates.enabled:
        await ensure_guanlan_package()

    if agent_config.mcp is None:
        agent_config.mcp = MCPConfig(clients={})

    agent_config.mcp.clients[GUANLAN_CLIENT_KEY] = build_guanlan_client_config(
        enabled=updates.enabled,
    )
    save_agent_config(agent.agent_id, agent_config)
    schedule_agent_reload(request, agent.agent_id)

    return await _build_guanlan_info(agent)


@router.get(
    "/officecli",
    response_model=OfficeCliMCPInfo,
    summary="Get built-in OfficeCLI MCP module status",
)
async def get_officecli_mcp(request: Request) -> OfficeCliMCPInfo:
    """Get status for the reserved OfficeCLI MCP module."""
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    return _build_officecli_info(agent)


@router.put(
    "/officecli",
    response_model=OfficeCliMCPInfo,
    summary="Enable or disable built-in OfficeCLI MCP",
)
async def set_officecli_mcp(
    request: Request,
    updates: OfficeCliToggleRequest = Body(...),
) -> OfficeCliMCPInfo:
    """Enable or disable the reserved OfficeCLI MCP client for this agent."""
    from ..agent_context import get_agent_for_request
    from ...config.config import MCPConfig, save_agent_config

    agent = await get_agent_for_request(request)
    agent_config = agent.config
    status = get_officecli_status()

    if updates.enabled and not status.available:
        raise HTTPException(
            status_code=400,
            detail=(
                "OfficeCLI is not installed. Install officecli or configure "
                "OFFICECLI_PATH before enabling the MCP client."
            ),
        )

    if agent_config.mcp is None:
        agent_config.mcp = MCPConfig(clients={})

    agent_config.mcp.clients[
        OFFICECLI_CLIENT_KEY
    ] = build_officecli_mcp_client_config(enabled=updates.enabled)
    save_agent_config(agent.agent_id, agent_config)
    schedule_agent_reload(request, agent.agent_id)

    return _build_officecli_info(agent)


@router.get(
    "/chrome-devtools",
    response_model=ChromeDevToolsMCPInfo,
    summary="Get bundled Chrome DevTools MCP status",
)
async def get_chrome_devtools_mcp(request: Request) -> ChromeDevToolsMCPInfo:
    """Get status for the reserved Chrome DevTools MCP module."""

    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    return await _build_chrome_devtools_info(agent)


@router.put(
    "/chrome-devtools",
    response_model=ChromeDevToolsMCPInfo,
    summary="Enable or disable bundled Chrome DevTools MCP",
)
async def set_chrome_devtools_mcp(
    request: Request,
    updates: ChromeDevToolsToggleRequest = Body(...),
) -> ChromeDevToolsMCPInfo:
    """Persist the module toggle without installing or updating packages."""

    from ..agent_context import get_agent_for_request
    from ...config.config import MCPConfig, save_agent_config

    agent = await get_agent_for_request(request)
    agent_config = agent.config
    runtime = resolve_chrome_devtools_runtime()
    if updates.enabled and not runtime.available:
        raise HTTPException(status_code=400, detail=runtime.message)
    if agent_config.mcp is None:
        agent_config.mcp = MCPConfig(clients={})
    agent_config.mcp.clients[
        CHROME_DEVTOOLS_CLIENT_KEY
    ] = build_chrome_devtools_client_config(
        enabled=updates.enabled,
        agent_id=agent.agent_id,
    )
    _PROBE_RECORDS.pop((agent.agent_id, CHROME_DEVTOOLS_CLIENT_KEY), None)
    save_agent_config(agent.agent_id, agent_config)
    schedule_agent_reload(request, agent.agent_id)
    return await _build_chrome_devtools_info(agent)


@router.get(
    "/openchatcut",
    response_model=OpenChatCutMCPInfo,
    summary="Get curated OpenChatCut MCP connector status",
)
async def get_openchatcut_mcp(request: Request) -> OpenChatCutMCPInfo:
    """Get status for the optional local OpenChatCut connector."""
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    return await _build_openchatcut_info(agent)


@router.put(
    "/openchatcut",
    response_model=OpenChatCutMCPInfo,
    summary="Configure the local OpenChatCut MCP connector",
)
async def configure_openchatcut_mcp(
    request: Request,
    updates: OpenChatCutConfigureRequest = Body(...),
) -> OpenChatCutMCPInfo:
    """Persist a loopback-only OpenChatCut connector for the current agent."""
    from ..agent_context import get_agent_for_request
    from ...config.config import MCPConfig, save_agent_config

    agent = await get_agent_for_request(request)
    agent_config = agent.config
    if agent_config.mcp is None:
        agent_config.mcp = MCPConfig(clients={})
    existing = (agent_config.mcp.clients or {}).get(OPENCHATCUT_CLIENT_KEY)
    endpoint = updates.endpoint or (
        existing.url if existing is not None else OPENCHATCUT_DEFAULT_ENDPOINT
    )
    token = updates.token or openchatcut_token(existing)
    try:
        client_config = build_openchatcut_client_config(
            endpoint=endpoint,
            token=token or "",
            enabled=updates.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    agent_config.mcp.clients[OPENCHATCUT_CLIENT_KEY] = client_config
    _PROBE_RECORDS.pop((agent.agent_id, OPENCHATCUT_CLIENT_KEY), None)
    save_agent_config(agent.agent_id, agent_config)
    schedule_agent_reload(request, agent.agent_id)
    return await _build_openchatcut_info(agent)


@router.get(
    "/oauth/callback",
    response_class=HTMLResponse,
    name="mcp_oauth_callback",
    summary="Complete an MCP OAuth authorization",
)
async def complete_mcp_oauth(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
) -> HTMLResponse:
    """Consume one PKCE state, persist tokens, and reload the agent."""

    if error:
        return HTMLResponse(
            "<h2>连接未完成</h2><p>认证服务拒绝了这次授权。请返回澜台重试。</p>",
            status_code=400,
        )
    try:
        pending = oauth_pending_registry.consume(state)
        from ...config.config import load_agent_config, save_agent_config

        agent_config = load_agent_config(pending.agent_id)
        client_config = (
            (agent_config.mcp.clients or {}).get(pending.client_key)
            if agent_config.mcp
            else None
        )
        if client_config is None or not client_config.oauth.enabled:
            raise ConnectorLifecycleError(
                "configuration_missing",
                "连接器已删除或 OAuth 已关闭。",
                status_code=404,
            )
        if not code:
            raise ConnectorLifecycleError(
                "oauth_code_missing",
                "认证服务没有返回授权码。",
                status_code=400,
            )
        client_config.oauth = await exchange_oauth_code(
            client_config.oauth,
            code=code,
            verifier=pending.verifier,
            redirect_uri=pending.redirect_uri,
            allow_private_network=client_config.allow_private_network,
        )
        save_agent_config(pending.agent_id, agent_config)
        _PROBE_RECORDS.pop((pending.agent_id, pending.client_key), None)
        schedule_agent_reload(request, pending.agent_id)
    except ConnectorLifecycleError as exc:
        return HTMLResponse(
            "<h2>连接未完成</h2><p>授权信息无效或已过期，请返回澜台重新连接。</p>",
            status_code=exc.status_code,
        )

    return HTMLResponse(
        "<h2>连接成功</h2><p>凭据已安全保存。你可以关闭此页面并返回澜台。</p>",
    )


@router.get(
    "/{client_key}/lifecycle",
    response_model=MCPConnectorLifecycleInfo,
    summary="Get connector lifecycle status",
)
async def get_mcp_lifecycle(
    request: Request,
    client_key: str = Path(...),
) -> MCPConnectorLifecycleInfo:
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    if agent.config.mcp is None or client_key not in agent.config.mcp.clients:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")
    return await _build_lifecycle_info(agent, client_key)


@router.post(
    "/{client_key}/probe",
    response_model=MCPConnectorLifecycleInfo,
    summary="Refresh authorization and probe an MCP connector",
)
async def probe_mcp_connector(
    request: Request,
    client_key: str = Path(...),
) -> MCPConnectorLifecycleInfo:
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    if agent.config.mcp is None or client_key not in agent.config.mcp.clients:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")
    client_config = agent.config.mcp.clients[client_key]
    record_key = (agent.agent_id, client_key)
    if not client_config.enabled:
        _PROBE_RECORDS.pop(record_key, None)
        return await _build_lifecycle_info(agent, client_key)
    if agent.mcp_manager is None:
        raise HTTPException(503, detail="MCP manager is not ready")

    try:
        await agent.mcp_manager.replace_client(
            client_key,
            client_config,
            timeout=20.0,
        )
        client = await agent.mcp_manager.get_client(client_key)
        if client is None:
            raise RuntimeError("MCP client did not enter the runtime")
        await client.list_tools()
        _PROBE_RECORDS[record_key] = {
            "last_probe_at": int(time.time()),
        }
    except Exception as exc:  # connector SDKs expose heterogeneous errors
        category, code = classify_connector_error(exc)
        logger.warning(
            "MCP connector probe failed: agent=%s client=%s "
            "category=%s code=%s",
            agent.agent_id,
            client_key,
            category,
            code,
            exc_info=True,
        )
        safe_messages = {
            "authorization": "认证失败或已过期，请重新授权。",
            "configuration": "连接器配置不完整，请检查地址、transport 和认证参数。",
            "connection": "当前无法连接服务，请检查服务状态和网络后重试。",
            "server": "服务已响应，但工具探测失败，请查看后端诊断日志。",
        }
        _PROBE_RECORDS[record_key] = {
            "last_probe_at": int(time.time()),
            "error_category": category,
            "error_code": code,
            "error_message": safe_messages.get(category, "连接器探测失败。"),
        }
    return await _build_lifecycle_info(agent, client_key)


@router.post(
    "/{client_key}/oauth/start",
    response_model=MCPOAuthStartResponse,
    summary="Start MCP OAuth Authorization Code + PKCE",
)
async def start_mcp_oauth(
    request: Request,
    client_key: str = Path(...),
) -> MCPOAuthStartResponse:
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    if agent.config.mcp is None or client_key not in agent.config.mcp.clients:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")
    client_config = agent.config.mcp.clients[client_key]
    if not client_config.oauth.enabled:
        raise HTTPException(
            409,
            detail={
                "code": "oauth_not_configured",
                "message": "请先在连接器配置中启用 OAuth。",
            },
        )

    redirect_uri = client_config.oauth.redirect_uri.strip() or (
        str(request.base_url).rstrip("/") + "/api/mcp/oauth/callback"
    )
    started = oauth_pending_registry.create(
        agent_id=agent.agent_id,
        client_key=client_key,
        oauth=client_config.oauth,
        redirect_uri=redirect_uri,
    )
    return MCPOAuthStartResponse(
        authorization_url=started.authorization_url,
        state=started.state,
        expires_at=started.expires_at,
    )


@router.get(
    "/{client_key}",
    response_model=MCPClientInfo,
    summary="Get MCP client details",
)
async def get_mcp_client(
    request: Request,
    client_key: str = Path(...),
) -> MCPClientInfo:
    """Get details of a specific MCP client."""
    from ..agent_context import get_agent_for_request

    agent = await get_agent_for_request(request)
    mcp_config = agent.config.mcp
    if mcp_config is None:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")

    client = mcp_config.clients.get(client_key)
    if client is None:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")
    return _build_client_info(client_key, client)


@router.post(
    "",
    response_model=MCPClientInfo,
    summary="Create a new MCP client",
    status_code=201,
)
async def create_mcp_client(
    request: Request,
    client_key: str = Body(..., embed=True),
    client: MCPClientCreateRequest = Body(..., embed=True),
) -> MCPClientInfo:
    """Create a new MCP client configuration."""
    from ..agent_context import get_agent_for_request
    from ...config.config import save_agent_config, MCPConfig

    agent = await get_agent_for_request(request)
    agent_config = agent.config

    if client_key in {
        CHROME_DEVTOOLS_CLIENT_KEY,
        OPENCHATCUT_CLIENT_KEY,
    }:
        raise HTTPException(
            400,
            detail=(
                f"MCP client '{client_key}' must be configured through "
                "its connector"
            ),
        )

    # Initialize mcp config if not exists
    if agent_config.mcp is None:
        agent_config.mcp = MCPConfig(clients={})

    # Check if client already exists
    if client_key in agent_config.mcp.clients:
        raise HTTPException(
            400,
            detail=f"MCP client '{client_key}' already exists. Use PUT to "
            f"update.",
        )

    # Create new client config
    new_client = MCPClientConfig(
        name=client.name,
        description=client.description,
        enabled=client.enabled,
        transport=client.transport,
        url=client.url,
        allow_private_network=client.allow_private_network,
        headers=client.headers,
        command=client.command,
        args=client.args,
        env=client.env,
        cwd=client.cwd,
        oauth=client.oauth,
        allowed_tools=client.allowed_tools,
    )

    # Add to agent's config and save
    agent_config.mcp.clients[client_key] = new_client
    save_agent_config(agent.agent_id, agent_config)

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(request, agent.agent_id)

    return _build_client_info(client_key, new_client)


@router.put(
    "/{client_key}",
    response_model=MCPClientInfo,
    summary="Update an MCP client",
)
async def update_mcp_client(
    request: Request,
    client_key: str = Path(...),
    updates: MCPClientUpdateRequest = Body(...),
) -> MCPClientInfo:
    """Update an existing MCP client configuration."""
    from ..agent_context import get_agent_for_request
    from ...config.config import save_agent_config

    agent = await get_agent_for_request(request)
    agent_config = agent.config

    if client_key == CHROME_DEVTOOLS_CLIENT_KEY:
        raise HTTPException(
            400,
            detail=(
                "Chrome DevTools is a reserved built-in module; use its "
                "dedicated toggle."
            ),
        )

    if agent_config.mcp is None or client_key not in agent_config.mcp.clients:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")

    existing = agent_config.mcp.clients[client_key]

    # Update fields if provided
    update_data = updates.model_dump(exclude_unset=True)
    expected_revision = update_data.pop("expected_revision", None)
    if expected_revision is not None and expected_revision != existing.revision:
        if set(update_data) == {"enabled"} and update_data["enabled"] == existing.enabled:
            return _build_client_info(client_key, existing)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "resource_revision_conflict",
                "expected_revision": expected_revision,
                "current_revision": existing.revision,
                "current_enabled": existing.enabled,
            },
        )

    # Restore masked env/header values to originals before replacing
    if "env" in update_data and update_data["env"] is not None:
        update_data["env"] = _restore_original_values(
            update_data["env"],
            existing.env or {},
        )

    if "headers" in update_data and update_data["headers"] is not None:
        update_data["headers"] = _restore_original_values(
            update_data["headers"],
            existing.headers or {},
        )

    if "oauth" in update_data and update_data["oauth"] is not None:
        incoming_oauth = dict(update_data["oauth"])
        existing_oauth = existing.oauth.model_dump(mode="json")
        for secret_field in ("client_secret", "access_token", "refresh_token"):
            incoming_value = incoming_oauth.get(secret_field)
            existing_value = existing_oauth.get(secret_field)
            if (
                isinstance(incoming_value, str)
                and isinstance(existing_value, str)
                and incoming_value == _mask_env_value(existing_value)
            ):
                incoming_oauth[secret_field] = existing_value
        existing_oauth.update(incoming_oauth)
        update_data["oauth"] = existing_oauth

    merged_data = existing.model_dump(mode="json")
    merged_data.update(update_data)
    if update_data:
        merged_data["revision"] = existing.revision + 1
    updated_client = MCPClientConfig.model_validate(merged_data)
    agent_config.mcp.clients[client_key] = updated_client
    _PROBE_RECORDS.pop((agent.agent_id, client_key), None)

    # Save updated config
    save_agent_config(agent.agent_id, agent_config)

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(request, agent.agent_id)

    return _build_client_info(client_key, updated_client)


@router.patch(
    "/{client_key}/toggle",
    response_model=MCPClientInfo,
    summary="Toggle MCP client enabled status",
)
async def toggle_mcp_client(
    request: Request,
    client_key: str = Path(...),
) -> MCPClientInfo:
    """Toggle the enabled status of an MCP client."""
    from ..agent_context import get_agent_for_request
    from ...config.config import save_agent_config

    agent = await get_agent_for_request(request)
    agent_config = agent.config

    if client_key == CHROME_DEVTOOLS_CLIENT_KEY:
        raise HTTPException(
            400,
            detail=(
                "Chrome DevTools is a reserved built-in module; use its "
                "dedicated toggle."
            ),
        )

    if agent_config.mcp is None or client_key not in agent_config.mcp.clients:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")

    client = agent_config.mcp.clients[client_key]

    # Toggle enabled status
    client.enabled = not client.enabled
    client.revision += 1
    _PROBE_RECORDS.pop((agent.agent_id, client_key), None)
    save_agent_config(agent.agent_id, agent_config)

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(request, agent.agent_id)

    return _build_client_info(client_key, client)


@router.delete(
    "/{client_key}",
    response_model=Dict[str, str],
    summary="Delete an MCP client",
)
async def delete_mcp_client(
    request: Request,
    client_key: str = Path(...),
) -> Dict[str, str]:
    """Delete an MCP client configuration."""
    from ..agent_context import get_agent_for_request
    from ...config.config import save_agent_config

    agent = await get_agent_for_request(request)
    agent_config = agent.config

    if client_key == CHROME_DEVTOOLS_CLIENT_KEY:
        raise HTTPException(
            400,
            detail="Chrome DevTools is a reserved built-in module.",
        )

    if agent_config.mcp is None or client_key not in agent_config.mcp.clients:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")

    # Remove client
    del agent_config.mcp.clients[client_key]
    _PROBE_RECORDS.pop((agent.agent_id, client_key), None)
    save_agent_config(agent.agent_id, agent_config)

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(request, agent.agent_id)

    return {"message": f"MCP client '{client_key}' deleted successfully"}
