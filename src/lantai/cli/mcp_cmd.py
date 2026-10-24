# -*- coding: utf-8 -*-
"""Stable CLI control seam for MCP Connectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import httpx

from .http import client, print_json, resolve_base_url


def _request(
    ctx: click.Context,
    method: str,
    path: str,
    *,
    base_url: str | None,
    json_body: Any = None,
) -> Any:
    try:
        with client(resolve_base_url(ctx, base_url)) as http:
            response = http.request(method, path, json=json_body)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail")
        except Exception:
            detail = exc.response.text
        raise click.ClickException(str(detail or exc)) from exc
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Cannot reach Lantai: {exc}") from exc


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        try:
            data = json.loads(Path(value).read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise click.BadParameter(f"{label} must be a JSON object or JSON file") from exc
    if not isinstance(data, dict):
        raise click.BadParameter(f"{label} must be a JSON object")
    return data


@click.group("mcp")
def mcp_group() -> None:
    """Manage MCP Connectors through the same Console control surface."""


def _base_url_option(function):
    return click.option("--base-url", default=None)(function)


@mcp_group.command("list")
@_base_url_option
@click.pass_context
def list_connectors(ctx: click.Context, base_url: str | None) -> None:
    """List configured custom Connectors."""
    print_json(_request(ctx, "GET", "/mcp", base_url=base_url))


@mcp_group.command("show")
@click.argument("key")
@_base_url_option
@click.pass_context
def show_connector(ctx: click.Context, key: str, base_url: str | None) -> None:
    """Show one Connector with secrets masked by the Console."""
    print_json(_request(ctx, "GET", f"/mcp/{key}", base_url=base_url))


@mcp_group.command("add")
@click.argument("key")
@click.option("--config", required=True, help="JSON object or path to JSON file")
@_base_url_option
@click.pass_context
def add_connector(
    ctx: click.Context,
    key: str,
    config: str,
    base_url: str | None,
) -> None:
    """Add a Connector from the canonical Console JSON contract."""
    payload = {"client_key": key, "client": _json_object(config, "config")}
    print_json(_request(ctx, "POST", "/mcp", base_url=base_url, json_body=payload))


@mcp_group.command("update")
@click.argument("key")
@click.option("--config", required=True, help="JSON object or path to JSON file")
@_base_url_option
@click.pass_context
def update_connector(
    ctx: click.Context,
    key: str,
    config: str,
    base_url: str | None,
) -> None:
    """Patch a Connector using the canonical Console JSON contract."""
    print_json(
        _request(
            ctx,
            "PUT",
            f"/mcp/{key}",
            base_url=base_url,
            json_body=_json_object(config, "config"),
        ),
    )


@mcp_group.command("toggle")
@click.argument("key")
@_base_url_option
@click.pass_context
def toggle_connector(ctx: click.Context, key: str, base_url: str | None) -> None:
    """Toggle whether a Connector is enabled."""
    print_json(_request(ctx, "PATCH", f"/mcp/{key}/toggle", base_url=base_url))


@mcp_group.command("remove")
@click.argument("key")
@click.option("--yes", is_flag=True, help="Skip confirmation")
@_base_url_option
@click.pass_context
def remove_connector(
    ctx: click.Context,
    key: str,
    yes: bool,
    base_url: str | None,
) -> None:
    """Remove a Connector."""
    if not yes:
        click.confirm(f"Remove MCP Connector '{key}'?", abort=True)
    print_json(_request(ctx, "DELETE", f"/mcp/{key}", base_url=base_url))


@mcp_group.command("tools")
@click.argument("key")
@_base_url_option
@click.pass_context
def connector_tools(ctx: click.Context, key: str, base_url: str | None) -> None:
    """List live tools advertised by a Connector."""
    print_json(_request(ctx, "GET", f"/mcp/{key}/tools", base_url=base_url))


@mcp_group.command("resources")
@click.argument("key")
@_base_url_option
@click.pass_context
def connector_resources(ctx: click.Context, key: str, base_url: str | None) -> None:
    """List live resources and templates advertised by a Connector."""
    print_json(_request(ctx, "GET", f"/mcp/{key}/resources", base_url=base_url))


@mcp_group.command("prompts")
@click.argument("key")
@_base_url_option
@click.pass_context
def connector_prompts(ctx: click.Context, key: str, base_url: str | None) -> None:
    """List live prompts advertised by a Connector."""
    print_json(_request(ctx, "GET", f"/mcp/{key}/prompts", base_url=base_url))


@mcp_group.command("read-resource")
@click.argument("key")
@click.argument("uri")
@_base_url_option
@click.pass_context
def read_resource(
    ctx: click.Context,
    key: str,
    uri: str,
    base_url: str | None,
) -> None:
    """Read one untrusted Connector resource by URI."""
    print_json(
        _request(
            ctx,
            "POST",
            f"/mcp/{key}/resources/read",
            base_url=base_url,
            json_body={"uri": uri},
        ),
    )


@mcp_group.command("get-prompt")
@click.argument("key")
@click.argument("name")
@click.option("--arguments", default="{}", help="JSON object or JSON file")
@_base_url_option
@click.pass_context
def get_prompt(
    ctx: click.Context,
    key: str,
    name: str,
    arguments: str,
    base_url: str | None,
) -> None:
    """Render an untrusted Connector prompt without changing its role."""
    parsed = _json_object(arguments, "arguments")
    if any(not isinstance(value, str) for value in parsed.values()):
        raise click.BadParameter("prompt argument values must be strings")
    print_json(
        _request(
            ctx,
            "POST",
            f"/mcp/{key}/prompts/get",
            base_url=base_url,
            json_body={"name": name, "arguments": parsed},
        ),
    )


@mcp_group.command("probe")
@click.argument("key")
@_base_url_option
@click.pass_context
def probe_connector(ctx: click.Context, key: str, base_url: str | None) -> None:
    """Probe transport, authentication, initialization, and tools."""
    print_json(_request(ctx, "POST", f"/mcp/{key}/probe", base_url=base_url))
