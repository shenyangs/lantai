# -*- coding: utf-8 -*-
"""Independent behavior locks for the S7 input-boundary oracle."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging

import pytest

import lantai.runtime.turn_assembly_oracle as oracle_module
from lantai.agents.persona_receipt import capture_persona_preparation_receipt
from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
from lantai.runtime.adapters.native_runtime import (
    NativeLantaiRuntime,
    _DeadlineBudget,
)
from lantai.runtime.adapters.legacy_turn_assembly import (
    capture_shadow_turn_assembly,
)
from lantai.runtime.messages import RuntimeMessage
from lantai.runtime.request_policy_receipt import (
    REQUEST_POLICY_CONSTRUCTION_RECEIPT_SCHEMA_V1,
    RequestPolicyConstructionReceipt,
    capture_request_policy_construction_receipt,
    claim_effective_policy_observation,
    mark_effective_policy_observation_exhausted,
)
from lantai.runtime.tools import (
    LantaiToolRegistry,
    McpClientSpec,
    SkillSpec,
    ToolCall,
    ToolPolicyContext,
    ToolPolicyOutcome,
    ToolSpec,
)
from lantai.runtime.turn_assembly import (
    TurnAssemblySnapshot,
    TurnAssemblySurface,
    build_turn_assembly_snapshot,
    thaw_turn_assembly_value,
)
from lantai.runtime.turn_assembly_oracle import (
    compare_turn_assembly_snapshot,
    log_turn_assembly_oracle_report,
    observe_native_input_boundary,
    observe_prepared_turn_sources,
    unavailable_turn_assembly_oracle_report,
)


def _golden_case():
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
                "id": "skills.active_index",
                "order": 1,
                "owner": "skills",
                "sha256": "sha256:skills",
            },
        ],
        "prompt_sha256": "sha256:prompt",
    }
    system = RuntimeMessage.text(
        "system",
        "PRIVATE PERSONA PROMPT",
        id="system-1",
    )
    turn = RuntimeMessage.text(
        "user",
        "PRIVATE USER TURN",
        id="turn-1",
    )
    manifest = RuntimeMessage.text(
        "system",
        "PRIVATE ATTACHMENT MANIFEST",
        id="attachment-1",
        metadata={
            "runtime_owned": True,
            "visibility": "internal",
            "kind": "trusted_attachment_manifest",
        },
    )
    request = RuntimeRequestContext(
        agent_id="default",
        session_id="session-private",
        run_id="run-private",
        workspace_dir="/private/workspace",
        metadata={
            "prompt_manifest": prompt_manifest,
            "persona_turn_plan": {
                "conversation_mode": "work",
                "subject_context": {},
                "strict_types": {
                    "bool": True,
                    "int": 1,
                    "float": 1.0,
                    "signed_zero": -0.0,
                },
            },
            "ququ_hard_required": True,
            "legacy_request_context": {
                "trusted_current_attachments": [
                    {
                        "canonical_path": "/private/input.pdf",
                        "display_name": "input.pdf",
                    }
                ],
                "workspace_access_mode": "read_write",
                "workspace_trust_state": "trusted",
            },
        },
    )
    runtime_input = RuntimeInput(
        request=request,
        messages=[system, manifest, turn],
    )
    policy = ToolPolicyContext(
        agent_id="default",
        session_id="session-private",
        turn_id="run-private",
        workspace_dir="/private/workspace",
        workspace_trust_state="trusted",
        permission_profile="full",
    )
    registry = LantaiToolRegistry()
    tool_spec = ToolSpec(
        name="write_file",
        description="PRIVATE TOOL DESCRIPTION",
        input_schema={"type": "object"},
    )
    skill_spec = SkillSpec(name="private-skill", path="/private/skill")
    mcp_spec = McpClientSpec(name="private-mcp", client=object())
    registry.register_tool(tool_spec, lambda: None)
    registry.register_skill(skill_spec)
    registry.register_mcp_client(mcp_spec)

    snapshot = build_turn_assembly_snapshot(
        snapshot_id="snapshot-private",
        turn_id="run-private",
        surfaces=[
            TurnAssemblySurface(
                name="prompt",
                value={
                    "compiled_system_message": system.model_dump(mode="json"),
                    "content_free_manifest": prompt_manifest,
                },
                source_ids=(
                    "runtime_input.messages",
                    "runtime_metadata.prompt_manifest",
                ),
                item_ids=("compiled.base", "skills.active_index"),
            ),
            TurnAssemblySurface(
                name="persona",
                value={
                    "turn_plan": {
                        "conversation_mode": "work",
                        "subject_context": {},
                        "strict_types": {
                            "bool": True,
                            "int": 1,
                            "float": 1.0,
                            "signed_zero": -0.0,
                        },
                    },
                    "ququ_hard_required": True,
                    "compiled_system_message_id": "system-1",
                },
                source_ids=(
                    "runtime_metadata.persona_turn_plan",
                    "runtime_metadata.ququ_hard_required",
                    "runtime_input.system_message",
                ),
            ),
            TurnAssemblySurface(
                name="memory_context",
                value={
                    "memory_messages": [system.model_dump(mode="json")],
                    "recall_messages": [],
                    "plan_hint_message": None,
                    "turn_messages": [
                        manifest.model_dump(mode="json"),
                        turn.model_dump(mode="json"),
                    ],
                    "projected_messages": [
                        system.model_dump(mode="json"),
                        manifest.model_dump(mode="json"),
                        turn.model_dump(mode="json"),
                    ],
                    "segment_counts": {
                        "memory": 1,
                        "recall": 0,
                        "plan_hint": 0,
                        "turn": 2,
                    },
                },
                source_ids=(
                    "runner.memory_messages",
                    "runner.recall_messages",
                    "runner.plan_hint_message",
                    "runner.turn_messages",
                    "runtime_input.messages",
                ),
                item_ids=(
                    "projected:0:system-1",
                    "projected:1:attachment-1",
                    "projected:2:turn-1",
                ),
            ),
            TurnAssemblySurface(
                name="skills",
                value={
                    "registered_skills": [skill_spec.model_dump(mode="json")],
                    "prompt_sections": [prompt_manifest["sections"][1]],
                },
                source_ids=(
                    "runtime_metadata.prompt_manifest",
                    "native_tool_registry",
                ),
                item_ids=("private-skill",),
            ),
            TurnAssemblySurface(
                name="attachments",
                value={
                    "presence": "present",
                    "trusted_current_attachments": [
                        {
                            "canonical_path": "/private/input.pdf",
                            "display_name": "input.pdf",
                        }
                    ],
                    "manifest_message": manifest.model_dump(mode="json"),
                    "workspace_access_mode": "read_write",
                    "workspace_trust_state": "trusted",
                },
                source_ids=(
                    "runtime_metadata.legacy_request_context",
                    "runner.attachment_manifest_message",
                ),
            ),
            TurnAssemblySurface(
                name="tools",
                value={
                    "registered_tools": [tool_spec.model_dump(mode="json")],
                    "registered_mcp_clients": [
                        mcp_spec.model_dump(mode="json", exclude={"client"})
                    ],
                    "blocked_tool_names": [],
                },
                source_ids=("native_tool_registry",),
                item_ids=("write_file",),
            ),
            TurnAssemblySurface(
                name="request_policy",
                value={
                    "runtime_request": request.model_dump(mode="json"),
                    "model_slot": None,
                    "tool_policy_context": {},
                    "runtime_metadata": {},
                    "native_tool_policy": policy.model_dump(mode="json"),
                },
                source_ids=(
                    "runtime_input",
                    "native_tool_policy_context",
                ),
            ),
            TurnAssemblySurface(
                name="compatibility",
                value={
                    "proof_status": "declared_unverified",
                    "runtime_message_projection": (
                        "legacy-runtime-message-converters.v1"
                    ),
                    "session_state_reader": "safe-json-session.read-old.v1",
                    "session_state_writer": (
                        "safe-json-session.current-writer.v1"
                    ),
                    "context_compaction": (
                        "legacy-context-pre-reasoning-adapter.v1"
                    ),
                    "formatter": "legacy-model-client-message-adapter.v1",
                },
                source_ids=("s7.compatibility.declaration.v1",),
            ),
        ],
    )
    return runtime_input, registry, policy, snapshot


def _golden_prepared_sources(runtime_input: RuntimeInput):
    return observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=[runtime_input.messages[1], runtime_input.messages[2]],
        attachment_manifest_message=runtime_input.messages[1],
        persona_preparation_receipt=None,
    )


def _persona_receipt(*, subject_context=None, subject_prompt="", presence_prompt=""):
    return capture_persona_preparation_receipt(
        source_mode="runner_override",
        language="zh",
        turn_key=(("user", "PRIVATE USER TURN"),),
        subject_context={} if subject_context is None else subject_context,
        subject_continuity_prompt=subject_prompt,
        perceptible_presence_prompt=presence_prompt,
    )


def test_persona_receipt_detects_joint_final_and_snapshot_context_mutation() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    receipt = _persona_receipt(subject_context={"attention": "ORIGINAL PRIVATE"})
    runtime_input.request.metadata["persona_turn_plan"]["subject_context"] = {
        "attention": "JOINT MUTATION"
    }
    mutated_snapshot = _mutated_snapshot(
        snapshot,
        "persona",
        field_name="turn_plan",
        field_value=runtime_input.request.metadata["persona_turn_plan"],
    )
    prepared = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
        persona_preparation_receipt=receipt,
    )
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=mutated_snapshot,
        prepared_sources=prepared,
    )

    assert report.status == "mismatched"
    assert "subject_context_linkage" in report.surfaces[1].mismatched_fields


def test_persona_receipt_verifies_real_prompt_manifest_semantics() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    subject_prompt = "PRIVATE SUBJECT PROMPT"
    presence_prompt = "PRIVATE PRESENCE PROMPT"
    manifest = runtime_input.request.metadata["prompt_manifest"]
    manifest["sections"].extend(
        [
            {
                "id": "session.subject_continuity",
                "order": 2,
                "owner": "persona",
                "source_kind": "runtime_projection",
                "cache_tier": "session",
                "sha256": hashlib.sha256(subject_prompt.encode()).hexdigest(),
                "chars": len(subject_prompt),
            },
            {
                "id": "turn.perceptible_presence",
                "order": 3,
                "owner": "persona",
                "source_kind": "runtime_projection",
                "cache_tier": "turn",
                "sha256": hashlib.sha256(presence_prompt.encode()).hexdigest(),
                "chars": len(presence_prompt),
            },
        ]
    )
    manifest["prompt_sha256"] = hashlib.sha256(
        runtime_input.messages[0].text_content().encode()
    ).hexdigest()
    snapshot = _mutated_snapshot(
        snapshot,
        "prompt",
        field_name="content_free_manifest",
        field_value=manifest,
    )
    prepared = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
        persona_preparation_receipt=_persona_receipt(
            subject_prompt=subject_prompt,
            presence_prompt=presence_prompt,
        ),
    )
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=prepared,
    )

    assert report.surfaces[1].status == "partial_match"
    assert report.surfaces[1].unverified_fields == (
        "persona_whole_plan_source_causality",
        "persona_projection_semantics",
    )


@pytest.mark.parametrize(
    "late_context",
    [
        {"attention": "PRIVATE LATE MUTATION"},
        [],
    ],
    ids=("value", "type"),
)
def test_persona_receipt_binds_prepared_context_across_later_joint_mutation(
    late_context: object,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    manifest = runtime_input.request.metadata["prompt_manifest"]
    manifest["prompt_sha256"] = hashlib.sha256(
        runtime_input.messages[0].text_content().encode()
    ).hexdigest()
    snapshot = _mutated_snapshot(
        snapshot,
        "prompt",
        field_name="content_free_manifest",
        field_value=manifest,
    )
    prepared = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
        persona_preparation_receipt=_persona_receipt(),
    )

    runtime_input.request.metadata["persona_turn_plan"]["subject_context"] = (
        late_context
    )
    snapshot = _mutated_snapshot(
        snapshot,
        "persona",
        field_name="turn_plan",
        field_value=runtime_input.request.metadata["persona_turn_plan"],
    )
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=prepared,
    )

    assert report.status == "mismatched"
    assert "subject_context_linkage" in report.surfaces[1].mismatched_fields


def test_persona_receipt_binds_manifest_across_later_joint_order_mutation() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    subject_prompt = "PRIVATE SUBJECT PROMPT"
    presence_prompt = "PRIVATE PRESENCE PROMPT"
    manifest = runtime_input.request.metadata["prompt_manifest"]
    manifest["sections"].extend(
        [
            {
                "id": "session.subject_continuity",
                "order": 2,
                "owner": "persona",
                "source_kind": "runtime_projection",
                "cache_tier": "session",
                "sha256": hashlib.sha256(subject_prompt.encode()).hexdigest(),
                "chars": len(subject_prompt),
            },
            {
                "id": "turn.perceptible_presence",
                "order": 3,
                "owner": "persona",
                "source_kind": "runtime_projection",
                "cache_tier": "turn",
                "sha256": hashlib.sha256(presence_prompt.encode()).hexdigest(),
                "chars": len(presence_prompt),
            },
        ]
    )
    manifest["prompt_sha256"] = hashlib.sha256(
        runtime_input.messages[0].text_content().encode()
    ).hexdigest()
    snapshot = _mutated_snapshot(
        snapshot,
        "prompt",
        field_name="content_free_manifest",
        field_value=manifest,
    )
    prepared = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
        persona_preparation_receipt=_persona_receipt(
            subject_prompt=subject_prompt,
            presence_prompt=presence_prompt,
        ),
    )

    manifest["sections"][2]["order"] = 7
    snapshot = _mutated_snapshot(
        snapshot,
        "prompt",
        field_name="content_free_manifest",
        field_value=manifest,
    )
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=prepared,
    )

    assert report.status == "mismatched"
    assert "persona_section_manifest" in report.surfaces[1].mismatched_fields


def test_disabled_persona_receipt_verifies_explicitly_empty_preparation() -> None:
    runtime_input, _registry, _policy, _snapshot = _golden_case()
    runtime_input.request.metadata["persona_turn_plan"] = None
    runtime_input.request.metadata["prompt_manifest"]["prompt_sha256"] = (
        hashlib.sha256(
            runtime_input.messages[0].text_content().encode()
        ).hexdigest()
    )
    receipt = capture_persona_preparation_receipt(
        source_mode="disabled",
        language="",
        turn_key=(("user", "PRIVATE USER TURN"),),
        subject_context={},
        subject_continuity_prompt="",
        perceptible_presence_prompt="",
    )

    prepared = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
        persona_preparation_receipt=receipt,
    )

    assert prepared.persona_provenance_available is True
    assert prepared.persona_mismatched_fields == ()


def test_persona_receipt_oracle_rejects_hostile_manifest_value_without_hooks() -> None:
    class HostileValue:
        def __eq__(self, _other):
            raise AssertionError("custom equality must not run")

        def __hash__(self):
            raise AssertionError("custom hashing must not run")

    runtime_input, _registry, _policy, _snapshot = _golden_case()
    runtime_input.request.metadata["prompt_manifest"]["sections"][0]["id"] = (
        HostileValue()
    )

    prepared = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
        persona_preparation_receipt=_persona_receipt(),
    )

    assert prepared.persona_mismatched_fields == (
        "persona_section_manifest",
    )


@pytest.mark.parametrize(
    ("mutation", "expected_field"),
    [
        ("duplicate", "persona_section_manifest"),
        ("wrong_hash", "subject_continuity_section"),
        ("wrong_order", "persona_section_order"),
        ("wrong_prompt_hash", "prompt_manifest_linkage"),
    ],
)
def test_persona_receipt_rejects_each_manifest_link_break(
    mutation: str,
    expected_field: str,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    subject_prompt = "PRIVATE SUBJECT PROMPT"
    presence_prompt = "PRIVATE PRESENCE PROMPT"
    manifest = runtime_input.request.metadata["prompt_manifest"]
    subject_row = {
        "id": "session.subject_continuity",
        "order": 2,
        "owner": "persona",
        "source_kind": "runtime_projection",
        "cache_tier": "session",
        "sha256": hashlib.sha256(subject_prompt.encode()).hexdigest(),
        "chars": len(subject_prompt),
    }
    presence_row = {
        "id": "turn.perceptible_presence",
        "order": 3,
        "owner": "persona",
        "source_kind": "runtime_projection",
        "cache_tier": "turn",
        "sha256": hashlib.sha256(presence_prompt.encode()).hexdigest(),
        "chars": len(presence_prompt),
    }
    manifest["sections"].extend([subject_row, presence_row])
    manifest["prompt_sha256"] = hashlib.sha256(
        runtime_input.messages[0].text_content().encode()
    ).hexdigest()
    if mutation == "duplicate":
        manifest["sections"].append(dict(subject_row))
    elif mutation == "wrong_hash":
        subject_row["sha256"] = "0" * 64
    elif mutation == "wrong_order":
        subject_row["order"] = 4
    else:
        manifest["prompt_sha256"] = "0" * 64
    snapshot = _mutated_snapshot(
        snapshot,
        "prompt",
        field_name="content_free_manifest",
        field_value=manifest,
    )
    prepared = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
        persona_preparation_receipt=_persona_receipt(
            subject_prompt=subject_prompt,
            presence_prompt=presence_prompt,
        ),
    )
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=prepared,
    )

    assert report.status == "mismatched"
    assert expected_field in report.surfaces[1].mismatched_fields


def _mutated_snapshot(
    snapshot: TurnAssemblySnapshot,
    surface_name: str,
    *,
    field_name: str | None = None,
    field_value: object = "changed",
    omit_field: bool = False,
    reverse_fields: bool = False,
    item_ids: tuple[str, ...] | None = None,
    source_ids: tuple[str, ...] | None = None,
) -> TurnAssemblySnapshot:
    surfaces: list[TurnAssemblySurface] = []
    for surface in snapshot.surfaces:
        value = thaw_turn_assembly_value(surface.value)
        assert isinstance(value, dict)
        if surface.name == surface_name and field_name is not None:
            if omit_field:
                value.pop(field_name)
            else:
                value[field_name] = field_value
        if surface.name == surface_name and reverse_fields:
            value = {key: value[key] for key in reversed(value)}
        surfaces.append(
            TurnAssemblySurface(
                name=surface.name,
                value=value,
                source_ids=(
                    source_ids
                    if surface.name == surface_name and source_ids is not None
                    else surface.source_ids
                ),
                item_ids=(
                    item_ids
                    if surface.name == surface_name and item_ids is not None
                    else surface.item_ids
                ),
            )
        )
    return build_turn_assembly_snapshot(
        snapshot_id="mutated-snapshot",
        turn_id="run-private",
        surfaces=surfaces,
    )


def test_oracle_is_independent_and_reports_truthful_partial_coverage() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()

    assert "snapshot" not in inspect.signature(
        observe_native_input_boundary
    ).parameters
    assert "agent" not in inspect.signature(
        observe_native_input_boundary
    ).parameters
    assert "legacy_turn_assembly" not in inspect.getsource(oracle_module)
    assert "snapshot" not in inspect.signature(
        observe_prepared_turn_sources
    ).parameters
    assert "agent" not in inspect.signature(
        observe_prepared_turn_sources
    ).parameters
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )
    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=snapshot,
    )

    assert report.status == "partial_match"
    statuses = {surface.name: surface.status for surface in report.surfaces}
    assert statuses == {
        "prompt": "matched",
        "persona": "partial_match",
        "memory_context": "partial_match",
        "skills": "matched",
        "attachments": "partial_match",
        "tools": "matched",
        "request_policy": "partial_match",
        "compatibility": "unverified",
    }
    assert report.surfaces[-1].unverified_fields == (
        "compatibility_runtime_behavior",
    )
    assert "pass" not in report.status


def test_oracle_observation_does_not_execute_tools_or_inspect_mcp_clients() -> None:
    runtime_input, _registry, policy, _snapshot = _golden_case()
    registry = LantaiToolRegistry()
    executor_called = False

    def executor() -> None:
        nonlocal executor_called
        executor_called = True
        raise AssertionError("executor must not run")

    class OpaqueClient:
        def __repr__(self) -> str:
            raise AssertionError("MCP client must not be inspected")

    registry.register_tool(ToolSpec(name="safe-tool"), executor)
    registry.register_mcp_client(
        McpClientSpec(name="opaque-client", client=OpaqueClient())
    )

    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    assert observation.surface("tools").item_ids == ("safe-tool",)
    assert executor_called is False


def test_independent_oracle_matches_the_live_shadow_producer_at_input_boundary() -> None:
    runtime_input, registry, policy, _snapshot = _golden_case()
    capture = capture_shadow_turn_assembly(
        snapshot_id="live-shadow",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
        tool_registry=registry,
        tool_policy_context=policy,
    )
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    assert capture.status == "captured"
    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=capture.snapshot,
    )

    assert report.status == "partial_match"
    assert all(
        surface.status != "mismatched" for surface in report.surfaces
    )


def test_prepared_sources_close_only_memory_and_attachment_coverage() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=_golden_prepared_sources(runtime_input),
    )

    assert report.status == "partial_match"
    statuses = {surface.name: surface.status for surface in report.surfaces}
    assert statuses == {
        "prompt": "matched",
        "persona": "partial_match",
        "memory_context": "matched",
        "skills": "matched",
        "attachments": "matched",
        "tools": "matched",
        "request_policy": "partial_match",
        "compatibility": "unverified",
    }
    assert report.surfaces[2].unverified_fields == ()
    assert report.surfaces[4].unverified_fields == ()
    assert report.surfaces[6].unverified_fields == (
        "native_policy_constructor_copy",
        "native_effective_turn_policy",
        "native_per_tool_policy_dispatch",
        "native_live_permission_override_and_guard_consumption",
    )


def test_policy_receipt_closes_only_constructor_copy_coverage() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    receipt = capture_request_policy_construction_receipt(
        policy.model_dump(mode="python")
    )

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=receipt,
    )

    policy_comparison = report.surfaces[6]
    assert report.status == "partial_match"
    assert policy_comparison.status == "partial_match"
    assert policy_comparison.mismatched_fields == ()
    assert policy_comparison.unverified_fields == (
        "native_effective_turn_policy",
        "native_per_tool_policy_dispatch",
        "native_live_permission_override_and_guard_consumption",
    )


def _effective_policy_receipt(runtime_input, registry, policy):
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: None,
        tool_registry=registry,
        tool_policy_context=policy,
    )
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    invocation = claim_effective_policy_observation(token)
    assert invocation is not None
    turn_id = runtime_input.request.run_id
    assert turn_id is not None
    context = runtime._policy_context_for_turn(
        runtime_input,
        turn_id=turn_id,
        budget=_DeadlineBudget(None),
    )
    runtime.request_policy_observer.capture_effective(
        invocation,
        runtime_input=runtime_input,
        actual_turn_id=turn_id,
        effective_context=context,
        deadline_monotonic=None,
    )
    mark_effective_policy_observation_exhausted(
        invocation,
        terminal_type="turn.completed",
        terminal_turn_id=turn_id,
    )
    receipt = runtime.request_policy_observer.take(
        invocation,
        terminal_type="turn.completed",
        terminal_turn_id=turn_id,
    )
    assert receipt is not None
    return runtime.request_policy_construction_receipt(), receipt


def _effective_and_dispatch_receipts(
    runtime_input,
    registry,
    policy,
    *,
    mutate_delivered_context: bool = False,
    live_permission_reached: bool = True,
    guard_reached: bool = True,
    mutate_consumed_context: bool = False,
):
    runtime = NativeLantaiRuntime(
        model_client_factory=lambda _agent_id: None,
        tool_registry=registry,
        tool_policy_context=policy,
    )
    token = runtime.request_policy_observer.begin(runtime_input)
    assert token is not None
    invocation = claim_effective_policy_observation(token)
    assert invocation is not None
    turn_id = runtime_input.request.run_id
    assert turn_id is not None
    context = runtime._policy_context_for_turn(
        runtime_input,
        turn_id=turn_id,
        budget=_DeadlineBudget(None),
    )
    runtime.request_policy_observer.capture_effective(
        invocation,
        runtime_input=runtime_input,
        actual_turn_id=turn_id,
        effective_context=context,
        deadline_monotonic=None,
    )
    evaluation = runtime.request_policy_observer.start_policy_evaluation(
        invocation,
        tool_call=ToolCall(id="call-dispatch", name="write_file"),
        policy_context=context,
    )
    assert evaluation is not None
    delivered_context = context
    if mutate_delivered_context:
        delivered_context = context.model_copy(
            update={"approval_level": "STRICT"}
        )
    runtime.request_policy_observer.record_policy_middleware_dispatch(
        evaluation,
        middleware_index=0,
        middleware_name="RuntimeToolPolicyMiddleware",
        delivered_context=delivered_context,
    )
    consumption_observation = (
        runtime.request_policy_observer.start_policy_consumption(
            evaluation,
            middleware_index=0,
            delivered_context=delivered_context,
        )
    )
    assert consumption_observation is not None
    runtime.request_policy_observer.record_policy_context_consumption(
        consumption_observation,
        stage="runtime_phase",
        phase="initial",
        context=delivered_context,
    )
    if live_permission_reached:
        post_override_context = delivered_context.model_copy(
            update={"permission_mode": "full_auto"}
        )
        runtime.request_policy_observer.record_live_permission_result(
            consumption_observation,
            mode="applied",
            context=post_override_context,
        )
        runtime.request_policy_observer.record_policy_context_consumption(
            consumption_observation,
            stage="full_access",
            phase="post_override",
            context=(
                delivered_context
                if mutate_consumed_context
                else post_override_context
            ),
        )
        if guard_reached:
            runtime.request_policy_observer.record_policy_context_consumption(
                consumption_observation,
                stage="tool_guard",
                phase="post_override",
                context=post_override_context,
            )
    else:
        runtime.request_policy_observer.record_live_permission_result(
            consumption_observation,
            mode="not_reached",
            context=delivered_context,
        )
    runtime.request_policy_observer.complete_policy_consumption(
        consumption_observation,
        outcome=ToolPolicyOutcome(action="allow"),
    )
    runtime.request_policy_observer.complete_policy_evaluation(evaluation)
    mark_effective_policy_observation_exhausted(
        invocation,
        terminal_type="turn.completed",
        terminal_turn_id=turn_id,
    )
    effective = runtime.request_policy_observer.take(
        invocation,
        terminal_type="turn.completed",
        terminal_turn_id=turn_id,
    )
    dispatch = runtime.request_policy_observer.take_dispatch(
        invocation,
        terminal_type="turn.completed",
        terminal_turn_id=turn_id,
    )
    consumption = runtime.request_policy_observer.take_consumption(
        invocation,
        terminal_type="turn.completed",
        terminal_turn_id=turn_id,
    )
    assert (
        effective is not None
        and dispatch is not None
        and consumption is not None
    )
    return (
        runtime.request_policy_construction_receipt(),
        effective,
        dispatch,
        consumption,
    )


def test_effective_policy_receipt_closes_only_deadline_free_formation() -> (
    None
):
    runtime_input, registry, policy, snapshot = _golden_case()
    construction, effective = _effective_policy_receipt(
        runtime_input,
        registry,
        policy,
    )

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=construction,
        request_policy_effective_receipt=effective,
    )

    assert report.status == "partial_match"
    assert report.surfaces[6].status == "partial_match"
    assert report.surfaces[6].mismatched_fields == ()
    assert report.surfaces[6].unverified_fields == (
        "native_per_tool_policy_dispatch",
        "native_live_permission_override_and_guard_consumption",
    )


def test_dispatch_receipt_closes_only_evaluator_and_middleware_delivery() -> (
    None
):
    runtime_input, registry, policy, snapshot = _golden_case()
    (
        construction,
        effective,
        dispatch,
        _consumption,
    ) = _effective_and_dispatch_receipts(
        runtime_input,
        registry,
        policy,
    )

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=construction,
        request_policy_effective_receipt=effective,
        request_policy_dispatch_receipt=dispatch,
    )

    assert report.status == "partial_match"
    assert report.surfaces[6].mismatched_fields == ()
    assert report.surfaces[6].unverified_fields == (
        "native_live_permission_override_and_guard_consumption",
    )


def test_consumption_receipt_closes_live_override_and_guard_coverage() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    (
        construction,
        effective,
        dispatch,
        consumption,
    ) = _effective_and_dispatch_receipts(runtime_input, registry, policy)

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=construction,
        request_policy_effective_receipt=effective,
        request_policy_dispatch_receipt=dispatch,
        request_policy_consumption_receipt=consumption,
    )

    assert report.surfaces[6].status == "matched"
    assert report.surfaces[6].mismatched_fields == ()
    assert report.surfaces[6].unverified_fields == ()


@pytest.mark.parametrize(
    ("live_permission_reached", "guard_reached"),
    [(False, False), (True, False)],
)
def test_incomplete_consumption_coverage_remains_unverified_without_mismatch(
    live_permission_reached: bool,
    guard_reached: bool,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    (
        construction,
        effective,
        dispatch,
        consumption,
    ) = _effective_and_dispatch_receipts(
        runtime_input,
        registry,
        policy,
        live_permission_reached=live_permission_reached,
        guard_reached=guard_reached,
    )

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=construction,
        request_policy_effective_receipt=effective,
        request_policy_dispatch_receipt=dispatch,
        request_policy_consumption_receipt=consumption,
    )

    assert report.surfaces[6].status == "partial_match"
    assert report.surfaces[6].mismatched_fields == ()
    assert report.surfaces[6].unverified_fields == (
        "native_live_permission_override_and_guard_consumption",
    )


def test_consumption_receipt_reports_changed_context_as_mismatch() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    (
        construction,
        effective,
        dispatch,
        consumption,
    ) = _effective_and_dispatch_receipts(
        runtime_input,
        registry,
        policy,
        mutate_consumed_context=True,
    )
    assert consumption.all_context_chains_valid is False

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=construction,
        request_policy_effective_receipt=effective,
        request_policy_dispatch_receipt=dispatch,
        request_policy_consumption_receipt=consumption,
    )

    assert report.status == "mismatched"
    assert report.surfaces[6].mismatched_fields == (
        "native_live_permission_override_and_guard_consumption",
    )


def test_consumption_receipt_detects_stale_observer_binding() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    (
        construction,
        effective,
        dispatch,
        _consumption,
    ) = _effective_and_dispatch_receipts(runtime_input, registry, policy)
    (
        _other_construction,
        _other_effective,
        _other_dispatch,
        stale_consumption,
    ) = _effective_and_dispatch_receipts(runtime_input, registry, policy)

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=construction,
        request_policy_effective_receipt=effective,
        request_policy_dispatch_receipt=dispatch,
        request_policy_consumption_receipt=stale_consumption,
    )

    assert report.status == "mismatched"
    assert report.surfaces[6].mismatched_fields == (
        "native_live_permission_override_and_guard_consumption",
    )


def test_consumption_receipt_rejects_wrong_exact_type() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()

    with pytest.raises(TypeError, match="exact consumption receipt"):
        compare_turn_assembly_snapshot(
            observation=observe_native_input_boundary(
                runtime_input=runtime_input,
                tool_registry=registry,
                tool_policy_context=policy,
            ),
            snapshot=snapshot,
            request_policy_consumption_receipt=object(),
        )


def test_dispatch_receipt_reports_changed_middleware_copy_as_mismatch() -> (
    None
):
    runtime_input, registry, policy, snapshot = _golden_case()
    (
        construction,
        effective,
        dispatch,
        _consumption,
    ) = _effective_and_dispatch_receipts(
        runtime_input,
        registry,
        policy,
        mutate_delivered_context=True,
    )
    assert dispatch.all_contexts_matched is False

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=construction,
        request_policy_effective_receipt=effective,
        request_policy_dispatch_receipt=dispatch,
    )

    assert report.status == "mismatched"
    assert report.surfaces[6].mismatched_fields == (
        "native_per_tool_policy_dispatch",
    )


def test_effective_policy_receipt_never_upgrades_deadline_input() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    construction, stale_effective = _effective_policy_receipt(
        runtime_input,
        registry,
        policy,
    )
    runtime_input.metadata["native_turn_timeout_seconds"] = 1
    snapshot = _mutated_snapshot(
        snapshot,
        "request_policy",
        field_name="runtime_metadata",
        field_value=runtime_input.metadata,
    )

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=construction,
        request_policy_effective_receipt=stale_effective,
    )

    assert report.status == "partial_match"
    assert "native_effective_turn_policy" in report.surfaces[6].unverified_fields
    assert report.surfaces[6].mismatched_fields == ()


def test_effective_policy_receipt_detects_joint_input_and_snapshot_mutation() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    construction, stale_effective = _effective_policy_receipt(
        runtime_input,
        registry,
        policy,
    )
    runtime_input.metadata["joint_mutation"] = "changed"
    snapshot = _mutated_snapshot(
        snapshot,
        "request_policy",
        field_name="runtime_metadata",
        field_value=runtime_input.metadata,
    )

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=construction,
        request_policy_effective_receipt=stale_effective,
    )

    assert report.status == "mismatched"
    assert report.surfaces[6].mismatched_fields == (
        "native_effective_turn_policy",
    )


def test_effective_policy_oracle_matches_100_independent_merge_cases() -> None:
    for index in range(100):
        runtime_input, registry, _policy, _snapshot = _golden_case()
        request = runtime_input.request.model_copy(
            update={
                "agent_id": f"agent-{index}",
                "session_id": f"session-{index}",
                "run_id": f"run-{index}",
                "root_session_id": f"root-{index}" if index % 2 else None,
                "chat_id": f"chat-{index}" if index % 3 else None,
                "workspace_dir": f"/workspace/{index}" if index % 4 else None,
            }
        )
        runtime_input = runtime_input.model_copy(
            update={
                "request": request,
                "model_slot": f"slot-{index}" if index % 5 else None,
                "tool_policy_context": {
                    "permission_profile": "full" if index % 2 else "workspace",
                    "request_context": {"input_only": index},
                    "metadata": {"input_meta": index},
                },
                "metadata": {"runtime_meta": index},
            }
        )
        policy = ToolPolicyContext(
            agent_id="base",
            session_id=f"base-{index}",
            workspace_dir=f"/base/{index}",
            request_context={"base_only": index},
            metadata={"base_meta": index},
        )
        capture = capture_shadow_turn_assembly(
            snapshot_id=f"merge-{index}",
            turn_id=request.run_id or "",
            runtime_input=runtime_input,
            memory_messages=[runtime_input.messages[0]],
            recall_messages=[],
            plan_hint_message=None,
            turn_messages=runtime_input.messages[1:],
            attachment_manifest_message=runtime_input.messages[1],
            tool_registry=registry,
            tool_policy_context=policy,
        )
        assert capture.status == "captured"
        construction, effective = _effective_policy_receipt(
            runtime_input,
            registry,
            policy,
        )

        report = compare_turn_assembly_snapshot(
            observation=observe_native_input_boundary(
                runtime_input=runtime_input,
                tool_registry=registry,
                tool_policy_context=policy,
            ),
            snapshot=capture.snapshot,
            request_policy_construction_receipt=construction,
            request_policy_effective_receipt=effective,
        )

        assert report.surfaces[6].mismatched_fields == ()
        assert report.surfaces[6].unverified_fields == (
            "native_per_tool_policy_dispatch",
            "native_live_permission_override_and_guard_consumption",
        )


def test_policy_receipt_detects_stale_receipt_after_joint_policy_mutation() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    stale_receipt = capture_request_policy_construction_receipt(
        policy.model_dump(mode="python")
    )
    mutated_policy = policy.model_copy(update={"approval_level": "NEVER"})
    mutated_snapshot = _mutated_snapshot(
        snapshot,
        "request_policy",
        field_name="native_tool_policy",
        field_value=mutated_policy.model_dump(mode="json"),
    )

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=mutated_policy,
        ),
        snapshot=mutated_snapshot,
        request_policy_construction_receipt=stale_receipt,
    )

    assert report.status == "mismatched"
    assert report.surfaces[6].mismatched_fields == (
        "native_policy_constructor_copy",
    )


def test_policy_receipt_rejects_wrong_exact_type() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()

    with pytest.raises(TypeError, match="exact policy receipt"):
        compare_turn_assembly_snapshot(
            observation=observe_native_input_boundary(
                runtime_input=runtime_input,
                tool_registry=registry,
                tool_policy_context=policy,
            ),
            snapshot=snapshot,
            request_policy_construction_receipt=object(),
        )


def test_policy_receipt_detects_forged_field_count() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    captured = capture_request_policy_construction_receipt(
        policy.model_dump(mode="python")
    )
    forged = RequestPolicyConstructionReceipt(
        schema_version=REQUEST_POLICY_CONSTRUCTION_RECEIPT_SCHEMA_V1,
        assigned_payload_sha256=captured.assigned_payload_sha256,
        top_level_field_count=captured.top_level_field_count + 1,
    )

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        request_policy_construction_receipt=forged,
    )

    assert report.status == "mismatched"
    assert report.surfaces[6].mismatched_fields == (
        "native_policy_constructor_copy",
    )


def test_policy_receipt_rejects_hostile_schema_without_hooks() -> None:
    calls: list[str] = []

    class HostileSchema:
        def __eq__(self, _other):
            calls.append("eq")
            raise AssertionError("schema comparison hook must not run")

        def __ne__(self, _other):
            calls.append("ne")
            raise AssertionError("schema comparison hook must not run")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("schema repr hook must not run")

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("schema str hook must not run")

    with pytest.raises(ValueError, match="Unsupported request policy"):
        RequestPolicyConstructionReceipt(
            schema_version=HostileSchema(),
            assigned_payload_sha256="sha256:" + ("0" * 64),
            top_level_field_count=0,
        )

    assert calls == []


def test_policy_receipt_repr_sanitizes_post_init_hostile_mutation() -> None:
    calls: list[str] = []

    class HostileValue:
        def __eq__(self, _other):
            calls.append("eq")
            raise AssertionError("receipt comparison hook must not run")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("receipt repr hook must not run")

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("receipt str hook must not run")

    receipt = RequestPolicyConstructionReceipt(
        schema_version=REQUEST_POLICY_CONSTRUCTION_RECEIPT_SCHEMA_V1,
        assigned_payload_sha256="sha256:" + ("0" * 64),
        top_level_field_count=0,
    )
    hostile = HostileValue()
    object.__setattr__(receipt, "schema_version", hostile)
    object.__setattr__(receipt, "assigned_payload_sha256", hostile)
    object.__setattr__(receipt, "top_level_field_count", hostile)

    assert "invalid" in repr(receipt)
    assert calls == []


def test_prepared_sources_match_the_live_shadow_memory_and_manifest() -> None:
    runtime_input, registry, policy, _snapshot = _golden_case()
    capture = capture_shadow_turn_assembly(
        snapshot_id="live-source-shadow",
        turn_id="run-private",
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
        tool_registry=registry,
        tool_policy_context=policy,
    )
    assert capture.status == "captured"
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=capture.snapshot,
        prepared_sources=_golden_prepared_sources(runtime_input),
    )

    assert report.status == "partial_match"
    assert report.surfaces[2].status == "matched"
    assert report.surfaces[4].status == "matched"


@pytest.mark.parametrize(
    ("segment", "expected_field"),
    [
        ("memory", "memory_messages"),
        ("recall", "recall_messages"),
        ("plan", "plan_hint_message"),
        ("turn", "turn_messages"),
    ],
)
def test_prepared_source_oracle_detects_each_segment_mutation(
    segment: str,
    expected_field: str,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    memory = [runtime_input.messages[0]]
    recall: list[RuntimeMessage] = []
    plan = None
    turns = runtime_input.messages[1:]
    if segment == "memory":
        memory = [
            RuntimeMessage.text(
                "system",
                "PRIVATE CHANGED MEMORY",
                id="system-1",
            )
        ]
    elif segment == "recall":
        recall = [
            RuntimeMessage.text(
                "assistant",
                "PRIVATE CHANGED RECALL",
                id="recall-1",
            )
        ]
    elif segment == "plan":
        plan = RuntimeMessage.text(
            "system",
            "PRIVATE CHANGED PLAN",
            id="plan-1",
        )
    else:
        turns = list(reversed(turns))

    sources = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=memory,
        recall_messages=recall,
        plan_hint_message=plan,
        turn_messages=turns,
        attachment_manifest_message=runtime_input.messages[1],
    )
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=sources,
    )
    memory_comparison = report.surfaces[2]

    assert report.status == "mismatched"
    assert memory_comparison.status == "mismatched"
    assert expected_field in memory_comparison.mismatched_fields
    assert "projected_composition" in memory_comparison.mismatched_fields


def test_prepared_source_oracle_detects_segment_boundary_changes_even_when_flattened_order_matches() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    sources = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[runtime_input.messages[0]],
        recall_messages=[runtime_input.messages[1]],
        plan_hint_message=None,
        turn_messages=[runtime_input.messages[2]],
        attachment_manifest_message=runtime_input.messages[1],
    )
    assert "projected_composition" not in sources.mismatched_fields
    assert "projected_item_ids" not in sources.mismatched_fields

    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=sources,
    )
    memory_comparison = report.surfaces[2]
    attachment_comparison = report.surfaces[4]

    assert report.status == "mismatched"
    assert {
        "recall_messages",
        "turn_messages",
        "segment_counts",
    }.issubset(memory_comparison.mismatched_fields)
    assert attachment_comparison.mismatched_fields == (
        "attachment_manifest_linkage",
    )


@pytest.mark.parametrize(
    ("field_value", "omit_field", "expected_fields"),
    [
        (None, False, {"manifest_message"}),
        ({"changed": True}, False, {"manifest_message"}),
        (None, True, {"missing_fields", "manifest_message"}),
    ],
    ids=("null", "different-present", "absent"),
)
def test_prepared_source_oracle_distinguishes_manifest_absent_null_and_present(
    field_value: object,
    omit_field: bool,
    expected_fields: set[str],
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=_mutated_snapshot(
            snapshot,
            "attachments",
            field_name="manifest_message",
            field_value=field_value,
            omit_field=omit_field,
        ),
        prepared_sources=_golden_prepared_sources(runtime_input),
    )
    comparison = report.surfaces[4]

    assert report.status == "mismatched"
    assert expected_fields.issubset(comparison.mismatched_fields)


def test_prepared_source_oracle_detects_final_input_only_mutation() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    original_messages = list(runtime_input.messages)
    mutated_input = RuntimeInput(
        request=runtime_input.request,
        messages=[
            original_messages[0],
            original_messages[1],
            RuntimeMessage.text(
                "user",
                "PRIVATE FINAL INPUT MUTATION",
                id="turn-1",
            ),
        ],
    )
    sources = observe_prepared_turn_sources(
        runtime_input=mutated_input,
        memory_messages=[original_messages[0]],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=original_messages[1:],
        attachment_manifest_message=original_messages[1],
    )
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=mutated_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=sources,
    )

    assert report.status == "mismatched"
    assert "projected_messages" in report.surfaces[2].mismatched_fields
    assert "projected_composition" in report.surfaces[2].mismatched_fields


def test_prepared_sources_cannot_override_a_contradictory_final_observation() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    sources = _golden_prepared_sources(runtime_input)
    runtime_input.messages[-1] = RuntimeMessage.text(
        "user",
        "PRIVATE LATE FINAL INPUT MUTATION",
        id="changed-final-id",
    )
    final_observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    report_without_sources = compare_turn_assembly_snapshot(
        observation=final_observation,
        snapshot=snapshot,
    )
    report_with_sources = compare_turn_assembly_snapshot(
        observation=final_observation,
        snapshot=snapshot,
        prepared_sources=sources,
    )

    assert report_without_sources.status == "mismatched"
    assert report_with_sources.status == "mismatched"
    assert report_with_sources.surfaces[2].status == "mismatched"
    assert {
        "projected_composition",
        "projected_item_ids",
    }.issubset(report_with_sources.surfaces[2].mismatched_fields)


def test_prepared_sources_remain_bound_to_final_payload_type_and_order() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    sources = _golden_prepared_sources(runtime_input)
    original = list(runtime_input.messages)
    cases = (
        (
            [
                original[0],
                original[1],
                RuntimeMessage.text(
                    "user",
                    "PRIVATE CONTENT-ONLY MUTATION",
                    id="turn-1",
                ),
            ],
            {"projected_composition"},
        ),
        (
            [original[0], original[2], original[1]],
            {"projected_composition", "projected_item_ids"},
        ),
    )

    for messages, expected_fields in cases:
        changed_input = RuntimeInput(
            request=runtime_input.request,
            messages=messages,
        )
        report = compare_turn_assembly_snapshot(
            observation=observe_native_input_boundary(
                runtime_input=changed_input,
                tool_registry=registry,
                tool_policy_context=policy,
            ),
            snapshot=snapshot,
            prepared_sources=sources,
        )

        assert report.status == "mismatched"
        assert expected_fields.issubset(
            report.surfaces[2].mismatched_fields
        )


def test_prepared_source_observer_is_non_mutating_and_preserves_aliases() -> None:
    runtime_input, _registry, _policy, _snapshot = _golden_case()
    memory = [runtime_input.messages[0]]
    recall: list[RuntimeMessage] = []
    turns = runtime_input.messages[1:]
    manifest = runtime_input.messages[1]
    before = tuple(runtime_input.messages)

    observation = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=memory,
        recall_messages=recall,
        plan_hint_message=None,
        turn_messages=turns,
        attachment_manifest_message=manifest,
    )

    assert tuple(runtime_input.messages) == before
    assert memory[0] is runtime_input.messages[0]
    assert turns[0] is manifest is runtime_input.messages[1]
    assert observation.mismatched_fields == ()
    assert "PRIVATE" not in repr(observation)


def test_missing_prepared_source_observation_keeps_truthful_partial_coverage() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=None,
    )

    assert report.surfaces[2].status == "partial_match"
    assert report.surfaces[4].status == "partial_match"
    assert report.surfaces[2].unverified_fields
    assert report.surfaces[4].unverified_fields


@pytest.mark.parametrize(
    ("surface_name", "field_name"),
    [
        ("prompt", "compiled_system_message"),
        ("persona", "ququ_hard_required"),
        ("memory_context", "projected_messages"),
        ("skills", "registered_skills"),
        ("attachments", "presence"),
        ("tools", "registered_tools"),
        ("request_policy", "model_slot"),
    ],
)
def test_oracle_detects_each_comparable_surface_mutation(
    surface_name: str,
    field_name: str,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=_mutated_snapshot(
            snapshot,
            surface_name,
            field_name=field_name,
        ),
    )

    assert report.status == "mismatched"
    comparison = next(
        surface for surface in report.surfaces if surface.name == surface_name
    )
    assert comparison.status == "mismatched"
    assert field_name in comparison.mismatched_fields


@pytest.mark.parametrize(
    ("surface_name", "mutation"),
    [
        ("prompt", "item_order"),
        ("memory_context", "item_identity"),
        ("tools", "item_count"),
        ("persona", "source_identity"),
        ("prompt", "field_omission"),
    ],
)
def test_oracle_detects_order_identity_count_source_and_omission_changes(
    surface_name: str,
    mutation: str,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )
    kwargs: dict[str, object] = {}
    expected_field = "item_ids"
    if mutation == "item_order":
        kwargs["item_ids"] = tuple(
            reversed(snapshot.surface(surface_name).item_ids)
        )
    elif mutation == "item_identity":
        kwargs["item_ids"] = (
            "projected:0:changed",
            "projected:1:attachment-1",
            "projected:2:turn-1",
        )
    elif mutation == "item_count":
        kwargs["item_ids"] = ()
    elif mutation == "source_identity":
        kwargs["source_ids"] = ("changed-source",)
        expected_field = "source_ids"
    else:
        kwargs.update(
            {
                "field_name": "compiled_system_message",
                "omit_field": True,
            }
        )
        expected_field = "compiled_system_message"

    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=_mutated_snapshot(snapshot, surface_name, **kwargs),
    )
    comparison = next(
        surface for surface in report.surfaces if surface.name == surface_name
    )
    assert comparison.status == "mismatched"
    assert expected_field in comparison.mismatched_fields


@pytest.mark.parametrize(
    ("mutation", "expected_field"),
    [
        ("bool_as_int", "ququ_hard_required"),
        ("extra_field", "unexpected_fields"),
        ("unexpected_item_id", "item_ids"),
    ],
)
def test_oracle_rejects_bool_int_extra_field_and_unexpected_empty_identity(
    mutation: str,
    expected_field: str,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )
    kwargs: dict[str, object] = {}
    if mutation == "bool_as_int":
        kwargs.update(
            {"field_name": "ququ_hard_required", "field_value": 1}
        )
    elif mutation == "extra_field":
        kwargs.update({"field_name": "unexpected", "field_value": "value"})
    else:
        kwargs["item_ids"] = ("unexpected-persona-item",)

    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=_mutated_snapshot(snapshot, "persona", **kwargs),
    )
    persona = report.surfaces[1]
    assert report.status == "mismatched"
    assert persona.status == "mismatched"
    assert expected_field in persona.mismatched_fields


@pytest.mark.parametrize(
    "turn_plan",
    [
        {
            "conversation_mode": "work",
            "strict_types": {
                "bool": 1,
                "int": 1,
                "float": 1.0,
                "signed_zero": -0.0,
            },
        },
        {
            "conversation_mode": "work",
            "strict_types": {
                "bool": True,
                "int": 1.0,
                "float": 1.0,
                "signed_zero": -0.0,
            },
        },
        {
            "conversation_mode": "work",
            "strict_types": {
                "bool": True,
                "int": 1,
                "float": 1.0,
                "signed_zero": 0.0,
            },
        },
        {
            "conversation_mode": "work",
            "strict_types": {
                "signed_zero": -0.0,
                "float": 1.0,
                "int": 1,
                "bool": True,
            },
        },
    ],
    ids=("nested-bool-int", "nested-int-float", "signed-zero", "nested-order"),
)
def test_oracle_uses_recursive_type_and_order_sensitive_comparison(
    turn_plan: dict[str, object],
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=_mutated_snapshot(
            snapshot,
            "persona",
            field_name="turn_plan",
            field_value=turn_plan,
        ),
    )

    assert report.status == "mismatched"
    assert report.surfaces[1].status == "mismatched"
    assert "turn_plan" in report.surfaces[1].mismatched_fields


def test_oracle_rejects_top_level_snapshot_field_reordering() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=_mutated_snapshot(
            snapshot,
            "prompt",
            reverse_fields=True,
        ),
    )

    assert report.status == "mismatched"
    assert report.surfaces[0].mismatched_fields == ("field_order",)


@pytest.mark.parametrize(
    "surface_name",
    ["persona", "attachments", "request_policy", "compatibility"],
)
def test_oracle_rejects_unexpected_ids_on_nominally_empty_surfaces(
    surface_name: str,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=_mutated_snapshot(
            snapshot,
            surface_name,
            item_ids=("private-unexpected-item",),
        ),
    )
    comparison = next(
        surface for surface in report.surfaces if surface.name == surface_name
    )

    assert report.status == "mismatched"
    assert comparison.status == "mismatched"
    assert comparison.mismatched_fields == ("item_ids",)


@pytest.mark.parametrize(
    "surface_name",
    ["memory_context", "attachments", "request_policy"],
)
def test_oracle_rejects_unknown_extra_fields_on_partial_surfaces(
    surface_name: str,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=_mutated_snapshot(
            snapshot,
            surface_name,
            field_name="private_unknown_field",
            field_value="PRIVATE VALUE",
        ),
    )
    comparison = next(
        surface for surface in report.surfaces if surface.name == surface_name
    )

    assert report.status == "mismatched"
    assert comparison.status == "mismatched"
    assert comparison.mismatched_fields == ("unexpected_fields",)
    assert "private_unknown_field" not in repr(comparison)


@pytest.mark.parametrize(
    ("surface_name", "field_name", "field_value", "expected_status"),
    [
        ("memory_context", "memory_messages", ["changed"], "partial_match"),
        ("attachments", "manifest_message", {"changed": True}, "partial_match"),
        ("compatibility", "proof_status", "changed", "unverified"),
    ],
)
def test_oracle_keeps_known_unverified_value_changes_unverified(
    surface_name: str,
    field_name: str,
    field_value: object,
    expected_status: str,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )

    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=_mutated_snapshot(
            snapshot,
            surface_name,
            field_name=field_name,
            field_value=field_value,
        ),
    )
    comparison = next(
        surface for surface in report.surfaces if surface.name == surface_name
    )

    assert report.status == "partial_match"
    assert comparison.status == expected_status
    assert comparison.mismatched_fields == ()


def test_oracle_does_not_claim_compatibility_value_is_verified() -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    observation = observe_native_input_boundary(
        runtime_input=runtime_input,
        tool_registry=registry,
        tool_policy_context=policy,
    )
    mutated = _mutated_snapshot(
        snapshot,
        "compatibility",
        field_name="proof_status",
        field_value="fabricated-pass",
    )

    report = compare_turn_assembly_snapshot(
        observation=observation,
        snapshot=mutated,
    )

    assert report.status == "partial_match"
    assert report.surfaces[-1].status == "unverified"


def test_unavailable_report_and_logs_never_expose_private_values(
    caplog,
) -> None:
    report = unavailable_turn_assembly_oracle_report("PRIVATE REASON")
    with caplog.at_level(logging.INFO):
        log_turn_assembly_oracle_report(report)

    assert report.status == "unavailable"
    assert report.reason_code == "unknown_oracle_failure"
    assert "PRIVATE REASON" not in repr(report)
    assert "PRIVATE REASON" not in caplog.text
    assert all(
        "PRIVATE REASON" not in str(record.__dict__)
        and record.exc_info is None
        for record in caplog.records
    )

    object.__setattr__(report, "status", "PRIVATE STATUS")
    object.__setattr__(report, "reason_code", "PRIVATE MUTATED REASON")
    object.__setattr__(report, "observation_sha256", "sha256:PRIVATE HASH")
    with caplog.at_level(logging.INFO):
        log_turn_assembly_oracle_report(report)
    assert "PRIVATE" not in repr(report)
    assert "PRIVATE" not in caplog.text
    assert all("PRIVATE" not in str(record.__dict__) for record in caplog.records)


def test_mutated_surface_diagnostics_are_resanitized_at_log_boundary(
    caplog,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
    )
    surface = report.surfaces[0]
    object.__setattr__(surface, "name", "PRIVATE SURFACE")
    object.__setattr__(surface, "status", "PRIVATE STATUS")
    object.__setattr__(surface, "matched_field_count", "PRIVATE COUNT")
    object.__setattr__(surface, "mismatched_fields", ("PRIVATE FIELD",))

    with caplog.at_level(logging.INFO):
        log_turn_assembly_oracle_report(report)

    assert "PRIVATE" not in repr(surface)
    assert "PRIVATE" not in caplog.text
    assert all("PRIVATE" not in str(record.__dict__) for record in caplog.records)


def test_prepared_source_evidence_logs_only_hashes_counts_and_safe_labels(
    caplog,
) -> None:
    runtime_input, registry, policy, snapshot = _golden_case()
    sources = observe_prepared_turn_sources(
        runtime_input=runtime_input,
        memory_messages=[
            RuntimeMessage.text(
                "system",
                "PRIVATE SOURCE CONTENT",
                id="private-source-id",
            )
        ],
        recall_messages=[],
        plan_hint_message=None,
        turn_messages=runtime_input.messages[1:],
        attachment_manifest_message=runtime_input.messages[1],
    )
    report = compare_turn_assembly_snapshot(
        observation=observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        ),
        snapshot=snapshot,
        prepared_sources=sources,
    )

    with caplog.at_level(logging.INFO):
        log_turn_assembly_oracle_report(report)

    private_tokens = (
        "PRIVATE SOURCE CONTENT",
        "private-source-id",
        "PRIVATE PERSONA PROMPT",
        "PRIVATE ATTACHMENT MANIFEST",
        "/private/input.pdf",
    )
    assert all(token not in repr(sources) for token in private_tokens)
    assert all(token not in repr(report) for token in private_tokens)
    assert all(token not in caplog.text for token in private_tokens)
    assert all(
        all(token not in str(record.__dict__) for token in private_tokens)
        and record.exc_info is None
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "terminal_error",
    [asyncio.CancelledError(), KeyboardInterrupt()],
)
def test_observation_never_swallows_cancellation_or_base_exception(
    monkeypatch,
    terminal_error: BaseException,
) -> None:
    runtime_input, registry, policy, _snapshot = _golden_case()

    def abort(_self):
        raise terminal_error

    monkeypatch.setattr(LantaiToolRegistry, "tool_items", abort)
    with pytest.raises(type(terminal_error)):
        observe_native_input_boundary(
            runtime_input=runtime_input,
            tool_registry=registry,
            tool_policy_context=policy,
        )


def test_oracle_logger_failure_is_diagnostic_only(monkeypatch) -> None:
    report = unavailable_turn_assembly_oracle_report(
        "shadow_snapshot_unavailable"
    )
    monkeypatch.setattr(
        oracle_module.logger,
        "info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("diagnostic failure")
        ),
    )

    log_turn_assembly_oracle_report(report)
