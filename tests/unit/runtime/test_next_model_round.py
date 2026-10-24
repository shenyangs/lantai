# -*- coding: utf-8 -*-
"""Behavior contracts for preparing a subsequent model round."""

import pytest

from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.models import ModelMessage
from lantai.runtime.next_model_round import (
    ModelRoundLineageError,
    capture_model_round_lineage,
    prepare_next_model_round,
)
from lantai.runtime.tools import ToolSpec


def _runtime_input() -> RuntimeInput:
    return RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            run_id="run-1",
            workspace_dir="/tmp/workspace",
        ),
        messages=[RuntimeMessage.text("user", "original request")],
        model_slot="provider/model",
        tool_policy_context={"workspace_dir": "/tmp/workspace"},
        metadata={
            "attempt_id": "attempt-1",
            "runtime_generation": 2,
            "runtime_revision": 7,
            "contract_hash": "a" * 64,
            "policy_snapshot_hash": "b" * 64,
        },
    )


def test_prepared_round_is_detached_from_mutable_source_collections() -> None:
    runtime_input = _runtime_input()
    messages = [ModelMessage.text("user", "first")]
    tools = [ToolSpec(name="read_file", read_only=True)]
    lineage = capture_model_round_lineage(
        runtime_input,
        allowed_tools=tools,
        model_slot=runtime_input.model_slot or "",
        tool_policy_context=runtime_input.tool_policy_context,
    )
    metadata = {
        "session_id": "session-1",
        "turn_id": "run-1",
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "runtime_generation": 2,
        "runtime_revision": 7,
        "contract_hash": "a" * 64,
        "policy_snapshot_hash": "b" * 64,
        "model_request_id": "run-1:model:1",
    }

    prepared = prepare_next_model_round(
        lineage=lineage,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        model_slot=runtime_input.model_slot,
        tool_policy_context=runtime_input.tool_policy_context,
        generation={"max_tokens": 512},
        metadata=metadata,
    )
    messages[0].content[0].text = "changed"
    tools[0].name = "write_file"
    metadata["attempt_id"] = "different"

    assert prepared.request.messages[0].text_content() == "first"
    assert prepared.request.tools[0].name == "read_file"
    assert prepared.request.metadata["attempt_id"] == "attempt-1"
    assert (
        prepared.request.metadata["model_round_lineage_sha256"]
        == lineage.binding_sha256
    )
    assert prepared.lineage_sha256 == lineage.binding_sha256


def test_prepared_round_rejects_attempt_or_policy_drift() -> None:
    runtime_input = _runtime_input()
    lineage = capture_model_round_lineage(
        runtime_input,
        allowed_tools=[],
        model_slot=runtime_input.model_slot or "",
        tool_policy_context=runtime_input.tool_policy_context,
    )

    for changed in (
        {"attempt_id": "attempt-2"},
        {"policy_snapshot_hash": "c" * 64},
        {"runtime_generation": 3},
    ):
        with pytest.raises(ModelRoundLineageError):
            prepare_next_model_round(
                lineage=lineage,
                messages=[ModelMessage.text("user", "request")],
                tools=[],
                tool_choice="auto",
                model_slot=runtime_input.model_slot,
                tool_policy_context=runtime_input.tool_policy_context,
                metadata=changed,
            )


def test_lineage_rejects_hostile_policy_values_without_running_hooks() -> None:
    hooks: list[str] = []

    class Hostile:
        def __getattr__(self, name: str):
            hooks.append(name)
            raise AssertionError("lineage capture must not call object hooks")

    runtime_input = _runtime_input()
    runtime_input.tool_policy_context = {"hostile": Hostile()}

    with pytest.raises(ModelRoundLineageError):
        capture_model_round_lineage(
            runtime_input,
            allowed_tools=[],
            model_slot=runtime_input.model_slot or "",
            tool_policy_context=runtime_input.tool_policy_context,
        )

    assert not hooks


def test_prepared_round_detaches_nested_generation_and_metadata() -> None:
    runtime_input = _runtime_input()
    lineage = capture_model_round_lineage(
        runtime_input,
        allowed_tools=[],
        model_slot=runtime_input.model_slot or "",
        tool_policy_context=runtime_input.tool_policy_context,
    )
    generation = {"response_format": {"type": "json_object"}}
    metadata = {"diagnostics": {"request": 1}}

    prepared = prepare_next_model_round(
        lineage=lineage,
        messages=[ModelMessage.text("user", "request")],
        tools=[],
        tool_choice="auto",
        model_slot=runtime_input.model_slot,
        tool_policy_context=runtime_input.tool_policy_context,
        generation=generation,
        metadata=metadata,
    )
    generation["response_format"]["type"] = "text"  # type: ignore[index]
    metadata["diagnostics"]["request"] = 2  # type: ignore[index]

    assert prepared.request.generation == {
        "response_format": {"type": "json_object"},
    }
    assert prepared.request.metadata["diagnostics"] == {"request": 1}


def test_prepared_round_rejects_model_tool_or_tool_policy_drift() -> None:
    runtime_input = _runtime_input()
    allowed_tool = ToolSpec(
        name="read_file",
        input_schema={"type": "object"},
        read_only=True,
    )
    lineage = capture_model_round_lineage(
        runtime_input,
        allowed_tools=[allowed_tool],
        model_slot=runtime_input.model_slot or "",
        tool_policy_context=runtime_input.tool_policy_context,
    )

    changed_tool = allowed_tool.model_copy(deep=True)
    changed_tool.read_only = False
    cases = (
        {
            "model_slot": "other/model",
            "tools": [allowed_tool],
            "tool_policy_context": runtime_input.tool_policy_context,
        },
        {
            "model_slot": runtime_input.model_slot,
            "tools": [changed_tool],
            "tool_policy_context": runtime_input.tool_policy_context,
        },
        {
            "model_slot": runtime_input.model_slot,
            "tools": [allowed_tool],
            "tool_policy_context": {"workspace_dir": "/other"},
        },
    )
    for changed in cases:
        with pytest.raises(ModelRoundLineageError):
            prepare_next_model_round(
                lineage=lineage,
                messages=[ModelMessage.text("user", "request")],
                tools=changed["tools"],
                tool_choice="auto",
                model_slot=changed["model_slot"],
                tool_policy_context=changed["tool_policy_context"],
            )


def test_route_snapshot_is_detached_and_protected_across_model_rounds() -> None:
    runtime_input = _runtime_input()
    route = {
        "model_id": "minimax-m2.7",
        "protocol": "openai:chat-completions",
        "verification_revision": "release-2026-09-12",
    }
    lineage = capture_model_round_lineage(
        runtime_input,
        allowed_tools=[],
        model_slot=runtime_input.model_slot or "",
        tool_policy_context=runtime_input.tool_policy_context,
        model_route=route,
    )
    route["protocol"] = "anthropic:messages"

    prepared = prepare_next_model_round(
        lineage=lineage,
        messages=[ModelMessage.text("user", "request")],
        tools=[],
        tool_choice="auto",
        model_slot=runtime_input.model_slot,
        tool_policy_context=runtime_input.tool_policy_context,
    )

    assert lineage.model_route["run_id"] == "run-1"
    assert lineage.model_route["protocol"] == "openai:chat-completions"
    assert prepared.request.metadata["model_route"] == lineage.model_route
    assert (
        prepared.request.metadata["model_route_sha256"]
        == lineage.model_route_sha256
    )
    with pytest.raises(ModelRoundLineageError):
        prepare_next_model_round(
            lineage=lineage,
            messages=[ModelMessage.text("user", "request")],
            tools=[],
            tool_choice="auto",
            model_slot=runtime_input.model_slot,
            tool_policy_context=runtime_input.tool_policy_context,
            metadata={
                "model_route": {
                    **lineage.model_route,
                    "protocol": "anthropic:messages",
                },
            },
        )
