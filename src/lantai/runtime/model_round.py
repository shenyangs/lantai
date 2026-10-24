# -*- coding: utf-8 -*-
"""Collect one provider model stream into a stable round result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from .models import ModelResponse, ModelStreamEvent, ModelUsage
from .tools import ToolCall


ModelRoundTermination = Literal[
    "completed",
    "provider_error",
    "cancelled",
    "timeout",
]


class ModelRoundProtocolError(RuntimeError):
    """Raised when a provider response violates the model-round contract."""


@dataclass(frozen=True, slots=True)
class ModelRoundResult:
    """Frozen facts observed during exactly one model request."""

    response: ModelResponse | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    usage: ModelUsage | None
    stream_message_ids: frozenset[str]
    termination: ModelRoundTermination
    saw_event: bool
    model_error: str | None
    protocol_findings: tuple[str, ...]


def _tool_call_protocol_error(
    tool_name: str,
    detail: str,
) -> ModelRoundProtocolError:
    return ModelRoundProtocolError(
        "Native runtime model protocol error: tool call "
        f"{tool_name} {detail}.",
    )


def _arguments_from_block(
    data: dict[str, object],
    *,
    tool_name: str,
) -> dict[str, object]:
    if "arguments" in data:
        arguments = data["arguments"]
    elif "input" in data:
        arguments = data["input"]
    else:
        return {}
    if not isinstance(arguments, dict):
        raise _tool_call_protocol_error(
            tool_name,
            "arguments must be an object",
        )
    return dict(arguments)


def tool_calls_from_model_response(
    response: ModelResponse,
) -> tuple[ToolCall, ...]:
    """Extract ordered tool calls from the final response content."""

    calls: list[ToolCall] = []
    for block in response.message.content:
        if block.type != "tool_call":
            continue
        data = dict(block.data)
        name = data.get("name")
        if not name:
            continue
        tool_name = str(name)
        arguments = _arguments_from_block(data, tool_name=tool_name)
        calls.append(
            ToolCall(
                id=str(
                    data.get("id")
                    or data.get("call_id")
                    or f"call_{uuid4().hex}",
                ),
                name=tool_name,
                arguments=arguments,
                metadata={"source": "model_response"},
            ),
        )
    return tuple(calls)


class ModelRoundAccumulator:
    """Accumulate model events without executing or projecting them."""

    _MESSAGE_EVENT_KINDS = frozenset(
        {
            "message_start",
            "thinking_delta",
            "text_delta",
            "message_completed",
        },
    )

    def __init__(self) -> None:
        self._response: ModelResponse | None = None
        self._tool_calls: list[ToolCall] = []
        self._tool_call_index: dict[str, int] = {}
        self._stream_message_ids: set[str] = set()
        self._last_event_finish_reason = ""
        self._model_error: str | None = None
        self._usage: ModelUsage | None = None
        self._saw_event = False

    def _merge_tool_call(self, tool_call: ToolCall) -> None:
        existing_index = self._tool_call_index.get(tool_call.id)
        if existing_index is None:
            self._tool_call_index[tool_call.id] = len(self._tool_calls)
            self._tool_calls.append(tool_call)
            return
        existing = self._tool_calls[existing_index]
        self._tool_calls[existing_index] = ToolCall(
            id=tool_call.id,
            name=tool_call.name or existing.name,
            arguments=dict(tool_call.arguments),
            metadata={**existing.metadata, **tool_call.metadata},
        )

    def accept(self, event: ModelStreamEvent) -> None:
        """Observe one event in provider order."""

        self._saw_event = True
        if event.message_id and event.kind in self._MESSAGE_EVENT_KINDS:
            self._stream_message_ids.add(event.message_id)
        if event.kind == "tool_call_completed" and event.tool_call is not None:
            self._merge_tool_call(event.tool_call)
        if event.response is not None:
            self._response = event.response
        if event.finish_reason:
            self._last_event_finish_reason = str(event.finish_reason)
        if event.usage is not None:
            self._usage = event.usage
        if event.kind == "error":
            self._model_error = event.error or "model error"

    def finish(
        self,
        termination: ModelRoundTermination = "completed",
    ) -> ModelRoundResult:
        """Freeze the accumulated facts and merge final response calls."""

        if self._response is not None:
            for tool_call in tool_calls_from_model_response(self._response):
                self._merge_tool_call(tool_call)
        response_finish_reason = str(
            self._response.finish_reason
            if self._response is not None
            and self._response.finish_reason is not None
            else "",
        ).strip()
        finish_reason = (
            str(
                response_finish_reason or self._last_event_finish_reason or "",
            )
            .strip()
            .lower()
        )
        usage = (
            self._response.usage
            if self._response is not None and self._response.usage is not None
            else self._usage
        )
        findings: list[str] = []
        if self._saw_event and self._response is None and not self._tool_calls:
            findings.append("final_response_missing")
        elif self._response is None and self._tool_calls:
            findings.append("tool_calls_without_final_response")
        if self._model_error:
            findings.append("provider_error_event")
        return ModelRoundResult(
            response=self._response,
            tool_calls=tuple(self._tool_calls),
            finish_reason=finish_reason,
            usage=usage,
            stream_message_ids=frozenset(self._stream_message_ids),
            termination=termination,
            saw_event=self._saw_event,
            model_error=self._model_error,
            protocol_findings=tuple(findings),
        )


__all__ = [
    "ModelRoundAccumulator",
    "ModelRoundProtocolError",
    "ModelRoundResult",
    "ModelRoundTermination",
    "tool_calls_from_model_response",
]
