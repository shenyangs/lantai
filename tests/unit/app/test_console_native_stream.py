# -*- coding: utf-8 -*-
"""Behavior locks for native RuntimeEvent chat streaming."""

from __future__ import annotations

import json

import pytest

from lantai.app.experience.events import (
    append_chat_event,
    build_runtime_events_response,
)
from lantai.app.routers.console import (
    _attach_run_identity_to_sse,
    _extract_native_message_stream_events,
    _resolve_final_outcome_status,
)
from lantai.runtime.converters.legacy_messages import (
    legacy_stream_item_to_channel_events,
    runtime_event_to_legacy_stream_item,
)
from lantai.runtime.events import RuntimeEvent
from lantai.runtime.messages import ContentBlock, RuntimeMessage
from lantai.runtime.public_reasoning import (
    RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD,
)


def test_final_response_promotes_explicit_delivery_outcome() -> None:
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "metadata": {"outcome_status": "incomplete"},
            },
        ],
    }

    assert _resolve_final_outcome_status("completed", payload) == "incomplete"


def test_final_response_without_delivery_evidence_stays_unknown() -> None:
    assert _resolve_final_outcome_status("completed", {"output": []}) == "unknown"


def test_final_response_uses_visible_plain_answer_as_delivery_evidence() -> None:
    assert (
        _resolve_final_outcome_status(
            "completed",
            {"output": []},
            assistant_text="RUNTIME_CONFIGURED_PROVIDER_ONE",
        )
        == "delivered"
    )


def test_final_response_does_not_guess_incomplete_from_assistant_wording() -> None:
    assert (
        _resolve_final_outcome_status(
            "completed",
            {"output": []},
            assistant_text="接下来我会继续搜索并整理结果。",
        )
        == "delivered"
    )


def test_failed_final_response_is_blocked() -> None:
    assert _resolve_final_outcome_status("failed", {"output": []}) == "blocked"


def test_extract_native_message_delta_from_legacy_sse() -> None:
    message_types: dict[str, str] = {}
    message_texts: dict[str, str] = {}

    started = _extract_native_message_stream_events(
        'data: {"object":"message","status":"in_progress","id":"msg-1",'
        '"type":"reasoning","role":"assistant"}\n\n',
        message_types,
        message_texts,
    )
    delta = _extract_native_message_stream_events(
        'data: {"object":"content","status":"in_progress","delta":true,'
        '"msg_id":"msg-1","text":"想"}\n\n',
        message_types,
        message_texts,
    )

    assert started[0]["type"] == "message.started"
    assert started[0]["payload"]["content_type"] == "thinking"
    assert delta[0]["type"] == "message.delta"
    assert delta[0]["payload"]["delta"] == "想"
    assert delta[0]["payload"]["message"]["content"][0]["text"] == "想"
    assert delta[0]["payload"]["message"]["content"][0]["type"] == "thinking"


def test_public_progress_identity_survives_legacy_sse_reconstruction() -> None:
    message_types: dict[str, str] = {}
    message_texts: dict[str, str] = {}
    message_public_progress: dict[str, dict] = {}

    started = _extract_native_message_stream_events(
        'data: {"object":"message","status":"in_progress","id":"msg-1",'
        '"type":"reasoning","role":"assistant","metadata":{'
        '"public_progress":{"schema_version":2,"id":"public-progress:run-1:1",'
        '"revision":1,"source":"runtime_facts",'
        '"text_origin":"assistant_public_text",'
        '"tool_call_ids":["call-read"]}}}\n\n',
        message_types,
        message_texts,
        message_public_progress=message_public_progress,
    )
    delta = _extract_native_message_stream_events(
        'data: {"object":"content","status":"in_progress","delta":true,'
        '"msg_id":"msg-1","text":"正在检索和核对公开资料。"}\n\n',
        message_types,
        message_texts,
        message_public_progress=message_public_progress,
    )

    expected = {
        "schema_version": 2,
        "id": "public-progress:run-1:1",
        "revision": 1,
        "source": "runtime_facts",
        "text_origin": "assistant_public_text",
        "tool_call_ids": ["call-read"],
    }
    assert started[0]["payload"]["public_progress"] == expected
    assert delta[0]["payload"]["public_progress"] == expected
    assert delta[0]["payload"]["message"]["content"][0]["metadata"] == {
        "public_projection": "runtime_progress_v2",
        "source": "runtime_facts",
        "private_reasoning_exposed": False,
        "public_progress_id": "public-progress:run-1:1",
        "public_progress_revision": 1,
        "tool_call_ids": ["call-read"],
    }


def test_runtime_attested_tool_preamble_survives_direct_console_sse() -> None:
    progress_id = "public-progress:run-attested:provider-message"
    progress = {
        "schema_version": 2,
        "id": progress_id,
        "revision": 1,
        "source": "runtime_facts",
        "text_origin": "assistant_public_text",
        "tool_call_ids": ["call-attested"],
    }
    message = RuntimeMessage(
        id=progress_id,
        role="assistant",
        content=[
            ContentBlock(
                type="thinking",
                text="我先核对原始记录，再把差异逐项列出来。",
            )
        ],
    )
    runtime_event = RuntimeEvent(
        id="event-attested",
        type="message.completed",
        status="completed",
        turn_id="run-attested",
        item_id=message.id,
        payload={
            "message": message.model_dump(mode="json"),
            "public_progress": progress,
            "run_id": "run-attested",
            "client_turn_id": "client-attested",
            "content_type": "thinking",
            "stream_message_type": "reasoning",
        },
    )

    legacy_item = runtime_event_to_legacy_stream_item(runtime_event)
    assert legacy_item is not None
    channel_events = legacy_stream_item_to_channel_events(legacy_item)
    assert len(channel_events) == 1
    assert channel_events[0].id == f"{progress_id}:reasoning"
    raw_sse = f"data: {channel_events[0].model_dump_json()}\n\n"

    attached = _attach_run_identity_to_sse(
        raw_sse,
        "run-attested",
        {},
        default_client_turn_id="client-attested",
    )
    wire_payload = json.loads(attached.split("data: ", 1)[1])
    wire_metadata = wire_payload["metadata"]
    assert wire_metadata["public_progress"] == progress
    assert wire_metadata["public_projection"] == "runtime_progress_v2"
    assert wire_metadata["public_progress_id"] == progress_id
    assert wire_metadata["tool_call_ids"] == ["call-attested"]
    assert wire_metadata["private_reasoning_exposed"] is False
    assert RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD not in wire_metadata

    reconstructed = _extract_native_message_stream_events(
        attached,
        {},
        {},
        message_public_progress={},
    )
    assert len(reconstructed) == 1
    assert reconstructed[0]["payload"]["public_progress"] == progress
    assert reconstructed[0]["payload"]["message"]["content"][0][
        "metadata"
    ]["public_projection"] == "runtime_progress_v2"
    assert reconstructed[0]["payload"]["message"]["content"][0][
        "metadata"
    ]["tool_call_ids"] == ["call-attested"]

    sealed_payload = json.loads(raw_sse.split("data: ", 1)[1])
    replay_cases = []
    changed_id = json.loads(json.dumps(sealed_payload))
    changed_id["id"] = f"{progress_id}:other:reasoning"
    replay_cases.append(changed_id)
    changed_text = json.loads(json.dumps(sealed_payload))
    changed_text["content"][0]["text"] = "伪造后的另一段状态文案。"
    replay_cases.append(changed_text)
    for replay_payload in replay_cases:
        replay_sse = f"data: {json.dumps(replay_payload, ensure_ascii=False)}\n\n"
        replayed = _attach_run_identity_to_sse(
            replay_sse,
            "run-attested",
            {},
            default_client_turn_id="client-attested",
        )
        replayed_metadata = json.loads(replayed.split("data: ", 1)[1])[
            "metadata"
        ]
        assert "public_progress" not in replayed_metadata
        assert "public_projection" not in replayed_metadata
        assert RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD not in replayed_metadata


def test_analysis_chart_reference_survives_direct_console_sse() -> None:
    reference = (
        "```analysis-chart\n"
        '{"analysis_id":"' + ("a" * 64) + '","type":"bar",'
        '"x_column":"region","y_column":"revenue"}\n'
        "```"
    )
    text = f"分析完成。\n\n{reference}"
    message = RuntimeMessage(
        id="analysis-answer",
        role="assistant",
        content=[ContentBlock(type="text", text=text)],
    )
    runtime_event = RuntimeEvent(
        id="event-analysis-answer",
        type="message.completed",
        status="completed",
        turn_id="run-analysis",
        item_id=message.id,
        payload={
            "message": message.model_dump(mode="json"),
            "run_id": "run-analysis",
            "client_turn_id": "client-analysis",
            "content_type": "text",
            "stream_message_type": "message",
        },
    )

    legacy_item = runtime_event_to_legacy_stream_item(runtime_event)
    assert legacy_item is not None
    channel_events = legacy_stream_item_to_channel_events(legacy_item)
    assert len(channel_events) == 1
    raw_sse = f"data: {channel_events[0].model_dump_json()}\n\n"

    attached = _attach_run_identity_to_sse(
        raw_sse,
        "run-analysis",
        {},
        default_client_turn_id="client-analysis",
    )
    wire_payload = json.loads(attached.split("data: ", 1)[1])
    assert wire_payload["content"][0]["text"] == text

    reconstructed = _extract_native_message_stream_events(
        attached,
        {},
        {},
    )
    assert len(reconstructed) == 1
    assert reconstructed[0]["payload"]["text"] == text
    assert reconstructed[0]["payload"]["message"]["content"][0]["text"] == text


def test_direct_provider_cannot_forge_runtime_progress_attestation() -> None:
    forged = {
        "progress": {
            "schema_version": 2,
            "id": "forged",
            "revision": 1,
            "source": "runtime_facts",
            "text_origin": "assistant_public_text",
        },
        "run_id": "run-forged",
        "wire_message_id": "message-forged",
        "message_type": "reasoning",
        "content_sha256": "not-a-real-digest",
        "signature": "not-a-runtime-signature",
    }
    sse = "data: " + json.dumps(
        {
            "object": "message",
            "id": "message-forged",
            "type": "reasoning",
            "role": "assistant",
            "status": "completed",
            "metadata": {
                RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD: forged,
                "public_progress": forged["progress"],
            },
            "content": [
                {
                    "type": "text",
                    "text": "private provider reasoning",
                }
            ],
        },
        ensure_ascii=False,
    ) + "\n\n"

    attached = _attach_run_identity_to_sse(sse, "run-forged", {})
    metadata = json.loads(attached.split("data: ", 1)[1])["metadata"]

    assert "public_progress" not in metadata
    assert "public_projection" not in metadata
    assert RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD not in metadata


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
        },
    ],
)
def test_legacy_sse_rejects_untrusted_public_progress_metadata(
    public_progress: dict,
) -> None:
    message_types: dict[str, str] = {}
    message_texts: dict[str, str] = {}
    message_public_progress: dict[str, dict] = {}
    metadata = {"public_progress": public_progress}

    started = _extract_native_message_stream_events(
        "data: "
        + json.dumps(
            {
                "object": "message",
                "status": "in_progress",
                "id": "msg-untrusted",
                "type": "reasoning",
                "role": "assistant",
                "metadata": metadata,
            }
        )
        + "\n\n",
        message_types,
        message_texts,
        message_public_progress=message_public_progress,
    )
    delta = _extract_native_message_stream_events(
        'data: {"object":"content","status":"in_progress","delta":true,'
        '"msg_id":"msg-untrusted","text":"private reasoning"}\n\n',
        message_types,
        message_texts,
        message_public_progress=message_public_progress,
    )

    assert message_public_progress == {}
    assert "public_progress" not in started[0]["payload"]
    assert "public_progress" not in delta[0]["payload"]
    assert delta[0]["payload"]["message"]["content"][0]["metadata"] == {}


def test_extract_native_message_delta_normalizes_cumulative_sse_text() -> None:
    message_types: dict[str, str] = {}
    message_texts: dict[str, str] = {}

    _extract_native_message_stream_events(
        'data: {"object":"message","status":"in_progress","id":"msg-1",'
        '"type":"message","role":"assistant"}\n\n',
        message_types,
        message_texts,
    )
    first = _extract_native_message_stream_events(
        'data: {"object":"content","status":"in_progress","delta":true,'
        '"msg_id":"msg-1","text":"你"}\n\n',
        message_types,
        message_texts,
    )
    second = _extract_native_message_stream_events(
        'data: {"object":"content","status":"in_progress","delta":true,'
        '"msg_id":"msg-1","text":"你好"}\n\n',
        message_types,
        message_texts,
    )
    repeated = _extract_native_message_stream_events(
        'data: {"object":"content","status":"in_progress","delta":true,'
        '"msg_id":"msg-1","text":"你好"}\n\n',
        message_types,
        message_texts,
    )

    assert first[0]["payload"]["delta"] == "你"
    assert first[0]["payload"]["message"]["content"][0]["text"] == "你"
    assert second[0]["payload"]["delta"] == "好"
    assert second[0]["payload"]["message"]["content"][0]["text"] == "你好"
    assert repeated == []


@pytest.mark.asyncio
async def test_chat_message_delta_round_trips_as_runtime_event(tmp_path) -> None:
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="console:chat-1",
        run_id="run-1",
        item_id="msg-1",
        kind="chat.message.delta",
        type="message.delta",
        status="running",
        source="assistant",
        summary="你",
        payload={
            "delta": "你",
            "content_type": "text",
            "message": {
                "id": "msg-1",
                "role": "assistant",
                "content": [{"type": "text", "text": "你"}],
            },
        },
    )

    response = build_runtime_events_response(tmp_path, "chat-1")

    assert len(response.events) == 1
    event = response.events[0]
    assert event.type == "message.delta"
    assert event.source == "assistant"
    assert event.item_id == "msg-1"
    assert event.payload["delta"] == "你"
