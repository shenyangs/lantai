from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lantai.app import docx_double_write
from lantai.app.docx_double_write import (
    DocxRewriteRequest,
    DocxRewriteResponse,
    rewrite_docx_selection,
)
from lantai.app.routers import docx_double_write as docx_double_write_router


@pytest.mark.asyncio
async def test_source_chat_context_keeps_only_visible_user_and_assistant_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = SimpleNamespace(
        session_id="console:source",
        user_id="user",
        channel="console",
    )
    workspace = SimpleNamespace(
        chat_manager=SimpleNamespace(get_chat=AsyncMock(return_value=chat)),
        runner=SimpleNamespace(
            session=SimpleNamespace(
                get_session_state_dict=AsyncMock(
                    return_value={"agent": {"memory": {"messages": []}}},
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        docx_double_write_router,
        "get_agent_for_request",
        AsyncMock(return_value=workspace),
    )
    converted = [
        SimpleNamespace(
            role="user",
            type="message",
            content=[SimpleNamespace(type="text", text="用户目标")],
        ),
        SimpleNamespace(
            role="assistant",
            type="message",
            content=[SimpleNamespace(type="text", text="已有结论")],
        ),
        SimpleNamespace(
            role="assistant",
            type="reasoning",
            content=[SimpleNamespace(type="text", text="不应发送的思考")],
        ),
        SimpleNamespace(
            role="tool",
            type="message",
            content=[SimpleNamespace(type="text", text="不应发送的工具输出")],
        ),
    ]
    monkeypatch.setattr(
        "lantai.runtime.converters.legacy_memory_state_to_chat_messages",
        AsyncMock(return_value=converted),
    )

    context = await docx_double_write_router.load_source_chat_context(
        SimpleNamespace(),
        agent_id="writer",
        chat_id="chat-source-1",
    )

    assert context == [
        {"role": "user", "content": "用户目标"},
        {"role": "assistant", "content": "已有结论"},
    ]


@pytest.mark.asyncio
async def test_rewrite_uses_full_document_and_source_chat_context_without_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = AsyncMock()
    consume = AsyncMock(
        return_value='preface {"replacement_text":"更清楚的表达","summary":"精简句子"}',
    )
    monkeypatch.setattr(
        docx_double_write,
        "resolve_active_model",
        lambda agent_id: SimpleNamespace(
            model=model,
            label=f"active/{agent_id}",
        ),
    )
    monkeypatch.setattr(docx_double_write, "consume_model_response", consume)

    result = await rewrite_docx_selection(
        DocxRewriteRequest(
            instruction="写得更直接",
            selected_text="原文",
            document_before="前文" * 5000,
            document_after="后文" * 5000,
            current_draft="已经改过一版的候选稿",
            document_name="report.docx",
            source_chat_id="chat-source-1",
        ),
        agent_id="writer",
        source_chat_context=[
            {"role": "user", "content": "把这份纪要整理清楚"},
            {"role": "assistant", "content": "我会保留关键事实"},
        ],
    )

    assert result.replacement_text == "更清楚的表达"
    assert result.summary == "精简句子"
    assert result.model == "active/writer"
    messages = consume.await_args.args[1]
    assert messages[0]["role"] == "system"
    assert "tools" in messages[0]["content"].lower()
    assert "原文" in messages[1]["content"]
    assert "已经改过一版的候选稿" in messages[1]["content"]
    assert "把这份纪要整理清楚" in messages[1]["content"]
    assert len(messages[1]["content"]) > 20_000
    assert consume.await_args.kwargs["total_timeout_seconds"] == 120


@pytest.mark.asyncio
async def test_router_uses_the_selected_agent_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rewrite = AsyncMock(
        return_value=DocxRewriteResponse(
            replacement_text="new",
            summary="done",
            model="provider/model",
        ),
    )
    monkeypatch.setattr(docx_double_write_router, "rewrite_docx_selection", rewrite)
    load_context = AsyncMock(return_value=[{"role": "user", "content": "source"}])
    monkeypatch.setattr(docx_double_write_router, "load_source_chat_context", load_context)
    request = SimpleNamespace(
        state=SimpleNamespace(agent_id=None),
        headers={"X-Agent-Id": "writer%20agent"},
    )
    body = DocxRewriteRequest(
        instruction="clearer",
        selected_text="old",
        source_chat_id="chat-source-1",
    )

    response = await docx_double_write_router.post_docx_rewrite(request, body)

    assert response.success is True
    assert rewrite.await_args.kwargs["agent_id"] == "writer agent"
    assert rewrite.await_args.kwargs["source_chat_context"] == [
        {"role": "user", "content": "source"},
    ]
    assert load_context.await_args.kwargs["chat_id"] == "chat-source-1"
