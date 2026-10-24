# -*- coding: utf-8 -*-
"""Contract tests for the local Office editor Agent bridge."""
from __future__ import annotations

import json

from lantai.app.routers import console as console_router
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelResponse,
    ModelStreamEvent,
)
from lantai.runtime.tools import ToolCall
from lantai.runtime.tool_response import TextBlock, ToolResponse
from starlette.requests import Request


def test_office_agent_builds_anthropic_compatible_image_blocks() -> None:
    messages = console_router._office_agent_messages(  # pylint: disable=protected-access
        console_router.OfficeAgentRequest(
            system="Inspect the current slide.",
            messages=[
                {
                    "role": "user",
                    "text": "美化这一页",
                    "images": [
                        {
                            "mime": "image/png",
                            "base64": "c2xpZGUtcmVuZGVy",
                        }
                    ],
                }
            ],
        )
    )

    image = messages[1].content[1]
    assert image.type == "image"
    assert image.data == {
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "c2xpZGUtcmVuZGVy",
        }
    }


def test_office_image_search_parser_keeps_only_public_image_urls() -> None:
    parser = console_router._BingImageResultParser(2)  # pylint: disable=protected-access
    parser.feed(
        '<a class="iusc" m=\'{"murl":"https://img.example/a.png",'
        '"turl":"https://img.example/t.png","t":"示例","ow":800,"oh":600}\'></a>'
        '<a class="iusc" m=\'{"murl":"file:///tmp/private.png"}\'></a>'
    )
    assert parser.results == [
        {
            "imageUrl": "https://img.example/a.png",
            "thumbnailUrl": "https://img.example/t.png",
            "title": "示例",
            "width": 800,
            "height": 600,
        }
    ]


async def test_office_web_search_uses_lantai_baseline(monkeypatch) -> None:
    async def fake_search(query: str, limit: int = 10) -> ToolResponse:
        assert (query, limit) == ("澜台", 3)
        return ToolResponse(
            content=[
                TextBlock(
                    text='{"results":[{"title":"澜台","url":"https://example.com",'
                    '"snippet":"结果"}]}'
                )
            ]
        )

    monkeypatch.setattr(console_router, "lantai_web_search", fake_search)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("127.0.0.1", 8765),
            "path": "/api/console/office-tool",
            "headers": [],
        }
    )
    result = await console_router.run_office_tool(
        console_router.OfficeToolRequest(
            action="web_search",
            query="澜台",
            max_results=3,
        ),
        request,
    )
    assert result == {
        "results": [
            {"title": "澜台", "url": "https://example.com", "snippet": "结果"}
        ],
        "method": "lantai-web",
    }


async def test_office_agent_routes_genoffice_dsl_through_active_lantai_model(
    monkeypatch,
) -> None:
    captured = {}

    class FakeClient:
        async def complete(self, request):
            captured["request"] = request
            return ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content=[
                        ModelContentBlock(type="text", text="先读取选区"),
                        ModelContentBlock(
                            type="tool_call",
                            data={
                                "id": "call-2",
                                "name": "read_range",
                                "arguments": {"range": "A1:C8"},
                            },
                        ),
                    ],
                )
            )

    def create_client(agent_id):
        captured["agent_id"] = agent_id
        return FakeClient()

    monkeypatch.setattr(console_router, "create_model_client", create_client)

    result = await console_router.run_office_agent(
        console_router.OfficeAgentRequest(
            agent_id="default",
            system="You edit the currently open workbook.",
            messages=[
                {"role": "user", "text": "分析并修改这张表"},
                {
                    "role": "assistant",
                    "text": "",
                    "toolCalls": [
                        {"id": "call-1", "name": "get_workbook_info", "input": {}}
                    ],
                },
                {
                    "role": "tool",
                    "results": [
                        {
                            "id": "call-1",
                            "name": "get_workbook_info",
                            "output": '{"sheets":["Sheet1"]}',
                        }
                    ],
                },
            ],
            tools=[
                {
                    "name": "read_range",
                    "description": "Read workbook cells",
                    "inputSchema": {"type": "object"},
                }
            ],
        )
    )

    assert captured["agent_id"] == "default"
    request = captured["request"]
    assert request.metadata == {"surface": "local_office_editor"}
    assert request.tools[0].name == "read_range"
    assert [message.role for message in request.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert request.messages[-1].tool_call_id == "call-1"
    assert result == {
        "text": "先读取选区",
        "toolCalls": [
            {
                "id": "call-2",
                "name": "read_range",
                "input": {"range": "A1:C8"},
            }
        ],
    }


async def test_office_agent_honors_a_window_model_binding_without_mutating_defaults(
    monkeypatch,
) -> None:
    """An editor window may pin a configured model without changing its agent."""

    captured = {}

    class FakeClient:
        async def complete(self, request):
            captured["request"] = request
            return ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content=[ModelContentBlock(type="text", text="已按当前窗口模型处理")],
                )
            )

    def create_client(agent_id, *, model_slot=None):
        captured["agent_id"] = agent_id
        captured["model_slot"] = model_slot
        return FakeClient()

    class FakeManager:
        def get_provider(self, provider_id):
            return self if provider_id == "minimax-cn" else None

        def has_model(self, model_id):
            return model_id == "MiniMax-M3"

        def is_model_slot_available(self, slot):
            return slot.provider_id == "minimax-cn" and slot.model == "MiniMax-M3"

    monkeypatch.setattr(console_router, "create_model_client", create_client)
    monkeypatch.setattr(
        console_router.ProviderManager,
        "get_instance",
        staticmethod(lambda: FakeManager()),
    )

    result = await console_router.run_office_agent(
        console_router.OfficeAgentRequest(
            agent_id="writing-agent",
            model_binding={
                "provider_id": "minimax-cn",
                "model": "MiniMax-M3",
                "source": "chat_snapshot",
            },
            system="Edit the current document.",
            messages=[{"role": "user", "text": "润色这段"}],
        )
    )

    assert captured["agent_id"] == "writing-agent"
    assert captured["model_slot"].provider_id == "minimax-cn"
    assert captured["model_slot"].model == "MiniMax-M3"
    assert captured["request"].metadata == {
        "surface": "local_office_editor",
        "model_binding": {
            "provider_id": "minimax-cn",
            "model": "MiniMax-M3",
            "source": "chat_snapshot",
        },
    }
    assert result == {"text": "已按当前窗口模型处理", "toolCalls": []}


async def test_office_agent_first_turn_leaves_tool_use_to_the_model(
    monkeypatch,
) -> None:
    requests = []

    class FakeClient:
        async def complete(self, request):
            requests.append(request)
            return ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content=[
                        ModelContentBlock(
                            type="tool_call",
                            data={
                                "id": "call-read",
                                "name": "read_workbook",
                                "arguments": {},
                            },
                        )
                    ],
                )
            )

    monkeypatch.setattr(
        console_router,
        "create_model_client",
        lambda _agent_id: FakeClient(),
    )

    result = await console_router.run_office_agent(
        console_router.OfficeAgentRequest(
            system="Analyze the workbook.",
            messages=[{"role": "user", "text": "谁的工作量最大？"}],
            tools=[
                {
                    "name": "read_workbook",
                    "description": "Read workbook data",
                    "inputSchema": {"type": "object"},
                }
            ],
        )
    )

    assert requests[0].tool_choice == "auto"
    assert "由你根据用户要求和当前上下文自行判断" in requests[0].messages[0].text_content()
    assert result["toolCalls"][0]["name"] == "read_workbook"


async def test_office_agent_retries_an_empty_model_step_without_forcing_tools(
    monkeypatch,
) -> None:
    requests = []

    class FakeClient:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                return ModelResponse(
                    message=ModelMessage(
                        role="assistant",
                        content=[ModelContentBlock(type="thinking", text="先分析")],
                    )
                )
            return ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content=[
                        ModelContentBlock(
                            type="tool_call",
                            data={
                                "id": "call-recover",
                                "name": "read_range",
                                "arguments": {"range": "A1:C8"},
                            },
                        )
                    ],
                )
            )

    monkeypatch.setattr(
        console_router,
        "create_model_client",
        lambda _agent_id: FakeClient(),
    )

    result = await console_router.run_office_agent(
        console_router.OfficeAgentRequest(
            system="Continue analyzing.",
            messages=[
                {"role": "user", "text": "分析一下"},
                {"role": "tool", "results": []},
            ],
            tools=[
                {
                    "name": "read_range",
                    "description": "Read a range",
                    "inputSchema": {"type": "object"},
                }
            ],
        )
    )

    assert [request.tool_choice for request in requests] == ["auto", "auto"]
    assert result["toolCalls"][0]["id"] == "call-recover"


async def test_office_agent_recovers_execute_mode_progress_without_an_operation(
    monkeypatch,
) -> None:
    requests = []

    class FakeClient:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                return ModelResponse(
                    message=ModelMessage.text(
                        "assistant",
                        "好的，继续执行。我会按照既定计划先简化第 2 段。",
                    )
                )
            return ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content=[
                        ModelContentBlock(
                            type="tool_call",
                            data={
                                "id": "call-edit",
                                "name": "replace_text",
                                "arguments": {"block": 2, "text": "简化后的段落"},
                            },
                        )
                    ],
                )
            )

    monkeypatch.setattr(
        console_router,
        "create_model_client",
        lambda _agent_id: FakeClient(),
    )

    result = await console_router.run_office_agent(
        console_router.OfficeAgentRequest(
            system="Edit the document.",
            messages=[
                {
                    "role": "user",
                    "text": (
                        "[LANTAI_OFFICE_CO_CREATION protocol=2.0 "
                        "suite=docs mode=execute]\n继续"
                    ),
                },
                {
                    "role": "tool",
                    "results": [
                        {
                            "id": "call-read",
                            "name": "read_document",
                            "output": "已读取文档",
                        }
                    ],
                },
            ],
            tools=[
                {
                    "name": "replace_text",
                    "description": "Replace one document block",
                    "inputSchema": {"type": "object"},
                }
            ],
        )
    )

    assert [request.tool_choice for request in requests] == ["auto", "required"]
    assert result["text"] == ""
    assert result["toolCalls"][0]["id"] == "call-edit"


async def test_office_agent_stream_is_provider_neutral_and_has_one_terminal_event(
    monkeypatch,
) -> None:
    captured = {}

    class FakeClient:
        async def stream(self, request):
            captured["request"] = request
            yield ModelStreamEvent(kind="provider_activity")
            yield ModelStreamEvent(kind="thinking_delta", delta="先理解结构")
            yield ModelStreamEvent(kind="text_delta", delta="正在修改")
            call = ToolCall(
                id="call-edit",
                name="replace_text",
                arguments={"block": 2},
            )
            yield ModelStreamEvent(kind="tool_call_completed", tool_call=call)
            yield ModelStreamEvent(kind="tool_call_completed", tool_call=call)
            yield ModelStreamEvent(kind="message_completed", finish_reason="stop")
            yield ModelStreamEvent(kind="message_completed", finish_reason="stop")

    monkeypatch.setattr(
        console_router,
        "create_model_client",
        lambda _agent_id: FakeClient(),
    )

    response = await console_router.stream_office_agent(
        console_router.OfficeAgentRequest(
            source_chat_id="chat-42",
            system="Edit the document.",
            messages=[{"role": "user", "text": "精简第二段"}],
            tools=[
                {
                    "name": "replace_text",
                    "description": "Replace one document block",
                    "inputSchema": {"type": "object"},
                }
            ],
        )
    )
    raw = ""
    async for chunk in response.body_iterator:
        raw += chunk.decode() if isinstance(chunk, bytes) else chunk
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]

    assert [event["type"] for event in events] == [
        "ping",
        "thinking-delta",
        "delta",
        "tool-call",
        "done",
    ]
    assert events[1]["text"] == "先理解结构"
    assert events[3]["toolCall"] == {
        "id": "call-edit",
        "name": "replace_text",
        "input": {"block": 2},
    }
    assert captured["request"].metadata == {
        "surface": "local_office_editor",
        "source_chat_id": "chat-42",
    }
