# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from lantai.providers.openai_chat_model_compat import (
    KIMI_CODING_BASE_URL,
    OpenAIChatModelCompat,
    _sanitize_tool_call,
)


@pytest.mark.parametrize("tool_choice", ["required", "any"])
async def test_fixed_kimi_legacy_wire_keeps_thinking_for_required_tools(
    tool_choice,
) -> None:
    calls = []

    async def create(**payload):
        calls.append(payload)
        return SimpleNamespace(choices=[], id="test-response")

    model = OpenAIChatModelCompat(
        "kimi-k2.7-code", api_key="sk-test", stream=False
    )
    model.client = SimpleNamespace(
        base_url="https://gateway.example/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    await model._send_request(
        [{"role": "user", "content": "use collaboration"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "collaborate_with_agents",
                    "parameters": {"type": "object"},
                },
            }
        ],
        tool_choice=tool_choice,
    )
    assert len(calls) == 1
    assert calls[0]["tool_choice"] == "auto"
    assert (
        calls[0].get("extra_body", {}).get("thinking", {}).get("type")
        != "disabled"
    )


class CompatHarnessOpenAIChatModel(OpenAIChatModelCompat):
    async def parse_stream_for_test(
        self,
        start_datetime: datetime,
        stream: Any,
    ) -> list[Any]:
        responses = []
        async for response in self._parse_openai_stream_response(
            start_datetime,
            stream,
        ):
            responses.append(response)
        return responses


class FakeAsyncStream:
    def __init__(self, items: list[Any]):
        self._items = items
        self._iter = None

    async def __aenter__(self) -> "FakeAsyncStream":
        self._iter = iter(self._items)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def __aiter__(self) -> "FakeAsyncStream":
        return self

    async def __anext__(self) -> Any:
        assert self._iter is not None
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _make_chunk(tool_calls: list[Any]) -> Any:
    delta = SimpleNamespace(
        reasoning_content=None,
        content=None,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(delta=delta)
    return SimpleNamespace(usage=None, choices=[choice])


async def test_stream_parser_preserves_usage_only_terminal_chunk() -> None:
    model = CompatHarnessOpenAIChatModel("dummy", api_key="sk-test")
    usage = SimpleNamespace(prompt_tokens=17, completion_tokens=5, total_tokens=22)
    responses = await model.parse_stream_for_test(
        datetime.now(),
        FakeAsyncStream([SimpleNamespace(id="usage-final", choices=[], usage=usage)]),
    )
    assert len(responses) == 1
    assert responses[0].content == []
    assert responses[0].usage is usage


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter"])
async def test_stream_parser_preserves_finish_only_terminal_chunk(reason) -> None:
    """An empty terminal chunk is evidence, not an ignorable content chunk."""
    model = CompatHarnessOpenAIChatModel("dummy", api_key="sk-test")
    responses = await model.parse_stream_for_test(
        datetime.now(),
        FakeAsyncStream([
            SimpleNamespace(
                choices=[SimpleNamespace(delta=None, finish_reason=reason)],
                usage=None,
            ),
        ]),
    )
    assert len(responses) == 1
    assert responses[0].content == []
    assert responses[0].finish_reason == reason


def test_nonstream_parser_preserves_finish_reason() -> None:
    model = CompatHarnessOpenAIChatModel("dummy", api_key="sk-test", stream=False)
    response = model._parse_openai_response(SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="length",
        )],
    ))
    assert response.finish_reason == "length"


async def test_stream_parser_preserves_usage_with_text_and_reasoning() -> None:
    model = CompatHarnessOpenAIChatModel("dummy", api_key="sk-test")
    usage = SimpleNamespace(prompt_tokens=17, completion_tokens=5, total_tokens=22)
    delta = SimpleNamespace(content="answer", reasoning_content="thinking", tool_calls=None)
    responses = await model.parse_stream_for_test(
        datetime.now(),
        FakeAsyncStream([SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)]),
    )
    assert responses[-1].usage is usage
    assert responses[-1].content == [
        {"type": "thinking", "thinking": "thinking"},
        {"type": "text", "text": "answer"},
    ]


def test_nonstream_parser_preserves_provider_usage() -> None:
    model = CompatHarnessOpenAIChatModel("dummy", api_key="sk-test", stream=False)
    usage = SimpleNamespace(prompt_tokens=17, completion_tokens=5, total_tokens=22)
    response = model._parse_openai_response(SimpleNamespace(
        id="response", usage=usage,
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
    ))
    assert response.usage is usage
    assert response.content == [{"type": "text", "text": "answer"}]


async def test_stream_parser_skips_tool_call_without_function() -> None:
    model = CompatHarnessOpenAIChatModel(
        "dummy",
        api_key="sk-test",
        stream=True,
    )

    malformed_tool_call = SimpleNamespace(
        index=0,
        id="call_bad",
        function=None,
    )
    none_arguments_tool_call = SimpleNamespace(
        index=1,
        id="call_partial",
        function=SimpleNamespace(name="ping", arguments=None),
    )
    valid_tool_call = SimpleNamespace(
        index=0,
        id="call_ok",
        function=SimpleNamespace(name="ping", arguments='{"x":1}'),
    )

    stream = FakeAsyncStream(
        [
            _make_chunk([malformed_tool_call]),
            _make_chunk([none_arguments_tool_call]),
            _make_chunk([valid_tool_call]),
        ],
    )

    responses = await model.parse_stream_for_test(
        datetime.now(),
        stream,
    )

    assert responses
    tool_blocks = [
        block
        for response in responses
        for block in response.content
        if block.get("type") == "tool_use"
    ]
    assert tool_blocks
    assert tool_blocks[-1]["name"] == "ping"
    assert tool_blocks[-1]["input"] == {"x": 1}


async def test_stream_parser_accepts_camel_case_reasoning_content() -> None:
    model = CompatHarnessOpenAIChatModel(
        "dummy",
        api_key="sk-test",
        stream=True,
    )
    delta = SimpleNamespace(
        reasoningContent="先算 13 * 17",
        content=None,
        tool_calls=None,
    )
    stream = FakeAsyncStream(
        [
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=delta)],
            ),
        ],
    )

    responses = await model.parse_stream_for_test(datetime.now(), stream)

    assert responses
    assert responses[-1].content == [
        {"type": "thinking", "thinking": "先算 13 * 17"},
    ]


async def test_stream_parser_accepts_structured_qwen_thinking_blocks() -> None:
    model = CompatHarnessOpenAIChatModel(
        "dummy",
        api_key="sk-test",
        stream=True,
    )
    delta = SimpleNamespace(
        content=[
            {"type": "thinking", "thinking": "先列出乘法步骤"},
            {"type": "text", "text": "13 * 17 = 221"},
        ],
        tool_calls=None,
    )
    stream = FakeAsyncStream(
        [
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=delta)],
            ),
        ],
    )

    responses = await model.parse_stream_for_test(datetime.now(), stream)

    assert responses
    assert responses[-1].content == [
        {"type": "thinking", "thinking": "先列出乘法步骤"},
        {"type": "text", "text": "13 * 17 = 221"},
    ]


def test_sanitize_tool_call_normalizes_non_string_arguments() -> None:
    none_arguments_tool_call = SimpleNamespace(
        index=0,
        id="call_partial",
        function=SimpleNamespace(name="ping", arguments=None),
    )
    non_string_arguments_tool_call = SimpleNamespace(
        index=1,
        id="call_dict",
        function=SimpleNamespace(name="ping", arguments={"x": 2}),
    )
    missing_arguments_tool_call = SimpleNamespace(
        index=2,
        id="call_missing_args",
        function=SimpleNamespace(name="ping"),
    )
    missing_name_tool_call = SimpleNamespace(
        index=3,
        id="call_missing_name",
        function=SimpleNamespace(arguments={"x": 3}),
    )
    missing_name_and_arguments_tool_call = SimpleNamespace(
        index=4,
        id="call_missing_both",
        function=SimpleNamespace(),
    )

    sanitized_none_arguments = _sanitize_tool_call(none_arguments_tool_call)
    assert sanitized_none_arguments is not None
    assert sanitized_none_arguments.function.name == "ping"
    assert sanitized_none_arguments.function.arguments == ""

    sanitized_non_string_arguments = _sanitize_tool_call(
        non_string_arguments_tool_call,
    )
    assert sanitized_non_string_arguments is not None
    assert sanitized_non_string_arguments.function.name == "ping"
    assert isinstance(sanitized_non_string_arguments.function.arguments, str)
    assert json.loads(sanitized_non_string_arguments.function.arguments) == {
        "x": 2,
    }

    sanitized_missing_arguments = _sanitize_tool_call(
        missing_arguments_tool_call,
    )
    assert sanitized_missing_arguments is not None
    assert sanitized_missing_arguments.function.name == "ping"
    assert sanitized_missing_arguments.function.arguments == ""

    sanitized_missing_name = _sanitize_tool_call(missing_name_tool_call)
    assert sanitized_missing_name is not None
    assert sanitized_missing_name.function.name == ""
    assert isinstance(sanitized_missing_name.function.arguments, str)
    assert json.loads(sanitized_missing_name.function.arguments) == {"x": 3}

    sanitized_missing_name_and_arguments = _sanitize_tool_call(
        missing_name_and_arguments_tool_call,
    )
    assert sanitized_missing_name_and_arguments is not None
    assert sanitized_missing_name_and_arguments.function.name == ""
    assert sanitized_missing_name_and_arguments.function.arguments == ""


async def test_normalize_content_to_text_collapses_local_content_blocks(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_call(
        self,
        messages,
        tools=None,
        tool_choice=None,
        structured_model=None,
        **kwargs,
    ):
        _ = self, tools, tool_choice, structured_model, kwargs
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr(OpenAIChatModelCompat, "_send_request", fake_call)

    model = OpenAIChatModelCompat(
        "dummy",
        api_key="sk-test",
        stream=True,
        normalize_content_to_text=True,
    )

    result = await model(
        [
            {
                "role": "user",
                "name": None,
                "reasoning_content": "provider-specific",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                    {"type": "text", "text": "world"},
                ],
            },
        ],
    )

    assert result == "ok"
    assert captured["messages"] == [
        {"role": "user", "content": "hello\nworld"},
    ]


async def test_kimi_coding_preserves_tool_results_without_tool_call_echo(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_call(
        self,
        messages,
        tools=None,
        tool_choice=None,
        structured_model=None,
        **kwargs,
    ):
        _ = self, tools, tool_choice, structured_model, kwargs
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr(OpenAIChatModelCompat, "_send_request", fake_call)

    model = OpenAIChatModelCompat("dummy", api_key="sk-test", stream=True)
    model.client = SimpleNamespace(base_url=KIMI_CODING_BASE_URL)

    result = await model(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "我会检查一下。"},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "execute_shell_command",
                        "input": {},
                        "raw_input": "{}",
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tool_1",
                "content": "ok",
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "继续"}],
            },
        ],
    )

    assert result == "ok"
    assert captured["messages"] == [
        {"role": "assistant", "content": "我会检查一下。"},
        {"role": "user", "content": "Previous tool execution result:\nok"},
        {"role": "user", "content": "继续"},
    ]


async def test_kimi_coding_preserves_multimodal_content(monkeypatch) -> None:
    """Kimi coding cleanup must not flatten image/video content arrays."""
    captured: dict[str, Any] = {}

    async def fake_call(
        self,
        messages,
        tools=None,
        tool_choice=None,
        structured_model=None,
        **kwargs,
    ):
        _ = self, tools, tool_choice, structured_model, kwargs
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr(OpenAIChatModelCompat, "_send_request", fake_call)

    model = OpenAIChatModelCompat("dummy", api_key="sk-test", stream=True)
    model.client = SimpleNamespace(base_url=KIMI_CODING_BASE_URL)

    image_block = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }
    video_block = {
        "type": "video_url",
        "video_url": {"url": "data:video/mp4;base64,BBBB"},
    }

    result = await model(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看一下这张图"},
                    image_block,
                ],
                "reasoning_content": "drop-me",
            },
            {
                "role": "tool",
                "tool_call_id": "tool_1",
                "content": [
                    {"type": "text", "text": "tool screenshot"},
                    video_block,
                ],
            },
        ],
    )

    assert result == "ok"
    assert captured["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看一下这张图"},
                image_block,
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Previous tool execution result:",
                },
                {"type": "text", "text": "tool screenshot"},
                video_block,
            ],
        },
    ]


async def test_kimi_coding_drops_legacy_tool_call_echoes(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_call(
        self,
        messages,
        tools=None,
        tool_choice=None,
        structured_model=None,
        **kwargs,
    ):
        _ = self, tools, tool_choice, structured_model, kwargs
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr(OpenAIChatModelCompat, "_send_request", fake_call)

    model = OpenAIChatModelCompat("dummy", api_key="sk-test", stream=True)
    model.client = SimpleNamespace(base_url=KIMI_CODING_BASE_URL)

    await model(
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": (
                            "Tool call requested: execute_shell_command "
                            'Input: {"command":"guanlan --help"}'
                        ),
                    },
                ],
            },
            {"role": "user", "content": "继续"},
        ],
    )

    assert captured["messages"] == [{"role": "user", "content": "继续"}]


async def test_kimi_coding_preserves_reasoning_and_native_tool_pair() -> None:
    captured: dict[str, Any] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return FakeAsyncStream([])

    model = OpenAIChatModelCompat("dummy", api_key="sk-test", stream=True)
    model.client = SimpleNamespace(
        base_url=KIMI_CODING_BASE_URL,
        chat=SimpleNamespace(completions=FakeCompletions()),
    )

    response = await model(
        [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "确认执行"}],
                "reasoning_content": "hidden",
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tool_1",
                        "type": "function",
                        "function": {
                            "name": "execute_shell_command",
                            "arguments": "{}",
                        },
                    },
                ],
                "reasoning_content": "hidden",
            },
            {
                "role": "tool",
                "tool_call_id": "tool_1",
                "content": "installed",
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "继续"}],
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "execute_shell_command",
                    "description": "Run a command",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
    )

    assert hasattr(response, "__aiter__")
    assert len(captured["messages"]) == 4
    assert captured["messages"][0] == {
        "role": "assistant",
        "content": "确认执行",
        "reasoning_content": "hidden",
    }
    assert captured["messages"][1] == {
        "role": "assistant",
        "content": None,
        "reasoning_content": "hidden",
        "tool_calls": [
            {
                "id": "tool_1",
                "type": "function",
                "function": {
                    "name": "execute_shell_command",
                    "arguments": "{}",
                },
            },
        ],
    }
    assert captured["messages"][2] == {
        "role": "tool",
        "tool_call_id": "tool_1",
        "content": "installed",
    }
    assert captured["messages"][3] == {"role": "user", "content": "继续"}
    assert all(
        "Tool call requested" not in message.get("content", "")
        for message in captured["messages"]
        if isinstance(message.get("content"), str)
    )
    assert captured["tools"][0]["function"]["name"] == "execute_shell_command"
