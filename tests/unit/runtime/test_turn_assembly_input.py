# -*- coding: utf-8 -*-
"""Behavior locks for the S7 Snapshot-to-Native input seam."""

from __future__ import annotations

import pytest

from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.turn_assembly import (
    REQUIRED_TURN_ASSEMBLY_SURFACES,
    TurnAssemblySurface,
    build_turn_assembly_snapshot,
)
from lantai.runtime.tools import LantaiToolRegistry, ToolPolicyContext


def _runtime_input() -> RuntimeInput:
    return RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-private",
            user_id="user-private",
            run_id="run-private",
            workspace_dir="/private/workspace",
            metadata={
                "prompt_manifest": {
                    "schema_version": "1",
                    "sections": [],
                },
                "persona_turn_plan": {"conversation_mode": "work"},
                "ququ_hard_required": True,
                "legacy_request_context": {
                    "workspace_access_mode": "read_write",
                    "workspace_trust_state": "trusted",
                },
            },
        ),
        messages=[
            RuntimeMessage.text("system", "PRIVATE SYSTEM", id="system-1"),
            RuntimeMessage.text("user", "PRIVATE USER", id="user-1"),
        ],
        model_slot="primary",
        tool_policy_context={"permission_mode": "default"},
        metadata={"runtime_engine": "native"},
    )


def _snapshot(
    runtime_input: RuntimeInput,
    *,
    memory_value: dict | None = None,
    request_value: dict | None = None,
    turn_id: str = "run-private",
):
    values = {name: {} for name in REQUIRED_TURN_ASSEMBLY_SURFACES}
    values["memory_context"] = {
        "projected_messages": [
            message.model_dump(mode="json")
            for message in runtime_input.messages
        ]
    }
    values["request_policy"] = {
        "runtime_request": runtime_input.request.model_dump(mode="json"),
        "model_slot": runtime_input.model_slot,
        "tool_policy_context": runtime_input.tool_policy_context,
        "runtime_metadata": runtime_input.metadata,
        "native_tool_policy": {},
    }
    if memory_value is not None:
        values["memory_context"] = memory_value
    if request_value is not None:
        values["request_policy"] = request_value
    return build_turn_assembly_snapshot(
        snapshot_id="snapshot-private",
        turn_id=turn_id,
        surfaces=[
            TurnAssemblySurface(name=name, value=values[name])
            for name in REQUIRED_TURN_ASSEMBLY_SURFACES
        ],
    )


def test_snapshot_input_failures_are_fixed_private_reason_codes() -> None:
    from lantai.runtime.turn_assembly_input import (
        TurnAssemblyInputUnavailable,
        runtime_input_from_turn_assembly_snapshot,
    )

    original = _runtime_input()
    cases = [
        (object(), "invalid_snapshot"),
        (_snapshot(original, memory_value={}), "memory_projection_missing"),
        (_snapshot(original, request_value={}), "request_projection_missing"),
        (_snapshot(original, turn_id="stale-turn"), "turn_binding_mismatch"),
        (
            _snapshot(
                original,
                memory_value={"projected_messages": [{"role": None}]},
            ),
            "runtime_input_validation_failed",
        ),
    ]

    for value, reason_code in cases:
        try:
            runtime_input_from_turn_assembly_snapshot(value)
        except TurnAssemblyInputUnavailable as exc:
            assert exc.reason_code == reason_code
            assert "PRIVATE" not in str(exc)
            assert "snapshot-private" not in str(exc)
        else:
            raise AssertionError(
                "invalid snapshot input unexpectedly succeeded"
            )


def test_snapshot_rehydrates_an_exact_detached_native_runtime_input() -> None:
    from lantai.runtime.turn_assembly_input import (
        runtime_input_from_turn_assembly_snapshot,
        runtime_inputs_are_exactly_equivalent,
    )

    original = _runtime_input()
    snapshot = _snapshot(original)

    rehydrated = runtime_input_from_turn_assembly_snapshot(snapshot)

    assert type(rehydrated) is RuntimeInput
    assert rehydrated is not original
    assert rehydrated.request is not original.request
    assert all(
        actual is not expected
        for actual, expected in zip(rehydrated.messages, original.messages)
    )
    assert rehydrated.model_dump(mode="json") == original.model_dump(
        mode="json"
    )
    assert runtime_inputs_are_exactly_equivalent(rehydrated, original)
    original.messages[-1].content[0].text = "MUTATED AFTER SNAPSHOT"
    assert rehydrated.messages[-1].text_content() == "PRIVATE USER"
    rehydrated.messages[-1].content[0].text = "MUTATED REHYDRATED COPY"
    assert (
        runtime_input_from_turn_assembly_snapshot(snapshot)
        .messages[-1]
        .text_content()
        == "PRIVATE USER"
    )


def test_real_shadow_adapter_round_trips_the_native_runtime_input() -> None:
    from lantai.runtime.adapters.legacy_turn_assembly import (
        capture_shadow_turn_assembly,
    )
    from lantai.runtime.turn_assembly_input import (
        runtime_input_from_turn_assembly_snapshot,
    )

    original = _runtime_input()
    capture = capture_shadow_turn_assembly(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        runtime_input=original,
        memory_messages=[original.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[original.messages[1]],
        attachment_manifest_message=None,
        tool_registry=LantaiToolRegistry(),
        tool_policy_context=ToolPolicyContext(),
    )

    assert capture.status == "captured"
    assert capture.snapshot is not None
    actual = runtime_input_from_turn_assembly_snapshot(capture.snapshot)
    assert actual.model_dump(mode="json") == original.model_dump(mode="json")


def test_exact_equivalence_rejects_json_equal_container_type_changes() -> None:
    from lantai.runtime.turn_assembly_input import (
        runtime_inputs_are_exactly_equivalent,
    )

    left = _runtime_input()
    right = left.model_copy(deep=True)
    left.metadata["sequence"] = ("one", "two")
    right.metadata["sequence"] = ["one", "two"]

    assert left.model_dump(mode="json") == right.model_dump(mode="json")
    assert runtime_inputs_are_exactly_equivalent(left, right) is False


def test_snapshot_input_rejects_hostile_wrong_types_without_hooks() -> None:
    from lantai.runtime.turn_assembly_input import (
        TurnAssemblyInputUnavailable,
        runtime_input_from_turn_assembly_snapshot,
        runtime_inputs_are_exactly_equivalent,
    )

    calls: list[str] = []

    class HostileRuntimeInput(RuntimeInput):
        def model_dump(self, *args, **kwargs):
            calls.append("model_dump")
            raise AssertionError("wrong exact types must not be inspected")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("private values must not reach repr")

    hostile = HostileRuntimeInput.model_construct(request=object())
    with pytest.raises(TurnAssemblyInputUnavailable) as captured:
        runtime_input_from_turn_assembly_snapshot(hostile)

    assert captured.value.reason_code == "invalid_snapshot"
    assert (
        runtime_inputs_are_exactly_equivalent(hostile, _runtime_input())
        is False
    )
    assert calls == []


def test_real_snapshot_input_round_trip_is_stable_across_100_turns() -> None:
    from lantai.runtime.adapters.legacy_turn_assembly import (
        capture_shadow_turn_assembly,
    )
    from lantai.runtime.turn_assembly_input import (
        runtime_input_from_turn_assembly_snapshot,
        runtime_inputs_are_exactly_equivalent,
    )

    for index in range(100):
        original = _runtime_input()
        turn_id = f"run-{index}"
        original.request.run_id = turn_id
        original.request.session_id = f"session-{index % 7}"
        original.request.metadata["replay"] = {
            "index": index,
            "flags": [index % 2 == 0, None, f"value-{index}"],
        }
        original.messages[-1].content[0].text = f"PRIVATE USER {index}"
        capture = capture_shadow_turn_assembly(
            snapshot_id=f"snapshot-{index}",
            turn_id=turn_id,
            runtime_input=original,
            memory_messages=[original.messages[0]],
            recall_messages=[],
            plan_hint_message=None,
            turn_messages=[original.messages[1]],
            attachment_manifest_message=None,
            tool_registry=LantaiToolRegistry(),
            tool_policy_context=ToolPolicyContext(),
        )

        assert capture.status == "captured"
        assert capture.snapshot is not None
        actual = runtime_input_from_turn_assembly_snapshot(capture.snapshot)
        assert runtime_inputs_are_exactly_equivalent(actual, original)
