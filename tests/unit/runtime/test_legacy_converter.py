# -*- coding: utf-8 -*-
"""Behavior locks for legacy runtime converters."""

from __future__ import annotations

import json

import pytest
from lantai.exceptions import AppBaseException

from lantai.app.channels.runtime_schema import (
    FileContent,
    ImageContent,
    Message,
    TextContent,
)
from lantai.runtime.converters.legacy_messages import (
    LegacyInMemoryMemory,
    LegacyMsg,
    legacy_memory_state_to_chat_messages,
    legacy_msg_to_chat_messages,
    legacy_msg_to_runtime_events,
    legacy_msg_to_runtime_messages,
    is_legacy_app_base_exception,
    legacy_assistant_text_msg,
    legacy_configuration_exception,
    legacy_create_plan_notebook,
    legacy_empty_plan_notebook_state,
    legacy_message,
    legacy_query_messages_to_runtime_messages,
    legacy_rewrite_last_message_text,
    legacy_stream_item_to_channel_events,
    legacy_task_cancelled_exception,
    legacy_user_text_msg,
    runtime_event_to_legacy_stream_item,
    runtime_message_to_legacy_msg,
)
from lantai.runtime.events import RuntimeEvent
from lantai.runtime.input import RuntimeRequestContext
from lantai.runtime.messages import ContentBlock, RuntimeMessage
from lantai.runtime.public_reasoning import (
    RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD,
    verify_runtime_public_progress_attestation,
)


def test_legacy_msg_to_runtime_messages_preserves_text_and_metadata() -> None:
    msg = LegacyMsg("Friday", "你好", "assistant", metadata={"k": "v"})

    converted = legacy_msg_to_runtime_messages(msg)

    assert len(converted) == 1
    assert converted[0].role == "assistant"
    assert converted[0].text_content() == "你好"
    assert converted[0].metadata["source"] == "legacy"
    assert converted[0].metadata["metadata"] == {"k": "v"}


def test_legacy_msg_to_runtime_messages_maps_tools() -> None:
    msg = LegacyMsg(
        "Friday",
        [
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": "wiki_search",
                "input": {"query": "澜台"},
            },
            {
                "type": "tool_result",
                "id": "tool-1",
                "name": "wiki_search",
                "output": [{"type": "text", "text": "命中"}],
            },
        ],
        "assistant",
    )

    converted = legacy_msg_to_runtime_messages(msg)[0]

    assert converted.content[0].type == "tool_call"
    assert converted.content[0].data["arguments"] == {"query": "澜台"}
    assert converted.content[1].type == "tool_result"
    assert converted.content[1].data["output"][0]["text"] == "命中"


def test_failed_tool_result_stays_failed_through_channel_conversion() -> None:
    msg = LegacyMsg(
        "Friday",
        [
            {
                "type": "tool_result",
                "id": "tool-1",
                "name": "wiki_search",
                "output": [{"type": "text", "text": "bad limit"}],
                "ok": False,
                "status": "failed",
                "error": "bad limit",
            },
        ],
        "assistant",
    )

    converted = legacy_msg_to_chat_messages(msg)

    assert len(converted) == 1
    assert converted[0].status == "failed"
    assert converted[0].error is not None
    assert converted[0].error.message == "bad limit"
    assert converted[0].content[0].data["ok"] is False
    assert converted[0].content[0].data["error"] == "bad limit"


def test_legacy_unknown_block_round_trips_without_stringifying() -> None:
    msg = LegacyMsg(
        "Friday",
        [
            {
                "type": "search_hit",
                "id": "hit-1",
                "score": 0.9,
                "payload": {"title": "澜台"},
            },
        ],
        "assistant",
    )

    runtime_msg = legacy_msg_to_runtime_messages(msg)[0]
    round_tripped = runtime_message_to_legacy_msg(runtime_msg)

    assert runtime_msg.content[0].type == "text"
    assert runtime_msg.content[0].text == ""
    assert runtime_msg.content[0].metadata == {
        "legacy_type": "search_hit",
        "legacy_unknown_block": True,
    }
    assert runtime_msg.content[0].data == {
        "id": "hit-1",
        "score": 0.9,
        "payload": {"title": "澜台"},
    }
    assert round_tripped.content == msg.content


def test_runtime_message_to_legacy_msg_preserves_text() -> None:
    msg = RuntimeMessage.text("user", "ping", id="runtime-user-1", name="user")

    converted = runtime_message_to_legacy_msg(msg)

    assert converted.role == "user"
    assert converted.content == "ping"
    assert converted.id == "runtime-user-1"


def test_runtime_message_to_legacy_msg_round_trips_mixed_blocks_in_order() -> None:
    msg = RuntimeMessage(
        id="runtime-mixed-1",
        role="assistant",
        name="Friday",
        content=[
            ContentBlock(type="text", text="答"),
            ContentBlock(type="thinking", text="想"),
            ContentBlock(
                type="tool_call",
                data={"id": "call-1", "name": "lookup", "arguments": {"q": "澜台"}},
            ),
            ContentBlock(
                type="tool_result",
                data={
                    "id": "call-1",
                    "name": "lookup",
                    "output": [{"type": "text", "text": "命中"}],
                },
            ),
            ContentBlock(
                type="image",
                data={"source": {"type": "url", "url": "file:///tmp/a.png"}},
            ),
            ContentBlock(
                type="file",
                data={"file_id": "file-1", "filename": "report.pdf"},
            ),
            ContentBlock(
                type="error",
                text="tool failed",
                data={"code": "E_TOOL"},
            ),
        ],
    )

    legacy_msg = runtime_message_to_legacy_msg(msg)
    round_tripped = legacy_msg_to_runtime_messages(legacy_msg)[0]

    assert [block["type"] for block in legacy_msg.content] == [
        "text",
        "thinking",
        "tool_use",
        "tool_result",
        "image",
        "file",
        "error",
    ]
    assert [block.type for block in round_tripped.content] == [
        "text",
        "thinking",
        "tool_call",
        "tool_result",
        "image",
        "file",
        "error",
    ]
    assert round_tripped.content[2].data["arguments"] == {"q": "澜台"}
    assert round_tripped.content[3].data["output"][0]["text"] == "命中"
    assert round_tripped.content[6].text == "tool failed"
    assert round_tripped.content[6].data["code"] == "E_TOOL"


def test_legacy_assistant_text_msg_builds_text_block_message() -> None:
    msg = legacy_assistant_text_msg("命令完成")

    assert msg.name == "Friday"
    assert msg.role == "assistant"
    assert msg.content[0]["type"] == "text"
    assert msg.content[0]["text"] == "命令完成"


def test_legacy_user_text_msg_builds_plain_user_message() -> None:
    msg = legacy_user_text_msg("run task")

    assert msg.name == "user"
    assert msg.role == "user"
    assert msg.content == "run task"


def test_legacy_message_builds_generic_message() -> None:
    msg = legacy_message(
        name="visual-user",
        role="user",
        content=[{"type": "image", "source": {"type": "url", "url": "file:///x"}}],
        metadata={"scope": "wiki"},
    )

    assert msg.name == "visual-user"
    assert msg.role == "user"
    assert msg.content[0]["type"] == "image"
    assert msg.metadata == {"scope": "wiki"}


def test_legacy_rewrite_last_message_text_updates_legacy_shapes() -> None:
    with_text = LegacyMsg(
        "user",
        [
            {"type": "text", "text": "old"},
            {"type": "image", "source": {"type": "url", "url": "file:///tmp/a.png"}},
        ],
        "user",
    )
    legacy_rewrite_last_message_text([with_text], "new")
    assert with_text.content[0]["text"] == "new"

    without_text = LegacyMsg(
        "user",
        [{"type": "image", "source": {"type": "url", "url": "file:///tmp/a.png"}}],
        "user",
    )
    legacy_rewrite_last_message_text([without_text], "inserted")
    assert without_text.content[0]["text"] == "inserted"
    assert len(without_text.content) == 2

    string_content = LegacyMsg("user", "old string", "user")
    legacy_rewrite_last_message_text([string_content], "new string")
    assert string_content.content == "new string"


def test_legacy_query_messages_to_runtime_messages_accepts_textlike_rows() -> None:
    class TextLikeMessage:
        role = "user"
        name = "sam"

        def get_text_content(self) -> str:
            return "hello"

    converted = legacy_query_messages_to_runtime_messages(
        [LegacyMsg("Friday", "你好", "assistant"), TextLikeMessage(), 42],
    )

    assert [message.role for message in converted] == ["assistant", "user", "user"]
    assert [message.text_content() for message in converted] == ["你好", "hello", "42"]
    assert converted[1].name == "sam"


def test_legacy_query_messages_preserves_typed_console_text_and_image() -> None:
    converted = legacy_query_messages_to_runtime_messages(
        [
            Message(
                id="msg-console-image",
                role="user",
                content=[
                    TextContent(text="请描述图片"),
                    ImageContent(image_url="file:///tmp/lantai.png"),
                ],
                metadata={"session_id": "session-1"},
            ),
        ],
    )

    assert len(converted) == 1
    assert converted[0].id == "msg-console-image"
    assert converted[0].role == "user"
    assert [block.type for block in converted[0].content] == ["text", "image"]
    assert converted[0].content[0].text == "请描述图片"
    assert converted[0].content[1].data == {
        "source": {
            "type": "url",
            "url": "file:///tmp/lantai.png",
        },
    }
    assert converted[0].metadata == {
        "session_id": "session-1",
        "source": "runtime_schema",
    }


def test_legacy_query_messages_keeps_image_only_message_nonempty() -> None:
    converted = legacy_query_messages_to_runtime_messages(
        [
            Message(
                role="user",
                content=[ImageContent(image_url="/tmp/lantai.png")],
            ),
        ],
    )

    assert [block.type for block in converted[0].content] == ["image"]
    legacy_msg = runtime_message_to_legacy_msg(converted[0])
    assert legacy_msg.content == [
        {
            "type": "image",
            "source": {
                "type": "url",
                "url": "/tmp/lantai.png",
            },
        },
    ]


def test_legacy_query_messages_preserves_typed_console_file() -> None:
    converted = legacy_query_messages_to_runtime_messages(
        [
            Message(
                role="user",
                content=[
                    TextContent(text="总结附件"),
                    FileContent(
                        file_url="/tmp/report.pdf",
                        filename="report.pdf",
                    ),
                ],
            ),
        ],
    )

    assert [block.type for block in converted[0].content] == ["text", "file"]
    assert converted[0].content[1].data == {
        "source": {"type": "url", "url": "/tmp/report.pdf"},
        "filename": "report.pdf",
    }


def test_legacy_empty_plan_notebook_state_is_loadable() -> None:
    state = legacy_empty_plan_notebook_state()

    assert isinstance(state, dict)
    assert "plans" in state or "storage" in state or state == {}


def test_legacy_create_plan_notebook_accepts_hint_generator() -> None:
    class HintGenerator:
        pass

    hint_gen = HintGenerator()
    notebook = legacy_create_plan_notebook(plan_to_hint=hint_gen)

    assert notebook.state_dict() == legacy_empty_plan_notebook_state()
    assert getattr(notebook, "plan_to_hint", hint_gen) is hint_gen


@pytest.mark.asyncio
async def test_legacy_plan_notebook_uses_lantai_owned_plan_contract() -> None:
    notebook = legacy_create_plan_notebook()
    seen_updates = []
    notebook.register_plan_change_hook(
        "capture",
        lambda nb, plan: seen_updates.append(plan.name if plan else None),
    )

    created = await notebook.create_plan(
        name="Ship fix",
        description="Fix regression",
        expected_outcome="Regression fixed",
        subtasks=[
            {
                "name": "Patch",
                "description": "Apply code changes",
                "expected_outcome": "Patch lands",
            },
            {
                "name": "Verify",
                "description": "Run tests",
                "expected_outcome": "Tests pass",
            },
        ],
    )

    assert "created successfully" in created.content[0]["text"]
    assert notebook.current_plan is not None
    assert notebook.current_plan.subtasks[0].state == "todo"
    assert seen_updates == ["Ship fix"]

    await notebook.update_subtask_state(0, "in_progress")
    await notebook.finish_subtask(0, "Patch landed")

    assert notebook.current_plan.subtasks[0].state == "done"
    assert notebook.current_plan.subtasks[1].state == "in_progress"

    state = notebook.state_dict()
    restored = legacy_create_plan_notebook()
    restored.load_state_dict(state)

    assert restored.current_plan is not None
    assert restored.current_plan.name == "Ship fix"
    assert restored.state_dict()["storage"] == {"plans": {}}

    finished = await restored.finish_plan("done", "All done")
    assert "finished successfully" in finished.content[0]["text"]
    assert restored.current_plan is None
    assert restored.state_dict()["storage"]["plans"]


def test_is_legacy_app_base_exception_identifies_runtime_errors() -> None:
    exc = AppBaseException(status=500, code="LEGACY_FAILED", message="legacy failed")

    assert is_legacy_app_base_exception(exc) is True
    assert is_legacy_app_base_exception(RuntimeError("plain")) is False


def test_legacy_task_cancelled_exception_preserves_exception_type() -> None:
    exc = legacy_task_cancelled_exception("Task has been cancelled!")

    assert type(exc).__name__ == "AgentException"
    assert "Task has been cancelled!" in str(exc)


def test_legacy_configuration_exception_preserves_exception_type() -> None:
    exc = legacy_configuration_exception("key path is empty")

    assert type(exc).__name__ == "ConfigurationException"
    assert "key path is empty" in str(exc)


def test_legacy_msg_to_runtime_events_maps_message_and_tool_rows() -> None:
    request = RuntimeRequestContext(
        agent_id="default",
        session_id="session-1",
        run_id="turn-1",
    )
    msg = LegacyMsg(
        "Friday",
        [
            {"type": "text", "text": "我先查一下"},
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": "wiki_search",
                "input": {"query": "澜台"},
            },
            {
                "type": "tool_result",
                "id": "tool-1",
                "name": "wiki_search",
                "output": [{"type": "text", "text": "命中"}],
            },
        ],
        "assistant",
    )

    events = legacy_msg_to_runtime_events(
        msg,
        request=request,
        seq_start=10,
    )

    assert [event.type for event in events] == [
        "tool.call.started",
        "tool.call.completed",
        "message.completed",
    ]
    assert events[0].seq == 11
    assert events[0].item_id == "tool-1"
    assert events[0].payload["tool_name"] == "wiki_search"
    assert events[-1].summary == "我先查一下"
    assert events[-1].session_id == "session-1"


def test_legacy_msg_to_runtime_events_preserves_failed_tool_status() -> None:
    request = RuntimeRequestContext(
        agent_id="default",
        session_id="session-1",
        run_id="turn-1",
    )
    msg = LegacyMsg(
        "Friday",
        [
            {
                "type": "tool_result",
                "id": "tool-1",
                "name": "wiki_search",
                "output": [{"type": "text", "text": "bad limit"}],
                "ok": False,
                "status": "failed",
                "error": "bad limit",
            },
        ],
        "assistant",
    )

    events = legacy_msg_to_runtime_events(msg, request=request, seq_start=10)

    assert len(events) == 1
    assert events[0].type == "tool.call.completed"
    assert events[0].status == "failed"
    assert events[0].payload["ok"] is False
    assert events[0].payload["error"] == "bad limit"


def test_legacy_tool_guard_prompt_becomes_an_approval_event_not_chat_text() -> None:
    request = RuntimeRequestContext(
        agent_id="default",
        session_id="console:session-1",
        root_session_id="chat-1",
        run_id="turn-approval",
    )
    msg = LegacyMsg(
        "Friday",
        [{"type": "text", "text": "internal legacy approval copy"}],
        "assistant",
        metadata={
            "message_type": "tool_guard_approval",
            "approval_request_id": "approval-1",
            "session_id": "console:session-1",
            "tool_name": "guanlan_search",
            "severity": "medium",
            "findings_count": 1,
            "findings_summary": "The search needs user confirmation.",
            "tool_params": {"query": "WPS 套娃"},
        },
    )

    events = legacy_msg_to_runtime_events(msg, request=request, seq_start=10)

    assert [event.type for event in events] == ["approval.requested"]
    event = events[0]
    assert event.seq == 11
    assert event.item_id == "approval-1"
    assert event.payload["approval_request_id"] == "approval-1"
    assert event.payload["root_session_id"] == "chat-1"
    assert event.payload["arguments"] == {"query": "WPS 套娃"}
    assert event.presentation is not None
    assert event.presentation.surface == "approval"


def test_runtime_event_to_legacy_stream_item_restores_message_event() -> None:
    runtime_msg = RuntimeMessage.text("assistant", "done", name="Friday")
    event = RuntimeEvent(
        id="event-message",
        type="message.completed",
        status="completed",
        session_id="session-1",
        payload={"message": runtime_msg.model_dump(mode="json")},
    )

    stream_item = runtime_event_to_legacy_stream_item(event)

    assert stream_item is not None
    msg, last = stream_item
    assert msg.content[0]["text"] == "done"
    assert last is False


def test_runtime_event_to_legacy_stream_item_preserves_public_progress_identity() -> None:
    runtime_msg = RuntimeMessage(
        id="reasoning-1",
        role="assistant",
        content=[ContentBlock(type="thinking", text="正在检索和核对公开资料。")],
    )
    progress = {
        "schema_version": 2,
        "id": "public-progress:run-1:1",
        "revision": 1,
        "source": "runtime_facts",
        "text_origin": "assistant_public_text",
    }
    event = RuntimeEvent(
        id="event-reasoning",
        type="message.delta",
        status="running",
        turn_id="run-1",
        item_id="reasoning-1",
        payload={
            "message": runtime_msg.model_dump(mode="json"),
            "delta": "正在检索和核对公开资料。",
            "public_progress": progress,
            "run_id": "run-1",
            "client_turn_id": "client-1",
        },
    )

    stream_item = runtime_event_to_legacy_stream_item(event)

    assert stream_item is not None
    msg, _ = stream_item
    assert msg.metadata["public_progress"] == progress
    assert msg.metadata["run_id"] == "run-1"
    assert msg.metadata["client_turn_id"] == "client-1"
    assert verify_runtime_public_progress_attestation(
        msg.metadata[RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD],
        run_id="run-1",
        wire_message_id="reasoning-1:reasoning",
        message_type="reasoning",
        text="正在检索和核对公开资料。",
    ) == progress


def test_runtime_event_to_legacy_stream_item_scrubs_untrusted_progress_claims() -> None:
    runtime_msg = RuntimeMessage(
        id="provider-reasoning",
        role="assistant",
        content=[ContentBlock(type="thinking", text="private reasoning")],
        metadata={
            "public_progress": {
                "schema_version": 2,
                "id": "forged",
                "revision": 1,
                "source": "runtime_facts",
                "text_origin": "assistant_public_text",
            },
            RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD: {
                "signature": "forged",
            },
            "public_projection": "runtime_progress_v2",
            "public_progress_id": "forged",
        },
    )
    event = RuntimeEvent(
        id="event-provider-reasoning",
        type="message.delta",
        status="running",
        turn_id="run-provider",
        item_id="provider-reasoning",
        payload={
            "message": runtime_msg.model_dump(mode="json"),
            "delta": "private reasoning",
        },
    )

    stream_item = runtime_event_to_legacy_stream_item(event)

    assert stream_item is not None
    metadata = stream_item[0].metadata
    assert "public_progress" not in metadata
    assert "public_projection" not in metadata
    assert "public_progress_id" not in metadata
    assert RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD not in metadata


def test_runtime_event_to_legacy_stream_item_marks_turn_completed_as_last() -> None:
    event = RuntimeEvent(
        id="event-turn-completed",
        type="turn.completed",
        status="completed",
        session_id="session-1",
        turn_id="turn-1",
    )

    stream_item = runtime_event_to_legacy_stream_item(event)

    assert stream_item is not None
    msg, last = stream_item
    assert msg.content == []
    assert last is True
    assert msg.metadata["runtime_event_type"] == "turn.completed"


def test_legacy_stream_item_to_channel_events_uses_turn_terminal_status() -> None:
    msg = LegacyMsg(
        "Friday",
        [],
        "assistant",
        metadata={"runtime_event_type": "turn.interrupted"},
    )

    events = legacy_stream_item_to_channel_events((msg, True))

    assert len(events) == 1
    assert events[0].object == "response"
    assert events[0].status == "incomplete"


def test_artifact_requirement_reaches_terminal_response_metadata() -> None:
    event = RuntimeEvent(
        id="event-artifact-required",
        type="turn.completed",
        status="completed",
        session_id="session-1",
        turn_id="turn-1",
        payload={"durable_artifact_required": True},
    )

    stream_item = runtime_event_to_legacy_stream_item(event)
    assert stream_item is not None
    responses = legacy_stream_item_to_channel_events(stream_item)

    assert len(responses) == 1
    assert responses[0].metadata == {"durable_artifact_required": True}


def test_failed_runtime_terminal_preserves_the_actual_reason() -> None:
    event = RuntimeEvent(
        id="event-turn-failed",
        type="turn.failed",
        status="failed",
        session_id="session-1",
        turn_id="turn-1",
        summary="Repeated deterministic tool failure stopped after 2 attempts.",
        payload={"error_code": "repeated_tool_failure"},
    )

    stream_item = runtime_event_to_legacy_stream_item(event)
    assert stream_item is not None
    responses = legacy_stream_item_to_channel_events(stream_item)

    assert len(responses) == 1
    assert responses[0].status == "failed"
    assert responses[0].error is not None
    assert responses[0].error.code == "repeated_tool_failure"
    assert "2 attempts" in responses[0].error.message


def test_runtime_event_to_legacy_stream_item_restores_text_delta() -> None:
    runtime_msg = RuntimeMessage.text(
        "assistant",
        "partial",
        id="runtime-output-1",
        name="Friday",
    )
    event = RuntimeEvent(
        id="event-message-delta",
        type="message.delta",
        status="running",
        session_id="session-1",
        turn_id="turn-1",
        item_id="runtime-output-1",
        payload={"message": runtime_msg.model_dump(mode="json")},
    )

    stream_item = runtime_event_to_legacy_stream_item(event)

    assert stream_item is not None
    msg, last = stream_item
    assert last is False
    assert msg.id == "runtime-output-1"
    assert msg.content[0]["type"] == "text"
    assert msg.content[0]["text"] == "partial"
    assert msg.metadata["runtime_event_type"] == "message.delta"


def test_approval_runtime_event_never_downgrades_to_transcript_text() -> None:
    event = RuntimeEvent(
        id="event-approval-requested",
        type="approval.requested",
        status="running",
        session_id="session-1",
        turn_id="turn-1",
        item_id="approval-1",
        payload={
            "request_id": "approval-1",
            "tool_name": "guanlan_search",
            "severity": "medium",
            "reason": "需要确认",
        },
    )

    stream_item = runtime_event_to_legacy_stream_item(event)

    assert stream_item is not None
    legacy_msg, _ = stream_item
    assert legacy_msg.metadata["message_type"] == "tool_guard_approval"
    assert legacy_stream_item_to_channel_events(stream_item) == []


def test_legacy_stream_item_to_channel_events_restores_text_stream() -> None:
    msg = LegacyMsg(
        "Friday",
        [{"type": "text", "text": "he"}],
        "assistant",
        metadata={"runtime_event_type": "message.delta"},
    )
    msg.id = "runtime-output-1"
    started: set[str] = set()

    events = legacy_stream_item_to_channel_events(
        (msg, False),
        started_message_ids=started,
    )

    assert [event.object for event in events] == ["message", "content"]
    assert events[0].id == "runtime-output-1"
    assert events[0].status == "in_progress"
    assert events[1].msg_id == "runtime-output-1"
    assert events[1].delta is True
    assert events[1].text == "he"


def test_legacy_stream_item_to_channel_events_converts_snapshots_to_deltas() -> None:
    snapshots = ["想", "想好了", "想好了"]
    started: set[str] = set()
    streamed: dict[str, str] = {}
    emitted: list[str] = []

    for text in snapshots:
        msg = LegacyMsg(
            "Friday",
            [{"type": "thinking", "thinking": text}],
            "assistant",
            metadata={"runtime_event_type": "message.delta"},
        )
        msg.id = "runtime-thinking-snapshot"
        events = legacy_stream_item_to_channel_events(
            (msg, False),
            started_message_ids=started,
            streamed_message_texts=streamed,
        )
        emitted.extend(
            event.text
            for event in events
            if getattr(event, "object", None) == "content"
        )

    assert emitted == ["想", "好了"]
    assert streamed == {"runtime-thinking-snapshot": "想好了"}


def test_legacy_stream_item_to_channel_events_prefers_explicit_delta_text() -> None:
    started: set[str] = set()
    streamed: dict[str, str] = {}
    emitted: list[str] = []

    for delta in ["哈", "哈"]:
        msg = LegacyMsg(
            "Friday",
            [{"type": "text", "text": delta}],
            "assistant",
            metadata={
                "runtime_event_type": "message.delta",
                "runtime_delta_text": delta,
            },
        )
        msg.id = "runtime-explicit-delta"
        events = legacy_stream_item_to_channel_events(
            (msg, False),
            started_message_ids=started,
            streamed_message_texts=streamed,
        )
        emitted.extend(
            event.text
            for event in events
            if getattr(event, "object", None) == "content"
        )

    assert emitted == ["哈", "哈"]
    assert streamed == {"runtime-explicit-delta": "哈哈"}


def test_legacy_stream_item_to_channel_events_restores_thinking_stream() -> None:
    delta = LegacyMsg(
        "Friday",
        [{"type": "thinking", "thinking": "想"}],
        "assistant",
        metadata={"runtime_event_type": "message.delta"},
    )
    delta.id = "runtime-thinking-1"
    completed = LegacyMsg(
        "Friday",
        [{"type": "thinking", "thinking": "想好了"}],
        "assistant",
        metadata={"runtime_event_type": "message.completed"},
    )
    completed.id = "runtime-thinking-1"
    started: set[str] = set()

    delta_events = legacy_stream_item_to_channel_events(
        (delta, False),
        started_message_ids=started,
    )
    completed_events = legacy_stream_item_to_channel_events(
        (completed, True),
        started_message_ids=started,
    )

    assert delta_events[0].object == "message"
    assert delta_events[0].type == "reasoning"
    assert delta_events[1].object == "content"
    assert delta_events[1].msg_id == "runtime-thinking-1"
    assert completed_events[0].object == "message"
    assert completed_events[0].id == "runtime-thinking-1"
    assert completed_events[0].type == "reasoning"
    assert completed_events[0].content[0].text == "想好了"
    assert completed_events[-1].object == "response"
    assert completed_events[-1].status == "completed"


def test_legacy_stream_item_to_channel_events_restarts_when_stream_type_changes() -> None:
    thinking = LegacyMsg(
        "Friday",
        [{"type": "thinking", "thinking": "想"}],
        "assistant",
        metadata={
            "runtime_event_type": "message.delta",
            "runtime_item_id": "provider-msg-1",
        },
    )
    thinking.id = "provider-msg-1"
    answer = LegacyMsg(
        "Friday",
        [{"type": "text", "text": "答"}],
        "assistant",
        metadata={
            "runtime_event_type": "message.delta",
            "runtime_item_id": "provider-msg-1",
        },
    )
    answer.id = "provider-msg-1"
    started: set[str] = set()

    thinking_events = legacy_stream_item_to_channel_events(
        (thinking, False),
        started_message_ids=started,
    )
    answer_events = legacy_stream_item_to_channel_events(
        (answer, False),
        started_message_ids=started,
    )

    assert thinking_events[0].object == "message"
    assert thinking_events[0].type == "reasoning"
    assert thinking_events[1].msg_id == "provider-msg-1:reasoning"
    assert answer_events[0].object == "message"
    assert answer_events[0].type == "message"
    assert answer_events[1].msg_id == "provider-msg-1:message"


def test_completed_runtime_event_splits_mixed_thinking_and_answer_ids() -> None:
    event = RuntimeEvent(
        id="event-message-completed",
        type="message.completed",
        status="completed",
        session_id="session-1",
        turn_id="turn-1",
        item_id="provider-msg-1",
        payload={
            "message": RuntimeMessage(
                id="provider-msg-1",
                role="assistant",
                name="Friday",
                content=[
                    ContentBlock(type="thinking", text="想完"),
                    ContentBlock(type="text", text="答完"),
                ],
            ).model_dump(mode="json"),
        },
    )
    stream_item = runtime_event_to_legacy_stream_item(event)

    assert stream_item is not None

    events = legacy_stream_item_to_channel_events(
        stream_item,
        started_message_ids=set(),
    )
    messages = [
        event for event in events if getattr(event, "object", None) == "message"
    ]

    assert [(message.id, message.type) for message in messages] == [
        ("provider-msg-1:reasoning", "reasoning"),
        ("provider-msg-1:message", "message"),
    ]
    assert messages[0].content[0].text == "想完"
    assert messages[1].content[0].text == "答完"
    assert all(getattr(event, "object", None) != "response" for event in events)


@pytest.mark.asyncio
async def test_runtime_delta_bridge_restores_stable_accumulated_text() -> None:
    events = [
        RuntimeEvent(
            id="event-message-delta",
            type="message.delta",
            status="running",
            session_id="session-1",
            turn_id="turn-1",
            item_id="runtime-output-1",
            payload={
                "message": RuntimeMessage.text(
                    "assistant",
                    "hel",
                    id="runtime-output-1",
                    name="Friday",
                ).model_dump(mode="json"),
            },
        ),
        RuntimeEvent(
            id="event-message-completed",
            type="message.completed",
            status="completed",
            session_id="session-1",
            turn_id="turn-1",
            item_id="runtime-output-1",
            payload={
                "message": RuntimeMessage.text(
                    "assistant",
                    "hello",
                    id="runtime-output-1",
                    name="Friday",
                ).model_dump(mode="json"),
            },
        ),
    ]

    streamed = [
        {
            "last": stream_item[1],
            "id": stream_item[0].id,
            "text": stream_item[0].content[0]["text"],
            "event_type": stream_item[0].metadata["runtime_event_type"],
        }
        for event in events
        if (stream_item := runtime_event_to_legacy_stream_item(event)) is not None
    ]

    assert streamed == [
        {
            "last": False,
            "id": "runtime-output-1",
            "text": "hel",
            "event_type": "message.delta",
        },
        {
            "last": False,
            "id": "runtime-output-1",
            "text": "hello",
            "event_type": "message.completed",
        },
    ]


@pytest.mark.asyncio
async def test_runtime_thinking_delta_bridge_restores_thinking_content() -> None:
    events = [
        RuntimeEvent(
            id="event-thinking-delta",
            type="message.delta",
            status="running",
            session_id="session-1",
            turn_id="turn-1",
            item_id="runtime-thinking-1",
            payload={
                "message": RuntimeMessage(
                    id="runtime-thinking-1",
                    role="assistant",
                    name="Friday",
                    content=[ContentBlock(type="thinking", text="想")],
                ).model_dump(mode="json"),
            },
        ),
        RuntimeEvent(
            id="event-thinking-completed",
            type="message.completed",
            status="completed",
            session_id="session-1",
            turn_id="turn-1",
            item_id="runtime-thinking-1",
            payload={
                "message": RuntimeMessage(
                    id="runtime-thinking-1",
                    role="assistant",
                    name="Friday",
                    content=[ContentBlock(type="thinking", text="想一下")],
                ).model_dump(mode="json"),
            },
        ),
    ]

    streamed = [
        {
            "last": stream_item[1],
            "id": stream_item[0].id,
            "type": stream_item[0].content[0]["type"],
            "thinking": stream_item[0].content[0]["thinking"],
            "event_type": stream_item[0].metadata["runtime_event_type"],
        }
        for event in events
        if (stream_item := runtime_event_to_legacy_stream_item(event)) is not None
    ]

    assert streamed == [
        {
            "last": False,
            "id": "runtime-thinking-1",
            "type": "thinking",
            "thinking": "想",
            "event_type": "message.delta",
        },
        {
            "last": False,
            "id": "runtime-thinking-1",
            "type": "thinking",
            "thinking": "想一下",
            "event_type": "message.completed",
        },
    ]


def test_runtime_event_to_legacy_stream_item_restores_tool_event() -> None:
    event = RuntimeEvent(
        id="event-tool",
        type="tool.call.started",
        status="running",
        item_id="tool-1",
        payload={
            "id": "tool-1",
            "name": "wiki_search",
            "arguments": {"query": "澜台"},
            "client_turn_id": "client-tool-turn",
        },
    )

    stream_item = runtime_event_to_legacy_stream_item(event)

    assert stream_item is not None
    msg, last = stream_item
    assert last is False
    assert msg.content[0]["type"] == "tool_use"
    assert msg.content[0]["input"] == {"query": "澜台"}
    assert msg.metadata["client_turn_id"] == "client-tool-turn"


def test_legacy_collaboration_event_exposes_identity_without_private_tasks() -> None:
    request = RuntimeRequestContext(
        agent_id="default",
        session_id="session-safe-collaboration",
        run_id="turn-safe-collaboration",
    )
    msg = LegacyMsg(
        "Friday",
        [
            {
                "type": "tool_use",
                "id": "tool-collaboration",
                "name": "collaborate_with_agents",
                "input": {
                    "tasks": [
                        {
                            "to_agent": "Lantai_Matcha_Agent",
                            "label": "起草主线",
                            "text": "读取 /Users/example/private.md 并使用 secret-key",
                        },
                        {
                            "to_agent": "Lantai_Cola_Agent",
                            "label": "打磨标题",
                            "text": "私有完整提示词",
                        },
                    ],
                },
            },
        ],
        "assistant",
    )

    events = legacy_msg_to_runtime_events(msg, request=request)
    serialized = json.dumps(events[0].payload, ensure_ascii=False)

    assert events[0].payload["arguments"] == {
        "tasks": [
            {"to_agent": "Lantai_Matcha_Agent", "label": "起草主线"},
            {"to_agent": "Lantai_Cola_Agent", "label": "打磨标题"},
        ],
    }
    assert events[0].presentation.surface == "subagent"
    assert "private.md" not in serialized
    assert "secret-key" not in serialized
    assert "私有完整提示词" not in serialized


def test_runtime_event_to_legacy_stream_item_preserves_tool_result_status() -> None:
    event = RuntimeEvent(
        id="event-tool-result",
        type="tool.call.completed",
        status="failed",
        item_id="tool-1",
        payload={
            "id": "tool-1",
            "name": "wiki_search",
            "ok": False,
            "error": {"code": "denied"},
            "output": [{"type": "text", "text": "blocked"}],
            "client_turn_id": "client-tool-turn",
        },
    )

    stream_item = runtime_event_to_legacy_stream_item(event)

    assert stream_item is not None
    msg, last = stream_item
    assert last is False
    assert msg.content[0] == {
        "type": "tool_result",
        "id": "tool-1",
        "name": "wiki_search",
        "output": [{"type": "text", "text": "blocked"}],
        "ok": False,
        "status": "failed",
        "error": {"code": "denied"},
    }
    assert msg.metadata["client_turn_id"] == "client-tool-turn"


@pytest.mark.asyncio
async def test_legacy_memory_state_to_chat_messages_restores_history() -> None:
    memory = LegacyInMemoryMemory()
    await memory.add(LegacyMsg("user", "历史消息", "user"))

    messages = await legacy_memory_state_to_chat_messages(
        memory.state_dict(),
    )

    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content[0].text == "历史消息"


@pytest.mark.asyncio
async def test_legacy_memory_state_to_chat_messages_preserves_original_time() -> None:
    memory = LegacyInMemoryMemory()
    await memory.add(
        LegacyMsg(
            "user",
            "下午的问题",
            "user",
            timestamp="2026-07-27 15:08:55.550",
        ),
    )
    await memory.add(
        LegacyMsg(
            "assistant",
            [{"type": "text", "text": "下午的回答"}],
            "assistant",
            timestamp="2026-07-27 15:09:02.274",
        ),
    )

    messages = await legacy_memory_state_to_chat_messages(memory.state_dict())

    assert [message.created_at for message in messages] == [
        "2026-07-27 15:08:55.550",
        "2026-07-27 15:09:02.274",
    ]


def test_legacy_message_restore_does_not_invent_missing_time() -> None:
    restored = LegacyMsg.from_dict(
        {
            "id": "legacy-without-time",
            "name": "assistant",
            "role": "assistant",
            "content": "无法恢复精确时间的旧消息",
            "metadata": {},
        },
    )

    assert restored.timestamp is None
