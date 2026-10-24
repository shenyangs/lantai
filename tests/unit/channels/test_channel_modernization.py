# -*- coding: utf-8 -*-
"""Behavior locks for the modern channel runtime contract."""

from __future__ import annotations

from typing import Any

import pytest

from lantai.app.channels.base import BaseChannel
from lantai.app.channels.renderer import ChannelDisplayConfig
from lantai.app.channels.runtime_schema import (
    AgentRequest,
    Message,
    MessageType,
    Role,
    RunStatus,
    TextContent,
)
from lantai.app.channels.schema import ChannelCapabilities
from lantai.config.config import ChannelConfig


class StreamingProbeChannel(BaseChannel):
    channel = "probe"
    capabilities = ChannelCapabilities(
        inbound_media=frozenset({"text", "image"}),
        outbound_media=frozenset({"text"}),
        streaming=True,
        message_edit=True,
    )

    def __init__(self, process, *, streaming_enabled: bool = True):
        super().__init__(
            process,
            display_config=ChannelDisplayConfig(),
            streaming_enabled=streaming_enabled,
        )
        self.stream_events: list[tuple[str, str]] = []
        self.sent: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(
        self,
        to_handle: str,
        text: str,
        meta: dict | None = None,
    ) -> None:
        del to_handle, meta
        self.sent.append(text)

    async def on_streaming_start(
        self,
        request: AgentRequest,
        to_handle: str,
        event: Any,
        send_meta: dict,
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        del request, to_handle, event, send_meta
        self.stream_events.append(("start", stream_type))
        assert accumulated_text == ""

    async def on_streaming_delta(
        self,
        request: AgentRequest,
        to_handle: str,
        event: Any,
        send_meta: dict,
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        del request, to_handle, event, send_meta, stream_type
        self.stream_events.append(("delta", accumulated_text))

    async def on_streaming_end(
        self,
        request: AgentRequest,
        to_handle: str,
        event: Any,
        send_meta: dict,
        stream_type: str,
        accumulated_text: str = "",
    ) -> None:
        del request, to_handle, event, send_meta, stream_type
        self.stream_events.append(("end", accumulated_text))

    def build_agent_request_from_native(self, native_payload: Any):
        raise NotImplementedError


def _request() -> AgentRequest:
    return AgentRequest(
        session_id="probe:u1",
        user_id="u1",
        channel="probe",
        input=[
            Message(
                type=MessageType.MESSAGE,
                role=Role.USER,
                content=[TextContent(text="hello")],
            ),
        ],
    )


async def _stream_events():
    message = Message(
        id="assistant-1",
        type=MessageType.MESSAGE,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[],
    )
    yield message
    yield TextContent(
        msg_id=message.id,
        index=0,
        delta=True,
        status=RunStatus.InProgress,
        text="澜",
    )
    yield TextContent(
        msg_id=message.id,
        index=0,
        delta=True,
        status=RunStatus.InProgress,
        text="台",
    )
    yield Message(
        id=message.id,
        type=MessageType.MESSAGE,
        role=Role.ASSISTANT,
        status=RunStatus.Completed,
        content=[TextContent(text="澜台")],
    )


@pytest.mark.asyncio
async def test_streaming_contract_consumes_completed_message_once():
    channel = StreamingProbeChannel(lambda _request: _stream_events())

    await channel._run_process_loop(_request(), "u1", {})

    assert channel.stream_events == [
        ("start", "message"),
        ("delta", "澜"),
        ("delta", "澜台"),
        ("end", "澜台"),
    ]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_disabled_streaming_preserves_completed_message_path():
    channel = StreamingProbeChannel(
        lambda _request: _stream_events(),
        streaming_enabled=False,
    )

    await channel._run_process_loop(_request(), "u1", {})

    assert channel.stream_events == []
    assert channel.sent == ["澜台"]


def test_capabilities_are_serializable_and_runtime_aware():
    channel = StreamingProbeChannel(
        lambda _request: _stream_events(),
        streaming_enabled=False,
    )

    data = channel.get_capabilities().to_dict()

    assert data["inboundMedia"] == ["image", "text"]
    assert data["messageEdit"] is True
    assert data["streaming"] is False


def test_display_config_keeps_legacy_filters_compatible():
    display = ChannelDisplayConfig.from_config(
        {
            "filter_tool_messages": True,
            "filter_thinking": True,
            "show_tool_calls": True,
            "show_tool_results": False,
            "tool_call_max_length": 80,
        },
        show_tool_details=False,
    )

    assert display.show_tool_details is False
    assert display.show_thinking is False
    assert display.show_tool_calls is False
    assert display.show_tool_results is False
    assert display.tool_call_max_length == 80


def test_new_channel_configs_use_secure_defaults():
    config = ChannelConfig()

    assert config.telegram.security_version == 2
    assert config.telegram.dm_policy == "allowlist"
    assert config.telegram.group_policy == "allowlist"


def test_legacy_channel_configs_preserve_open_behavior_explicitly():
    config = ChannelConfig.model_validate(
        {"telegram": {"enabled": True, "bot_token": "legacy"}},
    )

    assert config.telegram.security_version == 1
    assert config.telegram.dm_policy == "open"
    assert config.telegram.group_policy == "open"


def test_security_v2_can_disable_direct_messages():
    config = ChannelConfig.model_validate(
        {
            "telegram": {
                "security_version": 2,
                "dm_policy": "disabled",
            },
        },
    )

    assert config.telegram.dm_policy == "disabled"
