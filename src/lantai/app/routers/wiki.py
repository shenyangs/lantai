# -*- coding: utf-8 -*-
"""Local Wiki API for agent and desktop integrations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ..agent_context import get_agent_for_request
from ..wiki import core as wiki_core


router = APIRouter(prefix="/wiki", tags=["wiki"])

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _ensure_local_or_token(
    request: Request,
    token: str | None,
) -> None:
    configured_token = os.getenv("LANTAI_WIKI_API_TOKEN", "").strip()
    client_host = request.client.host if request.client else ""
    if client_host in LOCAL_HOSTS:
        if configured_token and token and token != configured_token:
            raise HTTPException(status_code=403, detail="Invalid wiki API token")
        return
    if configured_token and token == configured_token:
        return
    raise HTTPException(status_code=403, detail="Wiki API only accepts local requests")


def _safe_content_path(workspace_dir: Path, requested_path: str) -> Path:
    raw = (requested_path or "").strip().replace("\\", "/")
    if not raw:
        raise HTTPException(status_code=400, detail="Path is required")
    relative = Path(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(status_code=400, detail="Invalid path")
    if wiki_core.is_internal_runtime_relative_path(relative) or wiki_core.is_internal_workspace_md(relative.name):
        raise HTTPException(status_code=403, detail="Internal files are protected")
    if relative.parts and relative.parts[0] == wiki_core.WIKI_INTERNAL_DIR:
        allowed = False
        if len(relative.parts) >= 3 and relative.parts[1] == wiki_core.WIKI_PAGES_DIR:
            allowed = True
        if (
            len(relative.parts) >= 4
            and relative.parts[1] == wiki_core.WIKI_RAW_DIR
            and relative.parts[2] == wiki_core.WIKI_RAW_SOURCES_DIR
        ):
            allowed = True
        if len(relative.parts) == 2 and relative.parts[1] in {
            wiki_core.WIKI_PURPOSE_FILE,
            wiki_core.WIKI_INDEX_FILE,
            wiki_core.WIKI_OVERVIEW_FILE,
        }:
            allowed = True
        if not allowed:
            raise HTTPException(status_code=403, detail="Private wiki runtime files are protected")
    resolved = (workspace_dir / relative).resolve()
    if not str(resolved).startswith(str(workspace_dir.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


@router.get("/health", response_model=dict)
async def wiki_health(
    request: Request,
    x_lantai_wiki_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return local Wiki API health."""
    _ensure_local_or_token(request, x_lantai_wiki_token)
    workspace = await get_agent_for_request(request)
    wiki_core.ensure_wiki_layout(workspace.workspace_dir)
    return {"ok": True, "service": "wiki", "workspace_dir": str(workspace.workspace_dir)}


@router.get("/projects", response_model=dict)
async def wiki_projects(
    request: Request,
    x_lantai_wiki_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return the active local wiki project."""
    _ensure_local_or_token(request, x_lantai_wiki_token)
    workspace = await get_agent_for_request(request)
    return {
        "projects": [
            {
                "agent_id": workspace.agent_id,
                "workspace_dir": str(workspace.workspace_dir),
                "wiki_dir": str(wiki_core.wiki_root(workspace.workspace_dir)),
            },
        ],
    }


@router.post("/search", response_model=wiki_core.WikiQueryResponse)
async def wiki_search(
    body: wiki_core.WikiQueryRequest,
    request: Request,
    x_lantai_wiki_token: str | None = Header(default=None),
) -> wiki_core.WikiQueryResponse:
    """Search the active wiki and return cited local context."""
    _ensure_local_or_token(request, x_lantai_wiki_token)
    workspace = await get_agent_for_request(request)
    return wiki_core.build_wiki_query(workspace.workspace_dir, body)


@router.get("/files/content", response_model=dict)
async def wiki_file_content(
    request: Request,
    path: str = Query(..., min_length=1, max_length=600),
    x_lantai_wiki_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Read a public wiki page or raw source file."""
    _ensure_local_or_token(request, x_lantai_wiki_token)
    workspace = await get_agent_for_request(request)
    content_path = _safe_content_path(workspace.workspace_dir, path)
    return {
        "path": path,
        "content": wiki_core.read_text_safely(content_path, wiki_core.MAX_QUERY_CONTEXT_CHARS),
    }


@router.get("/graph", response_model=dict)
async def wiki_graph(
    request: Request,
    x_lantai_wiki_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return the active wiki graph."""
    _ensure_local_or_token(request, x_lantai_wiki_token)
    workspace = await get_agent_for_request(request)
    return wiki_core.collect_wiki_data(workspace.workspace_dir).graph


@router.post("/rescan", response_model=dict)
async def wiki_rescan(
    request: Request,
    x_lantai_wiki_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Refresh local indexes and queue status."""
    _ensure_local_or_token(request, x_lantai_wiki_token)
    workspace = await get_agent_for_request(request)
    return wiki_core.run_wiki_ingest(
        workspace.workspace_dir,
        wiki_core.WikiIngestRunRequest(mode="fast"),
    )
