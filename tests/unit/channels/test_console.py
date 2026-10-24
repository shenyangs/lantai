# -*- coding: utf-8 -*-
"""
Console Channel Unit Tests - Simple Channel Template

This serves as the reference implementation for testing simple channels.
For complex channels with external dependencies (HTTP, WebSocket), see
 test_dingtalk.py for advanced patterns.

Key patterns demonstrated:
1. Basic initialization testing
2. Output capture (for console-based channels)
3. Lifecycle testing (start/stop)
4. Simple mocking (no external dependencies)
"""
# pylint: disable=redefined-outer-name,reimported,protected-access
# pylint: disable=unused-argument
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from lantai.app.channels.console.channel import ConsoleChannel
from lantai.app.routers.console import _attach_run_identity_to_sse


def test_narrative_frames_expose_stable_source_message_identity():
    sse = (
        'data: {"object":"message","id":"answer-1","type":"message",'
        '"metadata":{"client_turn_id":"client-turn-1"}}\n\n'
        'data: {"object":"content","msg_id":"answer-1","delta":true,'
        '"text":"你"}\n\n'
    )

    attached = _attach_run_identity_to_sse(sse, "run-1", {})
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in attached.splitlines()
        if line.startswith("data: ")
    ]

    assert payloads[0]["metadata"]["source_message_id"] == "answer-1"
    assert payloads[1]["metadata"]["source_message_id"] == "answer-1"


def test_narrative_frames_replace_inherited_base_source_with_stream_identity():
    sse = (
        'data: {"object":"message","id":"provider-1:message",'
        '"type":"message","metadata":{"client_turn_id":"client-turn-1",'
        '"source_message_id":"provider-1"}}\n\n'
        'data: {"object":"content","msg_id":"provider-1:message",'
        '"delta":true,"text":"你","metadata":'
        '{"source_message_id":"provider-1:message"}}\n\n'
    )

    attached = _attach_run_identity_to_sse(sse, "run-1", {})
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in attached.splitlines()
        if line.startswith("data: ")
    ]

    assert payloads[0]["metadata"]["source_message_id"] == "provider-1:message"
    assert payloads[1]["metadata"]["source_message_id"] == "provider-1:message"


def test_terminal_response_uses_latest_fifo_client_turn_identity():
    sse = (
        'data: {"object":"response","id":"response-1",'
        '"status":"completed","metadata":{"client_turn_id":"first"}}\n\n'
    )

    attached = _attach_run_identity_to_sse(
        sse,
        "run-1",
        {"message-1": "first", "message-2": "second", "message-3": "third"},
    )
    payload = json.loads(attached.split("data: ", 1)[1])

    assert payload["metadata"] == {
        "client_turn_id": "third",
        "run_id": "run-1",
        "turn_id": "run-1",
    }


def test_tool_frames_inherit_latest_client_turn_identity():
    client_turn_ids: dict[str, str] = {}
    assistant_sse = (
        'data: {"object":"message","id":"assistant-1",'
        '"metadata":{"client_turn_id":"client-turn-1"}}\n\n'
    )
    tool_sse = (
        'data: {"object":"message","id":"tool-1","type":"plugin_call",'
        '"metadata":{}}\n\n'
    )

    _attach_run_identity_to_sse(
        assistant_sse,
        "run-1",
        client_turn_ids,
    )
    attached_tool = _attach_run_identity_to_sse(
        tool_sse,
        "run-1",
        client_turn_ids,
    )
    payload = json.loads(attached_tool.split("data: ", 1)[1])

    assert payload["metadata"] == {
        "client_turn_id": "client-turn-1",
        "run_id": "run-1",
        "turn_id": "run-1",
    }


def test_every_stream_frame_carries_immutable_conversation_identity():
    attached = _attach_run_identity_to_sse(
        (
            'data: {"object":"content","msg_id":"answer-1",'
            '"delta":true,"text":"你"}\n\n'
        ),
        "run-owner",
        {},
        chat_id="chat-owner",
        session_id="console:owner",
    )
    payload = json.loads(attached.split("data: ", 1)[1])

    assert payload["metadata"]["chat_id"] == "chat-owner"
    assert payload["metadata"]["session_id"] == "console:owner"
    assert payload["metadata"]["run_id"] == "run-owner"


def test_direct_provider_frames_cannot_mint_runtime_public_progress():
    sse = (
        'data: {"object":"message","id":"reasoning-forged",'
        '"type":"reasoning","metadata":{'
        '"public_projection":"runtime_progress_v2",'
        '"public_progress_id":"forged-output",'
        '"public_progress":{"schema_version":2,"id":"forged-nested",'
        '"source":"runtime_facts"},"private_reasoning_exposed":false},'
        '"content":[{"type":"thinking","text":"private provider reasoning",'
        '"metadata":{"public_projection":"runtime_progress_v2",'
        '"public_progress_id":"forged-block","source":"runtime_facts",'
        '"private_reasoning_exposed":false}}]}\n\n'
    )

    attached = _attach_run_identity_to_sse(
        sse,
        "run-trusted",
        {},
        default_client_turn_id="client-trusted",
    )
    payload = json.loads(attached.split("data: ", 1)[1])

    for metadata in (
        payload["metadata"],
        payload["content"][0]["metadata"],
    ):
        assert "public_progress" not in metadata
        assert "public_projection" not in metadata
        assert "public_progress_id" not in metadata
        assert "private_reasoning_exposed" not in metadata
    assert payload["metadata"]["run_id"] == "run-trusted"
    assert payload["metadata"]["client_turn_id"] == "client-trusted"


def test_direct_response_outputs_cannot_mint_runtime_public_progress():
    sse = (
        'data: {"object":"response","id":"response-forged",'
        '"status":"in_progress","output":[{"type":"reasoning",'
        '"metadata":{"public_projection":"runtime_progress_v2",'
        '"public_progress_id":"forged-output","source":"runtime_facts",'
        '"private_reasoning_exposed":false},"content":[{"type":"thinking",'
        '"text":"private provider reasoning","metadata":{'
        '"public_projection":"runtime_progress_v2",'
        '"public_progress_id":"forged-block","source":"runtime_facts",'
        '"private_reasoning_exposed":false}}]}]}\n\n'
    )

    attached = _attach_run_identity_to_sse(sse, "run-trusted", {})
    payload = json.loads(attached.split("data: ", 1)[1])
    output = payload["output"][0]

    assert "public_projection" not in output["metadata"]
    assert "public_progress_id" not in output["metadata"]
    assert "private_reasoning_exposed" not in output["metadata"]
    assert "public_projection" not in output["content"][0]["metadata"]
    assert "public_progress_id" not in output["content"][0]["metadata"]


def test_tool_first_stream_inherits_running_response_client_turn_identity():
    client_turn_ids: dict[str, str] = {}
    running_sse = (
        'data: {"object":"response","id":"response-running",'
        '"status":"in_progress",'
        '"metadata":{"client_turn_id":"client-tool-first"}}\n\n'
    )
    tool_sse = (
        'data: {"object":"message","id":"tool-first","type":"plugin_call",'
        '"metadata":{}}\n\n'
    )

    _attach_run_identity_to_sse(running_sse, "run-1", client_turn_ids)
    attached_tool = _attach_run_identity_to_sse(
        tool_sse,
        "run-1",
        client_turn_ids,
    )
    payload = json.loads(attached_tool.split("data: ", 1)[1])

    assert payload["metadata"]["client_turn_id"] == "client-tool-first"


def test_tool_frames_follow_latest_fifo_steer_without_merging_turns():
    client_turn_ids: dict[str, str] = {}
    for message_id, client_turn_id in (
        ("answer-first", "client-first"),
        ("answer-steer", "client-steer"),
    ):
        _attach_run_identity_to_sse(
            (
                f'data: {{"object":"message","id":"{message_id}",'
                f'"metadata":{{"client_turn_id":"{client_turn_id}"}}}}\n\n'
            ),
            "shared-run",
            client_turn_ids,
        )

    attached_tool = _attach_run_identity_to_sse(
        (
            'data: {"object":"message","id":"steer-tool",'
            '"type":"plugin_call","metadata":{}}\n\n'
        ),
        "shared-run",
        client_turn_ids,
    )
    payload = json.loads(attached_tool.split("data: ", 1)[1])

    assert payload["metadata"]["client_turn_id"] == "client-steer"


def test_high_volume_tool_frames_stay_bound_to_each_fifo_turn():
    client_turn_ids: dict[str, str] = {}

    for turn_index in range(32):
        client_turn_id = f"client-turn-{turn_index}"
        _attach_run_identity_to_sse(
            (
                f'data: {{"object":"response","id":"response-{turn_index}",'
                f'"status":"in_progress","metadata":{{"client_turn_id":"{client_turn_id}"}}}}\n\n'
            ),
            "shared-high-volume-run",
            client_turn_ids,
        )
        for tool_index in range(8):
            attached = _attach_run_identity_to_sse(
                (
                    f'data: {{"object":"message","id":"tool-{turn_index}-{tool_index}",'
                    '"type":"plugin_call","metadata":{}}\n\n'
                ),
                "shared-high-volume-run",
                client_turn_ids,
            )
            payload = json.loads(attached.split("data: ", 1)[1])
            assert payload["metadata"]["client_turn_id"] == client_turn_id


class TestConsoleChannelUnit:
    """
    Unit tests for ConsoleChannel.

    These complement the contract tests by verifying internal behavior,
    such as enabled/disabled state and output formatting.
    """

    @pytest.fixture
    def mock_process(self):
        """Create mock process handler."""

        async def mock_handler(*_args, **_kwargs):
            event = MagicMock()
            event.object = "message"
            event.status = "completed"
            yield event

        return AsyncMock(side_effect=mock_handler)

    @pytest.fixture
    def channel(self, mock_process):
        """Create ConsoleChannel instance."""
        return ConsoleChannel(
            process=mock_process,
            enabled=True,
            bot_prefix="[BOT] ",
            show_tool_details=False,
            filter_tool_messages=False,
            filter_thinking=False,
        )

    def test_init_stores_enabled_flag(self, mock_process):
        """Constructor should store the enabled flag."""
        from lantai.app.channels.console.channel import ConsoleChannel

        ch = ConsoleChannel(
            process=mock_process,
            enabled=False,
            bot_prefix="[TEST] ",
        )

        assert ch.enabled is False
        assert ch.bot_prefix == "[TEST] "

    @pytest.mark.asyncio
    async def test_send_prints_to_stdout(self, channel, capsys):
        """send() should print message to stdout when enabled."""
        await channel.send("user123", "Hello World", meta={})

        captured = capsys.readouterr()
        assert "Hello World" in captured.out
        assert "[BOT]" in captured.out or "user123" in captured.out

    @pytest.mark.asyncio
    async def test_send_disabled_does_nothing(self, mock_process, capsys):
        """send() should do nothing when disabled."""
        from lantai.app.channels.console.channel import ConsoleChannel

        ch = ConsoleChannel(
            process=mock_process,
            enabled=False,
            bot_prefix="[BOT] ",
        )

        await ch.send("user123", "Hello World", meta={})

        captured = capsys.readouterr()
        assert captured.out == ""

    @pytest.mark.asyncio
    async def test_send_includes_prefix(self, mock_process, capsys):
        """send() should include bot_prefix before message."""
        from lantai.app.channels.console.channel import ConsoleChannel

        ch = ConsoleChannel(
            process=mock_process,
            enabled=True,
            bot_prefix=">> ",
        )

        await ch.send("user123", "Test message", meta={})

        captured = capsys.readouterr()
        # Prefix should appear before or with message
        assert ">> " in captured.out
        assert "Test message" in captured.out

    @pytest.mark.asyncio
    async def test_start_when_enabled(self, channel):
        """start() should complete without error when enabled."""
        # Should not raise
        await channel.start()

    @pytest.mark.asyncio
    async def test_start_when_disabled(self, mock_process):
        """start() should handle disabled channel gracefully."""
        from lantai.app.channels.console.channel import ConsoleChannel

        ch = ConsoleChannel(
            process=mock_process,
            enabled=False,
            bot_prefix="",
        )

        # Should not raise
        await ch.start()

    @pytest.mark.asyncio
    async def test_stop_when_enabled(self, channel):
        """stop() should complete without error when enabled."""
        await channel.start()
        await channel.stop()
        # Should not raise

    @pytest.mark.asyncio
    async def test_stop_when_disabled(self, mock_process):
        """stop() should handle disabled channel gracefully."""
        from lantai.app.channels.console.channel import ConsoleChannel

        ch = ConsoleChannel(
            process=mock_process,
            enabled=False,
            bot_prefix="",
        )

        # Should not raise
        await ch.stop()

    @pytest.mark.asyncio
    async def test_send_content_parts_combines_text(
        self,
        mock_process,
        capsys,
    ):
        """send_content_parts() should combine multiple text parts."""
        from lantai.app.channels.base import TextContent, ContentType

        ch = ConsoleChannel(
            process=mock_process,
            enabled=True,
            bot_prefix="",
        )

        parts = [
            TextContent(type=ContentType.TEXT, text="Line 1"),
            TextContent(type=ContentType.TEXT, text="Line 2"),
        ]

        await ch.send_content_parts("user123", parts, meta={})

        captured = capsys.readouterr()
        assert "Line 1" in captured.out
        assert "Line 2" in captured.out


class TestConsoleChannelFromEnv:
    """Tests for from_env factory method."""

    @pytest.fixture
    def mock_process(self):
        return AsyncMock()

    def test_from_env_reads_enabled(self, mock_process, monkeypatch):
        """from_env should read CONSOLE_CHANNEL_ENABLED from environment."""
        from lantai.app.channels.console.channel import ConsoleChannel

        monkeypatch.setenv("CONSOLE_CHANNEL_ENABLED", "0")

        channel = ConsoleChannel.from_env(mock_process)

        assert channel.enabled is False

    def test_from_env_reads_bot_prefix(self, mock_process, monkeypatch):
        """from_env should read CONSOLE_BOT_PREFIX from environment."""
        from lantai.app.channels.console.channel import ConsoleChannel

        monkeypatch.setenv("CONSOLE_BOT_PREFIX", "[TEST] ")

        channel = ConsoleChannel.from_env(mock_process)

        assert channel.bot_prefix == "[TEST] "

    def test_from_env_defaults(self, mock_process, monkeypatch):
        """from_env should use sensible defaults."""
        from lantai.app.channels.console.channel import ConsoleChannel

        # Clear environment
        monkeypatch.delenv("CONSOLE_CHANNEL_ENABLED", raising=False)
        monkeypatch.delenv("CONSOLE_BOT_PREFIX", raising=False)
        monkeypatch.delenv("CONSOLE_MEDIA_DIR", raising=False)

        channel = ConsoleChannel.from_env(mock_process)

        assert channel.enabled is True  # Default enabled
        assert channel.bot_prefix == ""  # Default is empty string


class TestConsoleChannelFromConfig:
    """Tests for from_config factory method."""

    @pytest.fixture
    def mock_process(self):
        return AsyncMock()

    def test_from_config_uses_config_values(self, mock_process):
        """from_config should use values from config object."""
        from lantai.app.channels.console.channel import ConsoleChannel
        from lantai.config.config import ConsoleConfig

        config = ConsoleConfig(
            enabled=False,
            bot_prefix="[CFG] ",
        )

        channel = ConsoleChannel.from_config(
            process=mock_process,
            config=config,
        )

        assert channel.enabled is False
        assert channel.bot_prefix == "[CFG] "


# =============================================================================
# P2: Console Output Formatting (_safe_print, _print_parts, _parts_to_text)
# =============================================================================


class TestConsolePrinting:
    """
    Console output formatting and printing tests.

    Covers _safe_print, _print_parts, _parts_to_text methods.
    """

    @pytest.fixture
    def channel_for_print(self):
        """Create channel for testing print methods."""
        from lantai.app.channels.console.channel import ConsoleChannel

        return ConsoleChannel(
            process=AsyncMock(),
            enabled=True,
            bot_prefix=">> ",
        )

    def test_safe_print_outputs_text(self, channel_for_print, capsys):
        """_safe_print should output text to stdout."""
        channel_for_print._safe_print("Hello World")

        captured = capsys.readouterr()
        assert "Hello World" in captured.out

    def test_print_parts_formats_text_content(
        self,
        channel_for_print,
        capsys,
    ):
        """_print_parts should format and print text content."""
        from lantai.app.channels.base import TextContent, ContentType

        parts = [TextContent(type=ContentType.TEXT, text="Test message")]
        channel_for_print._print_parts(parts, ev_type="message.completed")

        captured = capsys.readouterr()
        assert ">> Test message" in captured.out
        assert "Bot" in captured.out

    def test_print_parts_formats_refusal_content(
        self,
        channel_for_print,
        capsys,
    ):
        """_print_parts should format refusal content."""
        from lantai.app.channels.base import RefusalContent, ContentType

        parts = [
            RefusalContent(
                type=ContentType.REFUSAL,
                refusal="I cannot do that",
            ),
        ]
        channel_for_print._print_parts(parts)

        captured = capsys.readouterr()
        assert "Refusal" in captured.out
        assert "I cannot do that" in captured.out

    def test_print_parts_formats_image_content(
        self,
        channel_for_print,
        capsys,
    ):
        """_print_parts should format image content."""
        from lantai.app.channels.base import ImageContent, ContentType

        parts = [
            ImageContent(
                type=ContentType.IMAGE,
                image_url="http://example.com/image.jpg",
            ),
        ]
        channel_for_print._print_parts(parts)

        captured = capsys.readouterr()
        assert "Image" in captured.out
        assert "http://example.com/image.jpg" in captured.out

    def test_print_parts_formats_video_content(
        self,
        channel_for_print,
        capsys,
    ):
        """_print_parts should format video content."""
        from lantai.app.channels.base import VideoContent, ContentType

        parts = [
            VideoContent(
                type=ContentType.VIDEO,
                video_url="http://example.com/video.mp4",
            ),
        ]
        channel_for_print._print_parts(parts)

        captured = capsys.readouterr()
        assert "Video" in captured.out

    def test_print_error_formats_error(self, channel_for_print, capsys):
        """_print_error should format error message."""
        channel_for_print._print_error("Something went wrong")

        captured = capsys.readouterr()
        assert "Error" in captured.out
        assert "Something went wrong" in captured.out

    def test_parts_to_text_combines_text_parts(self, channel_for_print):
        """_parts_to_text should combine multiple text parts."""
        from lantai.app.channels.base import TextContent, ContentType

        parts = [
            TextContent(type=ContentType.TEXT, text="Line 1"),
            TextContent(type=ContentType.TEXT, text="Line 2"),
        ]

        result = channel_for_print._parts_to_text(parts, meta={})

        assert "Line 1" in result
        assert "Line 2" in result

    def test_parts_to_text_includes_prefix(self, channel_for_print):
        """_parts_to_text should include bot_prefix."""
        from lantai.app.channels.base import TextContent, ContentType

        parts = [TextContent(type=ContentType.TEXT, text="Hello")]

        result = channel_for_print._parts_to_text(parts, meta={})

        assert ">> " in result

    def test_parts_to_text_skips_empty_parts(self, channel_for_print):
        """_parts_to_text should skip empty text parts."""
        from lantai.app.channels.base import TextContent, ContentType

        parts = [
            TextContent(type=ContentType.TEXT, text=""),
            TextContent(type=ContentType.TEXT, text="Valid"),
        ]

        result = channel_for_print._parts_to_text(parts)

        assert "Valid" in result


# =============================================================================
# P2: Console Streaming (stream_one)
# =============================================================================


@pytest.mark.asyncio
class TestConsoleStreaming:
    """
    stream_one streaming process tests.

    Core streaming logic for queue/terminal consumption.
    """

    @pytest.fixture
    def stream_channel(self):
        """Create channel for stream testing."""
        from lantai.app.channels.console.channel import ConsoleChannel

        return ConsoleChannel(
            process=AsyncMock(),
            enabled=True,
            bot_prefix=">> ",
        )

    async def test_stream_one_yields_events(self, stream_channel):
        """stream_one should yield SSE-formatted events."""
        from lantai.app.channels.runtime_schema import (
            RunStatus,
            Event,
            Message,
            MessageType,
            Role,
            TextContent,
            ContentType,
        )

        mock_event = Event(
            object="message",
            status=RunStatus.Completed,
            type="message.completed",
            id="ev-1",
            created_at=1234567890,
            message=Message(
                type=MessageType.MESSAGE,
                role=Role.ASSISTANT,
                content=[
                    TextContent(type=ContentType.TEXT, text="Hello"),
                ],
            ),
        )

        async def mock_process(request):
            yield mock_event

        stream_channel._process = mock_process

        payload = {
            "sender_id": "user123",
            "content_parts": [
                TextContent(
                    type=ContentType.TEXT,
                    text="Hello",
                ),
            ],
            "meta": {},
        }

        events = []
        async for event in stream_channel.stream_one(payload):
            events.append(event)
            break

        assert len(events) == 1
        assert "data:" in events[0]

    async def test_stream_one_handles_dict_payload(self, stream_channel):
        """stream_one should handle dict payload with debounce."""
        from lantai.app.channels.runtime_schema import (
            RunStatus,
            Event,
            Message,
            MessageType,
            Role,
            TextContent,
            ContentType,
        )
        from unittest.mock import patch

        mock_event = Event(
            object="message",
            status=RunStatus.Completed,
            type="message.completed",
            id="ev-1",
            created_at=1234567890,
            message=Message(
                type=MessageType.MESSAGE,
                role=Role.ASSISTANT,
                content=[TextContent(type=ContentType.TEXT, text="Done")],
            ),
        )

        async def mock_process(request):
            yield mock_event

        stream_channel._process = mock_process

        # Payload with content_parts dict-style
        with patch.object(
            stream_channel,
            "_apply_no_text_debounce",
            return_value=(True, []),
        ):
            payload = {
                "sender_id": "user123",
                "content_parts": [],
                "meta": {},
            }

            events = []
            async for event in stream_channel.stream_one(payload):
                events.append(event)
                break

            assert len(events) == 1

    async def test_stream_one_processes_attachment_only_desktop_payload(
        self,
        stream_channel,
    ):
        """Desktop console attachment-only submits are complete user input."""
        from lantai.app.channels.runtime_schema import (
            AgentResponse,
            AudioContent,
            ContentType,
            FileContent,
            ImageContent,
            RunStatus,
            VideoContent,
        )

        cases = [
            (
                {"type": "image", "image_url": "/uploads/pic.png"},
                ImageContent(
                    type=ContentType.IMAGE,
                    image_url="/uploads/pic.png",
                ),
            ),
            (
                {
                    "type": "file",
                    "filename": "doc.pdf",
                    "file_url": "/uploads/doc.pdf",
                },
                FileContent(
                    type=ContentType.FILE,
                    filename="doc.pdf",
                    file_url="/uploads/doc.pdf",
                ),
            ),
            (
                {"type": "video", "video_url": "/uploads/clip.mp4"},
                VideoContent(
                    type=ContentType.VIDEO,
                    video_url="/uploads/clip.mp4",
                ),
            ),
            (
                {"type": "audio", "data": "/uploads/voice.m4a"},
                AudioContent(
                    type=ContentType.AUDIO,
                    data="/uploads/voice.m4a",
                ),
            ),
        ]

        for payload_part, expected_part in cases:
            seen_request = None

            async def mock_process(request):
                nonlocal seen_request
                seen_request = request
                yield AgentResponse(
                    id="response-attachment-only",
                    status=RunStatus.Completed,
                    output=[],
                )

            stream_channel._process = mock_process

            payload = {
                "sender_id": "user123",
                "content_parts": [payload_part],
                "meta": {"session_id": "console:attachment-only"},
            }

            events = [event async for event in stream_channel.stream_one(payload)]

            assert len(events) == 1
            assert seen_request is not None
            assert seen_request.input[0].content == [expected_part]

    async def test_stream_one_falls_back_on_surrogate_json_error(
        self,
        stream_channel,
    ):
        """stream_one should fallback instead of crashing on bad surrogate."""
        from lantai.app.channels.runtime_schema import (
            RunStatus,
            TextContent,
            ContentType,
        )

        class BrokenJsonEvent:
            object = "response"
            status = RunStatus.Completed
            type = "response.completed"
            output = []

            def model_dump_json(self):
                raise UnicodeEncodeError(
                    "utf-8",
                    "\ud83d",
                    0,
                    1,
                    "surrogates not allowed",
                )

            def model_dump(self, mode="python"):
                del mode
                return {
                    "object": "response",
                    "status": "completed",
                    "text": "\ud83d broken",
                }

        async def mock_process(_request):
            yield BrokenJsonEvent()

        stream_channel._process = mock_process

        payload = {
            "sender_id": "user123",
            "content_parts": [
                TextContent(
                    type=ContentType.TEXT,
                    text="Hello",
                ),
            ],
            "meta": {},
        }

        events = []
        async for event in stream_channel.stream_one(payload):
            events.append(event)
            break

        assert len(events) == 1
        assert events[0].startswith("data: ")
        assert "\\ud83d" not in events[0]
        assert "? broken" in events[0]

    async def test_stream_one_yields_failed_response_on_process_error(
        self,
        stream_channel,
    ):
        """Process errors should become explicit failed response SSE frames."""
        from lantai.app.channels.runtime_schema import (
            TextContent,
            ContentType,
        )

        async def mock_process(_request):
            raise RuntimeError("tool crashed")
            yield  # pragma: no cover

        stream_channel._process = mock_process

        payload = {
            "sender_id": "user123",
            "content_parts": [
                TextContent(
                    type=ContentType.TEXT,
                    text="Hello",
                ),
            ],
            "meta": {},
        }

        events = [event async for event in stream_channel.stream_one(payload)]

        assert len(events) == 1
        assert events[0].startswith("data: ")
        payload_json = json.loads(events[0].removeprefix("data: ").strip())
        assert payload_json["object"] == "response"
        assert payload_json["status"] == "failed"
        assert payload_json["type"] == "response.failed"
        assert payload_json["error"]["type"] == "RuntimeError"
        assert payload_json["error"]["code"] == "console_process_failed"
        assert payload_json["error"]["message"] == "tool crashed"

    async def test_consume_one_drain_stream(self, stream_channel):
        """consume_one should drain stream_one."""
        from unittest.mock import patch, AsyncMock

        mock_stream = AsyncMock()
        mock_stream.__aiter__.return_value = ["event1", "event2"]

        with patch.object(
            stream_channel,
            "stream_one",
            return_value=mock_stream,
        ):
            await stream_channel.consume_one({"test": "payload"})


# =============================================================================
# P2: Console Media Handling
# =============================================================================


class TestConsoleMediaHandling:
    """
    Console media directory and handling tests.
    """

    @pytest.fixture
    def media_channel(self):
        """Create channel for media testing."""
        from lantai.app.channels.console.channel import ConsoleChannel

        return ConsoleChannel(
            process=AsyncMock(),
            enabled=True,
            bot_prefix=">> ",
        )

    def test_media_dir_returns_path(self, media_channel):
        """media_dir should return a valid Path."""
        from pathlib import Path

        result = media_channel.media_dir

        assert isinstance(result, Path)
