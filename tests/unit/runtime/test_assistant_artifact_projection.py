"""Behavior locks for trusted assistant artifact reference projection."""

from __future__ import annotations

import json

import pytest

from lantai.runtime.assistant_artifact_projection import (
    AssistantArtifactProjectionAccumulator,
)
from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
from lantai.runtime.events import RuntimeEvent
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelResponse,
    ModelStreamEvent,
)
from lantai.runtime.tool_response import FileBlock, TextBlock, ToolResponse
from lantai.runtime.tools import LantaiToolRegistry, ToolCall, ToolResult, ToolSpec


ANALYSIS_ID = "a" * 64
REFERENCE = {
    "analysis_id": ANALYSIS_ID,
    "x_column": "region",
    "y_column": "revenue",
    "type": "bar",
}


def _spec(
    *,
    origin: str = "builtin",
    marker: str | None = "analysis_chart_reference",
) -> ToolSpec:
    metadata = {}
    if marker is not None:
        metadata["assistant_artifact_projection"] = marker
    return ToolSpec(
        name="create_analysis_chart",
        origin=origin,
        read_only=True,
        metadata=metadata,
    )


def _result(reference: object = REFERENCE, *, ok: bool = True) -> ToolResult:
    return ToolResult(
        call_id="chart-call",
        name="create_analysis_chart",
        ok=ok,
        metadata={"chat_chart_reference": reference},
    )


def _completed(text: str = "图表已经生成。") -> RuntimeEvent:
    return RuntimeEvent(
        id="event-1",
        seq=7,
        type="message.completed",
        status="completed",
        source="assistant",
        session_id="session-1",
        turn_id="turn-1",
        item_id="message-1",
        summary=text,
        payload={
            "message": {
                "id": "message-1",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": text, "data": {}, "metadata": {}},
                    {"type": "file", "data": {"path": "chart.svg"}, "metadata": {}},
                ],
                "metadata": {"persona_delivery": {"perceptible": True}},
            },
            "source_message_id": "provider-message-1",
        },
    )


def _canonical_fence(reference: dict[str, str]) -> str:
    value = json.dumps(
        reference,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"```analysis-chart\n{value}\n```"


def test_projects_canonical_reference_into_terminal_assistant_message() -> None:
    accumulator = AssistantArtifactProjectionAccumulator()
    accumulator.observe(_spec(), _result())

    original = _completed()
    projected = accumulator.project(original)
    fence = _canonical_fence(REFERENCE)

    assert projected.summary == f"图表已经生成。\n\n{fence}"
    assert projected.payload["message"]["content"][0]["text"] == projected.summary
    assert (
        projected.payload["message"]["content"][1]
        == original.payload["message"]["content"][1]
    )
    assert (
        projected.payload["message"]["metadata"]
        == original.payload["message"]["metadata"]
    )
    assert projected.payload["source_message_id"] == "provider-message-1"
    assert original.summary == "图表已经生成。"


@pytest.mark.parametrize(
    "reference",
    [
        {**REFERENCE, "analysis_id": "A" * 64},
        {**REFERENCE, "analysis_id": "short"},
        {**REFERENCE, "x_column": ""},
        {**REFERENCE, "x_column": "x" * 121},
        {**REFERENCE, "x_column": "revenue"},
        {**REFERENCE, "type": "line"},
        {**REFERENCE, "extra": "not-allowed"},
        "not-a-reference",
    ],
)
def test_rejects_malformed_or_unsupported_references(reference: object) -> None:
    accumulator = AssistantArtifactProjectionAccumulator()
    accumulator.observe(_spec(), _result(reference))

    assert accumulator.project(_completed()) == _completed()


@pytest.mark.parametrize(
    ("spec", "result"),
    [
        (_spec(origin="plugin"), _result()),
        (_spec(marker=None), _result()),
        (_spec(marker="other"), _result()),
        (_spec(), _result(ok=False)),
    ],
)
def test_only_trusted_successful_builtin_results_can_project(
    spec: ToolSpec,
    result: ToolResult,
) -> None:
    accumulator = AssistantArtifactProjectionAccumulator()
    accumulator.observe(spec, result)

    assert accumulator.project(_completed()) == _completed()


def test_structurally_deduplicates_existing_reference() -> None:
    accumulator = AssistantArtifactProjectionAccumulator()
    accumulator.observe(_spec(), _result())
    existing = _canonical_fence(REFERENCE)

    projected = accumulator.project(_completed(f"已完成。\n\n{existing}"))

    assert projected.summary.count("```analysis-chart") == 1


def test_projects_each_observed_reference_at_most_once_across_completions() -> None:
    accumulator = AssistantArtifactProjectionAccumulator()
    accumulator.observe(_spec(), _result())

    first = accumulator.project(_completed("第一条终态。"))
    second = accumulator.project(_completed("第二条终态。"))

    assert first.summary.count("```analysis-chart") == 1
    assert second.summary == "第二条终态。"
    assert not accumulator.has_pending()


def test_pending_completion_contains_only_canonical_trusted_references() -> None:
    accumulator = AssistantArtifactProjectionAccumulator()
    accumulator.observe(_spec(), _result())

    fallback = accumulator.pending_completion(
        seq=8,
        session_id="session-1",
        turn_id="turn-1",
    )

    assert fallback is not None
    assert fallback.type == "message.completed"
    assert fallback.source == "assistant"
    assert fallback.summary == _canonical_fence(REFERENCE)
    assert fallback.payload["message"]["content"] == [
        {
            "type": "text",
            "text": _canonical_fence(REFERENCE),
            "data": {},
            "metadata": {},
        }
    ]
    assert fallback.payload["message"]["metadata"] == {
        "assistant_artifact_projection": "analysis_chart_reference",
    }
    assert accumulator.pending_completion(
        seq=9,
        session_id="session-1",
        turn_id="turn-1",
    ) is None


def test_projects_at_most_eight_ordered_unique_references() -> None:
    accumulator = AssistantArtifactProjectionAccumulator()
    for index in range(10):
        reference = {
            **REFERENCE,
            "analysis_id": f"{index:064x}",
            "x_column": f"region_{index}",
        }
        accumulator.observe(_spec(), _result(reference))
        accumulator.observe(_spec(), _result(reference))

    projected = accumulator.project(_completed())

    assert projected.summary.count("```analysis-chart") == 8
    assert f"{0:064x}" in projected.summary
    assert f"{7:064x}" in projected.summary
    assert f"{8:064x}" not in projected.summary


@pytest.mark.parametrize(
    ("event_type", "status", "source"),
    [
        ("message.delta", "running", "assistant"),
        ("message.completed", "running", "assistant"),
        ("message.completed", "completed", "system"),
    ],
)
def test_never_projects_outside_terminal_assistant_completion(
    event_type: str,
    status: str,
    source: str,
) -> None:
    accumulator = AssistantArtifactProjectionAccumulator()
    accumulator.observe(_spec(), _result())
    event = _completed().model_copy(
        update={"type": event_type, "status": status, "source": source},
    )

    assert accumulator.project(event) == event


@pytest.mark.asyncio
async def test_native_runtime_projects_observed_reference_only_at_final_reply(
    tmp_path,
) -> None:
    registry = LantaiToolRegistry()

    async def trusted_chart() -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text="verified chart")],
            metadata={"chat_chart_reference": REFERENCE},
        )

    registry.register_tool(
        ToolSpec(
            name="trusted_chart",
            origin="builtin",
            read_only=True,
            metadata={
                "assistant_artifact_projection": "analysis_chart_reference",
            },
        ),
        trusted_chart,
    )

    class ScriptedModel:
        provider_id = "test"
        model_name = "artifact-projection"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            if self.calls == 1:
                yield ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="tool-message",
                    tool_call=ToolCall(
                        id="chart-call",
                        name="trusted_chart",
                        arguments={},
                    ),
                )
                yield ModelStreamEvent(
                    kind="message_completed",
                    message_id="tool-message",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="tool-message",
                            role="assistant",
                        ),
                    ),
                )
                return
            yield ModelStreamEvent(
                kind="text_delta",
                message_id="final-message",
                delta="图表已经生成。",
            )
            yield ModelStreamEvent(
                kind="message_completed",
                message_id="final-message",
                response=ModelResponse(
                    message=ModelMessage.text(
                        "assistant",
                        "图表已经生成。",
                        id="final-message",
                    ),
                ),
            )

        async def complete(self, _request):
            return ModelResponse(message=ModelMessage.text("assistant", "完成"))

    model = ScriptedModel()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: model,
        tool_registry=registry,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            run_id="turn-1",
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "做一张图")],
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    completions = [event for event in events if event.type == "message.completed"]

    assert model.calls == 2
    assert len(completions) == 1
    assert completions[0].summary == (
        f"图表已经生成。\n\n{_canonical_fence(REFERENCE)}"
    )
    assert sum(event.type == "message.completed" for event in events) == 1


@pytest.mark.asyncio
async def test_native_runtime_emits_trusted_reference_when_provider_final_is_image_only(
    tmp_path,
) -> None:
    registry = LantaiToolRegistry()

    async def trusted_chart() -> ToolResponse:
        return ToolResponse(
            content=[TextBlock(text="verified chart")],
            metadata={"chat_chart_reference": REFERENCE},
        )

    registry.register_tool(
        ToolSpec(
            name="trusted_chart",
            origin="builtin",
            read_only=True,
            metadata={
                "assistant_artifact_projection": "analysis_chart_reference",
            },
        ),
        trusted_chart,
    )

    class ImageOnlyFinalModel:
        provider_id = "test"
        model_name = "image-only-artifact-projection"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            if self.calls == 1:
                yield ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="tool-message",
                    tool_call=ToolCall(
                        id="chart-call",
                        name="trusted_chart",
                        arguments={},
                    ),
                )
                yield ModelStreamEvent(
                    kind="message_completed",
                    message_id="tool-message",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="tool-message",
                            role="assistant",
                        ),
                    ),
                )
                return
            yield ModelStreamEvent(
                kind="message_completed",
                message_id="image-message",
                response=ModelResponse(
                    message=ModelMessage(
                        id="image-message",
                        role="assistant",
                        content=[
                            ModelContentBlock(
                                type="image",
                                data={"artifact": "provider-owned"},
                            )
                        ],
                    ),
                ),
            )

        async def complete(self, _request):
            return ModelResponse(message=ModelMessage.text("assistant", "完成"))

    model = ImageOnlyFinalModel()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: model,
        tool_registry=registry,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            run_id="turn-1",
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "做一张图")],
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    completions = [event for event in events if event.type == "message.completed"]
    projected = [
        event
        for event in completions
        if "```analysis-chart" in event.summary
    ]

    assert model.calls == 2
    assert len(projected) == 1
    assert projected[0].summary == _canonical_fence(REFERENCE)
    tool_completed_index = next(
        index
        for index, event in enumerate(events)
        if event.type in {"tool.call.completed", "tool.completed"}
        and event.status == "completed"
    )
    projection_index = events.index(projected[0])
    assert projection_index > tool_completed_index


@pytest.mark.asyncio
async def test_native_runtime_projects_delivered_chart_before_partial_failure_terminal(
    tmp_path,
) -> None:
    chart_path = tmp_path / "analysis-chart.svg"
    registry = LantaiToolRegistry()

    async def trusted_chart() -> ToolResponse:
        chart_path.write_text("<svg/>", encoding="utf-8")
        return ToolResponse(
            content=[
                FileBlock(
                    filename=chart_path.name,
                    source={"type": "url", "url": chart_path.as_uri()},
                ),
            ],
            metadata={"chat_chart_reference": REFERENCE},
        )

    registry.register_tool(
        ToolSpec(
            name="trusted_chart",
            origin="builtin",
            read_only=True,
            metadata={
                "assistant_artifact_projection": "analysis_chart_reference",
            },
        ),
        trusted_chart,
    )

    class PartialFailureModel:
        provider_id = "test"
        model_name = "partial-failure-artifact-projection"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            if self.calls == 1:
                yield ModelStreamEvent(
                    kind="tool_call_completed",
                    message_id="tool-message",
                    tool_call=ToolCall(
                        id="chart-call",
                        name="trusted_chart",
                        arguments={},
                    ),
                )
                yield ModelStreamEvent(
                    kind="message_completed",
                    message_id="tool-message",
                    response=ModelResponse(
                        message=ModelMessage(
                            id="tool-message",
                            role="assistant",
                        ),
                    ),
                )
                return
            yield ModelStreamEvent(
                kind="error",
                error="[422] MODEL_EXECUTION_FAILED",
            )

        async def complete(self, _request):
            return ModelResponse(message=ModelMessage.text("assistant", "完成"))

    model = PartialFailureModel()
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: model,
        tool_registry=registry,
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            run_id="turn-1",
            workspace_dir=str(tmp_path),
        ),
        messages=[RuntimeMessage.text("user", "做一张图")],
    )

    events = [event async for event in runtime.run_turn(runtime_input)]
    projected = [
        event
        for event in events
        if event.type == "message.completed"
        and "```analysis-chart" in event.summary
    ]

    assert model.calls == 2
    assert len(projected) == 1
    assert projected[0].summary == _canonical_fence(REFERENCE)
    assert events.index(projected[0]) < len(events) - 1
    assert not any(event.type == "turn.failed" for event in events)
    assert events[-1].type == "turn.completed"
    assert events[-1].payload == {
        "outcome_status": "incomplete",
        "details": {"partial_delivery": True},
        "error_type": "RuntimeError",
    }
