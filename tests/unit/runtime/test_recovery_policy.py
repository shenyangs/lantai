# -*- coding: utf-8 -*-
"""Behavior contracts for model-round recovery ownership."""

from lantai.runtime.model_round import ModelRoundAccumulator
from lantai.runtime.models import (
    ModelContentBlock,
    ModelMessage,
    ModelResponse,
    ModelStreamEvent,
)
from lantai.runtime.tools import ToolCall
from lantai.runtime.recovery_policy import (
    ModelRoundRecoveryState,
    decide_model_round_recovery,
)


def _truncated_text_round():
    accumulator = ModelRoundAccumulator()
    accumulator.accept(
        ModelStreamEvent(
            kind="message_completed",
            response=ModelResponse(
                message=ModelMessage.text("assistant", "unfinished"),
                finish_reason="length",
            ),
        ),
    )
    return accumulator.finish()


def test_truncated_text_has_one_bounded_recovery_owner() -> None:
    round_result = _truncated_text_round()

    first = decide_model_round_recovery(
        round_result,
        ModelRoundRecoveryState(
            output_recovery_attempts=0,
            output_recovery_limit=1,
        ),
    )
    exhausted = decide_model_round_recovery(
        round_result,
        ModelRoundRecoveryState(
            output_recovery_attempts=1,
            output_recovery_limit=1,
        ),
    )

    assert first.action == "recover_output"
    assert first.reason_code == "model_output_truncated"
    assert first.next_output_recovery_attempts == 1
    assert exhausted.action == "fail_incomplete_output"
    assert exhausted.next_output_recovery_attempts == 1


def test_tool_round_is_owned_by_tool_processing_before_output_recovery() -> (
    None
):
    accumulator = ModelRoundAccumulator()
    accumulator.accept(
        ModelStreamEvent(
            kind="tool_call_completed",
            tool_call=ToolCall(
                id="call-1",
                name="write_file",
                arguments={"path": "possibly-cut"},
            ),
        ),
    )
    accumulator.accept(
        ModelStreamEvent(
            kind="message_completed",
            response=ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content=[
                        ModelContentBlock(
                            type="tool_call",
                            data={
                                "id": "call-1",
                                "name": "write_file",
                                "arguments": {"path": "possibly-cut"},
                            },
                        ),
                    ],
                ),
                finish_reason="length",
            ),
        ),
    )

    decision = decide_model_round_recovery(
        accumulator.finish(),
        ModelRoundRecoveryState(),
    )

    assert decision.action == "process_tools"
    assert decision.reason_code == "tool_calls_observed"


def test_provider_error_prevents_tool_or_output_recovery() -> None:
    accumulator = ModelRoundAccumulator()
    accumulator.accept(
        ModelStreamEvent(
            kind="tool_call_completed",
            tool_call=ToolCall(
                id="call-1",
                name="write_file",
                arguments={"path": "unsafe"},
            ),
        ),
    )
    accumulator.accept(
        ModelStreamEvent(
            kind="error",
            error="provider failed after partial output",
        ),
    )

    decision = decide_model_round_recovery(
        accumulator.finish(),
        ModelRoundRecoveryState(),
    )

    assert decision.action == "fail_provider"
    assert decision.reason_code == "provider_error"


def test_best_effort_truncation_does_not_accept_degenerate_output() -> None:
    decision = decide_model_round_recovery(
        _truncated_text_round(),
        ModelRoundRecoveryState(
            accept_truncated_best_effort=True,
            degenerate_output=True,
        ),
    )

    assert decision.action == "fail_invalid_output"
    assert decision.reason_code == "placeholder_flood"
