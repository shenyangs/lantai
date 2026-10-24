# -*- coding: utf-8 -*-
"""Behavior contracts for one provider-neutral model round."""

from lantai.runtime.model_round import ModelRoundAccumulator
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
)
from lantai.runtime.tools import ToolCall


def test_round_merges_stream_and_final_tool_calls_by_stable_id() -> None:
    accumulator = ModelRoundAccumulator()
    accumulator.accept(
        ModelStreamEvent(
            kind="tool_call_completed",
            message_id="message-1",
            tool_call=ToolCall(
                id="call-1",
                name="draft_name",
                arguments={"path": "draft"},
                metadata={"stream": True},
            ),
        ),
    )
    usage = ModelUsage(input_tokens=4, output_tokens=3, total_tokens=7)
    response = ModelResponse(
        message=ModelMessage(
            id="message-1",
            role="assistant",
            content=[
                ModelContentBlock(
                    type="tool_call",
                    data={
                        "id": "call-1",
                        "name": "write_file",
                        "arguments": {"path": "final"},
                    },
                ),
                ModelContentBlock(
                    type="tool_call",
                    data={
                        "id": "call-2",
                        "name": "send_file",
                        "input": {"path": "final"},
                    },
                ),
            ],
        ),
        usage=usage,
        finish_reason="tool_calls",
    )
    accumulator.accept(
        ModelStreamEvent(
            kind="message_completed",
            message_id="message-1",
            response=response,
        ),
    )

    result = accumulator.finish()

    assert [call.id for call in result.tool_calls] == ["call-1", "call-2"]
    assert result.tool_calls[0].name == "write_file"
    assert result.tool_calls[0].arguments == {"path": "final"}
    assert result.tool_calls[0].metadata == {
        "stream": True,
        "source": "model_response",
    }
    assert result.finish_reason == "tool_calls"
    assert result.usage == usage
    assert result.stream_message_ids == frozenset({"message-1"})


def test_round_reports_provider_error_without_inventing_a_final_response() -> (
    None
):
    accumulator = ModelRoundAccumulator()
    accumulator.accept(
        ModelStreamEvent(
            kind="message_start",
            message_id="message-error",
        ),
    )
    accumulator.accept(
        ModelStreamEvent(
            kind="error",
            message_id="message-error",
            error="provider connection closed",
        ),
    )

    result = accumulator.finish(termination="provider_error")

    assert result.response is None
    assert not result.tool_calls
    assert result.termination == "provider_error"
    assert result.model_error == "provider connection closed"
    assert result.protocol_findings == (
        "final_response_missing",
        "provider_error_event",
    )


def test_round_preserves_event_usage_and_final_usage_takes_precedence() -> (
    None
):
    accumulator = ModelRoundAccumulator()
    event_usage = ModelUsage(input_tokens=3, output_tokens=2, total_tokens=5)
    final_usage = ModelUsage(input_tokens=5, output_tokens=3, total_tokens=8)
    accumulator.accept(ModelStreamEvent(kind="usage", usage=event_usage))
    accumulator.accept(
        ModelStreamEvent(
            kind="message_completed",
            response=ModelResponse(
                message=ModelMessage.text("assistant", "done"),
                usage=final_usage,
            ),
        ),
    )

    result = accumulator.finish()

    assert result.usage == final_usage

    error_round = ModelRoundAccumulator()
    error_round.accept(ModelStreamEvent(kind="usage", usage=event_usage))
    error_round.accept(ModelStreamEvent(kind="error", error="failed"))
    assert (
        error_round.finish(termination="provider_error").usage == event_usage
    )


def test_t038_missing_provider_cost_stays_unknown_instead_of_becoming_free() -> None:
    usage = ModelUsage(input_tokens=4, output_tokens=3, total_tokens=7)

    assert usage.cost_cny is None
    assert ModelUsage(cost_cny=0).cost_cny == 0
