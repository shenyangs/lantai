# -*- coding: utf-8 -*-
"""Tests for runtime message conversion helpers."""

from __future__ import annotations

import json

import pytest
from lantai.app.channels.runtime_schema import MessageType
from lantai.exceptions import AgentRuntimeErrorException

from lantai.runtime.converters import (
    LegacyMsg as Msg,
    legacy_msg_to_chat_messages as msg_to_message,
)


def test_legacy_msg_to_message_enriches_memory_tool_presentation() -> None:
    msg = Msg(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "tool-memory-1",
                "name": "memory_search",
                "input": {"query": "最近记忆"},
            },
            {
                "type": "tool_result",
                "id": "tool-memory-1",
                "name": "memory_search",
                "output": [{"type": "text", "text": "命中 3 条"}],
            },
        ],
        "assistant",
    )

    messages = msg_to_message(msg)

    assert [item.type for item in messages] == [
        MessageType.PLUGIN_CALL,
        MessageType.PLUGIN_CALL_OUTPUT,
    ]
    tool_input = messages[0].content[0].data
    tool_output = messages[1].content[0].data
    assert tool_input["presentation"]["surface"] == "debug"
    assert tool_input["presentation"]["visibility"] == "hidden"
    assert tool_output["presentation"]["surface"] == "debug"
    assert tool_output["presentation"]["visibility"] == "hidden"


def test_legacy_msg_to_message_enriches_subagent_tool_presentation() -> None:
    msg = Msg(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "tool-agent-1",
                "name": "chat_with_agent",
                "input": {"to_agent": "Lantai_Water_Agent"},
            },
            {
                "type": "tool_result",
                "id": "tool-agent-1",
                "name": "chat_with_agent",
                "output": [{"type": "text", "text": "子 agent 已返回"}],
            },
        ],
        "assistant",
    )

    messages = msg_to_message(msg)

    tool_input = messages[0].content[0].data
    tool_output = messages[1].content[0].data
    assert tool_input["presentation"]["surface"] == "subagent"
    assert tool_input["presentation"]["visibility"] == "prominent"
    assert tool_output["presentation"]["surface"] == "subagent"
    assert tool_output["presentation"]["visibility"] == "prominent"


def test_private_subagent_skill_read_is_hidden_but_explicit_skill_stays_visible() -> None:
    private_msg = Msg(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "tool-skill-private",
                "name": "read_skill",
                "input": {"skill_name": "puer"},
            },
        ],
        "assistant",
        metadata={
            "agent_id": "Lantai_Puer_Agent",
            "presentation_context": "subagent_internal",
            "skill_exposure": "subagent_internal",
        },
    )
    explicit_msg = Msg(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "tool-skill-explicit",
                "name": "read_skill",
                "input": {"skill_name": "puer"},
            },
        ],
        "assistant",
        metadata={
            "agent_id": "Lantai_Puer_Agent",
            "skill_exposure": "explicit",
            "skill_invocation_origin": "user_explicit",
        },
    )

    private_data = msg_to_message(private_msg)[0].content[0].data
    explicit_data = msg_to_message(explicit_msg)[0].content[0].data

    assert private_data["presentation"]["visibility"] == "hidden"
    assert private_data["presentation"]["surface"] == "debug"
    assert explicit_data["presentation"]["visibility"] == "visible"
    assert explicit_data["presentation"]["surface"] == "tool"


def test_msg_to_message_preserves_plain_text_contract() -> None:
    msg = Msg("Friday", "你好，世界", "assistant")

    messages = msg_to_message(msg)

    assert len(messages) == 1
    assert messages[0].type == MessageType.MESSAGE
    assert messages[0].role == "assistant"
    assert messages[0].status == "created"
    assert messages[0].content[0].type == "text"
    assert messages[0].content[0].text == "你好，世界"
    assert messages[0].metadata["original_name"] == "Friday"


def test_msg_to_message_splits_reasoning_and_resolves_local_media() -> None:
    msg = Msg(
        "Friday",
        [
            {"type": "text", "text": "先看图"},
            {"type": "thinking", "thinking": "需要检查附件"},
            {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": "file:///tmp/%E6%BE%9C%E5%8F%B0/a%20b.png",
                },
            },
            {
                "type": "file",
                "filename": "report.md",
                "source": "/tmp/澜台/report.md",
            },
        ],
        "assistant",
    )

    messages = msg_to_message(msg)

    assert [item.type for item in messages] == [
        MessageType.MESSAGE,
        MessageType.REASONING,
        MessageType.MESSAGE,
    ]
    assert messages[0].content[0].text == "先看图"
    assert messages[1].content[0].text == "需要检查附件"
    media = messages[2].content
    assert media[0].type == "image"
    assert media[0].image_url == "/tmp/澜台/a b.png"
    assert media[1].type == "file"
    assert media[1].filename == "report.md"
    assert media[1].file_url == "/tmp/澜台/report.md"


def test_msg_to_message_preserves_tool_call_and_result_payloads() -> None:
    msg = Msg(
        "Friday",
        [
            {
                "type": "tool_use",
                "id": "call-wiki-1",
                "name": "wiki_search",
                "input": {"query": "澜台", "limit": 3},
            },
            {
                "type": "tool_result",
                "id": "call-wiki-1",
                "name": "wiki_search",
                "output": [{"type": "text", "text": "命中 1 条"}],
            },
        ],
        "assistant",
    )

    messages = msg_to_message(msg)

    assert [item.type for item in messages] == [
        MessageType.PLUGIN_CALL,
        MessageType.PLUGIN_CALL_OUTPUT,
    ]
    call = messages[0].content[0].data
    result = messages[1].content[0].data
    assert call["call_id"] == "call-wiki-1"
    assert call["name"] == "wiki_search"
    assert json.loads(call["arguments"]) == {"query": "澜台", "limit": 3}
    assert call["presentation"]["surface"] == "service"
    assert call["presentation"]["visibility"] == "quiet"
    assert result["call_id"] == "call-wiki-1"
    assert result["name"] == "wiki_search"
    assert json.loads(result["output"]) == [
        {"type": "text", "text": "命中 1 条"},
    ]
    assert result["presentation"]["surface"] == "service"
    assert result["presentation"]["visibility"] == "quiet"


def test_msg_to_message_emits_media_from_tool_result_output() -> None:
    msg = Msg(
        "Friday",
        [
            {
                "type": "tool_result",
                "id": "call-file-1",
                "name": "send_file_to_user",
                "output": [
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": "file:///tmp/%E6%BE%9C%E5%8F%B0.png",
                        },
                    },
                    {"type": "text", "text": "File sent successfully."},
                ],
            },
        ],
        "assistant",
    )

    messages = msg_to_message(msg)

    assert [item.type for item in messages] == [
        MessageType.MESSAGE,
        MessageType.PLUGIN_CALL_OUTPUT,
    ]
    assert messages[0].content[0].type == "image"
    assert messages[0].content[0].image_url == "/tmp/澜台.png"
    output = messages[1].content[0].data
    assert output["call_id"] == "call-file-1"
    assert output["presentation"]["surface"] == "tool"
    assert json.loads(output["output"])[1]["text"] == "File sent successfully."


def test_msg_to_message_rejects_invalid_input_type() -> None:
    with pytest.raises(AgentRuntimeErrorException) as exc_info:
        msg_to_message(object())  # type: ignore[arg-type]

    assert exc_info.value.code == "INVALID_MESSAGE_TYPE"
