# -*- coding: utf-8 -*-
"""Rehydrate the exact Native RuntimeInput from an immutable S7 snapshot."""

from __future__ import annotations

from typing import Any, Final

from .input import RuntimeInput, RuntimeRequestContext
from .messages import RuntimeMessage
from .turn_assembly import (
    FrozenJsonObject,
    TurnAssemblySnapshot,
    thaw_turn_assembly_value,
)


_INPUT_REASON_CODES: Final = frozenset(
    {
        "invalid_snapshot",
        "memory_projection_missing",
        "request_projection_missing",
        "runtime_input_validation_failed",
        "turn_binding_mismatch",
        "unknown_input_failure",
    }
)


def _safe_reason_code(reason_code: object) -> str:
    if type(reason_code) is str and reason_code in _INPUT_REASON_CODES:
        return reason_code
    return "unknown_input_failure"


class TurnAssemblyInputUnavailable(ValueError):
    """A v1 snapshot cannot safely supply the Native Runtime input."""

    def __init__(self, reason_code: object) -> None:
        super().__init__("Turn assembly Runtime input is unavailable")
        self.reason_code = _safe_reason_code(reason_code)


def _object_surface(
    snapshot: TurnAssemblySnapshot,
    name: str,
    *,
    reason_code: str,
) -> dict[str, object]:
    surface = snapshot.surface(name)
    if type(surface.value) is not FrozenJsonObject:
        raise TurnAssemblyInputUnavailable(reason_code)
    payload = thaw_turn_assembly_value(surface.value)
    if type(payload) is not dict:
        raise TurnAssemblyInputUnavailable(reason_code)
    return payload


def runtime_input_from_turn_assembly_snapshot(
    snapshot: TurnAssemblySnapshot,
) -> RuntimeInput:
    """Build a detached RuntimeInput without consulting pre-snapshot state."""

    if type(snapshot) is not TurnAssemblySnapshot:
        raise TurnAssemblyInputUnavailable("invalid_snapshot")
    try:
        memory = _object_surface(
            snapshot,
            "memory_context",
            reason_code="memory_projection_missing",
        )
        request_policy = _object_surface(
            snapshot,
            "request_policy",
            reason_code="request_projection_missing",
        )
        message_rows = memory.get("projected_messages")
        request_row = request_policy.get("runtime_request")
        model_slot = request_policy.get("model_slot")
        tool_policy_context = request_policy.get("tool_policy_context")
        runtime_metadata = request_policy.get("runtime_metadata")
        if type(message_rows) is not list or any(
            type(row) is not dict for row in message_rows
        ):
            raise TurnAssemblyInputUnavailable("memory_projection_missing")
        if (
            type(request_row) is not dict
            or (model_slot is not None and type(model_slot) is not str)
            or type(tool_policy_context) is not dict
            or type(runtime_metadata) is not dict
        ):
            raise TurnAssemblyInputUnavailable("request_projection_missing")
        request = RuntimeRequestContext.model_validate(request_row)
        runtime_input = RuntimeInput(
            request=request,
            messages=[
                RuntimeMessage.model_validate(row) for row in message_rows
            ],
            model_slot=model_slot,
            tool_policy_context=tool_policy_context,
            metadata=runtime_metadata,
        )
    except TurnAssemblyInputUnavailable:
        raise
    except Exception:
        raise TurnAssemblyInputUnavailable(
            "runtime_input_validation_failed"
        ) from None

    effective_turn_id = runtime_input.request.run_id or (
        runtime_input.request.session_id
    )
    if effective_turn_id != snapshot.turn_id:
        raise TurnAssemblyInputUnavailable("turn_binding_mismatch")
    return runtime_input


def _exact_builtin_value_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if tuple(left) != tuple(right):
            return False
        return all(
            _exact_builtin_value_equal(left[key], right[key]) for key in left
        )
    if type(left) in {list, tuple}:
        return len(left) == len(right) and all(
            _exact_builtin_value_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if left is None or type(left) in {str, bool, int, float}:
        return bool(left == right)
    return False


def runtime_inputs_are_exactly_equivalent(
    left: RuntimeInput,
    right: RuntimeInput,
) -> bool:
    """Compare exact built-in input payload types, order, and values."""

    if type(left) is not RuntimeInput or type(right) is not RuntimeInput:
        return False
    try:
        left_payload = left.model_dump(mode="python")
        right_payload = right.model_dump(mode="python")
    except Exception:
        return False
    return _exact_builtin_value_equal(left_payload, right_payload)


__all__ = [
    "TurnAssemblyInputUnavailable",
    "runtime_input_from_turn_assembly_snapshot",
    "runtime_inputs_are_exactly_equivalent",
]
