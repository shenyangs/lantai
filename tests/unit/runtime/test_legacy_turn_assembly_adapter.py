# -*- coding: utf-8 -*-
"""Locks for the S7 shadow-only Legacy intelligence projection."""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from lantai.runtime.adapters import legacy_turn_assembly
from lantai.runtime.adapters.legacy_turn_assembly import (
    ShadowTurnAssemblyCapture,
    TurnAssemblyShadowUnavailable,
    capture_shadow_turn_assembly,
)
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.tools import (
    LantaiToolRegistry,
    McpClientSpec,
    SkillSpec,
    ToolPolicyContext,
    ToolSpec,
)


def _prepared_turn():
    prompt_manifest = {
        "schema_version": "1",
        "sections": [
            {
                "id": "compiled.base",
                "order": 0,
                "owner": "agent_runtime",
                "sha256": "sha256:base",
            },
            {
                "id": "turn.perceptible_presence",
                "order": 1,
                "owner": "persona",
                "sha256": "sha256:persona",
            },
            {
                "id": "skills.active_index",
                "order": 2,
                "owner": "skills",
                "sha256": "sha256:skills",
            },
        ],
        "prompt_sha256": "sha256:prompt",
    }
    system = RuntimeMessage.text(
        "system",
        "PRIVATE PERSONA SYSTEM PROMPT",
        id="system-1",
    )
    memory = RuntimeMessage.text(
        "user",
        "PRIVATE MEMORY",
        id="memory-1",
    )
    turn = RuntimeMessage.text(
        "user",
        "PRIVATE CURRENT TURN",
        id="turn-1",
    )
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-private",
            run_id="run-private",
            workspace_dir="/private/workspace",
            metadata={
                "prompt_manifest": prompt_manifest,
                "persona_turn_plan": {
                    "conversation_mode": "work",
                    "subject_context": {"private": "PRIVATE SUBJECT"},
                },
                "ququ_hard_required": True,
                "legacy_request_context": {
                    "trusted_current_attachments": [
                        {
                            "canonical_path": "/private/attachment.pdf",
                            "display_name": "attachment.pdf",
                        }
                    ],
                    "workspace_access_mode": "read_write",
                    "workspace_trust_state": "trusted",
                },
            },
        ),
        messages=[system, memory, turn],
    )
    policy = ToolPolicyContext(
        agent_id="default",
        session_id="session-private",
        turn_id="run-private",
        workspace_dir="/private/workspace",
        workspace_trust_state="trusted",
        permission_profile="full",
    )
    return runtime_input, policy, system, memory, turn


def test_shadow_capture_projects_materialized_inputs_without_executing_tools() -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()
    registry = LantaiToolRegistry()
    executor_called = False

    def private_executor() -> None:
        nonlocal executor_called
        executor_called = True
        raise AssertionError("executor must not run")

    class OpaqueClient:
        def __repr__(self) -> str:
            raise AssertionError("client must not be inspected")

    registry.register_tool(
        ToolSpec(
            name="write_file",
            description="PRIVATE TOOL DESCRIPTION",
            input_schema={"type": "object"},
        ),
        private_executor,
    )
    registry.register_skill(
        SkillSpec(name="private-skill", path="/private/skill"),
    )
    registry.register_mcp_client(
        McpClientSpec(name="private-mcp", client=OpaqueClient()),
    )

    capture = capture_shadow_turn_assembly(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[system, memory],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[turn],
        attachment_manifest_message=None,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    assert capture.status == "captured"
    assert capture.snapshot is not None
    assert capture.snapshot.surface("prompt").item_ids == (
        "compiled.base",
        "turn.perceptible_presence",
        "skills.active_index",
    )
    assert capture.snapshot.surface("tools").item_ids == ("write_file",)
    assert capture.snapshot.surface("skills").item_ids == ("private-skill",)
    assert (
        capture.snapshot.surface("compatibility").value["proof_status"]
        == "declared_unverified"
    )
    assert executor_called is False
    evidence_text = str(capture.snapshot.privacy_evidence())
    assert "PRIVATE PERSONA SYSTEM PROMPT" not in evidence_text
    assert "/private/attachment.pdf" not in evidence_text
    assert "snapshot-private" not in repr(capture)


def test_shadow_capture_marks_missing_surface_unavailable_without_agent_access() -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()
    runtime_input.request.metadata.pop("prompt_manifest")

    capture = capture_shadow_turn_assembly(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[system, memory],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[turn],
        attachment_manifest_message=None,
        tool_registry=LantaiToolRegistry(),
        tool_policy_context=policy,
    )

    assert capture.status == "unavailable"
    assert capture.reason_code == "prompt_manifest_missing"
    assert capture.snapshot is None
    assert "agent" not in inspect.signature(capture_shadow_turn_assembly).parameters


@pytest.mark.parametrize(
    ("missing_surface", "expected_reason"),
    [
        ("persona_turn_plan", "persona_turn_plan_missing"),
        ("ququ_hard_required", "ququ_contract_missing"),
        ("legacy_request_context", "legacy_request_context_missing"),
        ("workspace_access_mode", "workspace_access_mode_missing"),
        ("workspace_trust_state", "workspace_trust_state_missing"),
    ],
)
def test_shadow_capture_never_fabricates_missing_protected_surfaces(
    missing_surface: str,
    expected_reason: str,
) -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()
    if missing_surface in {
        "persona_turn_plan",
        "ququ_hard_required",
        "legacy_request_context",
    }:
        runtime_input.request.metadata.pop(missing_surface)
    else:
        runtime_input.request.metadata["legacy_request_context"].pop(
            missing_surface
        )

    capture = capture_shadow_turn_assembly(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[system, memory],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[turn],
        attachment_manifest_message=None,
        tool_registry=LantaiToolRegistry(),
        tool_policy_context=policy,
    )

    assert capture.status == "unavailable"
    assert capture.reason_code == expected_reason
    assert capture.snapshot is None


@pytest.mark.parametrize(
    ("surface", "invalid_value", "expected_reason"),
    [
        ("persona_turn_plan", None, "persona_turn_plan_type"),
        ("ququ_hard_required", "true", "ququ_contract_missing"),
        ("trusted_current_attachments", None, "attachment_manifest_type"),
    ],
)
def test_shadow_capture_rejects_unrepresentable_protected_surfaces(
    surface: str,
    invalid_value: object,
    expected_reason: str,
) -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()
    if surface == "trusted_current_attachments":
        runtime_input.request.metadata["legacy_request_context"][surface] = (
            invalid_value
        )
    else:
        runtime_input.request.metadata[surface] = invalid_value

    capture = capture_shadow_turn_assembly(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[system, memory],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[turn],
        attachment_manifest_message=None,
        tool_registry=LantaiToolRegistry(),
        tool_policy_context=policy,
    )

    assert capture.status == "unavailable"
    assert capture.reason_code == expected_reason


def test_shadow_capture_records_explicit_absence_provenance_for_attachments() -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()
    runtime_input.request.metadata["legacy_request_context"].pop(
        "trusted_current_attachments"
    )

    capture = capture_shadow_turn_assembly(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[system, memory],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[turn],
        attachment_manifest_message=None,
        tool_registry=LantaiToolRegistry(),
        tool_policy_context=policy,
    )

    assert capture.status == "captured"
    assert capture.snapshot is not None
    attachment_surface = capture.snapshot.surface("attachments").value
    assert attachment_surface["presence"] == "absent"
    assert attachment_surface["trusted_current_attachments"] == ()


def test_shadow_capture_accepts_explicit_empty_persona_and_attachments() -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()
    runtime_input.request.metadata["persona_turn_plan"] = {}
    runtime_input.request.metadata["legacy_request_context"][
        "trusted_current_attachments"
    ] = []

    capture = capture_shadow_turn_assembly(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[system, memory],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[turn],
        attachment_manifest_message=None,
        tool_registry=LantaiToolRegistry(),
        tool_policy_context=policy,
    )

    assert capture.status == "captured"
    assert capture.snapshot is not None
    assert len(capture.snapshot.surface("persona").value["turn_plan"]) == 0
    assert (
        capture.snapshot.surface("attachments").value[
            "trusted_current_attachments"
        ]
        == ()
    )
    assert (
        capture.snapshot.surface("attachments").value["presence"]
        == "present"
    )


def test_shadow_reason_codes_cannot_expose_private_injected_text(
    monkeypatch,
    caplog,
) -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()

    def fail_with_private_reason(**_kwargs):
        error = TurnAssemblyShadowUnavailable("prompt_manifest_missing")
        error.reason_code = "PRIVATE REASON PAYLOAD"
        raise error

    monkeypatch.setattr(
        legacy_turn_assembly,
        "_build_shadow_snapshot",
        fail_with_private_reason,
    )
    with caplog.at_level(logging.INFO):
        capture = capture_shadow_turn_assembly(
            snapshot_id="snapshot-private",
            turn_id="run-private",
            runtime_input=runtime_input,
            memory_messages=[system, memory],
            recall_messages=[],
            plan_hint_message=None,
            turn_messages=[turn],
            attachment_manifest_message=None,
            tool_registry=LantaiToolRegistry(),
            tool_policy_context=policy,
        )

    assert capture.reason_code == "unknown_shadow_failure"
    assert "PRIVATE REASON PAYLOAD" not in repr(capture)
    assert "PRIVATE REASON PAYLOAD" not in caplog.text
    assert all(
        "PRIVATE REASON PAYLOAD" not in str(record.__dict__)
        and record.exc_info is None
        for record in caplog.records
    )
    direct = ShadowTurnAssemblyCapture(
        status="unavailable",
        reason_code="PRIVATE DIRECT REASON",
    )
    assert direct.reason_code == "unknown_shadow_failure"
    assert "PRIVATE DIRECT REASON" not in repr(direct)


def test_shadow_extraction_and_diagnostic_failures_are_private_and_fail_open(
    monkeypatch,
    caplog,
) -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()
    registry = LantaiToolRegistry()

    def fail_projection(_self):
        raise RuntimeError("PRIVATE EXTRACTION FAILURE")

    monkeypatch.setattr(LantaiToolRegistry, "tool_items", fail_projection)
    with caplog.at_level(logging.INFO):
        capture = capture_shadow_turn_assembly(
            snapshot_id="snapshot-private",
            turn_id="run-private",
            runtime_input=runtime_input,
            memory_messages=[system, memory],
            recall_messages=[],
            plan_hint_message=None,
            turn_messages=[turn],
            attachment_manifest_message=None,
            tool_registry=registry,
            tool_policy_context=policy,
        )

    assert capture.status == "unavailable"
    assert capture.reason_code == "shadow_extraction_failed"
    assert "PRIVATE EXTRACTION FAILURE" not in caplog.text
    assert "snapshot-private" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)

    def fail_diagnostic(*_args, **_kwargs):
        raise RuntimeError("diagnostic sink failed")

    monkeypatch.setattr(legacy_turn_assembly.logger, "info", fail_diagnostic)
    capture = capture_shadow_turn_assembly(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[system, memory],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[turn],
        attachment_manifest_message=None,
        tool_registry=registry,
        tool_policy_context=policy,
    )
    assert capture.status == "unavailable"


def test_shadow_rejects_model_subclasses_without_invoking_callbacks() -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()
    registry = LantaiToolRegistry()
    callback_calls = 0

    class HostileToolSpec(ToolSpec):
        def model_dump(self, *args, **kwargs):
            nonlocal callback_calls
            callback_calls += 1
            raise AssertionError("hostile model callback must not run")

    registry.register_tool(
        HostileToolSpec(name="hostile", input_schema={"type": "object"}),
        lambda: None,
    )
    capture = capture_shadow_turn_assembly(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[system, memory],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[turn],
        attachment_manifest_message=None,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    assert capture.status == "unavailable"
    assert capture.reason_code == "tool_spec_type"
    assert callback_calls == 0


@pytest.mark.parametrize("terminal_error", [asyncio.CancelledError(), KeyboardInterrupt()])
def test_shadow_capture_never_swallows_cancellation_or_base_exceptions(
    monkeypatch,
    terminal_error: BaseException,
) -> None:
    runtime_input, policy, system, memory, turn = _prepared_turn()
    registry = LantaiToolRegistry()

    def abort_projection(_self):
        raise terminal_error

    monkeypatch.setattr(LantaiToolRegistry, "tool_items", abort_projection)
    with pytest.raises(type(terminal_error)):
        capture_shadow_turn_assembly(
            snapshot_id="snapshot-private",
            turn_id="run-private",
            runtime_input=runtime_input,
            memory_messages=[system, memory],
            recall_messages=[],
            plan_hint_message=None,
            turn_messages=[turn],
            attachment_manifest_message=None,
            tool_registry=registry,
            tool_policy_context=policy,
        )
