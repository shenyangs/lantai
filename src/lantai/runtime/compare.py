# -*- coding: utf-8 -*-
"""Dev-only helpers for comparing runtime event streams.

The comparator is intentionally fixture-gated.  It is meant for replayed or
scripted adapters, not for live side-effecting tools.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field

from .adapter import RuntimeAdapter
from .converters import runtime_event_to_legacy_stream_item
from .events import RuntimeEvent
from .input import FixtureRuntimeInput, ReplayRuntimeInput, RuntimeInput


RuntimeComparisonKind = Literal[
    "event_sequence",
    "message_blocks",
    "tool_call",
    "tool_result",
    "approval",
    "memory_context",
    "frontend_stream",
]


class RuntimeEventProjection(BaseModel):
    """Small semantic shape used to compare runtime streams."""

    type: str
    status: str | None = None
    source: str = "system"
    message_block_types: list[str] = Field(default_factory=list)
    message_text: str = ""
    tool_name: str = ""
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    tool_output: Any = None
    tool_ok: bool | None = None
    approval_request_id: str = ""
    approval_decision: str = ""
    memory_context: dict[str, Any] = Field(default_factory=dict)
    frontend_stream: dict[str, Any] | None = None


class RuntimeEventMismatch(BaseModel):
    """One semantic mismatch between two runtime streams."""

    index: int
    kind: RuntimeComparisonKind
    expected: Any = None
    actual: Any = None
    detail: str = ""


class RuntimeComparisonReport(BaseModel):
    """Result of comparing legacy adapter output and native output."""

    ok: bool
    mismatches: list[RuntimeEventMismatch] = Field(default_factory=list)
    expected: list[RuntimeEventProjection] = Field(default_factory=list)
    actual: list[RuntimeEventProjection] = Field(default_factory=list)


class DualRunSafetyError(RuntimeError):
    """Raised when a dual-run compare is attempted on non-fixture input."""


def _stable_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if isinstance(value, dict):
        return {
            str(key): _stable_json(value[key])
            for key in sorted(value.keys(), key=str)
        }
    if isinstance(value, (list, tuple)):
        return [_stable_json(item) for item in value]
    return str(value)


def _message_projection(event: RuntimeEvent) -> tuple[list[str], str]:
    message = event.payload.get("message")
    if not isinstance(message, dict):
        return [], ""
    content = message.get("content")
    if not isinstance(content, list):
        return [], ""
    block_types: list[str] = []
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type:
            block_types.append(block_type)
        text = block.get("text")
        if isinstance(text, str):
            text_parts.append(text)
    return block_types, "".join(text_parts)


def _tool_payload(event: RuntimeEvent) -> tuple[str, dict[str, Any], Any, bool | None]:
    payload = event.payload
    name = str(payload.get("name") or payload.get("tool_name") or "")
    arguments = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("input")
    if not isinstance(arguments, dict):
        arguments = {}
    output = payload.get("output")
    ok = payload.get("ok")
    return name, _stable_json(arguments), _stable_json(output), ok if isinstance(ok, bool) else None


def _approval_projection(event: RuntimeEvent) -> tuple[str, str]:
    return (
        str(event.payload.get("request_id") or event.item_id or ""),
        str(event.payload.get("decision") or ""),
    )


def _memory_context_projection(event: RuntimeEvent) -> dict[str, Any]:
    if event.type not in {"memory.updated", "plan.updated"}:
        return {}
    payload = dict(event.payload)
    for unstable_key in ("created_at", "updated_at", "timestamp"):
        payload.pop(unstable_key, None)
    return _stable_json(payload)


def _frontend_stream_projection(event: RuntimeEvent) -> dict[str, Any] | None:
    item = runtime_event_to_legacy_stream_item(event)
    if item is None:
        return None
    msg, last = item
    content = getattr(msg, "content", None)
    content_types: list[str] = []
    text_parts: list[str] = []
    tool_blocks: list[dict[str, Any]] = []
    if isinstance(content, str):
        content_types.append("text")
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                block_type = str(block.get("type") or "")
                text = block.get("text")
                if block_type in {"tool_use", "tool_result"}:
                    tool_blocks.append(
                        {
                            "type": block_type,
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": _stable_json(block.get("input")),
                            "output": _stable_json(block.get("output")),
                        },
                    )
            else:
                block_type = str(getattr(block, "type", "") or "")
                text = getattr(block, "text", None)
                if block_type in {"tool_use", "tool_result"}:
                    tool_blocks.append(
                        {
                            "type": block_type,
                            "id": getattr(block, "id", None),
                            "name": getattr(block, "name", None),
                            "input": _stable_json(getattr(block, "input", None)),
                            "output": _stable_json(getattr(block, "output", None)),
                        },
                    )
            if block_type:
                content_types.append(block_type)
            if isinstance(text, str):
                text_parts.append(text)
    metadata = getattr(msg, "metadata", None) or {}
    message_type = ""
    if isinstance(metadata, dict):
        message_type = str(metadata.get("message_type") or "")
    return {
        "role": str(getattr(msg, "role", "") or ""),
        "last": bool(last),
        "content_types": content_types,
        "text": "".join(text_parts),
        "tools": tool_blocks,
        "message_type": message_type,
    }


def project_runtime_event(event: RuntimeEvent) -> RuntimeEventProjection:
    """Project a RuntimeEvent to comparable semantic fields."""

    message_block_types, message_text = _message_projection(event)
    tool_name, tool_arguments, tool_output, tool_ok = _tool_payload(event)
    approval_request_id, approval_decision = _approval_projection(event)
    return RuntimeEventProjection(
        type=event.type,
        status=event.status,
        source=event.source,
        message_block_types=message_block_types,
        message_text=message_text,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        tool_output=tool_output,
        tool_ok=tool_ok,
        approval_request_id=approval_request_id,
        approval_decision=approval_decision,
        memory_context=_memory_context_projection(event),
        frontend_stream=_frontend_stream_projection(event),
    )


def project_runtime_events(
    events: Iterable[RuntimeEvent],
) -> list[RuntimeEventProjection]:
    """Project a stream of RuntimeEvent rows."""

    return [project_runtime_event(event) for event in events]


def compare_runtime_events(
    expected_events: Iterable[RuntimeEvent],
    actual_events: Iterable[RuntimeEvent],
    *,
    compare_text: bool = False,
    compare_frontend_text: bool = False,
) -> RuntimeComparisonReport:
    """Compare two runtime streams by stable semantics, not ids or timestamps."""

    expected = project_runtime_events(expected_events)
    actual = project_runtime_events(actual_events)
    mismatches: list[RuntimeEventMismatch] = []
    max_len = max(len(expected), len(actual))
    for index in range(max_len):
        if index >= len(expected) or index >= len(actual):
            mismatches.append(
                RuntimeEventMismatch(
                    index=index,
                    kind="event_sequence",
                    expected=expected[index].model_dump(mode="json")
                    if index < len(expected)
                    else None,
                    actual=actual[index].model_dump(mode="json")
                    if index < len(actual)
                    else None,
                    detail="event count differs",
                ),
            )
            continue

        left = expected[index]
        right = actual[index]
        if (left.type, left.status, left.source) != (
            right.type,
            right.status,
            right.source,
        ):
            mismatches.append(
                RuntimeEventMismatch(
                    index=index,
                    kind="event_sequence",
                    expected=[left.type, left.status, left.source],
                    actual=[right.type, right.status, right.source],
                    detail="event type/status/source differ",
                ),
            )

        if left.message_block_types != right.message_block_types:
            mismatches.append(
                RuntimeEventMismatch(
                    index=index,
                    kind="message_blocks",
                    expected=left.message_block_types,
                    actual=right.message_block_types,
                    detail="message block types differ",
                ),
            )
        elif compare_text and left.message_text != right.message_text:
            mismatches.append(
                RuntimeEventMismatch(
                    index=index,
                    kind="message_blocks",
                    expected=left.message_text,
                    actual=right.message_text,
                    detail="message text differs",
                ),
            )

        if left.tool_name or right.tool_name:
            if (left.tool_name, left.tool_arguments) != (
                right.tool_name,
                right.tool_arguments,
            ):
                mismatches.append(
                    RuntimeEventMismatch(
                        index=index,
                        kind="tool_call",
                        expected={
                            "name": left.tool_name,
                            "arguments": left.tool_arguments,
                        },
                        actual={
                            "name": right.tool_name,
                            "arguments": right.tool_arguments,
                        },
                        detail="tool call name or arguments differ",
                    ),
                )
            if event_is_tool_result(left.type) or event_is_tool_result(right.type):
                if (left.tool_ok, left.tool_output) != (right.tool_ok, right.tool_output):
                    mismatches.append(
                        RuntimeEventMismatch(
                            index=index,
                            kind="tool_result",
                            expected={"ok": left.tool_ok, "output": left.tool_output},
                            actual={"ok": right.tool_ok, "output": right.tool_output},
                            detail="tool result differs",
                        ),
                    )

        if left.type.startswith("approval.") or right.type.startswith("approval."):
            if (left.approval_request_id, left.approval_decision) != (
                right.approval_request_id,
                right.approval_decision,
            ):
                mismatches.append(
                    RuntimeEventMismatch(
                        index=index,
                        kind="approval",
                        expected={
                            "request_id": left.approval_request_id,
                            "decision": left.approval_decision,
                        },
                        actual={
                            "request_id": right.approval_request_id,
                            "decision": right.approval_decision,
                        },
                        detail="approval request or decision differs",
                    ),
                )

        if left.memory_context or right.memory_context:
            if left.memory_context != right.memory_context:
                mismatches.append(
                    RuntimeEventMismatch(
                        index=index,
                        kind="memory_context",
                        expected=left.memory_context,
                        actual=right.memory_context,
                        detail="memory/context payload differs",
                    ),
                )

        if left.frontend_stream or right.frontend_stream:
            left_frontend = dict(left.frontend_stream or {})
            right_frontend = dict(right.frontend_stream or {})
            if not compare_frontend_text:
                left_frontend.pop("text", None)
                right_frontend.pop("text", None)
            if left_frontend != right_frontend:
                mismatches.append(
                    RuntimeEventMismatch(
                        index=index,
                        kind="frontend_stream",
                        expected=left_frontend,
                        actual=right_frontend,
                        detail="legacy frontend stream projection differs",
                    ),
                )

    return RuntimeComparisonReport(
        ok=not mismatches,
        mismatches=mismatches,
        expected=expected,
        actual=actual,
    )


def event_is_tool_result(event_type: str) -> bool:
    """True when the event carries a completed tool result."""

    return event_type in {"tool.completed", "tool.call.completed"}


async def collect_runtime_events(
    adapter: RuntimeAdapter,
    runtime_input: RuntimeInput,
) -> list[RuntimeEvent]:
    """Collect adapter output for fixture comparison."""

    return [event async for event in adapter.run_turn(runtime_input)]


class RuntimeDualRunComparator:
    """Compare two adapters in fixture/replay mode."""

    def __init__(
        self,
        *,
        expected_adapter: RuntimeAdapter,
        actual_adapter: RuntimeAdapter,
        require_fixture: bool = True,
    ) -> None:
        self._expected_adapter = expected_adapter
        self._actual_adapter = actual_adapter
        self._require_fixture = require_fixture
        _validate_side_effect_free_adapters(
            expected_adapter=self._expected_adapter,
            actual_adapter=self._actual_adapter,
        )

    async def compare(
        self,
        runtime_input: RuntimeInput,
        *,
        compare_text: bool = False,
        compare_frontend_text: bool = False,
    ) -> RuntimeComparisonReport:
        """Run both adapters and compare their normalized RuntimeEvents."""

        if self._require_fixture and not _is_fixture_safe(runtime_input):
            raise DualRunSafetyError(
                "dual-run comparison requires FixtureRuntimeInput or "
                "ReplayRuntimeInput; request metadata cannot enable dual-run",
            )
        expected_events, actual_events = await asyncio.gather(
            collect_runtime_events(self._expected_adapter, runtime_input),
            collect_runtime_events(self._actual_adapter, runtime_input),
        )
        return compare_runtime_events(
            expected_events,
            actual_events,
            compare_text=compare_text,
            compare_frontend_text=compare_frontend_text,
        )


def _is_fixture_safe(runtime_input: RuntimeInput) -> bool:
    return isinstance(runtime_input, (FixtureRuntimeInput, ReplayRuntimeInput))


def _validate_side_effect_free_adapters(
    *,
    expected_adapter: RuntimeAdapter,
    actual_adapter: RuntimeAdapter,
) -> None:
    unsafe: list[str] = []
    for name, adapter in (
        ("expected_adapter", expected_adapter),
        ("actual_adapter", actual_adapter),
    ):
        if getattr(adapter, "side_effect_free", False) is not True:
            unsafe.append(name)
    if unsafe:
        raise DualRunSafetyError(
            "dual-run adapters must declare side_effect_free=True before "
            f"comparison: {', '.join(unsafe)}",
        )


def report_to_json(report: RuntimeComparisonReport) -> str:
    """Return a stable JSON report for CLI/dev tooling."""

    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)


__all__ = [
    "DualRunSafetyError",
    "RuntimeComparisonReport",
    "RuntimeDualRunComparator",
    "RuntimeEventMismatch",
    "RuntimeEventProjection",
    "collect_runtime_events",
    "compare_runtime_events",
    "project_runtime_event",
    "project_runtime_events",
    "report_to_json",
]
