# -*- coding: utf-8 -*-
"""Agent-native, approval-gated management of Skills and MCP connectors."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from ...app.agent_context import get_current_agent_id
from ...config.context import get_current_workspace_context
from ...runtime.tool_response import TextBlock, ToolResponse
from ..skills_hub import install_skill_from_source
from .agent_management import create_agent_api_client


def _text_response(data: Any, *, is_error: bool = False) -> ToolResponse:
    text = data if isinstance(data, str) else json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
    )
    return ToolResponse(
        content=[TextBlock(type="text", text=text)],
        is_error=is_error,
    )


def _workspace_context_or_error(
    *,
    require_write: bool = True,
) -> tuple[Any | None, ToolResponse | None]:
    context = get_current_workspace_context()
    if context is None:
        return None, _text_response(
            "Error: No active Agent workspace context.",
            is_error=True,
        )
    if require_write and context.access_mode != "read_write":
        return None, _text_response(
            "Error: This workspace is read-only.",
            is_error=True,
        )
    return context, None


async def install_agent_skill(
    source: str,
    enable: bool = True,
    target_name: str = "",
) -> ToolResponse:
    """Install one Skill from a local directory, ZIP, or fixed HTTP(S) URL.

    Relative paths resolve inside the current topic workspace. The source is
    staged, structurally validated, security scanned, conflict checked, and
    installed into the current Agent's Skill scope. With ``enable=True`` the
    Skill can be read immediately in this turn with ``read_skill``.
    """

    context, error = _workspace_context_or_error()
    if error is not None:
        return error
    try:
        result = await asyncio.to_thread(
            install_skill_from_source,
            workspace_dir=Path(context.agent_home),
            source=source,
            source_root=Path(context.project_root),
            enable=enable,
            target_name=target_name or None,
        )
    except Exception as exc:  # validated domain errors are user-safe here
        return _text_response(f"Error: {exc}", is_error=True)
    return _text_response(
        {
            "status": "installed",
            "name": result.name,
            "enabled": result.enabled,
            "scope": "agent",
            "source": result.source_url,
            "immediately_readable": bool(result.enabled),
            "next_step": (
                f"Call read_skill('{result.name}') now. The prompt catalog "
                "will also include it on the next turn."
            ),
        },
    )


def _resolve_config_file(project_root: Path, config_file: str) -> Path:
    candidate = Path(config_file).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ValueError(
            "MCP config_file must be inside the current topic workspace",
        ) from exc
    if not resolved.is_file():
        raise ValueError(f"MCP config file was not found: {config_file}")
    return resolved


def _load_mcp_config(project_root: Path, config_file: str) -> dict[str, Any]:
    path = _resolve_config_file(project_root, config_file)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MCP config file must contain one JSON object")
    return payload


def _mcp_request(
    *,
    action: str,
    connector_key: str,
    config: dict[str, Any] | None,
    agent_id: str,
    base_url: str,
) -> Any:
    headers = {"X-Agent-Id": agent_id}
    with create_agent_api_client(base_url or None) as client:
        if action == "list":
            response = client.get("/mcp", headers=headers)
            response.raise_for_status()
            connectors = response.json()
            native_capabilities = []
            for path, fallback in (
                (
                    "/mcp/guanlan",
                    {
                        "client_key": "guanlan",
                        "name": "观澜 / Guanlan",
                        "capability_status": "error",
                        "connected": False,
                        "runtime_message": (
                            "Native capability status is temporarily unavailable."
                        ),
                    },
                ),
                (
                    "/mcp/chrome-devtools",
                    {
                        "client_key": "chrome-devtools",
                        "name": "Chrome DevTools",
                        "enabled": False,
                        "available": False,
                        "capability_status": "error",
                        "connected": False,
                        "runtime_message": (
                            "Native capability status is temporarily unavailable."
                        ),
                    },
                ),
            ):
                native_response = client.get(path, headers=headers)
                try:
                    native_response.raise_for_status()
                    native_capabilities.append(native_response.json())
                except httpx.HTTPError:
                    # Each native projection fails independently: one broken
                    # status endpoint cannot erase another built-in or any
                    # healthy custom connector.
                    native_capabilities.append(fallback)
            return {
                "native_capabilities": native_capabilities,
                "connectors": connectors,
                "model_guidance": (
                    "Guanlan and Chrome DevTools are Lantai-native system "
                    "capabilities. Never ask the user to install, clone, or "
                    "configure either as a custom connector. Use enabled, "
                    "available, connected, and capability_status as runtime "
                    "truth; disabled or unavailable does not mean ready."
                ),
            }
        elif action == "show":
            response = client.get(f"/mcp/{connector_key}", headers=headers)
        elif action == "add":
            response = client.post(
                "/mcp",
                headers=headers,
                json={"client_key": connector_key, "client": config},
            )
        elif action == "update":
            response = client.put(
                f"/mcp/{connector_key}",
                headers=headers,
                json=config,
            )
        elif action == "toggle":
            response = client.patch(
                f"/mcp/{connector_key}/toggle",
                headers=headers,
            )
        elif action == "remove":
            response = client.delete(
                f"/mcp/{connector_key}",
                headers=headers,
            )
        elif action == "probe":
            response = client.post(
                f"/mcp/{connector_key}/probe",
                headers=headers,
            )
        else:  # guarded by caller, retained as a fail-closed boundary
            raise ValueError(f"Unsupported MCP action: {action}")
        response.raise_for_status()
        return response.json()


async def manage_mcp_connector(
    action: str,
    connector_key: str = "",
    config_file: str = "",
    base_url: str = "",
) -> ToolResponse:
    """Add, update, toggle, remove, or probe an MCP connector.

    ``add`` and ``update`` read configuration from a JSON file inside the
    current topic workspace, keeping credentials out of tool-call arguments.
    Changes use Lantai's canonical authenticated control plane, including
    validation, secret masking, persistence, and Agent hot reload.
    """

    context, error = _workspace_context_or_error()
    if error is not None:
        return error
    normalized_action = str(action or "").strip().lower()
    allowed = {"add", "update", "toggle", "remove", "probe"}
    if normalized_action not in allowed:
        return _text_response(
            f"Error: action must be one of {', '.join(sorted(allowed))}.",
            is_error=True,
        )
    key = str(connector_key or "").strip()
    if normalized_action != "list" and not key:
        return _text_response(
            "Error: connector_key is required for this action.",
            is_error=True,
        )
    try:
        if normalized_action in {"add", "update"} and not config_file:
            raise ValueError("config_file is required for add or update")
        config = (
            _load_mcp_config(Path(context.project_root), config_file)
            if normalized_action in {"add", "update"}
            else None
        )
        result = await asyncio.to_thread(
            _mcp_request,
            action=normalized_action,
            connector_key=key,
            config=config,
            agent_id=get_current_agent_id(),
            base_url=base_url,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return _text_response(f"Error: {exc}", is_error=True)
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail")
        except Exception:
            detail = None
        return _text_response(
            f"Error: MCP control plane rejected the request"
            f"{f': {detail}' if detail else '.'}",
            is_error=True,
        )
    except httpx.HTTPError as exc:
        return _text_response(
            f"Error: MCP control plane is unavailable: {exc}",
            is_error=True,
        )
    return _text_response(
        {
            "status": "ok",
            "action": normalized_action,
            "connector_key": key,
            "hot_reload_requested": normalized_action
            in {"add", "update", "toggle", "remove"},
            "result": result,
        },
    )


async def inspect_mcp_connectors(
    connector_key: str = "",
    base_url: str = "",
) -> ToolResponse:
    """List MCP connectors or inspect one connector without changing state."""

    context, error = _workspace_context_or_error(require_write=False)
    if error is not None:
        return error
    del context
    key = str(connector_key or "").strip()
    try:
        result = await asyncio.to_thread(
            _mcp_request,
            action="show" if key else "list",
            connector_key=key,
            config=None,
            agent_id=get_current_agent_id(),
            base_url=base_url,
        )
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail")
        except Exception:
            detail = None
        return _text_response(
            f"Error: MCP control plane rejected the request"
            f"{f': {detail}' if detail else '.'}",
            is_error=True,
        )
    except httpx.HTTPError as exc:
        return _text_response(
            f"Error: MCP control plane is unavailable: {exc}",
            is_error=True,
        )
    return _text_response(result)
