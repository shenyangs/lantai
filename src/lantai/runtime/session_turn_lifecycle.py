# -*- coding: utf-8 -*-
"""Produce exactly one terminal projection for a Native session turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from .events import RuntimeEvent
from .models import ModelMessage


TerminalEventType = Literal[
    "turn.completed",
    "turn.failed",
    "turn.interrupted",
]


class SessionTurnAlreadySettled(RuntimeError):
    """Raised when one execution path attempts a second terminal event."""


class SessionTurnAlreadyStarted(RuntimeError):
    """Raised when one execution path attempts a second start event."""


@dataclass(frozen=True, slots=True)
class PostAnswerAdmission:
    """Background work allowed after the foreground terminal is visible."""

    schedule_reflection: bool
    schedule_memory_capture: bool


def post_answer_admission(
    *,
    terminal_type: str,
    input_committed: bool,
) -> PostAnswerAdmission:
    """Derive background admission without performing or delaying it."""

    completed = terminal_type == "turn.completed"
    return PostAnswerAdmission(
        schedule_reflection=completed,
        schedule_memory_capture=completed and input_committed,
    )


class SessionTurnLifecycle:
    """Own terminal event identity and single-settlement enforcement."""

    def __init__(self, *, session_id: str, turn_id: str) -> None:
        self._session_id = session_id
        self._turn_id = turn_id
        self._started = False
        self._terminal_type: TerminalEventType | None = None

    @property
    def terminal_type(self) -> TerminalEventType | None:
        return self._terminal_type

    def start(
        self,
        *,
        seq: int,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        """Create the single start event for this turn identity."""

        if self._started:
            raise SessionTurnAlreadyStarted("Session turn already started")
        self._started = True
        return RuntimeEvent(
            id=str(uuid4()),
            seq=seq,
            type="turn.started",
            status="running",
            source="user",
            session_id=self._session_id,
            turn_id=self._turn_id,
            summary=summary,
            payload=dict(payload or {}),
        )

    def _terminal(
        self,
        *,
        seq: int,
        event_type: TerminalEventType,
        status: Literal["completed", "failed", "interrupted"],
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        if self._terminal_type is not None:
            raise SessionTurnAlreadySettled(
                f"Session turn already settled as {self._terminal_type}",
            )
        self._terminal_type = event_type
        return RuntimeEvent(
            id=str(uuid4()),
            seq=seq,
            type=event_type,
            status=status,
            source="system",
            session_id=self._session_id,
            turn_id=self._turn_id,
            summary=summary,
            payload=dict(payload or {}),
        )

    def complete(
        self,
        *,
        seq: int,
        summary: str = "turn completed",
        payload: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        return self._terminal(
            seq=seq,
            event_type="turn.completed",
            status="completed",
            summary=summary,
            payload=payload,
        )

    def interrupt(self, *, seq: int) -> RuntimeEvent:
        return self._terminal(
            seq=seq,
            event_type="turn.interrupted",
            status="interrupted",
            summary="turn interrupted",
        )

    def fail(
        self,
        *,
        seq: int,
        summary: str,
        payload: dict[str, Any],
    ) -> RuntimeEvent:
        return self._terminal(
            seq=seq,
            event_type="turn.failed",
            status="failed",
            summary=summary,
            payload=payload,
        )

    def partial_delivery_failure(
        self,
        *,
        seq: int,
        failure_summary: str,
        error_type: str,
    ) -> tuple[RuntimeEvent, RuntimeEvent]:
        warning = RuntimeEvent(
            id=str(uuid4()),
            seq=seq,
            type="runtime.warning",
            status="failed",
            source="system",
            session_id=self._session_id,
            turn_id=self._turn_id,
            summary="请求的文件已交付，但后续说明生成失败。",
            payload={
                "error": failure_summary,
                "error_type": error_type,
                "outcome_status": "incomplete",
                "details": {"partial_delivery": True},
            },
        )
        terminal = self.complete(
            seq=seq + 1,
            summary="artifact delivered; follow-up incomplete",
            payload={
                "outcome_status": "incomplete",
                "details": {"partial_delivery": True},
                "error_type": error_type,
            },
        )
        return warning, terminal

    def restore_model_messages(
        self,
        current: list[ModelMessage],
        *,
        attempt_reason: str,
        checkpoint_metadata: dict[str, Any] | None,
    ) -> list[ModelMessage]:
        """Stitch valid checkpoint continuation into the durable transcript.

        The current transcript is authoritative for history that has already
        been committed.  A checkpoint may contain an active window that overlaps
        that history and then continues beyond it.  Stable message and tool
        identities let us insert only the continuation instead of concatenating
        two independently loaded copies of the same transaction.
        """

        if attempt_reason != "resume" or checkpoint_metadata is None:
            return list(current)
        raw_messages = checkpoint_metadata.get("compressed_context")
        if not isinstance(raw_messages, (list, tuple)):
            return list(current)
        restored: list[ModelMessage] = []
        for raw_message in raw_messages:
            if type(raw_message) is not str:
                continue
            try:
                restored.append(ModelMessage.model_validate_json(raw_message))
            except (TypeError, ValueError):
                continue
        if not restored:
            return list(current)
        return _stitch_model_message_history(restored, list(current))


def _model_message_identity(message: ModelMessage) -> tuple[str, ...] | None:
    """Return a persisted identity without inventing one from message text."""

    if message.role == "tool":
        call_id = str(message.tool_call_id or message.id or "").strip()
        if call_id:
            return ("tool_result", call_id)
    message_id = str(message.id or "").strip()
    if message_id:
        return ("message", message.role, message_id)
    tool_call_ids = tuple(
        str(block.data.get("id") or "").strip()
        for block in message.content
        if block.type == "tool_call"
        and str(block.data.get("id") or "").strip()
    )
    if tool_call_ids:
        return ("assistant_tool_calls", *tool_call_ids)
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    instruction_origin = str(metadata.get("instruction_origin") or "").strip()
    if instruction_origin:
        return ("runtime_instruction", instruction_origin)
    return None


def _unique_model_messages(messages: list[ModelMessage]) -> list[ModelMessage]:
    unique: list[ModelMessage] = []
    seen: set[tuple[str, ...]] = set()
    for message in messages:
        identity = _model_message_identity(message)
        if identity is not None and identity in seen:
            continue
        if identity is not None:
            seen.add(identity)
        unique.append(message)
    return unique


def _stitch_model_message_history(
    restored: list[ModelMessage],
    current: list[ModelMessage],
) -> list[ModelMessage]:
    """Join checkpoint and transcript at their latest shared identity."""

    restored = _unique_model_messages(restored)
    current = _unique_model_messages(current)
    current_positions = {
        identity: index
        for index, message in enumerate(current)
        if (identity := _model_message_identity(message)) is not None
    }
    shared = [
        (restored_index, current_positions[identity])
        for restored_index, message in enumerate(restored)
        if (identity := _model_message_identity(message)) in current_positions
    ]
    if not shared:
        return _unique_model_messages(restored + current)

    restored_index, current_index = max(shared)
    seen = {
        identity
        for message in current
        if (identity := _model_message_identity(message)) is not None
    }
    continuation: list[ModelMessage] = []
    for message in restored[restored_index + 1 :]:
        identity = _model_message_identity(message)
        if identity is not None and identity in seen:
            continue
        if identity is not None:
            seen.add(identity)
        continuation.append(message)
    return current[: current_index + 1] + continuation + current[current_index + 1 :]


__all__ = [
    "PostAnswerAdmission",
    "SessionTurnAlreadySettled",
    "SessionTurnAlreadyStarted",
    "SessionTurnLifecycle",
    "TerminalEventType",
    "post_answer_admission",
]
