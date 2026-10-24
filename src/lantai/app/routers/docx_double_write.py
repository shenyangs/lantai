# -*- coding: utf-8 -*-
"""Active-Agent rewrite endpoint for the local DOCX editor."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from ..agent_context import get_current_agent_id
from ..agent_context import get_agent_for_request
from ..docx_double_write import (
    DocxRewriteRequest,
    DocxRewriteResponse,
    DocxRewriteUnavailableError,
    rewrite_docx_selection,
)
from ..header_utils import decode_header_value


router = APIRouter(prefix="/console/docx", tags=["console-docx"])


async def load_source_chat_context(
    request: Request,
    *,
    agent_id: str,
    chat_id: str,
) -> list[dict[str, str]]:
    """Load only visible user/assistant text from the document's source chat."""
    if not chat_id:
        return []
    try:
        workspace = await get_agent_for_request(request, agent_id)
        chat = await workspace.chat_manager.get_chat(chat_id)
        session = getattr(workspace.runner, "session", None)
        if chat is None or session is None:
            return []
        state = await session.get_session_state_dict(
            chat.session_id,
            chat.user_id,
            chat.channel,
        )
        from ...runtime.converters import legacy_memory_state_to_chat_messages

        rows = await legacy_memory_state_to_chat_messages(
            state.get("agent", {}).get("memory", {}) if state else {},
        )
        context: list[dict[str, str]] = []
        for row in rows:
            role_value = getattr(row, "role", "") or ""
            role = str(getattr(role_value, "value", role_value))
            if role not in {"user", "assistant"} or getattr(row, "type", "") != "message":
                continue
            text = "\n".join(
                str(getattr(item, "text", "") or "")
                for item in (getattr(row, "content", None) or [])
                if str(getattr(getattr(item, "type", ""), "value", getattr(item, "type", ""))) == "text"
                and getattr(item, "text", None)
            ).strip()
            if text:
                context.append({"role": role, "content": text})
        return context
    except Exception:
        # A missing/deleted source chat must not prevent local document editing.
        return []


@router.post("/rewrite", response_model=DocxRewriteResponse)
async def post_docx_rewrite(
    request: Request,
    body: DocxRewriteRequest = Body(...),
) -> DocxRewriteResponse:
    agent_id = (
        getattr(request.state, "agent_id", None)
        or decode_header_value(request.headers.get("X-Agent-Id"))
        or get_current_agent_id()
        or "default"
    )
    try:
        source_chat_context = await load_source_chat_context(
            request,
            agent_id=agent_id,
            chat_id=body.source_chat_id,
        )
        return await rewrite_docx_selection(
            body,
            agent_id=agent_id,
            source_chat_context=source_chat_context,
        )
    except DocxRewriteUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


__all__ = ["load_source_chat_context", "router"]
