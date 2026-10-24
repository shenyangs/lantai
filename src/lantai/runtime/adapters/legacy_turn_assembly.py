# -*- coding: utf-8 -*-
"""Side-effect-free shadow projection of the preserved intelligence facade.

The producer accepts only objects that the Runner has already materialized for
the current Native turn.  It never reads the agent, discovers capabilities,
executes tools, calls a provider, recompiles a prompt, or performs I/O.  During
the S7 shadow phase, an unavailable snapshot must not change the old path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, Literal

from ..input import RuntimeInput, RuntimeRequestContext
from ..messages import RuntimeMessage
from ..tools import (
    LantaiToolRegistry,
    McpClientSpec,
    SkillSpec,
    ToolPolicyContext,
    ToolSpec,
)
from ..turn_assembly import (
    TurnAssemblySnapshot,
    TurnAssemblySurface,
    build_turn_assembly_snapshot,
)


logger = logging.getLogger(__name__)

ShadowCaptureStatus = Literal["captured", "unavailable"]

_SHADOW_REASON_CODES: Final = frozenset(
    {
        "captured",
        "shadow_extraction_failed",
        "unknown_shadow_failure",
        "attachment_manifest_missing",
        "attachment_manifest_type",
        "blocked_tool_names_type",
        "compiled_system_message_missing",
        "legacy_request_context_missing",
        "mcp_spec_projection",
        "mcp_spec_type",
        "mcp_specs_type",
        "persona_turn_plan_missing",
        "persona_turn_plan_type",
        "projected_runtime_message_list_type",
        "projected_runtime_message_type",
        "prompt_manifest_missing",
        "prompt_manifest_section_identity",
        "prompt_manifest_sections",
        "ququ_contract_missing",
        "request_metadata_type",
        "runtime_input_type",
        "runtime_message_identity_type",
        "runtime_message_list_type",
        "runtime_message_missing",
        "runtime_message_projection",
        "runtime_message_type",
        "runtime_request_type",
        "skill_spec_projection",
        "skill_spec_type",
        "skill_specs_type",
        "tool_item_type",
        "tool_items_type",
        "tool_policy_context_type",
        "tool_policy_projection",
        "tool_registry_type",
        "tool_spec_projection",
        "tool_spec_type",
        "workspace_access_mode_missing",
        "workspace_trust_state_missing",
    }
)

_COMPATIBILITY_DECLARATION = {
    "proof_status": "declared_unverified",
    "runtime_message_projection": "legacy-runtime-message-converters.v1",
    "session_state_reader": "safe-json-session.read-old.v1",
    "session_state_writer": "safe-json-session.current-writer.v1",
    "context_compaction": "legacy-context-pre-reasoning-adapter.v1",
    "formatter": "legacy-model-client-message-adapter.v1",
}


class TurnAssemblyShadowUnavailable(ValueError):
    """A private surface could not be represented by the v1 shadow schema."""

    def __init__(self, reason_code: object) -> None:
        super().__init__("Turn assembly shadow capture unavailable")
        self.reason_code = _safe_reason_code(reason_code)


def _safe_reason_code(reason_code: object) -> str:
    if type(reason_code) is str and reason_code in _SHADOW_REASON_CODES:
        return reason_code
    return "unknown_shadow_failure"


@dataclass(frozen=True, slots=True, repr=False)
class ShadowTurnAssemblyCapture:
    """Turn-local result whose default representation cannot expose content."""

    status: ShadowCaptureStatus
    reason_code: str
    snapshot: TurnAssemblySnapshot | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in {
            "captured",
            "unavailable",
        }:
            raise ValueError("Invalid shadow capture status")
        safe_reason_code = _safe_reason_code(self.reason_code)
        object.__setattr__(self, "reason_code", safe_reason_code)
        if self.status == "captured" and self.snapshot is None:
            raise ValueError("Captured shadow result requires a snapshot")
        if self.status == "unavailable" and self.snapshot is not None:
            raise ValueError("Unavailable shadow result cannot contain a snapshot")
        if self.status == "captured" and safe_reason_code != "captured":
            raise ValueError("Captured shadow result requires captured reason")
        if self.status == "unavailable" and safe_reason_code == "captured":
            raise ValueError("Unavailable shadow result cannot use captured reason")

    def __repr__(self) -> str:
        return (
            "ShadowTurnAssemblyCapture("
            f"status={self.status!r}, reason_code={self.reason_code!r}, "
            f"snapshot_present={self.snapshot is not None}"
            ")"
        )


def _unavailable(reason_code: str) -> None:
    raise TurnAssemblyShadowUnavailable(reason_code)


def _message_dump(message: RuntimeMessage | None) -> dict[str, Any] | None:
    if message is None:
        return None
    if type(message) is not RuntimeMessage:
        _unavailable("runtime_message_type")
    payload = message.model_dump(mode="json")
    if type(payload) is not dict:
        _unavailable("runtime_message_projection")
    return payload


def _message_dumps(messages: list[RuntimeMessage]) -> list[dict[str, Any]]:
    if type(messages) is not list:
        _unavailable("runtime_message_list_type")
    rows: list[dict[str, Any]] = []
    for message in messages:
        payload = _message_dump(message)
        if payload is None:
            _unavailable("runtime_message_missing")
        rows.append(payload)
    return rows


def _message_item_ids(
    messages: list[RuntimeMessage],
    *,
    segment: str,
) -> tuple[str, ...]:
    item_ids: list[str] = []
    for index, message in enumerate(messages):
        message_id = message.id
        if message_id is not None and type(message_id) is not str:
            _unavailable("runtime_message_identity_type")
        item_ids.append(f"{segment}:{index}:{message_id or 'anonymous'}")
    return tuple(item_ids)


def _prompt_section_ids(prompt_manifest: dict[str, Any]) -> tuple[str, ...]:
    sections = prompt_manifest.get("sections")
    if type(sections) is not list:
        _unavailable("prompt_manifest_sections")
    section_ids: list[str] = []
    for section in sections:
        if type(section) is not dict or type(section.get("id")) is not str:
            _unavailable("prompt_manifest_section_identity")
        section_ids.append(section["id"])
    return tuple(section_ids)


def _skill_prompt_sections(prompt_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    sections = prompt_manifest.get("sections")
    if type(sections) is not list:
        _unavailable("prompt_manifest_sections")
    return [
        section
        for section in sections
        if type(section) is dict
        and (
            section.get("owner") == "skills"
            or (
                type(section.get("id")) is str
                and section["id"].startswith("skills.")
            )
        )
    ]


def _projection_names(rows: list[dict[str, Any]], reason_code: str) -> tuple[str, ...]:
    names: list[str] = []
    for row in rows:
        name = row.get("name")
        if type(name) is not str or not name:
            _unavailable(reason_code)
        names.append(name)
    return tuple(names)


def _registry_projection(
    registry: LantaiToolRegistry,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if type(registry) is not LantaiToolRegistry:
        _unavailable("tool_registry_type")

    tool_items = registry.tool_items()
    if type(tool_items) is not list:
        _unavailable("tool_items_type")
    tool_rows: list[dict[str, Any]] = []
    for item in tool_items:
        if type(item) is not tuple or len(item) != 2:
            _unavailable("tool_item_type")
        spec, _executor = item
        if type(spec) is not ToolSpec:
            _unavailable("tool_spec_type")
        payload = spec.model_dump(mode="json")
        if type(payload) is not dict:
            _unavailable("tool_spec_projection")
        tool_rows.append(payload)

    skill_specs = registry.skill_specs()
    if type(skill_specs) is not list:
        _unavailable("skill_specs_type")
    skill_rows: list[dict[str, Any]] = []
    for spec in skill_specs:
        if type(spec) is not SkillSpec:
            _unavailable("skill_spec_type")
        payload = spec.model_dump(mode="json")
        if type(payload) is not dict:
            _unavailable("skill_spec_projection")
        skill_rows.append(payload)

    mcp_client_specs = registry.mcp_client_specs()
    if type(mcp_client_specs) is not list:
        _unavailable("mcp_specs_type")
    mcp_rows: list[dict[str, Any]] = []
    for spec in mcp_client_specs:
        if type(spec) is not McpClientSpec:
            _unavailable("mcp_spec_type")
        payload = spec.model_dump(mode="json", exclude={"client"})
        if type(payload) is not dict:
            _unavailable("mcp_spec_projection")
        mcp_rows.append(payload)

    blocked_tool_names = registry.blocked_tool_names()
    if type(blocked_tool_names) is not frozenset or any(
        type(name) is not str for name in blocked_tool_names
    ):
        _unavailable("blocked_tool_names_type")

    skills = {"registered_skills": skill_rows}
    tools = {
        "registered_tools": tool_rows,
        "registered_mcp_clients": mcp_rows,
        "blocked_tool_names": sorted(blocked_tool_names),
    }
    return skills, tools


def _build_shadow_snapshot(
    *,
    snapshot_id: str,
    turn_id: str,
    runtime_input: RuntimeInput,
    memory_messages: list[RuntimeMessage],
    recall_messages: list[RuntimeMessage],
    plan_hint_message: RuntimeMessage | None,
    turn_messages: list[RuntimeMessage],
    attachment_manifest_message: RuntimeMessage | None,
    tool_registry: LantaiToolRegistry,
    tool_policy_context: ToolPolicyContext,
) -> TurnAssemblySnapshot:
    if type(runtime_input) is not RuntimeInput:
        _unavailable("runtime_input_type")
    if type(runtime_input.request) is not RuntimeRequestContext:
        _unavailable("runtime_request_type")
    if type(runtime_input.messages) is not list:
        _unavailable("projected_runtime_message_list_type")
    if type(tool_policy_context) is not ToolPolicyContext:
        _unavailable("tool_policy_context_type")

    request_metadata = runtime_input.request.metadata
    if type(request_metadata) is not dict:
        _unavailable("request_metadata_type")
    prompt_manifest = request_metadata.get("prompt_manifest")
    if type(prompt_manifest) is not dict:
        _unavailable("prompt_manifest_missing")

    projected_messages = list(runtime_input.messages)
    if any(type(message) is not RuntimeMessage for message in projected_messages):
        _unavailable("projected_runtime_message_type")
    system_message = next(
        (message for message in projected_messages if message.role == "system"),
        None,
    )
    if system_message is None:
        _unavailable("compiled_system_message_missing")

    if "persona_turn_plan" not in request_metadata:
        _unavailable("persona_turn_plan_missing")
    persona_plan = request_metadata["persona_turn_plan"]
    if type(persona_plan) is not dict:
        _unavailable("persona_turn_plan_type")
    ququ_hard_required = request_metadata.get("ququ_hard_required")
    if type(ququ_hard_required) is not bool:
        _unavailable("ququ_contract_missing")

    legacy_context = request_metadata.get("legacy_request_context")
    if type(legacy_context) is not dict:
        _unavailable("legacy_request_context_missing")
    if "trusted_current_attachments" in legacy_context:
        attachments = legacy_context["trusted_current_attachments"]
        if type(attachments) is not list:
            _unavailable("attachment_manifest_type")
        attachment_presence = "present"
    else:
        # The Runner's attachment projection has already completed.  An
        # absent key at this point is a first-class "no attachment" outcome,
        # not an invented empty manifest; retain that provenance explicitly.
        attachments = []
        attachment_presence = "absent"
    if type(legacy_context.get("workspace_access_mode")) is not str:
        _unavailable("workspace_access_mode_missing")
    if type(legacy_context.get("workspace_trust_state")) is not str:
        _unavailable("workspace_trust_state_missing")

    skills_projection, tools_projection = _registry_projection(tool_registry)
    skills_projection["prompt_sections"] = _skill_prompt_sections(prompt_manifest)

    memory_value = {
        "memory_messages": _message_dumps(memory_messages),
        "recall_messages": _message_dumps(recall_messages),
        "plan_hint_message": _message_dump(plan_hint_message),
        "turn_messages": _message_dumps(turn_messages),
        "projected_messages": _message_dumps(projected_messages),
        "segment_counts": {
            "memory": len(memory_messages),
            "recall": len(recall_messages),
            "plan_hint": 1 if plan_hint_message is not None else 0,
            "turn": len(turn_messages),
        },
    }
    projected_item_ids = _message_item_ids(
        projected_messages,
        segment="projected",
    )
    policy_payload = tool_policy_context.model_dump(mode="json")
    if type(policy_payload) is not dict:
        _unavailable("tool_policy_projection")

    return build_turn_assembly_snapshot(
        snapshot_id=snapshot_id,
        turn_id=turn_id,
        surfaces=[
            TurnAssemblySurface(
                name="prompt",
                value={
                    "compiled_system_message": _message_dump(system_message),
                    "content_free_manifest": prompt_manifest,
                },
                source_ids=("runtime_input.messages", "runtime_metadata.prompt_manifest"),
                item_ids=_prompt_section_ids(prompt_manifest),
            ),
            TurnAssemblySurface(
                name="persona",
                value={
                    "turn_plan": persona_plan,
                    "ququ_hard_required": ququ_hard_required,
                    "compiled_system_message_id": system_message.id or "anonymous",
                },
                source_ids=(
                    "runtime_metadata.persona_turn_plan",
                    "runtime_metadata.ququ_hard_required",
                    "runtime_input.system_message",
                ),
            ),
            TurnAssemblySurface(
                name="memory_context",
                value=memory_value,
                source_ids=(
                    "runner.memory_messages",
                    "runner.recall_messages",
                    "runner.plan_hint_message",
                    "runner.turn_messages",
                    "runtime_input.messages",
                ),
                item_ids=projected_item_ids,
            ),
            TurnAssemblySurface(
                name="skills",
                value=skills_projection,
                source_ids=("runtime_metadata.prompt_manifest", "native_tool_registry"),
                item_ids=_projection_names(
                    skills_projection["registered_skills"],
                    "skill_spec_identity",
                ),
            ),
            TurnAssemblySurface(
                name="attachments",
                value={
                    "presence": attachment_presence,
                    "trusted_current_attachments": attachments,
                    "manifest_message": _message_dump(attachment_manifest_message),
                    "workspace_access_mode": legacy_context.get(
                        "workspace_access_mode"
                    ),
                    "workspace_trust_state": legacy_context.get(
                        "workspace_trust_state"
                    ),
                },
                source_ids=(
                    "runtime_metadata.legacy_request_context",
                    "runner.attachment_manifest_message",
                ),
            ),
            TurnAssemblySurface(
                name="tools",
                value=tools_projection,
                source_ids=("native_tool_registry",),
                item_ids=_projection_names(
                    tools_projection["registered_tools"],
                    "tool_spec_identity",
                ),
            ),
            TurnAssemblySurface(
                name="request_policy",
                value={
                    "runtime_request": runtime_input.request.model_dump(mode="json"),
                    "model_slot": runtime_input.model_slot,
                    "tool_policy_context": runtime_input.tool_policy_context,
                    "runtime_metadata": runtime_input.metadata,
                    "native_tool_policy": policy_payload,
                },
                source_ids=("runtime_input", "native_tool_policy_context"),
            ),
            TurnAssemblySurface(
                name="compatibility",
                value=_COMPATIBILITY_DECLARATION,
                source_ids=("s7.compatibility.declaration.v1",),
            ),
        ],
    )


def capture_shadow_turn_assembly(
    *,
    snapshot_id: str,
    turn_id: str,
    runtime_input: RuntimeInput,
    memory_messages: list[RuntimeMessage],
    recall_messages: list[RuntimeMessage],
    plan_hint_message: RuntimeMessage | None,
    turn_messages: list[RuntimeMessage],
    attachment_manifest_message: RuntimeMessage | None,
    tool_registry: LantaiToolRegistry,
    tool_policy_context: ToolPolicyContext,
) -> ShadowTurnAssemblyCapture:
    """Capture a private snapshot or fail open without exposing its content."""

    try:
        snapshot = _build_shadow_snapshot(
            snapshot_id=snapshot_id,
            turn_id=turn_id,
            runtime_input=runtime_input,
            memory_messages=memory_messages,
            recall_messages=recall_messages,
            plan_hint_message=plan_hint_message,
            turn_messages=turn_messages,
            attachment_manifest_message=attachment_manifest_message,
            tool_registry=tool_registry,
            tool_policy_context=tool_policy_context,
        )
    except TurnAssemblyShadowUnavailable as exc:
        # Exception attributes are mutable after construction.  Normalize at
        # the final observability boundary as well as in the constructor.
        reason_code = _safe_reason_code(exc.reason_code)
    except Exception:
        reason_code = "shadow_extraction_failed"
    else:
        return ShadowTurnAssemblyCapture(
            status="captured",
            reason_code="captured",
            snapshot=snapshot,
        )

    try:
        logger.info(
            "Turn assembly shadow capture unavailable",
            extra={"turn_assembly_shadow_reason_code": reason_code},
        )
    except Exception:
        pass
    return ShadowTurnAssemblyCapture(
        status="unavailable",
        reason_code=reason_code,
    )


__all__ = [
    "ShadowTurnAssemblyCapture",
    "TurnAssemblyShadowUnavailable",
    "capture_shadow_turn_assembly",
]
