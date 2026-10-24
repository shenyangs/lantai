# -*- coding: utf-8 -*-
"""Do not execute valid-looking tool arguments from a truncated response."""

from pathlib import Path

import pytest

from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)
from lantai.runtime.tools import LantaiToolRegistry, ToolCall, ToolSpec


class ScriptedClient:
    provider_id = "test"
    model_name = "test-model"

    def __init__(self, scripts):
        self.scripts = iter(scripts)
        self.requests: list[ModelRequest] = []

    async def stream(self, request):
        self.requests.append(request.model_copy(deep=True))
        script = next(self.scripts, None)
        if script is None:
            raise AssertionError("Unexpected extra model request")
        for event in script:
            yield event

    async def complete(self, _request):
        raise AssertionError("This probe must only use the main stream")


def tool_round(reason, *, streamed=False, prefix="cut"):
    calls = [
        ToolCall(
            id=f"{prefix}-{index}",
            name="record_value",
            arguments={"value": f"{prefix}-{index}"},
        )
        for index in range(2)
    ]
    response = ModelResponse(
        message=ModelMessage(
            role="assistant",
            content=[
                ModelContentBlock(type="tool_call", data=call.model_dump())
                for call in calls
            ],
        ),
        finish_reason=reason,
    )
    events = (
        [
            ModelStreamEvent(kind="tool_call_completed", tool_call=call)
            for call in calls
        ]
        if streamed
        else []
    )
    return [
        *events,
        ModelStreamEvent(kind="message_completed", response=response),
    ]


def final_round():
    return [
        ModelStreamEvent(
            kind="message_completed",
            response=ModelResponse(
                message=ModelMessage.text("assistant", "Done."),
                finish_reason="stop",
            ),
        ),
    ]


def make_runtime(client, executed, tmp_path):
    def record_value(value: str):
        executed.append(value)
        return value

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(
            name="record_value",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        ),
        record_value,
    )
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: client,
        tool_registry=registry,
    )
    request = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="probe-session",
            run_id="probe-run",
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "Record the values.")],
    )
    return runtime, request


@pytest.mark.parametrize(
    "finish_reason",
    ["length", "max_tokens", "max_output_tokens", "token_limit"],
)
@pytest.mark.parametrize("streamed", [False, True])
async def test_truncation_rejects_entire_valid_batch(
    finish_reason: str,
    streamed: bool,
    tmp_path: Path,
) -> None:
    client = ScriptedClient(
        [
            tool_round(finish_reason, streamed=streamed),
            final_round(),
        ],
    )
    executed: list[str] = []
    runtime, request = make_runtime(client, executed, tmp_path)
    # Best-effort prose is not permission to execute an incomplete call.
    request.metadata["native_accept_truncated_best_effort"] = True

    events = [event async for event in runtime.run_turn(request)]

    assert not executed
    assert events[-1].type == "turn.completed"
    assert not any(event.type == "tool.started" for event in events)
    repair = client.requests[1].messages[-3:]
    assert [message.role for message in repair] == [
        "assistant",
        "tool",
        "tool",
    ]
    assert [message.tool_call_id for message in repair[1:]] == [
        "cut-0",
        "cut-1",
    ]
    for message in repair[1:]:
        assert "truncat" in message.model_dump_json().lower()


async def test_reissued_complete_batch_executes_once(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            tool_round("length"),
            tool_round("tool_calls", prefix="fixed"),
            final_round(),
        ],
    )
    executed: list[str] = []
    runtime, request = make_runtime(client, executed, tmp_path)

    events = [event async for event in runtime.run_turn(request)]

    assert executed == ["fixed-0", "fixed-1"]
    assert events[-1].type == "turn.completed"
    assert len(client.requests) == 3


async def test_event_only_truncation_reason_rejects_tool_batch(
    tmp_path: Path,
) -> None:
    truncated = tool_round(None)
    truncated[-1].finish_reason = "length"
    client = ScriptedClient(
        [
            truncated,
            tool_round("tool_calls", prefix="fixed"),
            final_round(),
        ],
    )
    executed: list[str] = []
    runtime, request = make_runtime(client, executed, tmp_path)

    events = [event async for event in runtime.run_turn(request)]

    assert executed == ["fixed-0", "fixed-1"]
    assert events[-1].type == "turn.completed"
    assert len(client.requests) == 3


async def test_repeated_truncation_uses_existing_repair_budget(tmp_path: Path):
    client = ScriptedClient([tool_round("length") for _ in range(3)])
    executed: list[str] = []
    runtime, request = make_runtime(client, executed, tmp_path)
    events = []

    with pytest.raises(RuntimeError):
        async for event in runtime.run_turn(request):
            events.append(event)

    assert not executed
    assert len(client.requests) == 3
    assert events[-1].type == "turn.failed"
    assert events[-1].payload["error_code"] == "tool_argument_repair_exhausted"
