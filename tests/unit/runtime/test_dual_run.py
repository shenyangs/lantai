# -*- coding: utf-8 -*-
"""Behavior locks for Phase 6 dual-runtime comparison and fallback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

import pytest

from lantai.runtime.compare import (
    DualRunSafetyError,
    RuntimeDualRunComparator,
    compare_runtime_events,
    project_runtime_events,
)
from lantai.runtime.events import RuntimeEvent
from lantai.runtime.input import (
    FixtureRuntimeInput,
    RuntimeInput,
    RuntimeRequestContext,
)
from lantai.runtime.messages import RuntimeMessage


_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "runtime"
    / "phase6_recorded_events.json"
)


class ScriptedRuntimeAdapter:
    side_effect_free = True

    def __init__(
        self,
        events: list[RuntimeEvent],
        *,
        fail_after: int | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.events = events
        self.fail_after = fail_after
        self.failure = failure or RuntimeError("scripted failure")
        self.seen_inputs: list[RuntimeInput] = []

    async def run_turn(
        self,
        runtime_input: RuntimeInput,
    ) -> AsyncIterator[RuntimeEvent]:
        self.seen_inputs.append(runtime_input)
        for index, event in enumerate(self.events):
            if self.fail_after is not None and index >= self.fail_after:
                raise self.failure
            yield event
        if self.fail_after is not None and self.fail_after >= len(self.events):
            raise self.failure


class LiveSideEffectRuntimeAdapter:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run_turn(
        self,
        runtime_input: RuntimeInput,
    ) -> AsyncIterator[RuntimeEvent]:
        self.calls.append(runtime_input.request.run_id or "run")
        yield _event(1, "turn.completed")


def _build_input(
    input_type: type[RuntimeInput],
    *,
    dual_run_safe: bool = True,
) -> RuntimeInput:
    return input_type(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            user_id="user-1",
            run_id="run-1",
            metadata={"dual_run_safe": dual_run_safe},
        ),
        messages=[RuntimeMessage.text("user", "hello")],
        metadata={"dual_run_safe": dual_run_safe},
    )


def _input(*, dual_run_safe: bool = True) -> FixtureRuntimeInput:
    runtime_input = _build_input(FixtureRuntimeInput, dual_run_safe=dual_run_safe)
    assert isinstance(runtime_input, FixtureRuntimeInput)
    return runtime_input


def _plain_input(*, dual_run_safe: bool = True) -> RuntimeInput:
    return _build_input(RuntimeInput, dual_run_safe=dual_run_safe)


def _event(
    seq: int,
    event_type: str,
    *,
    status: str = "completed",
    source: str = "system",
    payload: dict | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        id=f"evt-{seq}-{event_type}",
        seq=seq,
        type=event_type,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        session_id="session-1",
        turn_id="run-1",
        payload=payload or {},
    )


def _message_event(seq: int, event_type: str, block_type: str) -> RuntimeEvent:
    return _event(
        seq,
        event_type,
        status="running" if event_type == "message.delta" else "completed",
        source="assistant",
        payload={
            "message": {
                "id": "msg-1",
                "role": "assistant",
                "name": "Friday",
                "content": [{"type": block_type, "text": f"{block_type}-{seq}"}],
                "metadata": {},
            },
        },
    )


def _tool_started(seq: int, name: str, arguments: dict) -> RuntimeEvent:
    return _event(
        seq,
        "tool.call.started",
        status="running",
        source="tool",
        payload={
            "id": f"call-{seq}",
            "name": name,
            "tool_name": name,
            "arguments": arguments,
        },
    )


def _tool_completed(
    seq: int,
    name: str,
    *,
    ok: bool = True,
    text: str = "ok",
) -> RuntimeEvent:
    return _event(
        seq,
        "tool.call.completed",
        status="completed" if ok else "failed",
        source="tool",
        payload={
            "id": f"call-{seq}",
            "name": name,
            "tool_name": name,
            "ok": ok,
            "output": [{"type": "text", "text": text}],
        },
    )


def _phase6_fixture_events() -> list[RuntimeEvent]:
    return [
        _event(1, "turn.started", status="running", source="user"),
        _message_event(2, "message.delta", "thinking"),
        _message_event(3, "message.delta", "text"),
        _tool_started(4, "lookup", {"query": "澜台"}),
        _tool_completed(5, "lookup", text="found"),
        _tool_started(6, "mcp_echo", {"value": "ok"}),
        _tool_completed(7, "mcp_echo", text="mcp_echo:ok"),
        _event(
            8,
            "approval.requested",
            status="running",
            source="system",
            payload={
                "request_id": "approval-1",
                "tool_name": "shell",
                "arguments": {"command": "pwd"},
            },
        ),
        _event(
            9,
            "approval.resolved",
            status="completed",
            source="system",
            payload={
                "request_id": "approval-1",
                "tool_name": "shell",
                "decision": "approved",
            },
        ),
        _tool_started(10, "shell", {"command": "pwd"}),
        _tool_completed(11, "shell", ok=False, text="permission denied"),
        _event(
            12,
            "memory.updated",
            source="system",
            payload={"scope": "session", "message_count": 2, "updated_at": "unstable"},
        ),
        _event(
            13,
            "turn.interrupted",
            status="interrupted",
            source="system",
        ),
    ]


def _load_recorded_fixture_cases() -> dict[str, list[RuntimeEvent]]:
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return {
        name: [RuntimeEvent.model_validate(item) for item in events]
        for name, events in raw.items()
    }


def test_runtime_compare_covers_phase6_core_event_shapes() -> None:
    expected = _phase6_fixture_events()
    actual = _phase6_fixture_events()

    report = compare_runtime_events(expected, actual)

    assert report.ok is True
    projected = report.actual
    assert projected[1].message_block_types == ["thinking"]
    assert projected[3].tool_name == "lookup"
    assert projected[5].tool_name == "mcp_echo"
    assert projected[7].approval_request_id == "approval-1"
    assert projected[11].memory_context == {"message_count": 2, "scope": "session"}
    assert projected[3].frontend_stream == {
        "role": "assistant",
        "last": False,
        "content_types": ["tool_use"],
        "text": "",
        "tools": [
            {
                "type": "tool_use",
                "id": "call-4",
                "name": "lookup",
                "input": {"query": "澜台"},
                "output": None,
            },
        ],
        "message_type": "",
    }


def test_runtime_compare_reports_tool_and_frontend_mismatch() -> None:
    expected = [_tool_started(1, "lookup", {"query": "a"})]
    actual = [_tool_started(1, "lookup", {"query": "b"})]

    report = compare_runtime_events(expected, actual)

    assert report.ok is False
    assert {mismatch.kind for mismatch in report.mismatches} == {
        "tool_call",
        "frontend_stream",
    }


def test_runtime_compare_recorded_fixture_matrix() -> None:
    cases = _load_recorded_fixture_cases()

    assert set(cases) == {
        "plain_answer",
        "thinking",
        "single_tool",
        "multi_tool",
        "approval",
        "mcp",
        "tool_failure",
        "cancel_interrupt",
    }
    for events in cases.values():
        assert compare_runtime_events(events, list(events)).ok is True

    plain = project_runtime_events(cases["plain_answer"])
    assert [event.type for event in cases["plain_answer"]] == [
        "turn.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    assert plain[1].message_block_types == ["text"]
    assert plain[1].frontend_stream == {
        "role": "assistant",
        "last": False,
        "content_types": ["text"],
        "text": "hello",
        "tools": [],
        "message_type": "",
    }
    assert plain[2].frontend_stream and plain[2].frontend_stream["last"] is False
    assert plain[3].frontend_stream == {
        "role": "assistant",
        "last": True,
        "content_types": [],
        "text": "",
        "tools": [],
        "message_type": "",
    }

    thinking = project_runtime_events(cases["thinking"])
    assert [event.message_block_types for event in thinking] == [
        ["thinking"],
        ["text"],
    ]
    assert thinking[0].frontend_stream == {
        "role": "assistant",
        "last": False,
        "content_types": ["thinking"],
        "text": "",
        "tools": [],
        "message_type": "",
    }
    assert thinking[1].frontend_stream and thinking[1].frontend_stream["text"] == "答"

    single_tool = project_runtime_events(cases["single_tool"])
    assert single_tool[0].tool_name == "lookup"
    assert single_tool[0].tool_arguments == {"query": "澜台"}
    assert single_tool[0].frontend_stream
    assert single_tool[0].frontend_stream["content_types"] == ["tool_use"]
    assert single_tool[1].tool_ok is True
    assert single_tool[1].tool_output == [{"text": "found", "type": "text"}]
    assert single_tool[1].frontend_stream
    assert single_tool[1].frontend_stream["content_types"] == ["tool_result"]

    multi_tool = project_runtime_events(cases["multi_tool"])
    assert {
        event.tool_name
        for event in multi_tool
        if event.type == "tool.call.started"
    } == {"lookup", "calculator"}
    assert all(
        event.tool_ok is True
        for event in multi_tool
        if event.type == "tool.call.completed"
    )

    approval = project_runtime_events(cases["approval"])
    assert approval[0].approval_request_id == "approval-1"
    assert approval[0].tool_name == "shell"
    assert approval[0].frontend_stream
    assert approval[0].frontend_stream["message_type"] == "tool_guard_approval"
    assert approval[1].approval_request_id == "approval-1"
    assert approval[1].approval_decision == "approved"

    mcp = project_runtime_events(cases["mcp"])
    assert mcp[0].tool_name == "mcp_echo"
    assert mcp[0].tool_arguments == {"value": "ok"}
    assert mcp[1].tool_output == [{"text": "mcp_echo:ok", "type": "text"}]

    tool_failure = project_runtime_events(cases["tool_failure"])
    assert tool_failure[0].tool_name == "shell"
    assert tool_failure[0].tool_arguments == {"command": "pwd"}
    assert tool_failure[1].status == "failed"
    assert tool_failure[1].tool_ok is False
    assert tool_failure[1].tool_output == [
        {"text": "permission denied", "type": "text"},
    ]

    interrupted = project_runtime_events(cases["cancel_interrupt"])
    assert interrupted[0].type == "turn.interrupted"
    assert interrupted[0].status == "interrupted"


@pytest.mark.asyncio
async def test_dual_run_comparator_rejects_metadata_forged_input() -> None:
    expected = ScriptedRuntimeAdapter(_phase6_fixture_events())
    actual = ScriptedRuntimeAdapter(_phase6_fixture_events())
    comparator = RuntimeDualRunComparator(
        expected_adapter=expected,
        actual_adapter=actual,
    )

    with pytest.raises(DualRunSafetyError, match="metadata cannot enable"):
        await comparator.compare(_plain_input(dual_run_safe=True))

    assert expected.seen_inputs == []
    assert actual.seen_inputs == []


def test_dual_run_comparator_rejects_live_adapter_without_running() -> None:
    calls: list[str] = []
    live_adapter = LiveSideEffectRuntimeAdapter(calls)

    with pytest.raises(DualRunSafetyError, match="side_effect_free=True"):
        RuntimeDualRunComparator(
            expected_adapter=live_adapter,
            actual_adapter=ScriptedRuntimeAdapter(_phase6_fixture_events()),
        )

    assert calls == []


@pytest.mark.asyncio
async def test_dual_run_comparator_runs_scripted_fixture_adapters() -> None:
    expected = ScriptedRuntimeAdapter(_phase6_fixture_events())
    actual = ScriptedRuntimeAdapter(_phase6_fixture_events())
    comparator = RuntimeDualRunComparator(
        expected_adapter=expected,
        actual_adapter=actual,
    )

    report = await comparator.compare(_input())

    assert report.ok is True
    assert expected.seen_inputs and actual.seen_inputs
