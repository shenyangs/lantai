# -*- coding: utf-8 -*-
"""Read-only local Wiki tools for the main agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...app.wiki import core as wiki_core
from ...config.context import get_current_workspace_dir
from ...constant import WORKING_DIR
from ...runtime.tool_response import TextBlock, ToolResponse


def _workspace_dir() -> Path:
    return Path(get_current_workspace_dir() or WORKING_DIR)


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def _json_response(data: Any) -> ToolResponse:
    return _text_response(json.dumps(data, ensure_ascii=False, indent=2))


def _safe_read_path(workspace_dir: Path, path: str) -> Path:
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("path is required")
    relative = Path(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("invalid path")
    if wiki_core.is_internal_workspace_md(relative.name) or wiki_core.is_internal_runtime_relative_path(relative):
        raise PermissionError("internal runtime files are protected")

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
            raise PermissionError("private wiki runtime files are protected")
        resolved = (workspace_dir / relative).resolve()
    else:
        resolved = wiki_core.resolve_public_page_path(workspace_dir, raw).resolve()

    if not str(resolved).startswith(str(workspace_dir.resolve())):
        raise ValueError("invalid path")
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(raw)
    return resolved


async def wiki_search(query: str, limit: int = 8) -> ToolResponse:
    """Search the local Wiki knowledge base.

    Use this before answering questions that may depend on user-imported
    documents or previously saved knowledge pages. The tool is read-only.

    Args:
        query: Search query.
        limit: Maximum number of cited results to return.
    """
    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        normalized_limit = 8
    normalized_limit = max(1, min(normalized_limit, 30))
    workspace_dir = _workspace_dir()
    result = wiki_core.build_wiki_query(
        workspace_dir,
        wiki_core.WikiQueryRequest(query=query, limit=normalized_limit),
    )
    return _json_response(result.model_dump())


async def wiki_search_for_sources(
    query: str,
    source_paths: list[str],
    limit: int = 8,
) -> ToolResponse:
    """Runtime-only exact-scope search used by the tool registry."""

    try:
        normalized_limit = max(1, min(int(limit), 30))
    except (TypeError, ValueError):
        normalized_limit = 8
    result = wiki_core.build_wiki_query(
        _workspace_dir(),
        wiki_core.WikiQueryRequest(
            query=query,
            limit=normalized_limit,
            source_paths=source_paths,
        ),
    )
    return _json_response(result.model_dump())


async def wiki_read(path: str) -> ToolResponse:
    """Read a public wiki page or raw source file from the local Wiki.

    Args:
        path: Wiki page id, `.wiki/pages/...` path, or `.wiki/raw/sources/...`
            path returned by `wiki_search`.
    """
    workspace_dir = _workspace_dir()
    try:
        resolved = _safe_read_path(workspace_dir, path)
    except Exception as exc:
        return _text_response(f"无法读取知识库文件：{exc}")
    content = wiki_core.read_text_safely(resolved, wiki_core.MAX_QUERY_CONTEXT_CHARS)
    return _text_response(content)


async def wiki_read_for_sources(
    path: str,
    source_paths: list[str],
) -> ToolResponse:
    """Runtime-only reader that rejects paths outside an exact source scope."""

    workspace_dir = _workspace_dir()
    normalized = str(path or "").strip().replace("\\", "/")
    allowed_sources = {
        str(value or "").strip().replace("\\", "/")
        for value in source_paths
        if str(value or "").strip()
    }
    allowed_paths = set(allowed_sources)
    data = wiki_core.collect_wiki_data(workspace_dir)
    for page in data.pages:
        page_sources = set(page.sources)
        if page_sources and page_sources.issubset(allowed_sources):
            allowed_paths.add(page.filename)
            allowed_paths.add(page.path)
            allowed_paths.add(
                f"{wiki_core.WIKI_INTERNAL_DIR}/{wiki_core.WIKI_PAGES_DIR}/{page.filename}"
            )
    if normalized not in allowed_paths:
        return _text_response("无法读取：这条内容不在当前对话选择的资料范围内。")
    return await wiki_read(normalized)


async def wiki_graph_neighbors(node: str, depth: int = 1) -> ToolResponse:
    """Read neighboring nodes from the local Wiki graph.

    Args:
        node: Page id or source path.
        depth: Graph traversal depth, 1 to 3.
    """
    workspace_dir = _workspace_dir()
    result = wiki_core.graph_neighbors(workspace_dir, node, depth)
    return _json_response(result)


async def wiki_ingest_status() -> ToolResponse:
    """Read local Wiki ingest queue counters and recent events."""
    workspace_dir = _workspace_dir()
    return _json_response(wiki_core.queue_summary(workspace_dir).model_dump())
