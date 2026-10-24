# -*- coding: utf-8 -*-
"""Behavior locks for restoring Native Runtime control cards from the ledger."""

import pytest

from lantai.app.experience.schemas import RuntimeEventsResponse
from lantai.app.runner.api import (
    _message_plain_text,
    _merge_persisted_runtime_control_messages,
    _project_persisted_public_reasoning,
    _project_public_runtime_event_response,
)
from lantai.runtime.adapters.native_runtime import (
    _tool_preamble_public_progress_event,
)
from lantai.runtime.converters import (
    LegacyInMemoryMemory,
    legacy_memory_state_to_chat_messages,
    legacy_msg_to_chat_messages,
    legacy_stream_item_to_channel_events,
    runtime_event_to_legacy_stream_item,
)
from lantai.runtime.converters.legacy_message_components import LegacyMsg
from lantai.runtime.events import RuntimeEvent
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import ContentBlock, RuntimeMessage
from lantai.runtime.models import ModelContentBlock, ModelMessage, ModelResponse
from lantai.runtime.public_reasoning import PublicReasoningProjector


@pytest.mark.parametrize(
    "public_progress",
    [
        {
            "schema_version": 1,
            "id": "spoofed-schema",
            "revision": 1,
            "source": "runtime_facts",
        },
        {
            "schema_version": 2,
            "id": "spoofed-source",
            "revision": 1,
            "source": "provider",
            "text_origin": "assistant_public_text",
        },
        {
            "schema_version": 2,
            "id": "spoofed-origin",
            "revision": 1,
            "source": "runtime_facts",
            "text_origin": "provider_thinking",
        },
        {
            "schema_version": 2,
            "id": "spoofed-revision",
            "revision": 0,
            "source": "runtime_facts",
            "text_origin": "assistant_public_text",
        },
        {
            "schema_version": 2,
            "id": "boolean-revision",
            "revision": True,
            "source": "runtime_facts",
            "text_origin": "assistant_public_text",
        },
    ],
)
def test_history_projection_does_not_promote_untrusted_progress_metadata(
    public_progress: dict,
) -> None:
    legacy = LegacyMsg(
        name="Friday",
        role="assistant",
        content=[{"type": "thinking", "thinking": "private reasoning"}],
        metadata={
            "public_projection": "runtime_progress_v2",
            "public_progress_id": "forged-history-marker",
            "source": "runtime_facts",
            "private_reasoning_exposed": False,
            "metadata": {
                "public_progress": {
                    "schema_version": 2,
                    "id": "forged-history-nested",
                    "source": "runtime_facts",
                }
            },
        },
    )
    legacy.id = "provider-untrusted"
    messages = legacy_msg_to_chat_messages(legacy)
    events = [
        RuntimeEvent(
            id="reasoning-untrusted",
            seq=1,
            type="message.completed",
            status="completed",
            source="assistant",
            item_id="provider-untrusted:reasoning",
            payload={
                "source_message_id": "provider-untrusted",
                "public_progress": public_progress,
                "message": {
                    "id": "provider-untrusted:reasoning",
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "private reasoning"}
                    ],
                },
            },
        )
    ]

    restored = _project_persisted_public_reasoning(messages, events)
    reasoning = [message for message in restored if message.type == "reasoning"]

    assert len(reasoning) == 1
    assert reasoning[0].content[0].text == "private reasoning"
    assert "public_projection" not in reasoning[0].metadata
    assert "public_progress_id" not in reasoning[0].metadata
    assert "public_progress" not in reasoning[0].metadata.get("metadata", {})
    assert reasoning[0].metadata["reasoning_source"] == "provider"


def test_history_public_progress_keeps_runtime_and_client_turn_identity() -> None:
    legacy = LegacyMsg(
        name="Friday",
        role="assistant",
        content=[{"type": "thinking", "thinking": "正在核对交付内容。"}],
    )
    legacy.id = "runtime-progress-source"
    messages = legacy_msg_to_chat_messages(legacy)
    events = [
        RuntimeEvent(
            id="runtime-progress-event",
            seq=1,
            type="message.completed",
            status="completed",
            source="assistant",
            turn_id="run-history-progress",
            item_id="runtime-progress-source:reasoning",
            payload={
                "client_turn_id": "client-history-progress",
                "source_message_id": "runtime-progress-source",
                "public_progress": {
                    "schema_version": 2,
                    "id": "public-progress:history:1",
                    "revision": 1,
                    "source": "runtime_facts",
                    "text_origin": "assistant_public_text",
                },
                "message": {
                    "id": "runtime-progress-source:reasoning",
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "正在核对交付内容。"}
                    ],
                },
            },
        )
    ]

    restored = _project_persisted_public_reasoning(messages, events)
    reasoning = [message for message in restored if message.type == "reasoning"]

    assert len(reasoning) == 1
    assert reasoning[0].metadata["run_id"] == "run-history-progress"
    assert reasoning[0].metadata["runtime_turn_id"] == "run-history-progress"
    assert reasoning[0].metadata["client_turn_id"] == "client-history-progress"


def test_tool_preamble_progress_survives_legacy_history_identity_suffixes() -> None:
    """Lock the exact Native producer -> legacy -> history projection shape."""

    progress_id = "public-progress:run-history:provider-message"
    progress = {
        "schema_version": 2,
        "id": progress_id,
        "revision": 1,
        "source": "runtime_facts",
        "text_origin": "assistant_public_text",
        "tool_call_ids": ["call-history"],
    }
    runtime_message = RuntimeMessage(
        id=f"{progress_id}:reasoning",
        role="assistant",
        content=[
            ContentBlock(
                type="thinking",
                text="我先核对原始记录，再把差异逐项列出来。",
            )
        ],
    )
    runtime_event = RuntimeEvent(
        id="runtime-tool-preamble",
        seq=2,
        type="message.completed",
        status="completed",
        source="assistant",
        turn_id="run-history",
        item_id=runtime_message.id,
        payload={
            "message": runtime_message.model_dump(mode="json"),
            "source_message_id": progress_id,
            "content_type": "thinking",
            "stream_message_type": "reasoning",
            "public_progress": progress,
            "run_id": "run-history",
            "client_turn_id": "client-history",
        },
    )
    legacy_item = runtime_event_to_legacy_stream_item(runtime_event)
    assert legacy_item is not None
    memory_messages = legacy_msg_to_chat_messages(legacy_item[0])

    restored = _project_persisted_public_reasoning(
        memory_messages,
        [runtime_event],
    )

    reasoning = [message for message in restored if message.type == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0].content[0].text == (
        "我先核对原始记录，再把差异逐项列出来。"
    )
    assert reasoning[0].metadata["public_progress_id"] == progress_id
    assert reasoning[0].metadata["public_projection"] == (
        "runtime_progress_v2"
    )
    assert reasoning[0].metadata["text_origin"] == "assistant_public_text"
    assert reasoning[0].metadata["run_id"] == "run-history"
    assert reasoning[0].metadata["client_turn_id"] == "client-history"
    assert reasoning[0].metadata["tool_call_ids"] == ["call-history"]


@pytest.mark.asyncio
async def test_same_run_progress_keeps_distinct_client_turns_through_history(
) -> None:
    """Two logical client turns must not collapse around one provider id."""

    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-history",
            run_id="run-shared",
        ),
        messages=[RuntimeMessage.text("user", "继续")],
    )
    response = ModelResponse(
        message=ModelMessage(
            id="provider-shared",
            role="assistant",
            content=[
                ModelContentBlock(
                    type="text",
                    text="我先核对原始记录，再继续处理。",
                ),
                ModelContentBlock(
                    type="tool_call",
                    data={
                        "id": "call-shared",
                        "name": "lookup",
                        "arguments": {"query": "records"},
                    },
                ),
            ],
        )
    )
    events = [
        _tool_preamble_public_progress_event(
            response,
            runtime_input=runtime_input,
            turn_id="run-shared",
            seq=index,
            client_turn_id=client_turn_id,
        )
        for index, client_turn_id in enumerate(
            ("client-first", "client-second"),
            start=1,
        )
    ]
    assert all(event is not None for event in events)
    progress_events = [event for event in events if event is not None]
    progress_ids = [
        event.payload["public_progress"]["id"] for event in progress_events
    ]
    assert len(set(progress_ids)) == 2

    legacy_items = [
        runtime_event_to_legacy_stream_item(event)
        for event in progress_events
    ]
    assert all(item is not None for item in legacy_items)
    stream_items = [item for item in legacy_items if item is not None]
    channel_messages = [
        channel_event
        for item in stream_items
        for channel_event in legacy_stream_item_to_channel_events(item)
        if str(getattr(channel_event, "type", "")) == "reasoning"
    ]
    assert [message.id for message in channel_messages] == [
        f"{progress_id}:reasoning" for progress_id in progress_ids
    ]
    assert all(
        not message.id.endswith(":reasoning:reasoning")
        for message in channel_messages
    )

    memory = LegacyInMemoryMemory()
    await memory.add([item[0] for item in stream_items])
    reloaded = await legacy_memory_state_to_chat_messages(memory.state_dict())
    restored = _project_persisted_public_reasoning(
        reloaded,
        progress_events,
    )
    progress_rows = [
        message
        for message in restored
        if message.type == "reasoning"
        and message.metadata.get("public_projection")
        == "runtime_progress_v2"
    ]

    assert len(progress_rows) == 2
    assert {
        (
            message.metadata["public_progress_id"],
            message.metadata["run_id"],
            message.metadata["client_turn_id"],
        )
        for message in progress_rows
    } == {
        (progress_ids[0], "run-shared", "client-first"),
        (progress_ids[1], "run-shared", "client-second"),
    }


def test_runtime_event_replay_preserves_old_provider_reasoning() -> None:
    response = RuntimeEventsResponse(
        chat_id="chat-old-reasoning",
        events=[
            {
                "id": "old-thinking",
                "seq": 1,
                "type": "message.completed",
                "status": "completed",
                "source": "assistant",
                "item_id": "old-provider:reasoning",
                "payload": {
                    "message": {
                        "id": "old-provider:reasoning",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "text": "quiet 模式和人格协议的内部规则",
                            }
                        ],
                    }
                },
                "created_at": "2026-08-24T10:00:00Z",
            }
        ],
    )

    projected = _project_public_runtime_event_response(response)
    serialized = projected.model_dump_json()

    assert "quiet 模式" in serialized
    assert "人格协议" in serialized
    assert len(projected.events) == 1


def test_history_projection_restores_latest_user_turn_identity() -> None:
    user = legacy_msg_to_chat_messages(
        LegacyMsg(
            name="user",
            role="user",
            content="生成五张 CSV 表",
        )
    )[0]
    assistant = legacy_msg_to_chat_messages(
        LegacyMsg(
            name="Friday",
            role="assistant",
            content="已生成并附上。",
            metadata={"client_turn_id": "user-turn-csv"},
        )
    )[0]

    restored = _merge_persisted_runtime_control_messages(
        [user, assistant],
        [],
        latest_client_turn_id="user-turn-csv",
    )

    assert [message.role for message in restored] == ["user", "assistant"]
    assert restored[0].metadata["client_turn_id"] == "user-turn-csv"


def test_history_projection_restores_every_user_turn_identity() -> None:
    legacy_messages = [
        LegacyMsg(name="user", role="user", content="第一轮请求"),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content="第一轮完成",
            metadata={"client_turn_id": "user-turn-one"},
        ),
        LegacyMsg(name="user", role="user", content="第二轮请求"),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content="第二轮完成",
            metadata={"client_turn_id": "user-turn-two"},
        ),
    ]
    restored = _merge_persisted_runtime_control_messages(
        legacy_msg_to_chat_messages(legacy_messages),
        [],
        latest_client_turn_id="user-turn-two",
    )

    users = [message for message in restored if message.role == "user"]
    assert [message.metadata["client_turn_id"] for message in users] == [
        "user-turn-one",
        "user-turn-two",
    ]


def test_history_projection_restores_canonical_terminal_answer() -> None:
    """A terminal ledger fact must repair an incomplete legacy snapshot."""

    client_turn_id = "user-ppt-turn"
    memory_messages = legacy_msg_to_chat_messages(
        [
            LegacyMsg(
                name="User",
                role="user",
                content="把这个做成PPT。",
                metadata={"client_turn_id": client_turn_id},
            ),
            LegacyMsg(
                name="Friday",
                role="assistant",
                content="主题名不被识别，我去掉主题参数。",
                metadata={"client_turn_id": client_turn_id},
            ),
        ]
    )
    runtime_events = [
        RuntimeEvent(
            id="event-ppt-final-answer",
            seq=193,
            type="item.completed",
            status="completed",
            source="assistant",
            turn_id="run-ppt",
            item_id="event-ppt-final-answer",
            payload={
                "legacy_kind": "chat.final_answer",
                "client_turn_id": client_turn_id,
                "text": (
                    "做好了，文件已发给你："
                    "金山办公媒体采访回复要点.pptx，14 页，可编辑。"
                ),
            },
            created_at="2026-09-15T08:43:22Z",
        )
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
        latest_client_turn_id=client_turn_id,
    )

    assert _message_plain_text(restored[-1]) == (
        "做好了，文件已发给你：金山办公媒体采访回复要点.pptx，"
        "14 页，可编辑。"
    )
    assert restored[-1].metadata["canonical_terminal_reply"] is True
    assert restored[-1].metadata["client_turn_id"] == client_turn_id


def test_history_projection_marks_legacy_final_after_file_delivery() -> None:
    """Old final events bind the last post-tool message as the terminal reply."""

    client_turn_id = "user-legacy-ppt-turn"
    memory_messages = legacy_msg_to_chat_messages(
        [
            LegacyMsg(
                name="User",
                role="user",
                content="把这个做成PPT。",
                metadata={"client_turn_id": client_turn_id},
            ),
            LegacyMsg(
                name="Friday",
                role="assistant",
                content="主题名不被识别，我去掉主题参数。",
                metadata={"client_turn_id": client_turn_id},
            ),
            LegacyMsg(
                name="Friday",
                role="assistant",
                content="做好了，文件已发给你：interview.pptx。",
                metadata={"client_turn_id": client_turn_id},
            ),
        ]
    )
    runtime_events = [
        RuntimeEvent(
            id="event-legacy-ppt-delivery",
            seq=187,
            type="tool.call.completed",
            status="completed",
            source="tool",
            turn_id="run-legacy-ppt",
            item_id="call-legacy-ppt-delivery",
            payload={
                "id": "call-legacy-ppt-delivery",
                "name": "send_file_to_user",
                "client_turn_id": client_turn_id,
                "ok": True,
                "output": [
                    {
                        "type": "file",
                        "source": {
                            "type": "url",
                            "url": "file:///tmp/interview.pptx",
                        },
                        "filename": "interview.pptx",
                    }
                ],
            },
        ),
        RuntimeEvent(
            id="event-legacy-ppt-message",
            seq=191,
            type="message.completed",
            status="completed",
            source="assistant",
            turn_id="run-legacy-ppt",
            item_id="provider-legacy-ppt-final:message",
            payload={
                "source_message_id": "provider-legacy-ppt-final:message",
                "client_turn_id": client_turn_id,
                "text": "做好了，文件已发给你：interview.pptx。",
                "message": {
                    "id": "provider-legacy-ppt-final:message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "做好了，文件已发给你：interview.pptx。",
                        }
                    ],
                    "metadata": {"client_turn_id": client_turn_id},
                },
            },
        ),
        RuntimeEvent(
            id="event-legacy-ppt-final-answer",
            seq=192,
            type="item.completed",
            status="completed",
            source="assistant",
            turn_id="run-legacy-ppt",
            item_id="event-legacy-ppt-final-answer",
            payload={
                "legacy_kind": "chat.final_answer",
                "outcome_status": "delivered",
                "delivery_status": "verified",
            },
            created_at="2026-09-15T08:43:22Z",
        )
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
        latest_client_turn_id=client_turn_id,
    )

    assert _message_plain_text(restored[-1]) == (
        "做好了，文件已发给你：interview.pptx。"
    )
    assert restored[-1].metadata["canonical_terminal_reply"] is True


def test_history_projection_terminal_authority_preserves_queued_turn_order() -> None:
    """Finalization must not move an earlier answer past queued guidance."""

    root_turn_id = "client-root-queued"
    second_turn_id = "client-second-queued"
    third_turn_id = "client-third-queued"
    legacy_messages = [
        LegacyMsg(
            name="User",
            role="user",
            content="第一条指令",
            metadata={"client_turn_id": root_turn_id},
        ),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content="第一条回答",
            metadata={"client_turn_id": root_turn_id},
        ),
        LegacyMsg(
            name="User",
            role="user",
            content="第二条指令",
            metadata={"client_turn_id": second_turn_id, "operation": "steer"},
        ),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content="第二条回答",
            metadata={"client_turn_id": second_turn_id},
        ),
        LegacyMsg(
            name="User",
            role="user",
            content="第三条指令",
            metadata={"client_turn_id": third_turn_id, "operation": "steer"},
        ),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content="第三条回答",
            metadata={"client_turn_id": third_turn_id},
        ),
    ]
    source_ids = [
        "user-first",
        "provider-first",
        "user-second",
        "provider-second",
        "user-third",
        "provider-third",
    ]
    for message, source_id in zip(legacy_messages, source_ids, strict=True):
        message.id = source_id
    memory_messages = legacy_msg_to_chat_messages(legacy_messages)

    def assistant_event(
        seq: int,
        source_id: str,
        text: str,
        client_turn_id: str,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            id=f"event-{source_id}",
            seq=seq,
            type="message.completed",
            status="completed",
            source="assistant",
            turn_id="continuous-run",
            item_id=f"{source_id}:message",
            payload={
                "source_message_id": source_id,
                "client_turn_id": client_turn_id,
                "message": {
                    "id": f"{source_id}:message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "metadata": {"client_turn_id": client_turn_id},
                },
            },
        )

    def steer_event(seq: int, client_turn_id: str, text: str) -> RuntimeEvent:
        return RuntimeEvent(
            id=f"steer-{client_turn_id}",
            seq=seq,
            type="runtime.warning",
            status="info",
            source="user",
            turn_id="continuous-run",
            payload={
                "kind": "run_inbox_consumed",
                "client_turn_id": client_turn_id,
                "steer_text": text,
            },
        )

    runtime_events = [
        assistant_event(11, "provider-first", "第一条回答", root_turn_id),
        steer_event(12, second_turn_id, "第二条指令"),
        assistant_event(18, "provider-second", "第二条回答", second_turn_id),
        steer_event(19, third_turn_id, "第三条指令"),
        assistant_event(25, "provider-third", "第三条回答", third_turn_id),
        RuntimeEvent(
            id="continuous-run-finalizer",
            seq=26,
            type="item.completed",
            status="completed",
            source="assistant",
            turn_id="continuous-run",
            payload={
                "legacy_kind": "chat.final_answer",
                "client_turn_id": root_turn_id,
            },
        ),
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
    )

    assert [
        (message.role, _message_plain_text(message))
        for message in restored
        if message.type == "message"
    ] == [
        ("user", "第一条指令"),
        ("assistant", "第一条回答"),
        ("user", "第二条指令"),
        ("assistant", "第二条回答"),
        ("user", "第三条指令"),
        ("assistant", "第三条回答"),
    ]
    first_answer = next(
        message
        for message in restored
        if _message_plain_text(message) == "第一条回答"
    )
    assert first_answer.metadata["canonical_terminal_reply"] is True
    assert (
        first_answer.metadata["canonical_terminal_event_id"]
        == "continuous-run-finalizer"
    )
    assert first_answer.metadata.get("runtime_event_id") != (
        "continuous-run-finalizer"
    )


def test_history_projection_does_not_promote_pre_tool_progress_to_terminal() -> None:
    """A legacy completion cannot turn pre-delivery narration into an answer."""

    client_turn_id = "user-incomplete-ppt-turn"
    memory_messages = legacy_msg_to_chat_messages(
        [
            LegacyMsg(
                name="User",
                role="user",
                content="把这个做成PPT。",
                metadata={"client_turn_id": client_turn_id},
            ),
            LegacyMsg(
                name="Friday",
                role="assistant",
                content="我先读取原文并开始生成。",
                metadata={"client_turn_id": client_turn_id},
            ),
        ]
    )
    runtime_events = [
        RuntimeEvent(
            id="event-incomplete-progress",
            seq=1,
            type="message.completed",
            status="completed",
            source="assistant",
            turn_id="run-incomplete-ppt",
            item_id="provider-incomplete-progress:message",
            payload={
                "source_message_id": "provider-incomplete-progress:message",
                "client_turn_id": client_turn_id,
                "message": {
                    "id": "provider-incomplete-progress:message",
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "我先读取原文并开始生成。"}
                    ],
                    "metadata": {"client_turn_id": client_turn_id},
                },
            },
        ),
        RuntimeEvent(
            id="event-incomplete-tool",
            seq=2,
            type="tool.call.completed",
            status="completed",
            source="tool",
            turn_id="run-incomplete-ppt",
            item_id="call-incomplete-ppt",
            payload={
                "id": "call-incomplete-ppt",
                "name": "create_presentation",
                "client_turn_id": client_turn_id,
                "ok": False,
                "output": [{"type": "text", "text": "generation failed"}],
            },
        ),
        RuntimeEvent(
            id="event-incomplete-final",
            seq=3,
            type="item.completed",
            status="completed",
            source="assistant",
            turn_id="run-incomplete-ppt",
            payload={
                "legacy_kind": "chat.final_answer",
                "outcome_status": "delivered",
                "delivery_status": "not_delivered",
            },
        ),
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
        latest_client_turn_id=client_turn_id,
    )

    assert not any(
        (getattr(message, "metadata", None) or {}).get(
            "canonical_terminal_reply"
        )
        for message in restored
    )


def test_runtime_event_replay_preserves_ordinary_assistant_text() -> None:
    response = RuntimeEventsResponse(
        chat_id="chat-ordinary-text",
        events=[
            {
                "id": "assistant-text-delta",
                "seq": 1,
                "type": "message.delta",
                "status": "running",
                "source": "assistant",
                "item_id": "assistant-message",
                "payload": {
                    "delta": "正在整理结论。",
                    "content_type": "text",
                },
                "created_at": "2026-08-24T10:00:00Z",
            },
            {
                "id": "assistant-text-completed",
                "seq": 2,
                "type": "message.completed",
                "status": "completed",
                "source": "assistant",
                "item_id": "assistant-message",
                "payload": {
                    "message": {
                        "id": "assistant-message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "结论已完成。"}],
                    }
                },
                "created_at": "2026-08-24T10:00:01Z",
            },
        ],
    )

    projected = _project_public_runtime_event_response(response)

    assert projected.events[0].payload["delta"] == "正在整理结论。"
    assert projected.events[1].payload["message"]["content"][0]["text"] == "结论已完成。"


def test_paged_runtime_event_projection_does_not_replace_provider_reasoning() -> None:
    projector_response = RuntimeEventsResponse(
        chat_id="chat-paged-progress",
        events=[
            {
                "id": "search-completed",
                "seq": 1,
                "type": "tool.call.completed",
                "status": "completed",
                "source": "tool",
                "item_id": "search-1",
                "payload": {
                    "id": "search-1",
                    "name": "web_search",
                    "ok": True,
                },
                "created_at": "2026-08-24T10:00:00Z",
            }
        ],
    )
    page = RuntimeEventsResponse(
        chat_id="chat-paged-progress",
        events=[
            {
                "id": "reasoning-after-cursor",
                "seq": 2,
                "type": "message.delta",
                "status": "running",
                "source": "assistant",
                "item_id": "provider-message",
                "payload": {"delta": "private", "content_type": "thinking"},
                "created_at": "2026-08-24T10:00:01Z",
            }
        ],
    )

    projector = PublicReasoningProjector()
    _project_public_runtime_event_response(projector_response, projector)
    projected_page = _project_public_runtime_event_response(page, projector)

    assert projected_page.events[0].payload["delta"] == "private"


def test_history_projection_restores_tool_card_and_generated_image() -> None:
    memory_messages = legacy_msg_to_chat_messages(
        [
            LegacyMsg(
                name="User",
                role="user",
                content="画一张图",
                timestamp="2026-07-28T09:00:00Z",
            ),
            LegacyMsg(
                name="Friday",
                role="assistant",
                content="图片已经生成。",
                timestamp="2026-07-28T09:00:04Z",
            ),
        ]
    )
    runtime_events = [
        RuntimeEvent(
            id="event-call",
            seq=1,
            type="tool.call.started",
            status="running",
            source="tool",
            session_id="session-1",
            turn_id="run-1",
            item_id="call-image",
            summary="tool call: generate_image",
            payload={
                "id": "call-image",
                "name": "generate_image",
                "tool_name": "generate_image",
            },
            created_at="2026-07-28T09:00:01Z",
        ),
        RuntimeEvent(
            id="event-result",
            seq=2,
            type="tool.call.completed",
            status="completed",
            source="tool",
            session_id="session-1",
            turn_id="run-1",
            item_id="call-image",
            summary="generated",
            payload={
                "id": "call-image",
                "name": "generate_image",
                "tool_name": "generate_image",
                "ok": True,
                "output": [
                    {
                        "type": "image",
                        "image_url": "/tmp/generated.png",
                        "source": {
                            "type": "url",
                            "url": "/tmp/generated.png",
                        },
                    }
                ],
            },
            created_at="2026-07-28T09:00:03Z",
        ),
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
    )

    assert [message.type for message in restored] == [
        "message",
        "plugin_call",
        "message",
        "plugin_call_output",
        "message",
    ]
    assert restored[1].content[0].data["call_id"] == "call-image"
    assert restored[2].content[0].image_url == "/tmp/generated.png"
    assert restored[3].content[0].data["call_id"] == "call-image"


def test_history_projection_deduplicates_tools_already_in_memory() -> None:
    memory_messages = legacy_msg_to_chat_messages(
        LegacyMsg(
            name="Friday",
            role="assistant",
            content=[
                {
                    "type": "tool_use",
                    "id": "call-existing",
                    "name": "generate_image",
                    "input": {},
                },
                {
                    "type": "tool_result",
                    "id": "call-existing",
                    "name": "generate_image",
                    "output": [{"type": "text", "text": "done"}],
                },
            ],
            timestamp="2026-07-28T09:00:01Z",
        )
    )
    runtime_events = [
        RuntimeEvent(
            id="event-call",
            seq=1,
            type="tool.call.started",
            status="running",
            source="tool",
            item_id="call-existing",
            payload={
                "id": "call-existing",
                "name": "generate_image",
            },
            created_at="2026-07-28T09:00:01Z",
        ),
        RuntimeEvent(
            id="event-result",
            seq=2,
            type="tool.call.completed",
            status="completed",
            source="tool",
            item_id="call-existing",
            payload={
                "id": "call-existing",
                "name": "generate_image",
                "ok": True,
                "output": [{"type": "text", "text": "done"}],
            },
            created_at="2026-07-28T09:00:02Z",
        ),
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
    )

    assert [message.type for message in restored].count("plugin_call") == 1
    assert [message.type for message in restored].count("plugin_call_output") == 1


def test_history_projection_keeps_tool_events_below_their_user_turn_anchor() -> None:
    """Late memory persistence must not put the prompt inside its own run."""

    client_turn_id = "user-1787437301571-neb8xdmj"
    memory_messages = legacy_msg_to_chat_messages(
        [
            LegacyMsg(
                name="User",
                role="user",
                content="莫言和余华打架谁会赢？",
                # The prompt is persisted after execution has already emitted
                # the tool-start event. Wall-clock sorting alone is therefore
                # not a valid transcript order.
                timestamp="2026-08-23 06:21:57.670",
            ),
            LegacyMsg(
                name="Friday",
                role="assistant",
                content="综合结论。",
                metadata={"client_turn_id": client_turn_id},
                timestamp="2026-08-23 06:22:46.814",
            ),
        ]
    )
    runtime_events = [
        RuntimeEvent(
            id="event-collaboration-started",
            seq=5,
            type="tool.call.started",
            status="running",
            source="tool",
            turn_id="run-collaboration",
            item_id="call-collaboration",
            payload={
                "id": "call-collaboration",
                "name": "collaborate_with_agents",
                "client_turn_id": client_turn_id,
            },
            # This is 670 ms before the user row is persisted.
            created_at="2026-08-22T22:21:57Z",
        ),
        RuntimeEvent(
            id="event-collaboration-completed",
            seq=25,
            type="tool.call.completed",
            status="completed",
            source="tool",
            turn_id="run-collaboration",
            item_id="call-collaboration",
            payload={
                "id": "call-collaboration",
                "name": "collaborate_with_agents",
                "client_turn_id": client_turn_id,
                "ok": True,
                "output": [{"type": "text", "text": "done"}],
            },
            created_at="2026-08-22T22:22:24Z",
        ),
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
    )

    assert [message.type for message in restored] == [
        "message",
        "plugin_call",
        "plugin_call_output",
        "message",
    ]
    assert restored[0].role == "user"


def test_history_projection_omits_legacy_reasoning_without_runtime_facts() -> None:
    memory_messages = legacy_msg_to_chat_messages(
        [
            LegacyMsg(name="User", role="user", content="继续"),
            LegacyMsg(
                name="Friday",
                role="assistant",
                content=[
                    {
                        "type": "thinking",
                        "thinking": "人格规则与系统配置的同义改写",
                    },
                    {"type": "text", "text": "正常回答"},
                ],
            ),
        ]
    )

    restored = _merge_persisted_runtime_control_messages(memory_messages, [])

    assert [message.type for message in restored] == ["message", "message"]
    assert "人格规则" not in " ".join(
        content.text
        for message in restored
        for content in (message.content or [])
        if getattr(content, "text", None)
    )
    assert restored[-1].role == "assistant"


def test_history_projection_places_consumed_steer_after_prior_runtime_output() -> None:
    """A late user steer must not jump above the process it interrupted."""

    root_turn_id = "client-root"
    steer_turn_id = "client-steer"
    memory_messages = legacy_msg_to_chat_messages(
        [
            LegacyMsg(
                name="User",
                role="user",
                content="先整理这个文档",
                timestamp="2026-08-24T08:00:00Z",
            ),
            # Legacy memory can flush accepted steer input before the prior
            # tool rows and answer have completed.
            LegacyMsg(
                name="User",
                role="user",
                content="把客户偏好也列出来",
                metadata={"client_turn_id": steer_turn_id, "operation": "steer"},
                timestamp="2026-08-24T08:00:03Z",
            ),
            LegacyMsg(
                name="Friday",
                role="assistant",
                content="我已经把需要的字段记下了。",
                metadata={"client_turn_id": root_turn_id},
                timestamp="2026-08-24T08:00:08Z",
            ),
        ]
    )
    runtime_events = [
        RuntimeEvent(
            id="tool-start",
            seq=2,
            type="tool.call.started",
            status="running",
            source="tool",
            item_id="read-sheet",
            payload={
                "id": "read-sheet",
                "name": "read_spreadsheet",
                "client_turn_id": root_turn_id,
            },
            created_at="2026-08-24T08:00:01Z",
        ),
        RuntimeEvent(
            id="steer-consumed",
            seq=3,
            type="runtime.warning",
            status="info",
            source="user",
            item_id="",
            payload={
                "kind": "run_inbox_consumed",
                "client_turn_id": steer_turn_id,
                "steer_text": "把客户偏好也列出来",
            },
            created_at="2026-08-24T08:00:03Z",
        ),
        RuntimeEvent(
            id="tool-finish",
            seq=4,
            type="tool.call.completed",
            status="completed",
            source="tool",
            item_id="read-sheet",
            payload={
                "id": "read-sheet",
                "name": "read_spreadsheet",
                "client_turn_id": root_turn_id,
                "ok": True,
                "output": [{"type": "text", "text": "已读取"}],
            },
            created_at="2026-08-24T08:00:04Z",
        ),
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
    )

    steer_index = next(
        index
        for index, message in enumerate(restored)
        if getattr(message, "role", "") == "user"
        and "客户偏好" in str(getattr(message, "content", ""))
    )
    tool_start_index = next(
        index
        for index, message in enumerate(restored)
        if (
            getattr(message, "metadata", {}).get("original_id")
            == "runtime-control:tool-start"
        )
    )
    assert tool_start_index < steer_index


def test_history_projection_preserves_intra_turn_runtime_causality() -> None:
    """Thinking, narration, tools and final answer keep ledger sequence."""

    client_turn_id = "turn-causal-order"
    legacy_messages = [
        LegacyMsg(
            name="User",
            role="user",
            content="请核对这两篇稿子",
            timestamp="2026-08-23T09:00:00Z",
        ),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content=[
                {"type": "thinking", "thinking": "先看原文。"},
                {"type": "text", "text": "我先核对一下。"},
            ],
            metadata={"client_turn_id": client_turn_id},
            timestamp="2026-08-23T09:00:01Z",
        ),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content=[{"type": "thinking", "thinking": "再核对型号和日期。"}],
            metadata={"client_turn_id": client_turn_id},
            timestamp="2026-08-23T09:00:04Z",
        ),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content="结论如下。",
            metadata={"client_turn_id": client_turn_id},
            timestamp="2026-08-23T09:00:06Z",
        ),
    ]
    for message, message_id in zip(
        legacy_messages,
        ("user-message", "provider-1", "provider-2", "provider-final"),
        strict=True,
    ):
        message.id = message_id
    memory_messages = legacy_msg_to_chat_messages(legacy_messages)

    def message_event(seq: int, item_id: str, block_type: str, text: str) -> RuntimeEvent:
        return RuntimeEvent(
            id=f"message-{seq}",
            seq=seq,
            type="message.completed",
            status="completed",
            source="assistant",
            item_id=item_id,
            payload={
                # Native runtime rows retain the channel suffix here.  History
                # restoration must still bind the row to the base provider
                # message identity used by the legacy chat projection.
                "source_message_id": item_id,
                "message": {
                    "id": item_id,
                    "role": "assistant",
                    "content": [{"type": block_type, "text": text}],
                    "metadata": {"client_turn_id": client_turn_id},
                },
            },
            created_at=f"2026-08-23T09:00:{seq:02d}Z",
        )

    runtime_events = [
        message_event(4, "provider-1:reasoning", "thinking", "先看原文。"),
        message_event(5, "provider-1:message", "text", "我先核对一下。"),
        RuntimeEvent(
            id="tool-started",
            seq=6,
            type="tool.call.started",
            status="running",
            source="tool",
            item_id="web-search",
            payload={
                "id": "web-search",
                "name": "web_search",
                "client_turn_id": client_turn_id,
            },
            created_at="2026-08-23T09:00:02Z",
        ),
        RuntimeEvent(
            id="tool-completed",
            seq=7,
            type="tool.call.completed",
            status="completed",
            source="tool",
            item_id="web-search",
            payload={
                "id": "web-search",
                "name": "web_search",
                "client_turn_id": client_turn_id,
                "ok": True,
                "output": [{"type": "text", "text": "done"}],
            },
            created_at="2026-08-23T09:00:03Z",
        ),
        message_event(
            8,
            "provider-2:reasoning",
            "thinking",
            "再核对型号和日期。",
        ),
        message_event(9, "provider-final:message", "text", "结论如下。"),
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
    )

    assert [message.type for message in restored] == [
        "message",
        "reasoning",
        "message",
        "plugin_call",
        "plugin_call_output",
        "reasoning",
        "message",
    ]
    assert restored[0].role == "user"
    reasoning_texts = [
        message.content[0].text
        for message in restored
        if message.type == "reasoning"
    ]
    assert reasoning_texts == [
        "先看原文。",
        "再核对型号和日期。",
    ]
    serialized = " ".join(reasoning_texts)
    assert "先看原文" in serialized
    assert "再核对型号和日期" in serialized


def test_history_projection_restores_completed_interim_narration_missing_from_memory() -> None:
    """A durable ordinary message must survive hydration and restart.

    The direct stream can display a completed progress sentence before a tool
    call even when legacy memory only flushes the final answer.  History is an
    additive projection of the runtime ledger, so that already-visible sentence
    must not disappear when the UI hydrates from the durable transcript.
    """

    client_turn_id = "turn-completed-interim"
    legacy_messages = [
        LegacyMsg(
            name="User",
            role="user",
            content="帮我核对这份材料",
            timestamp="2026-08-29T11:00:00Z",
        ),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content="核对完成，结论如下。",
            metadata={"client_turn_id": client_turn_id},
            timestamp="2026-08-29T11:00:05Z",
        ),
    ]
    legacy_messages[0].id = "user-message"
    legacy_messages[1].id = "provider-final"
    memory_messages = legacy_msg_to_chat_messages(legacy_messages)

    def completed_message(
        seq: int,
        source_id: str,
        text: str,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            id=f"message-{seq}",
            seq=seq,
            type="message.completed",
            status="completed",
            source="assistant",
            turn_id="runtime-turn",
            item_id=f"{source_id}:message",
            payload={
                "source_message_id": source_id,
                "client_turn_id": client_turn_id,
                "message": {
                    "id": f"{source_id}:message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "metadata": {"client_turn_id": client_turn_id},
                },
            },
            created_at=f"2026-08-29T11:00:0{seq}Z",
        )

    runtime_events = [
        completed_message(1, "provider-progress", "我先读取原文并核对数据。"),
        RuntimeEvent(
            id="tool-started",
            seq=2,
            type="tool.call.started",
            status="running",
            source="tool",
            turn_id="runtime-turn",
            item_id="read-source",
            payload={
                "id": "read-source",
                "name": "read_file",
                "client_turn_id": client_turn_id,
            },
            created_at="2026-08-29T11:00:02Z",
        ),
        RuntimeEvent(
            id="tool-completed",
            seq=3,
            type="tool.call.completed",
            status="completed",
            source="tool",
            turn_id="runtime-turn",
            item_id="read-source",
            payload={
                "id": "read-source",
                "name": "read_file",
                "client_turn_id": client_turn_id,
                "ok": True,
                "output": [{"type": "text", "text": "done"}],
            },
            created_at="2026-08-29T11:00:03Z",
        ),
        completed_message(4, "provider-final", "核对完成，结论如下。"),
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
    )

    assert [message.type for message in restored] == [
        "message",
        "message",
        "plugin_call",
        "plugin_call_output",
        "message",
    ]
    restored_text = [
        message.content[0].text
        for message in restored
        if message.type == "message" and message.role == "assistant"
    ]
    assert restored_text == [
        "我先读取原文并核对数据。",
        "核对完成，结论如下。",
    ]


def test_history_projection_keeps_multiple_progress_revisions_for_one_source() -> None:
    client_turn_id = "turn-reused-source"
    legacy_messages = [
        LegacyMsg(name="User", role="user", content="继续核对"),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content=[{"type": "thinking", "thinking": "private before"}],
            metadata={"client_turn_id": client_turn_id},
        ),
        LegacyMsg(
            name="Friday",
            role="assistant",
            content=[{"type": "thinking", "thinking": "private after"}],
            metadata={"client_turn_id": client_turn_id},
        ),
    ]
    legacy_messages[1].id = "provider-reused"
    legacy_messages[2].id = "provider-reused"
    memory_messages = legacy_msg_to_chat_messages(legacy_messages)

    def reasoning_event(seq: int, text: str) -> RuntimeEvent:
        return RuntimeEvent(
            id=f"reasoning-{seq}",
            seq=seq,
            type="message.completed",
            status="completed",
            source="assistant",
            item_id="provider-reused:reasoning",
            payload={
                "source_message_id": "provider-reused",
                "message": {
                    "id": "provider-reused:reasoning",
                    "role": "assistant",
                    "content": [{"type": "thinking", "text": text}],
                },
            },
        )

    runtime_events = [
        reasoning_event(1, "private before"),
        RuntimeEvent(
            id="search-completed",
            seq=2,
            type="tool.call.completed",
            status="completed",
            source="tool",
            item_id="search-1",
            payload={
                "id": "search-1",
                "name": "web_search",
                "ok": True,
                "output": "done",
            },
        ),
        reasoning_event(3, "private after"),
    ]

    restored = _merge_persisted_runtime_control_messages(
        memory_messages,
        runtime_events,
    )
    reasoning = [message for message in restored if message.type == "reasoning"]

    assert [message.content[0].text for message in reasoning] == [
        "private before",
        "private after",
    ]
    assert "private" in " ".join(
        message.content[0].text for message in reasoning
    )
