# -*- coding: utf-8 -*-
"""Pure ownership decisions for one completed model round."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .model_round import ModelRoundResult


ModelRoundRecoveryAction = Literal[
    "accept_response",
    "process_tools",
    "recover_output",
    "fail_incomplete_output",
    "fail_invalid_output",
    "fail_missing_response",
    "fail_provider",
    "fail_timeout",
    "cancel",
]

TRUNCATED_FINISH_REASONS = frozenset(
    {"length", "max_tokens", "max_output_tokens", "token_limit"},
)


@dataclass(frozen=True, slots=True)
class ModelRoundRecoveryState:
    """Existing recovery state supplied without introducing a new budget."""

    output_recovery_attempts: int = 0
    output_recovery_limit: int = 1
    accept_truncated_best_effort: bool = False
    degenerate_output: bool = False


@dataclass(frozen=True, slots=True)
class ModelRoundRecoveryDecision:
    """Exactly one action owned by the caller after a model round."""

    action: ModelRoundRecoveryAction
    reason_code: str
    next_output_recovery_attempts: int


def _decision(
    action: ModelRoundRecoveryAction,
    reason_code: str,
    state: ModelRoundRecoveryState,
    *,
    next_attempts: int | None = None,
) -> ModelRoundRecoveryDecision:
    return ModelRoundRecoveryDecision(
        action=action,
        reason_code=reason_code,
        next_output_recovery_attempts=(
            state.output_recovery_attempts
            if next_attempts is None
            else next_attempts
        ),
    )


def decide_model_round_recovery(
    result: ModelRoundResult,
    state: ModelRoundRecoveryState,
) -> ModelRoundRecoveryDecision:
    """Choose one next action without I/O, retries, or state mutation."""

    if result.termination == "cancelled":
        action: ModelRoundRecoveryAction = "cancel"
        reason_code = "model_round_cancelled"
    elif result.termination == "timeout":
        action = "fail_timeout"
        reason_code = "model_round_timeout"
    elif result.termination == "provider_error" or result.model_error:
        action = "fail_provider"
        reason_code = "provider_error"
    elif not result.saw_event or (
        result.response is None and not result.tool_calls
    ):
        action = "fail_missing_response"
        reason_code = "final_response_missing"
    elif result.tool_calls:
        action = "process_tools"
        reason_code = "tool_calls_observed"
    elif (
        result.finish_reason in TRUNCATED_FINISH_REASONS
        and not state.accept_truncated_best_effort
    ):
        reason_code = "model_output_truncated"
        if state.output_recovery_attempts < state.output_recovery_limit:
            action = "recover_output"
            return _decision(
                action,
                reason_code,
                state,
                next_attempts=state.output_recovery_attempts + 1,
            )
        action = "fail_incomplete_output"
    elif state.degenerate_output:
        action = "fail_invalid_output"
        reason_code = "placeholder_flood"
    else:
        action = "accept_response"
        reason_code = (
            "truncated_best_effort_accepted"
            if result.finish_reason in TRUNCATED_FINISH_REASONS
            else "response_complete"
        )
    return _decision(
        action,
        reason_code,
        state,
    )


__all__ = [
    "ModelRoundRecoveryAction",
    "ModelRoundRecoveryDecision",
    "ModelRoundRecoveryState",
    "TRUNCATED_FINISH_REASONS",
    "decide_model_round_recovery",
]
