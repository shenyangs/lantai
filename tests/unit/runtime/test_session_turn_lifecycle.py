# -*- coding: utf-8 -*-
"""Behavior contracts for one Native session-turn lifecycle."""

import pytest

from lantai.runtime.models import ModelMessage
from lantai.runtime.session_turn_lifecycle import (
    SessionTurnAlreadyStarted,
    SessionTurnAlreadySettled,
    SessionTurnLifecycle,
    post_answer_admission,
)


def test_partial_delivery_failure_has_one_authoritative_terminal_event() -> (
    None
):
    lifecycle = SessionTurnLifecycle(
        session_id="session-1",
        turn_id="turn-1",
    )

    events = lifecycle.partial_delivery_failure(
        seq=4,
        failure_summary="follow-up generation failed",
        error_type="RuntimeError",
    )

    assert [event.seq for event in events] == [4, 5]
    assert [event.type for event in events] == [
        "runtime.warning",
        "turn.completed",
    ]
    assert events[1].payload["outcome_status"] == "incomplete"
    assert events[1].payload["details"] == {"partial_delivery": True}
    with pytest.raises(SessionTurnAlreadySettled):
        lifecycle.complete(seq=6)


def test_resume_restores_valid_checkpoint_before_current_input() -> None:
    lifecycle = SessionTurnLifecycle(
        session_id="session-1",
        turn_id="turn-1",
    )
    current = [ModelMessage.text("user", "current")]
    previous = ModelMessage.text("assistant", "previous")

    restored = lifecycle.restore_model_messages(
        current,
        attempt_reason="resume",
        checkpoint_metadata={
            "compressed_context": [
                previous.model_dump_json(),
                "{malformed",
            ],
        },
    )

    assert [message.text_content() for message in restored] == [
        "previous",
        "current",
    ]
    assert [message.text_content() for message in current] == ["current"]


def test_resume_stitches_overlapping_history_by_message_identity() -> None:
    lifecycle = SessionTurnLifecycle(
        session_id="session-1",
        turn_id="turn-1",
    )
    current = [ModelMessage.text("user", "run it", id="user-1")]
    checkpoint_user = ModelMessage.text("user", "run it", id="user-1")
    checkpoint_assistant = ModelMessage(
        id="assistant-1",
        role="assistant",
        content=[
            {
                "type": "tool_call",
                "data": {
                    "id": "call-1",
                    "name": "write_file",
                    "arguments": {"path": "report.md"},
                },
            }
        ],
    )
    checkpoint_result = ModelMessage(
        id="call-1",
        role="tool",
        tool_call_id="call-1",
        content=[
            {
                "type": "tool_result",
                "data": {
                    "id": "call-1",
                    "name": "write_file",
                    "output": [{"type": "text", "text": "done"}],
                },
            }
        ],
    )

    restored = lifecycle.restore_model_messages(
        current,
        attempt_reason="resume",
        checkpoint_metadata={
            "compressed_context": [
                checkpoint_user.model_dump_json(),
                checkpoint_assistant.model_dump_json(),
                checkpoint_result.model_dump_json(),
            ],
        },
    )

    assert [(message.role, message.id) for message in restored] == [
        ("user", "user-1"),
        ("assistant", "assistant-1"),
        ("tool", "call-1"),
    ]
    assert restored[-1].tool_call_id == "call-1"


def test_resume_does_not_duplicate_completed_tool_transaction() -> None:
    lifecycle = SessionTurnLifecycle(
        session_id="session-1",
        turn_id="turn-1",
    )
    current = [
        ModelMessage.text("user", "run it", id="user-1"),
        ModelMessage(
            id="assistant-1",
            role="assistant",
            content=[
                {
                    "type": "tool_call",
                    "data": {
                        "id": "call-1",
                        "name": "write_file",
                        "arguments": {},
                    },
                }
            ],
        ),
        ModelMessage(
            id="call-1",
            role="tool",
            tool_call_id="call-1",
            content=[
                {
                    "type": "tool_result",
                    "data": {
                        "id": "call-1",
                        "name": "write_file",
                        "output": [],
                    },
                }
            ],
        ),
    ]

    restored = lifecycle.restore_model_messages(
        current,
        attempt_reason="resume",
        checkpoint_metadata={
            "compressed_context": [
                message.model_dump_json() for message in current
            ],
        },
    )

    assert [(message.role, message.id) for message in restored] == [
        ("user", "user-1"),
        ("assistant", "assistant-1"),
        ("tool", "call-1"),
    ]


def test_resume_keeps_latest_continuation_capsule_before_new_input() -> None:
    lifecycle = SessionTurnLifecycle(
        session_id="session-1",
        turn_id="turn-1",
    )
    ownership = ModelMessage.text(
        "system",
        "primary owner",
        metadata={"instruction_origin": "runtime.primary_turn_ownership"},
    )
    capsule = ModelMessage.text(
        "system",
        "sealed capsule",
        metadata={
            "instruction_origin": "runtime.continuation_capsule",
            "runtime_continuation_capsule": True,
            "capsule_sha256": "a" * 64,
        },
    )
    previous = ModelMessage.text("assistant", "checkpoint progress", id="assistant-1")
    current = [ownership.model_copy(deep=True), ModelMessage.text("user", "continue")]

    restored = lifecycle.restore_model_messages(
        current,
        attempt_reason="resume",
        checkpoint_metadata={
            "compressed_context": [
                ownership.model_dump_json(),
                capsule.model_dump_json(),
                previous.model_dump_json(),
            ]
        },
    )

    assert [message.text_content() for message in restored] == [
        "primary owner",
        "sealed capsule",
        "checkpoint progress",
        "continue",
    ]
    assert sum(
        message.metadata.get("runtime_continuation_capsule") is True
        for message in restored
    ) == 1


def test_start_and_terminal_share_identity_and_start_is_emitted_once() -> None:
    lifecycle = SessionTurnLifecycle(
        session_id="session-1",
        turn_id="turn-1",
    )

    started = lifecycle.start(
        seq=1,
        summary="begin",
        payload={"run_id": "turn-1"},
    )
    completed = lifecycle.complete(seq=2)

    assert started.type == "turn.started"
    assert started.session_id == completed.session_id == "session-1"
    assert started.turn_id == completed.turn_id == "turn-1"
    with pytest.raises(SessionTurnAlreadyStarted):
        lifecycle.start(seq=3, summary="duplicate")


def test_post_answer_work_is_admitted_only_after_completed_foreground() -> (
    None
):
    completed = post_answer_admission(
        terminal_type="turn.completed",
        input_committed=True,
    )
    before_input_commit = post_answer_admission(
        terminal_type="turn.completed",
        input_committed=False,
    )
    failed = post_answer_admission(
        terminal_type="turn.failed",
        input_committed=True,
    )

    assert completed.schedule_reflection is True
    assert completed.schedule_memory_capture is True
    assert before_input_commit.schedule_reflection is True
    assert before_input_commit.schedule_memory_capture is False
    assert failed.schedule_reflection is False
    assert failed.schedule_memory_capture is False
