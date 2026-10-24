# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import Any, Optional

import click
import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8088"


def client(base_url: str) -> httpx.Client:
    """Create HTTP client with /api prefix added to all requests."""
    from ..agents.tools.agent_management import create_agent_api_client

    # Reuse origin-scoped desktop authentication; an explicit remote or other
    # local endpoint must never receive the desktop credential.
    return create_agent_api_client(base_url, default_timeout=30.0)


def print_json(data: Any) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


def resolve_base_url(ctx: click.Context, base_url: Optional[str]) -> str:
    """Resolve base_url with priority:
    1) command --base-url
    2) global --host/--port (from ctx.obj)

    Args:
        ctx: Click context containing global options
        base_url: Optional base_url override from command option

    Returns:
        Resolved base URL string
    """
    if base_url:
        return base_url.rstrip("/")
    host = (ctx.obj or {}).get("host", "127.0.0.1")
    port = (ctx.obj or {}).get("port", 8088)
    return f"http://{host}:{port}"
