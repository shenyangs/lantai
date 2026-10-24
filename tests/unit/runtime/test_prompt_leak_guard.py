# -*- coding: utf-8 -*-
"""Prompt canary isolation across public output surfaces."""

from lantai.runtime.events import RuntimeEvent
from lantai.runtime.prompt_leak_guard import PromptLeakGuard
from lantai.runtime.public_reasoning import PublicReasoningProjector


def test_prompt_canary_is_redacted_from_answer_tool_log_and_resume_payload() -> None:
    canary = "LANTAI_PRIVATE_PROMPT_CANARY_91f0"
    guard = PromptLeakGuard(canaries=(canary,), fingerprints=("sha256:kernel",))
    event = RuntimeEvent(
        id="event",
        seq=1,
        type="message.completed",
        status="completed",
        source="assistant",
        session_id="session",
        turn_id="turn",
        summary=f"answer {canary}",
        payload={
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": canary}],
                "metadata": {
                    "tool_log": f"tool saw {canary}",
                    "resume": {"checkpoint": "sha256:kernel"},
                },
            },
        },
    )

    projected = guard.project_event(event, surface="public_runtime_event")

    dumped = projected.model_dump_json()
    assert canary not in dumped
    assert "sha256:kernel" not in dumped
    assert dumped.count("[REDACTED_PROMPT_FINGERPRINT]") >= 1
    assert len(guard.findings) == 2
    assert all(canary not in str(item) for item in guard.findings)


def test_private_model_object_is_not_mutated_by_public_projection() -> None:
    canary = "LANTAI_PRIVATE_PROMPT_CANARY_private"
    guard = PromptLeakGuard(canaries=(canary,))
    private = {"thinking": canary, "final": f"public {canary}"}

    projected = guard.project_value(private, surface="public")

    assert private["thinking"] == canary
    assert canary not in str(projected)


def test_prompt_canary_cannot_reassemble_across_public_stream_deltas() -> None:
    canary = "LANTAI_PRIVATE_PROMPT_CANARY_split_7f31"
    projector = PublicReasoningProjector(PromptLeakGuard(canaries=(canary,)))
    visible: list[str] = []
    for index, delta in enumerate(
        ("answer ", "LANTAI_PRIVATE_", "PROMPT_CANARY_", "split_7f31", " done")
    ):
        event = RuntimeEvent(
            id=f"split-{index}",
            type="message.delta",
            source="assistant",
            item_id="split-message",
            summary=delta,
            payload={"delta": delta, "content_type": "text"},
        )
        projected = projector.project(event)
        if projected is not None:
            visible.append(str(projected.payload.get("delta") or ""))

    public_text = "".join(visible)
    assert canary not in public_text
    assert public_text == "answer [REDACTED_PROMPT_FINGERPRINT] done"


def test_safe_terminal_prefix_is_flushed_instead_of_silently_discarded() -> None:
    guard = PromptLeakGuard(fingerprints=("1. protected system instruction",))

    assert (
        guard.project_stream_text(
            "ordinary answer1",
            surface="public_runtime_event",
            stream_id="answer",
        )
        == "ordinary answer"
    )
    assert (
        guard.finalize_stream(
            surface="public_runtime_event",
            stream_id="answer",
        )
        == "1"
    )
    assert (
        guard.finalize_stream(
            surface="public_runtime_event",
            stream_id="answer",
        )
        == ""
    )
