# -*- coding: utf-8 -*-
"""Behavior locks for the direct Console SSE -> RuntimeEvent projection."""

import json

from lantai.app.routers.console import _extract_native_message_stream_events


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def test_tool_protocol_message_frames_do_not_complete_assistant_replies() -> None:
    message_types: dict[str, str] = {}
    message_texts: dict[str, str] = {}

    for message_type in ("plugin_call", "plugin_call_output"):
        events = _extract_native_message_stream_events(
            _sse(
                {
                    "object": "message",
                    "id": f"frame-{message_type}",
                    "type": message_type,
                    "status": "completed",
                    "content": [],
                }
            ),
            message_types,
            message_texts,
        )
        assert events == []


def test_assistant_message_frame_still_projects_once() -> None:
    events = _extract_native_message_stream_events(
        _sse(
            {
                "object": "message",
                "id": "answer-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "text", "text": "done"}],
            }
        ),
        {},
        {},
    )

    assert len(events) == 1
    assert events[0]["type"] == "message.completed"
    assert events[0]["summary"] == "done"
