# -*- coding: utf-8 -*-
"""Independent input-boundary oracle for S7 TurnAssembly shadow evidence.

The observation step never accepts a shadow snapshot and does not import the
shadow producer.  It observes only the exact ``RuntimeInput``, registry, and
policy objects about to cross the Native Runtime boundary.  The comparison
step receives that independent observation and a snapshot.  Results are
diagnostic-only and never claim full runtime compatibility: Native currently
copies policy data in its constructor, while original memory/recall segments
and the attachment-manifest local cannot be reconstructed at this boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Final, Literal

from .input import RuntimeInput, RuntimeRequestContext
from ..agents.persona_receipt import (
    PersonaPreparationReceipt,
    freeze_persona_receipt_value,
    persona_receipt_value_sha256,
)
from .messages import RuntimeMessage
from .request_policy_receipt import (
    RequestPolicyConstructionReceipt,
    RequestPolicyConsumptionReceipt,
    RequestPolicyDispatchReceipt,
    RequestPolicyEffectiveTurnReceipt,
    is_valid_request_policy_construction_receipt,
    is_valid_request_policy_consumption_receipt,
    is_valid_request_policy_dispatch_receipt,
    is_valid_request_policy_effective_receipt,
    project_tool_policy_context,
    request_policy_payload_is_deadline_free,
    request_policy_payload_sha256,
)
from .tools import (
    LantaiToolRegistry,
    McpClientSpec,
    SkillSpec,
    ToolPolicyContext,
    ToolSpec,
)
from .turn_assembly import (
    FrozenJsonObject,
    REQUIRED_TURN_ASSEMBLY_SURFACES,
    TurnAssemblySnapshot,
    freeze_turn_assembly_value,
    thaw_turn_assembly_value,
)


logger = logging.getLogger(__name__)

OracleSurfaceStatus = Literal[
    "matched",
    "partial_match",
    "mismatched",
    "unverified",
    "unavailable",
]
OracleReportStatus = Literal["partial_match", "mismatched", "unavailable"]

_ORACLE_REASON_CODES: Final = frozenset(
    {
        "compared",
        "shadow_snapshot_unavailable",
        "snapshot_runtime_input_unavailable",
        "oracle_projection_failed",
        "oracle_comparison_failed",
        "unknown_oracle_failure",
    }
)
_COMPARISON_FIELDS: Final = {
    "prompt": (
        "compiled_system_message",
        "content_free_manifest",
    ),
    "persona": (
        "turn_plan",
        "ququ_hard_required",
        "compiled_system_message_id",
    ),
    "memory_context": ("projected_messages",),
    "skills": ("registered_skills", "prompt_sections"),
    "attachments": (
        "presence",
        "trusted_current_attachments",
        "workspace_access_mode",
        "workspace_trust_state",
    ),
    "tools": (
        "registered_tools",
        "registered_mcp_clients",
        "blocked_tool_names",
    ),
    "request_policy": (
        "runtime_request",
        "model_slot",
        "tool_policy_context",
        "runtime_metadata",
        "native_tool_policy",
    ),
    "compatibility": (),
}
_UNVERIFIED_FIELDS: Final = {
    "prompt": (),
    "persona": (
        "persona_preparation_receipt",
        "persona_whole_plan_source_causality",
        "persona_projection_semantics",
    ),
    "memory_context": (
        "memory_messages",
        "recall_messages",
        "plan_hint_message",
        "turn_messages",
        "segment_counts",
    ),
    "skills": (),
    "attachments": ("manifest_message",),
    "tools": (),
    "request_policy": (
        "native_policy_constructor_copy",
        "native_effective_turn_policy",
        "native_per_tool_policy_dispatch",
        "native_live_permission_override_and_guard_consumption",
    ),
    "compatibility": ("compatibility_runtime_behavior",),
}
_UNVERIFIED_SNAPSHOT_FIELDS: Final = {
    "prompt": (),
    "persona": (),
    "memory_context": (
        "memory_messages",
        "recall_messages",
        "plan_hint_message",
        "turn_messages",
        "segment_counts",
    ),
    "skills": (),
    "attachments": ("manifest_message",),
    "tools": (),
    "request_policy": (),
    "compatibility": (),
}
_SNAPSHOT_FIELD_ORDER: Final = {
    "prompt": (
        "compiled_system_message",
        "content_free_manifest",
    ),
    "persona": (
        "turn_plan",
        "ququ_hard_required",
        "compiled_system_message_id",
    ),
    "memory_context": (
        "memory_messages",
        "recall_messages",
        "plan_hint_message",
        "turn_messages",
        "projected_messages",
        "segment_counts",
    ),
    "skills": ("registered_skills", "prompt_sections"),
    "attachments": (
        "presence",
        "trusted_current_attachments",
        "manifest_message",
        "workspace_access_mode",
        "workspace_trust_state",
    ),
    "tools": (
        "registered_tools",
        "registered_mcp_clients",
        "blocked_tool_names",
    ),
    "request_policy": (
        "runtime_request",
        "model_slot",
        "tool_policy_context",
        "runtime_metadata",
        "native_tool_policy",
    ),
    "compatibility": (),
}
_EXPECTED_SOURCE_IDS: Final = {
    "prompt": ("runtime_input.messages", "runtime_metadata.prompt_manifest"),
    "persona": (
        "runtime_metadata.persona_turn_plan",
        "runtime_metadata.ququ_hard_required",
        "runtime_input.system_message",
    ),
    "memory_context": (
        "runner.memory_messages",
        "runner.recall_messages",
        "runner.plan_hint_message",
        "runner.turn_messages",
        "runtime_input.messages",
    ),
    "skills": ("runtime_metadata.prompt_manifest", "native_tool_registry"),
    "attachments": (
        "runtime_metadata.legacy_request_context",
        "runner.attachment_manifest_message",
    ),
    "tools": ("native_tool_registry",),
    "request_policy": ("runtime_input", "native_tool_policy_context"),
    "compatibility": ("s7.compatibility.declaration.v1",),
}
_STRUCTURAL_COMPARISON_FIELDS: Final = frozenset(
    {
        "source_ids",
        "item_ids",
        "unexpected_fields",
        "missing_fields",
        "field_order",
        "projected_composition",
        "projected_item_ids",
        "attachment_manifest_linkage",
        "persona_preparation_receipt",
        "subject_context_linkage",
        "subject_continuity_section",
        "perceptible_presence_section",
        "persona_section_manifest",
        "persona_section_order",
        "prompt_manifest_linkage",
    }
)
_SOURCE_MISMATCH_FIELDS: Final = frozenset(
    {
        "projected_composition",
        "projected_item_ids",
        "attachment_manifest_linkage",
        "subject_context_linkage",
        "subject_continuity_section",
        "perceptible_presence_section",
        "persona_section_manifest",
        "persona_section_order",
        "prompt_manifest_linkage",
    }
)
_ALL_SAFE_FIELD_NAMES: Final = frozenset(
    {
        field
        for fields in (*_COMPARISON_FIELDS.values(), *_UNVERIFIED_FIELDS.values())
        for field in fields
    }
    | _STRUCTURAL_COMPARISON_FIELDS
)


def _safe_reason_code(value: object) -> str:
    if type(value) is str and value in _ORACLE_REASON_CODES:
        return value
    return "unknown_oracle_failure"


def _safe_field_names(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple:
        return ()
    return tuple(
        value
        for value in values
        if type(value) is str and value in _ALL_SAFE_FIELD_NAMES
    )


def _safe_sha256(value: object) -> str | None:
    if (
        type(value) is str
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    ):
        return value
    return None


def _privacy_hash(value: Any) -> str:
    frozen = freeze_turn_assembly_value(value)

    def canonical(item: Any) -> Any:
        if type(item) is FrozenJsonObject:
            return {
                "$object": [
                    [key, canonical(child)] for key, child in item.items()
                ]
            }
        if type(item) is tuple:
            return {"$array": [canonical(child) for child in item]}
        if item is None:
            return {"$null": True}
        if type(item) is bool:
            return {"$bool": item}
        if type(item) is int:
            return {"$int": item}
        if type(item) is float:
            return {"$float": repr(item)}
        if type(item) is str:
            return {"$string": item}
        raise TypeError("Unsupported frozen oracle value")

    encoded = json.dumps(
        canonical(frozen),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class InputBoundarySurfaceObservation:
    """Private independently projected fields for one protected surface."""

    name: str
    fields: FrozenJsonObject
    source_ids: tuple[str, ...]
    item_ids: tuple[str, ...] = ()
    unverified_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.name) is not str or self.name not in _COMPARISON_FIELDS:
            raise ValueError("Invalid oracle surface name")
        if type(self.fields) is not FrozenJsonObject:
            raise TypeError("Oracle fields must be frozen")
        if tuple(self.fields) != _COMPARISON_FIELDS[self.name]:
            raise ValueError("Oracle surface fields do not match the fixed schema")
        if type(self.source_ids) is not tuple or any(
            type(value) is not str for value in self.source_ids
        ):
            raise TypeError("Oracle source identities must be built-in strings")
        if self.source_ids != _EXPECTED_SOURCE_IDS[self.name]:
            raise ValueError("Oracle source identities do not match the fixed schema")
        if type(self.item_ids) is not tuple or any(
            type(value) is not str for value in self.item_ids
        ):
            raise TypeError("Oracle item identities must be built-in strings")
        if type(self.unverified_fields) is not tuple:
            raise TypeError("Oracle unverified fields must be a tuple")
        if self.unverified_fields != _UNVERIFIED_FIELDS[self.name]:
            raise ValueError("Oracle unverified fields do not match the fixed schema")

    def __repr__(self) -> str:
        safe_name = (
            self.name
            if type(self.name) is str and self.name in _COMPARISON_FIELDS
            else "invalid"
        )
        field_count: int | str = (
            len(self.fields)
            if type(self.fields) is FrozenJsonObject
            else "invalid"
        )
        unverified_count: int | str = (
            len(self.unverified_fields)
            if type(self.unverified_fields) is tuple
            else "invalid"
        )
        return (
            "InputBoundarySurfaceObservation("
            f"name={safe_name!r}, private_fields=True, "
            f"field_count={field_count!r}, "
            f"unverified_count={unverified_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TurnAssemblyInputBoundaryObservation:
    """Private observation built without consulting the shadow producer."""

    surfaces: tuple[InputBoundarySurfaceObservation, ...]

    def __post_init__(self) -> None:
        if type(self.surfaces) is not tuple or any(
            type(surface) is not InputBoundarySurfaceObservation
            for surface in self.surfaces
        ):
            raise TypeError("Oracle observation surfaces must be exact values")
        if tuple(surface.name for surface in self.surfaces) != (
            REQUIRED_TURN_ASSEMBLY_SURFACES
        ):
            raise ValueError("Oracle observation surface order is invalid")

    def surface(self, name: str) -> InputBoundarySurfaceObservation:
        for surface in self.surfaces:
            if surface.name == name:
                return surface
        raise KeyError(name)

    def __repr__(self) -> str:
        surface_count: int | str = (
            len(self.surfaces) if type(self.surfaces) is tuple else "invalid"
        )
        return (
            "TurnAssemblyInputBoundaryObservation("
            "private_fields=True, "
            f"surface_count={surface_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PreparedTurnSourceObservation:
    """Independent projection of already-materialized Runner source segments."""

    memory_fields: FrozenJsonObject
    memory_item_ids: tuple[str, ...]
    attachment_manifest_message: Any
    persona_provenance_available: bool = False
    persona_receipt_sha256: str | None = None
    persona_subject_context_binding_sha256: str | None = None
    persona_manifest_binding_sha256: str | None = None
    persona_system_message_binding_sha256: str | None = None
    persona_mismatched_fields: tuple[str, ...] = ()
    mismatched_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.memory_fields) is not FrozenJsonObject:
            raise TypeError("Prepared source memory fields must be frozen")
        if tuple(self.memory_fields) != _SNAPSHOT_FIELD_ORDER["memory_context"]:
            raise ValueError("Prepared source memory schema is invalid")
        if type(self.memory_item_ids) is not tuple or any(
            type(value) is not str for value in self.memory_item_ids
        ):
            raise TypeError("Prepared source message identities are invalid")
        manifest_type = type(self.attachment_manifest_message)
        if self.attachment_manifest_message is not None and (
            manifest_type is not FrozenJsonObject
        ):
            raise TypeError("Prepared source manifest must be frozen")
        if type(self.mismatched_fields) is not tuple or any(
            type(value) is not str or value not in _SOURCE_MISMATCH_FIELDS
            for value in self.mismatched_fields
        ):
            raise TypeError("Prepared source mismatches are invalid")
        if type(self.persona_provenance_available) is not bool:
            raise TypeError("Prepared Persona availability must be a bool")
        if self.persona_receipt_sha256 is not None and _safe_sha256(
            self.persona_receipt_sha256
        ) is None:
            raise ValueError("Prepared Persona receipt digest is invalid")
        persona_binding_hashes = (
            self.persona_receipt_sha256,
            self.persona_subject_context_binding_sha256,
            self.persona_manifest_binding_sha256,
            self.persona_system_message_binding_sha256,
        )
        if any(
            value is not None and _safe_sha256(value) is None
            for value in persona_binding_hashes
        ):
            raise ValueError("Prepared Persona binding digest is invalid")
        if self.persona_provenance_available != all(
            value is not None for value in persona_binding_hashes
        ):
            raise ValueError("Prepared Persona receipt availability is inconsistent")
        if type(self.persona_mismatched_fields) is not tuple or any(
            type(value) is not str or value not in _SOURCE_MISMATCH_FIELDS
            for value in self.persona_mismatched_fields
        ):
            raise TypeError("Prepared Persona mismatches are invalid")

    def __repr__(self) -> str:
        memory_field_count: int | str = (
            len(self.memory_fields)
            if type(self.memory_fields) is FrozenJsonObject
            else "invalid"
        )
        mismatch_count: int | str = (
            len(self.mismatched_fields) + len(self.persona_mismatched_fields)
            if type(self.mismatched_fields) is tuple
            and type(self.persona_mismatched_fields) is tuple
            else "invalid"
        )
        manifest_present = type(self.attachment_manifest_message) is FrozenJsonObject
        return (
            "PreparedTurnSourceObservation("
            "private_fields=True, "
            f"memory_field_count={memory_field_count!r}, "
            f"manifest_present={manifest_present!r}, "
            f"persona_receipt_present={self.persona_provenance_available!r}, "
            f"mismatch_count={mismatch_count!r})"
        )


def _persona_receipt_digest(receipt: PersonaPreparationReceipt) -> str:
    return _privacy_hash(
        {
            "source_mode": receipt.source_mode,
            "language": receipt.language,
            "turn_key_sha256": persona_receipt_value_sha256(receipt.turn_key),
            "subject_context_sha256": persona_receipt_value_sha256(
                receipt.subject_context
            ),
            "subject_continuity_prompt_sha256": hashlib.sha256(
                receipt.subject_continuity_prompt.encode("utf-8")
            ).hexdigest(),
            "perceptible_presence_prompt_sha256": hashlib.sha256(
                receipt.perceptible_presence_prompt.encode("utf-8")
            ).hexdigest(),
        }
    )


_PERSONA_MANIFEST_BINDING_FIELDS: Final = (
    "id",
    "order",
    "owner",
    "source_kind",
    "cache_tier",
    "chars",
    "sha256",
)


def _safe_persona_binding_scalar(value: object) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    return {"unsupported_exact_type": True}


def _persona_manifest_binding_sha256(manifest: object) -> str:
    if type(manifest) not in {dict, FrozenJsonObject}:
        return _privacy_hash({"manifest_state": "unavailable"})
    sections = manifest.get("sections")
    if type(sections) not in {list, tuple}:
        section_projection: list[dict[str, object]] = [
            {"section_state": "unavailable"}
        ]
    else:
        section_projection = []
        for row in sections:
            if type(row) not in {dict, FrozenJsonObject}:
                section_projection.append({"section_state": "unavailable"})
                continue
            section_projection.append(
                {
                    field: _safe_persona_binding_scalar(row.get(field))
                    for field in _PERSONA_MANIFEST_BINDING_FIELDS
                }
            )
    return _privacy_hash(
        {
            "prompt_sha256": _safe_persona_binding_scalar(
                manifest.get("prompt_sha256")
            ),
            "sections": section_projection,
        }
    )


def _persona_subject_context_binding_sha256(
    plan: object,
    *,
    source_mode: str,
) -> str:
    if source_mode == "disabled":
        return _privacy_hash(
            {"disabled_plan_state": "absent" if plan is None else "present"}
        )
    if type(plan) not in {dict, FrozenJsonObject}:
        return _privacy_hash({"subject_context_state": "unavailable"})
    try:
        subject_context = plan["subject_context"]
    except KeyError:
        return _privacy_hash({"subject_context_state": "missing"})
    if type(plan) is dict:
        try:
            freeze_persona_receipt_value(subject_context)
        except Exception:
            return _privacy_hash({"subject_context_state": "unsupported"})
    return _privacy_hash(subject_context)


def _persona_final_binding_hashes(
    *,
    runtime_input: RuntimeInput,
    receipt: PersonaPreparationReceipt,
) -> tuple[str, str, str]:
    metadata = runtime_input.request.metadata
    plan = metadata.get("persona_turn_plan") if type(metadata) is dict else None
    manifest = metadata.get("prompt_manifest") if type(metadata) is dict else None
    system_message = next(
        (
            message
            for message in runtime_input.messages
            if type(message) is RuntimeMessage and message.role == "system"
        ),
        None,
    )
    system_binding = (
        _privacy_hash(_message_payload(system_message))
        if system_message is not None
        else _privacy_hash({"system_message_state": "unavailable"})
    )
    return (
        _persona_subject_context_binding_sha256(
            plan,
            source_mode=receipt.source_mode,
        ),
        _persona_manifest_binding_sha256(manifest),
        system_binding,
    )


def _observed_persona_binding_hashes(
    observation: TurnAssemblyInputBoundaryObservation,
) -> tuple[str, str, str]:
    persona = observation.surface("persona")
    prompt = observation.surface("prompt")
    return (
        _persona_subject_context_binding_sha256(
            persona.fields["turn_plan"],
            source_mode="observed",
        ),
        _persona_manifest_binding_sha256(
            prompt.fields["content_free_manifest"]
        ),
        _privacy_hash(prompt.fields["compiled_system_message"]),
    )


def _persona_receipt_mismatches(
    *,
    runtime_input: RuntimeInput,
    receipt: PersonaPreparationReceipt,
) -> tuple[str, ...]:
    mismatched: list[str] = []
    metadata = runtime_input.request.metadata
    plan = metadata.get("persona_turn_plan") if type(metadata) is dict else None
    if receipt.source_mode == "disabled":
        if plan is not None:
            mismatched.append("subject_context_linkage")
    elif type(plan) is not dict or "subject_context" not in plan:
        mismatched.append("subject_context_linkage")
    else:
        try:
            final_context = freeze_persona_receipt_value(plan["subject_context"])
        except Exception:
            mismatched.append("subject_context_linkage")
        else:
            if final_context != receipt.subject_context:
                mismatched.append("subject_context_linkage")

    manifest = metadata.get("prompt_manifest") if type(metadata) is dict else None
    sections = manifest.get("sections") if type(manifest) is dict else None
    if type(sections) is not list or any(type(row) is not dict for row in sections):
        return tuple(dict.fromkeys((*mismatched, "persona_section_manifest")))
    ids = [row.get("id") for row in sections]
    if any(type(section_id) is not str for section_id in ids) or len(ids) != len(
        set(ids)
    ):
        mismatched.append("persona_section_manifest")
        if any(type(section_id) is not str for section_id in ids):
            return tuple(dict.fromkeys(mismatched))

    expected_sections = (
        (
            "session.subject_continuity",
            receipt.subject_continuity_prompt,
            "session",
            "subject_continuity_section",
        ),
        (
            "turn.perceptible_presence",
            receipt.perceptible_presence_prompt,
            "turn",
            "perceptible_presence_section",
        ),
    )
    orders: dict[str, int] = {}
    for section_id, content, cache_tier, mismatch_name in expected_sections:
        matching = [
            (index, row)
            for index, row in enumerate(sections)
            if row.get("id") == section_id
        ]
        if not content:
            if matching:
                mismatched.append(mismatch_name)
            continue
        if len(matching) != 1:
            mismatched.append(mismatch_name)
            continue
        expected_order, row = matching[0]
        order = row.get("order")
        if type(order) is not int or order != expected_order:
            mismatched.append("persona_section_order")
        else:
            orders[section_id] = order
        if (
            type(row.get("owner")) is not str
            or row.get("owner") != "persona"
            or type(row.get("source_kind")) is not str
            or row.get("source_kind") != "runtime_projection"
            or type(row.get("cache_tier")) is not str
            or row.get("cache_tier") != cache_tier
            or type(row.get("chars")) is not int
            or row.get("chars") != len(content)
            or type(row.get("sha256")) is not str
            or row.get("sha256")
            != hashlib.sha256(content.encode("utf-8")).hexdigest()
        ):
            mismatched.append(mismatch_name)
    subject_order = orders.get("session.subject_continuity")
    presence_order = orders.get("turn.perceptible_presence")
    if (
        subject_order is not None
        and presence_order is not None
        and subject_order >= presence_order
    ):
        mismatched.append("persona_section_order")

    system_message = next(
        (
            message
            for message in runtime_input.messages
            if type(message) is RuntimeMessage and message.role == "system"
        ),
        None,
    )
    expected_prompt_sha256 = (
        hashlib.sha256(system_message.text_content().encode("utf-8")).hexdigest()
        if system_message is not None
        else None
    )
    if (
        expected_prompt_sha256 is None
        or type(manifest) is not dict
        or type(manifest.get("prompt_sha256")) is not str
        or manifest.get("prompt_sha256") != expected_prompt_sha256
    ):
        mismatched.append("prompt_manifest_linkage")
    return tuple(dict.fromkeys(mismatched))


@dataclass(frozen=True, slots=True, repr=False)
class OracleSurfaceComparison:
    """Content-free comparison status for one protected surface."""

    name: str
    status: OracleSurfaceStatus
    matched_field_count: int
    mismatched_fields: tuple[str, ...] = ()
    unverified_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.name) is not str or self.name not in _COMPARISON_FIELDS:
            raise ValueError("Invalid oracle comparison surface")
        if type(self.status) is not str or self.status not in {
            "matched",
            "partial_match",
            "mismatched",
            "unverified",
            "unavailable",
        }:
            raise ValueError("Invalid oracle surface status")
        if type(self.matched_field_count) is not int or self.matched_field_count < 0:
            raise ValueError("Invalid oracle matched field count")
        object.__setattr__(
            self,
            "mismatched_fields",
            _safe_field_names(self.mismatched_fields),
        )
        object.__setattr__(
            self,
            "unverified_fields",
            _safe_field_names(self.unverified_fields),
        )

    def privacy_evidence(self) -> dict[str, Any]:
        safe_name = (
            self.name
            if type(self.name) is str and self.name in _COMPARISON_FIELDS
            else "invalid"
        )
        safe_status = (
            self.status
            if type(self.status) is str
            and self.status
            in {
                "matched",
                "partial_match",
                "mismatched",
                "unverified",
                "unavailable",
            }
            else "unavailable"
        )
        mismatched_fields = _safe_field_names(self.mismatched_fields)
        unverified_fields = _safe_field_names(self.unverified_fields)
        matched_field_count = (
            self.matched_field_count
            if type(self.matched_field_count) is int
            and self.matched_field_count >= 0
            else 0
        )
        return {
            "surface": safe_name,
            "status": safe_status,
            "matched_field_count": matched_field_count,
            "mismatched_field_count": len(mismatched_fields),
            "mismatched_fields_sha256": _privacy_hash(
                list(mismatched_fields)
            ),
            "unverified_field_count": len(unverified_fields),
            "unverified_fields_sha256": _privacy_hash(
                list(unverified_fields)
            ),
        }

    def __repr__(self) -> str:
        safe_name = (
            self.name
            if type(self.name) is str and self.name in _COMPARISON_FIELDS
            else "invalid"
        )
        safe_status = (
            self.status
            if type(self.status) is str
            and self.status
            in {
                "matched",
                "partial_match",
                "mismatched",
                "unverified",
                "unavailable",
            }
            else "unavailable"
        )
        matched_field_count: int | str = (
            self.matched_field_count
            if type(self.matched_field_count) is int
            and self.matched_field_count >= 0
            else "invalid"
        )
        mismatched_field_count: int | str = (
            len(self.mismatched_fields)
            if type(self.mismatched_fields) is tuple
            else "invalid"
        )
        unverified_field_count: int | str = (
            len(self.unverified_fields)
            if type(self.unverified_fields) is tuple
            else "invalid"
        )
        return (
            "OracleSurfaceComparison("
            f"name={safe_name!r}, status={safe_status!r}, "
            f"matched_field_count={matched_field_count!r}, "
            f"mismatched_field_count={mismatched_field_count!r}, "
            f"unverified_field_count={unverified_field_count!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TurnAssemblyOracleReport:
    """Diagnostic-only report; it deliberately has no whole-assembly pass."""

    status: OracleReportStatus
    reason_code: str
    surfaces: tuple[OracleSurfaceComparison, ...] = ()
    observation_sha256: str | None = None
    snapshot_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in {
            "partial_match",
            "mismatched",
            "unavailable",
        }:
            raise ValueError("Invalid oracle report status")
        object.__setattr__(self, "reason_code", _safe_reason_code(self.reason_code))
        if type(self.surfaces) is not tuple or any(
            type(surface) is not OracleSurfaceComparison
            for surface in self.surfaces
        ):
            raise TypeError("Oracle report surfaces must be exact values")
        if self.status == "unavailable" and self.surfaces:
            raise ValueError("Unavailable oracle report cannot contain comparisons")
        if self.status != "unavailable" and tuple(
            surface.name for surface in self.surfaces
        ) != REQUIRED_TURN_ASSEMBLY_SURFACES:
            raise ValueError("Oracle report requires all protected surfaces")
        for value in (self.observation_sha256, self.snapshot_sha256):
            if value is not None and _safe_sha256(value) is None:
                raise ValueError("Oracle evidence requires SHA-256 values")

    def privacy_evidence(self) -> dict[str, Any]:
        safe_status = (
            self.status
            if type(self.status) is str
            and self.status in {"partial_match", "mismatched", "unavailable"}
            else "unavailable"
        )
        safe_surfaces = (
            self.surfaces
            if type(self.surfaces) is tuple
            and all(type(surface) is OracleSurfaceComparison for surface in self.surfaces)
            else ()
        )
        return {
            "status": safe_status,
            "reason_code": _safe_reason_code(self.reason_code),
            "surface_count": len(safe_surfaces),
            "matched_surface_count": sum(
                surface.status == "matched" for surface in safe_surfaces
            ),
            "partial_surface_count": sum(
                surface.status == "partial_match" for surface in safe_surfaces
            ),
            "mismatched_surface_count": sum(
                surface.status == "mismatched" for surface in safe_surfaces
            ),
            "unverified_surface_count": sum(
                surface.status == "unverified" for surface in safe_surfaces
            ),
            "observation_sha256": _safe_sha256(self.observation_sha256),
            "snapshot_sha256": _safe_sha256(self.snapshot_sha256),
            "surfaces": [surface.privacy_evidence() for surface in safe_surfaces],
        }

    def __repr__(self) -> str:
        safe_status = (
            self.status
            if type(self.status) is str
            and self.status in {"partial_match", "mismatched", "unavailable"}
            else "unavailable"
        )
        surface_count: int | str = (
            len(self.surfaces) if type(self.surfaces) is tuple else "invalid"
        )
        return (
            "TurnAssemblyOracleReport("
            f"status={safe_status!r}, "
            f"reason_code={_safe_reason_code(self.reason_code)!r}, "
            f"surface_count={surface_count!r}, private_values=True)"
        )


def _message_payload(message: RuntimeMessage) -> dict[str, Any]:
    if type(message) is not RuntimeMessage:
        raise TypeError("Oracle requires exact RuntimeMessage values")
    payload = message.model_dump(mode="json")
    if type(payload) is not dict:
        raise TypeError("Oracle message projection is unavailable")
    return payload


def _message_item_ids(messages: list[RuntimeMessage]) -> tuple[str, ...]:
    item_ids: list[str] = []
    for index, message in enumerate(messages):
        if type(message) is not RuntimeMessage:
            raise TypeError("Oracle requires exact RuntimeMessage values")
        if message.id is not None and type(message.id) is not str:
            raise TypeError("Oracle message identity is unavailable")
        item_ids.append(f"projected:{index}:{message.id or 'anonymous'}")
    return tuple(item_ids)


def _exact_message_list(
    value: object,
    *,
    label: str,
) -> list[RuntimeMessage]:
    if type(value) is not list or any(
        type(message) is not RuntimeMessage for message in value
    ):
        raise TypeError(f"Oracle {label} messages are unavailable")
    return value


def observe_prepared_turn_sources(
    *,
    runtime_input: RuntimeInput,
    memory_messages: list[RuntimeMessage],
    recall_messages: list[RuntimeMessage],
    plan_hint_message: RuntimeMessage | None,
    turn_messages: list[RuntimeMessage],
    attachment_manifest_message: RuntimeMessage | None,
    persona_preparation_receipt: object | None = None,
) -> PreparedTurnSourceObservation:
    """Observe prepared source segments without consulting their snapshot.

    This projection also checks that the exact final RuntimeInput message
    sequence is the documented concatenation of the already-prepared segments.
    It never re-reads or recomputes a preparation source.
    """

    if type(runtime_input) is not RuntimeInput:
        raise TypeError("Oracle requires the exact RuntimeInput")
    if type(runtime_input.messages) is not list or any(
        type(message) is not RuntimeMessage for message in runtime_input.messages
    ):
        raise TypeError("Oracle final messages are unavailable")
    memory = _exact_message_list(memory_messages, label="memory")
    recall = _exact_message_list(recall_messages, label="recall")
    turns = _exact_message_list(turn_messages, label="turn")
    if plan_hint_message is not None and type(plan_hint_message) is not RuntimeMessage:
        raise TypeError("Oracle plan hint is unavailable")
    if attachment_manifest_message is not None and (
        type(attachment_manifest_message) is not RuntimeMessage
    ):
        raise TypeError("Oracle attachment manifest is unavailable")

    expected_messages = [
        *memory,
        *recall,
        *([plan_hint_message] if plan_hint_message is not None else []),
        *turns,
    ]
    final_messages = runtime_input.messages
    expected_payloads = [_message_payload(message) for message in expected_messages]
    final_payloads = [_message_payload(message) for message in final_messages]
    expected_item_ids = _message_item_ids(expected_messages)
    final_item_ids = _message_item_ids(final_messages)
    mismatched: list[str] = []
    if _privacy_hash(expected_payloads) != _privacy_hash(final_payloads):
        mismatched.append("projected_composition")
    if expected_item_ids != final_item_ids:
        mismatched.append("projected_item_ids")

    if attachment_manifest_message is None:
        if any(
            message.metadata.get("kind") == "trusted_attachment_manifest"
            for message in turns
        ):
            mismatched.append("attachment_manifest_linkage")
        manifest_payload = None
    else:
        manifest_payload = _message_payload(attachment_manifest_message)
        if not turns or turns[0] is not attachment_manifest_message:
            mismatched.append("attachment_manifest_linkage")

    memory_fields = freeze_turn_assembly_value(
        {
            "memory_messages": [_message_payload(message) for message in memory],
            "recall_messages": [_message_payload(message) for message in recall],
            "plan_hint_message": (
                _message_payload(plan_hint_message)
                if plan_hint_message is not None
                else None
            ),
            "turn_messages": [_message_payload(message) for message in turns],
            "projected_messages": final_payloads,
            "segment_counts": {
                "memory": len(memory),
                "recall": len(recall),
                "plan_hint": 1 if plan_hint_message is not None else 0,
                "turn": len(turns),
            },
        }
    )
    frozen_manifest = freeze_turn_assembly_value(manifest_payload)
    if type(memory_fields) is not FrozenJsonObject:
        raise TypeError("Oracle prepared source projection is unavailable")
    persona_available = type(persona_preparation_receipt) is PersonaPreparationReceipt
    persona_digest = None
    persona_subject_context_binding = None
    persona_manifest_binding = None
    persona_system_message_binding = None
    persona_mismatches: tuple[str, ...] = ()
    if persona_available:
        receipt = persona_preparation_receipt
        persona_digest = _persona_receipt_digest(receipt)
        (
            persona_subject_context_binding,
            persona_manifest_binding,
            persona_system_message_binding,
        ) = _persona_final_binding_hashes(
            runtime_input=runtime_input,
            receipt=receipt,
        )
        persona_mismatches = _persona_receipt_mismatches(
            runtime_input=runtime_input,
            receipt=receipt,
        )
    return PreparedTurnSourceObservation(
        memory_fields=memory_fields,
        memory_item_ids=final_item_ids,
        attachment_manifest_message=frozen_manifest,
        persona_provenance_available=persona_available,
        persona_receipt_sha256=persona_digest,
        persona_subject_context_binding_sha256=(
            persona_subject_context_binding
        ),
        persona_manifest_binding_sha256=persona_manifest_binding,
        persona_system_message_binding_sha256=persona_system_message_binding,
        persona_mismatched_fields=persona_mismatches,
        mismatched_fields=tuple(dict.fromkeys(mismatched)),
    )


def _registry_fields(
    registry: LantaiToolRegistry,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if type(registry) is not LantaiToolRegistry:
        raise TypeError("Oracle requires the exact Native tool registry")

    tool_rows: list[dict[str, Any]] = []
    tool_items = registry.tool_items()
    if type(tool_items) is not list:
        raise TypeError("Oracle tool items are unavailable")
    for item in tool_items:
        if type(item) is not tuple or len(item) != 2 or type(item[0]) is not ToolSpec:
            raise TypeError("Oracle tool spec is unavailable")
        payload = item[0].model_dump(mode="json")
        if type(payload) is not dict:
            raise TypeError("Oracle tool spec projection is unavailable")
        tool_rows.append(payload)

    skill_rows: list[dict[str, Any]] = []
    skill_specs = registry.skill_specs()
    if type(skill_specs) is not list:
        raise TypeError("Oracle skill specs are unavailable")
    for spec in skill_specs:
        if type(spec) is not SkillSpec:
            raise TypeError("Oracle skill spec is unavailable")
        payload = spec.model_dump(mode="json")
        if type(payload) is not dict:
            raise TypeError("Oracle skill projection is unavailable")
        skill_rows.append(payload)

    mcp_rows: list[dict[str, Any]] = []
    mcp_specs = registry.mcp_client_specs()
    if type(mcp_specs) is not list:
        raise TypeError("Oracle MCP specs are unavailable")
    for spec in mcp_specs:
        if type(spec) is not McpClientSpec:
            raise TypeError("Oracle MCP spec is unavailable")
        payload = spec.model_dump(mode="json", exclude={"client"})
        if type(payload) is not dict:
            raise TypeError("Oracle MCP projection is unavailable")
        mcp_rows.append(payload)

    blocked = registry.blocked_tool_names()
    if type(blocked) is not frozenset or any(type(name) is not str for name in blocked):
        raise TypeError("Oracle blocked tool names are unavailable")
    return tool_rows, skill_rows, mcp_rows, sorted(blocked)


def _name_ids(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    names: list[str] = []
    for row in rows:
        name = row.get("name")
        if type(name) is not str or not name:
            raise TypeError("Oracle registry identity is unavailable")
        names.append(name)
    return tuple(names)


def _surface(
    name: str,
    fields: dict[str, Any],
    *,
    item_ids: tuple[str, ...] = (),
) -> InputBoundarySurfaceObservation:
    frozen = freeze_turn_assembly_value(fields)
    if type(frozen) is not FrozenJsonObject:
        raise TypeError("Oracle surface projection must be an object")
    return InputBoundarySurfaceObservation(
        name=name,
        fields=frozen,
        source_ids=_EXPECTED_SOURCE_IDS[name],
        item_ids=item_ids,
        unverified_fields=_UNVERIFIED_FIELDS[name],
    )


def observe_native_input_boundary(
    *,
    runtime_input: RuntimeInput,
    tool_registry: LantaiToolRegistry,
    tool_policy_context: ToolPolicyContext,
) -> TurnAssemblyInputBoundaryObservation:
    """Project expected fields without accepting or consulting a snapshot."""

    if type(runtime_input) is not RuntimeInput:
        raise TypeError("Oracle requires the exact RuntimeInput")
    if type(runtime_input.request) is not RuntimeRequestContext:
        raise TypeError("Oracle requires the exact RuntimeRequestContext")
    if type(runtime_input.messages) is not list:
        raise TypeError("Oracle requires a built-in message list")
    if type(tool_policy_context) is not ToolPolicyContext:
        raise TypeError("Oracle requires the exact input-boundary policy")

    messages = runtime_input.messages
    if any(type(message) is not RuntimeMessage for message in messages):
        raise TypeError("Oracle requires exact RuntimeMessage values")
    system_message = next(
        (message for message in messages if message.role == "system"),
        None,
    )
    if system_message is None:
        raise ValueError("Oracle system message is unavailable")

    request_metadata = runtime_input.request.metadata
    if type(request_metadata) is not dict:
        raise TypeError("Oracle request metadata is unavailable")
    prompt_manifest = request_metadata.get("prompt_manifest")
    if type(prompt_manifest) is not dict:
        raise ValueError("Oracle prompt manifest is unavailable")
    sections = prompt_manifest.get("sections")
    if type(sections) is not list:
        raise ValueError("Oracle prompt sections are unavailable")
    section_ids: list[str] = []
    skill_prompt_sections: list[dict[str, Any]] = []
    for section in sections:
        if type(section) is not dict or type(section.get("id")) is not str:
            raise ValueError("Oracle prompt section identity is unavailable")
        section_ids.append(section["id"])
        if section.get("owner") == "skills" or section["id"].startswith("skills."):
            skill_prompt_sections.append(section)

    if "persona_turn_plan" not in request_metadata or type(
        request_metadata["persona_turn_plan"]
    ) is not dict:
        raise ValueError("Oracle persona projection is unavailable")
    if type(request_metadata.get("ququ_hard_required")) is not bool:
        raise ValueError("Oracle ququ contract is unavailable")
    legacy_context = request_metadata.get("legacy_request_context")
    if type(legacy_context) is not dict:
        raise ValueError("Oracle legacy request context is unavailable")
    if type(legacy_context.get("workspace_access_mode")) is not str or type(
        legacy_context.get("workspace_trust_state")
    ) is not str:
        raise ValueError("Oracle workspace projection is unavailable")
    if "trusted_current_attachments" in legacy_context:
        attachments = legacy_context["trusted_current_attachments"]
        if type(attachments) is not list:
            raise ValueError("Oracle attachment projection is unavailable")
        attachment_presence = "present"
    else:
        attachments = []
        attachment_presence = "absent"

    tool_rows, skill_rows, mcp_rows, blocked_names = _registry_fields(
        tool_registry
    )
    policy_payload = tool_policy_context.model_dump(mode="json")
    if type(policy_payload) is not dict:
        raise TypeError("Oracle policy projection is unavailable")
    message_payloads = [_message_payload(message) for message in messages]

    return TurnAssemblyInputBoundaryObservation(
        surfaces=(
            _surface(
                "prompt",
                {
                    "compiled_system_message": _message_payload(system_message),
                    "content_free_manifest": prompt_manifest,
                },
                item_ids=tuple(section_ids),
            ),
            _surface(
                "persona",
                {
                    "turn_plan": request_metadata["persona_turn_plan"],
                    "ququ_hard_required": request_metadata["ququ_hard_required"],
                    "compiled_system_message_id": system_message.id or "anonymous",
                },
            ),
            _surface(
                "memory_context",
                {"projected_messages": message_payloads},
                item_ids=_message_item_ids(messages),
            ),
            _surface(
                "skills",
                {
                    "registered_skills": skill_rows,
                    "prompt_sections": skill_prompt_sections,
                },
                item_ids=_name_ids(skill_rows),
            ),
            _surface(
                "attachments",
                {
                    "presence": attachment_presence,
                    "trusted_current_attachments": attachments,
                    "workspace_access_mode": legacy_context["workspace_access_mode"],
                    "workspace_trust_state": legacy_context["workspace_trust_state"],
                },
            ),
            _surface(
                "tools",
                {
                    "registered_tools": tool_rows,
                    "registered_mcp_clients": mcp_rows,
                    "blocked_tool_names": blocked_names,
                },
                item_ids=_name_ids(tool_rows),
            ),
            _surface(
                "request_policy",
                {
                    "runtime_request": runtime_input.request.model_dump(mode="json"),
                    "model_slot": runtime_input.model_slot,
                    "tool_policy_context": runtime_input.tool_policy_context,
                    "runtime_metadata": runtime_input.metadata,
                    "native_tool_policy": policy_payload,
                },
            ),
            _surface("compatibility", {}),
        )
    )


def compare_turn_assembly_snapshot(
    *,
    observation: TurnAssemblyInputBoundaryObservation,
    snapshot: TurnAssemblySnapshot,
    prepared_sources: PreparedTurnSourceObservation | None = None,
    request_policy_construction_receipt: (
        RequestPolicyConstructionReceipt | None
    ) = None,
    request_policy_effective_receipt: (
        RequestPolicyEffectiveTurnReceipt | None
    ) = None,
    request_policy_dispatch_receipt: (
        RequestPolicyDispatchReceipt | None
    ) = None,
    request_policy_consumption_receipt: (
        RequestPolicyConsumptionReceipt | None
    ) = None,
) -> TurnAssemblyOracleReport:
    """Compare independent expected fields with one private shadow snapshot."""

    if type(observation) is not TurnAssemblyInputBoundaryObservation:
        raise TypeError("Oracle comparison requires an exact observation")
    if type(snapshot) is not TurnAssemblySnapshot:
        raise TypeError("Oracle comparison requires an exact snapshot")
    if prepared_sources is not None and (
        type(prepared_sources) is not PreparedTurnSourceObservation
    ):
        raise TypeError("Oracle comparison requires exact prepared sources")
    if request_policy_construction_receipt is not None and not (
        is_valid_request_policy_construction_receipt(
            request_policy_construction_receipt
        )
    ):
        raise TypeError("Oracle comparison requires an exact policy receipt")
    if request_policy_effective_receipt is not None and not (
        is_valid_request_policy_effective_receipt(
            request_policy_effective_receipt
        )
    ):
        raise TypeError(
            "Oracle comparison requires an exact effective receipt"
        )
    if request_policy_dispatch_receipt is not None and not (
        is_valid_request_policy_dispatch_receipt(
            request_policy_dispatch_receipt
        )
    ):
        raise TypeError("Oracle comparison requires an exact dispatch receipt")
    if request_policy_consumption_receipt is not None and not (
        is_valid_request_policy_consumption_receipt(
            request_policy_consumption_receipt
        )
    ):
        raise TypeError(
            "Oracle comparison requires an exact consumption receipt"
        )

    comparisons: list[OracleSurfaceComparison] = []
    for observed in observation.surfaces:
        captured = snapshot.surface(observed.name)
        mismatched: list[str] = []
        matched_count = 0
        expected_fields = observed.fields
        unverified_fields = observed.unverified_fields
        if prepared_sources is not None and observed.name == "memory_context":
            expected_fields = prepared_sources.memory_fields
            unverified_fields = ()
            mismatched.extend(
                field
                for field in prepared_sources.mismatched_fields
                if field in {"projected_composition", "projected_item_ids"}
            )
            if _privacy_hash(
                prepared_sources.memory_fields["projected_messages"]
            ) != _privacy_hash(observed.fields["projected_messages"]):
                mismatched.append("projected_composition")
            if prepared_sources.memory_item_ids != observed.item_ids:
                mismatched.append("projected_item_ids")
        elif prepared_sources is not None and observed.name == "persona":
            if prepared_sources.persona_provenance_available:
                unverified_fields = tuple(
                    field
                    for field in unverified_fields
                    if field != "persona_preparation_receipt"
                )
                mismatched.extend(prepared_sources.persona_mismatched_fields)
                (
                    observed_context_binding,
                    observed_manifest_binding,
                    observed_system_message_binding,
                ) = _observed_persona_binding_hashes(observation)
                if (
                    prepared_sources.persona_subject_context_binding_sha256
                    != observed_context_binding
                ):
                    mismatched.append("subject_context_linkage")
                if (
                    prepared_sources.persona_manifest_binding_sha256
                    != observed_manifest_binding
                ):
                    mismatched.append("persona_section_manifest")
                if (
                    prepared_sources.persona_system_message_binding_sha256
                    != observed_system_message_binding
                ):
                    mismatched.append("prompt_manifest_linkage")
        elif prepared_sources is not None and observed.name == "attachments":
            attachment_fields = {
                "presence": observed.fields["presence"],
                "trusted_current_attachments": observed.fields[
                    "trusted_current_attachments"
                ],
                "manifest_message": prepared_sources.attachment_manifest_message,
                "workspace_access_mode": observed.fields["workspace_access_mode"],
                "workspace_trust_state": observed.fields["workspace_trust_state"],
            }
            frozen_attachment_fields = freeze_turn_assembly_value(attachment_fields)
            if type(frozen_attachment_fields) is not FrozenJsonObject:
                raise TypeError("Oracle attachment projection is unavailable")
            expected_fields = frozen_attachment_fields
            unverified_fields = ()
            mismatched.extend(
                field
                for field in prepared_sources.mismatched_fields
                if field == "attachment_manifest_linkage"
            )
        if (
            observed.name == "request_policy"
            and request_policy_construction_receipt is not None
        ):
            try:
                observed_policy_payload = thaw_turn_assembly_value(
                    observed.fields["native_tool_policy"]
                )
                captured_policy_payload = thaw_turn_assembly_value(
                    captured.value["native_tool_policy"]
                )
                observed_policy_sha256 = request_policy_payload_sha256(
                    observed_policy_payload
                )
                captured_policy_sha256 = request_policy_payload_sha256(
                    captured_policy_payload
                )
            except Exception:
                pass
            else:
                if (
                    request_policy_construction_receipt.assigned_payload_sha256
                    == observed_policy_sha256
                    == captured_policy_sha256
                    and request_policy_construction_receipt.top_level_field_count
                    == len(observed_policy_payload)
                    == len(captured_policy_payload)
                ):
                    unverified_fields = tuple(
                        field
                        for field in unverified_fields
                        if field != "native_policy_constructor_copy"
                    )
                else:
                    mismatched.append("native_policy_constructor_copy")
        if (
            observed.name == "request_policy"
            and request_policy_effective_receipt is not None
        ):
            try:
                policy_input = thaw_turn_assembly_value(observed.fields)
                if not request_policy_payload_is_deadline_free(policy_input):
                    raise ValueError("Effective policy deadline is unverified")
                runtime_request = policy_input["runtime_request"]
                base_payload = dict(policy_input["native_tool_policy"])
                input_payload = dict(policy_input["tool_policy_context"])
                runtime_metadata = {
                    **dict(runtime_request["metadata"]),
                    **dict(policy_input["runtime_metadata"]),
                }
                base_request_context = dict(
                    base_payload.pop("request_context", {}) or {}
                )
                input_request_context = dict(
                    input_payload.pop("request_context", {}) or {}
                )
                base_metadata = dict(base_payload.pop("metadata", {}) or {})
                input_metadata = dict(input_payload.pop("metadata", {}) or {})
                turn_id = runtime_request["run_id"]
                if type(turn_id) is not str or not turn_id:
                    raise ValueError("Effective policy turn binding is unavailable")
                request_context_payload = {
                    key: value
                    for key, value in runtime_request.items()
                    if key != "metadata" and value not in (None, {}, [])
                }
                request_context_payload["turn_id"] = turn_id
                expected_context = ToolPolicyContext.model_validate(
                    {
                        **base_payload,
                        **input_payload,
                        "agent_id": runtime_request["agent_id"],
                        "session_id": runtime_request["session_id"],
                        "root_session_id": (
                            runtime_request["root_session_id"]
                            or runtime_request["session_id"]
                        ),
                        "chat_id": runtime_request["chat_id"],
                        "turn_id": turn_id,
                        "workspace_dir": (
                            runtime_request["workspace_dir"]
                            or base_payload.get("workspace_dir")
                        ),
                        "request_context": {
                            **base_request_context,
                            **input_request_context,
                            **request_context_payload,
                            **runtime_metadata,
                        },
                        "metadata": {
                            **base_metadata,
                            **input_metadata,
                            **runtime_metadata,
                        },
                    }
                )
                expected_effective_payload = project_tool_policy_context(
                    expected_context
                )
                if not request_policy_payload_is_deadline_free(
                    expected_effective_payload
                ):
                    raise ValueError("Effective policy deadline is unverified")
                expected_input_sha256 = request_policy_payload_sha256(policy_input)
                expected_effective_sha256 = request_policy_payload_sha256(
                    expected_effective_payload
                )
                expected_run_sha256 = request_policy_payload_sha256(
                    {"run_id": turn_id}
                )
                expected_turn_sha256 = request_policy_payload_sha256(
                    {"turn_id": turn_id}
                )
            except Exception:
                pass
            else:
                if (
                    request_policy_effective_receipt.run_id_sha256
                    == expected_run_sha256
                    and request_policy_effective_receipt.actual_turn_id_sha256
                    == expected_turn_sha256
                    and request_policy_effective_receipt.policy_input_sha256
                    == expected_input_sha256
                    and request_policy_effective_receipt.effective_payload_sha256
                    == expected_effective_sha256
                    and request_policy_effective_receipt.top_level_field_count
                    == len(expected_effective_payload)
                ):
                    unverified_fields = tuple(
                        field
                        for field in unverified_fields
                        if field != "native_effective_turn_policy"
                    )
                else:
                    mismatched.append("native_effective_turn_policy")
        if (
            observed.name == "request_policy"
            and request_policy_dispatch_receipt is not None
            and request_policy_effective_receipt is not None
        ):
            dispatch_matches = bool(
                request_policy_dispatch_receipt.observer_sha256
                == request_policy_effective_receipt.observer_sha256
                and request_policy_dispatch_receipt.nonce_sha256
                == request_policy_effective_receipt.nonce_sha256
                and request_policy_dispatch_receipt.invocation_sha256
                == request_policy_effective_receipt.invocation_sha256
                and request_policy_dispatch_receipt.effective_payload_sha256
                == request_policy_effective_receipt.effective_payload_sha256
                and request_policy_dispatch_receipt.evaluation_count >= 1
                and request_policy_dispatch_receipt.middleware_dispatch_count >= 0
                and request_policy_dispatch_receipt.all_contexts_matched
            )
            if dispatch_matches:
                unverified_fields = tuple(
                    field
                    for field in unverified_fields
                    if field != "native_per_tool_policy_dispatch"
                )
            else:
                mismatched.append("native_per_tool_policy_dispatch")
        if (
            observed.name == "request_policy"
            and request_policy_consumption_receipt is not None
            and request_policy_dispatch_receipt is not None
            and request_policy_effective_receipt is not None
        ):
            consumption = request_policy_consumption_receipt
            dispatch = request_policy_dispatch_receipt
            effective = request_policy_effective_receipt
            consumption_binding_matches = bool(
                consumption.observer_sha256 == effective.observer_sha256
                and consumption.nonce_sha256 == effective.nonce_sha256
                and consumption.invocation_sha256
                == effective.invocation_sha256
                and consumption.dispatch_evaluations_sha256
                == dispatch.evaluations_sha256
                and consumption.default_middleware_dispatch_count
                <= dispatch.middleware_dispatch_count
                and consumption.all_context_chains_valid
            )
            consumption_coverage_complete = bool(
                consumption.live_permission_evaluation_count
                == consumption.consumption_count
                and consumption.guard_consumption_count
                == consumption.consumption_count
            )
            if consumption_binding_matches and consumption_coverage_complete:
                unverified_fields = tuple(
                    field
                    for field in unverified_fields
                    if field
                    != "native_live_permission_override_and_guard_consumption"
                )
            elif not consumption_binding_matches:
                mismatched.append(
                    "native_live_permission_override_and_guard_consumption"
                )
        if type(captured.value) is not FrozenJsonObject:
            mismatched.extend(tuple(expected_fields))
        else:
            if observed.name != "compatibility":
                actual_field_order = tuple(captured.value)
                expected_field_order = _SNAPSHOT_FIELD_ORDER[observed.name]
                expected_contract_fields = frozenset(
                    (
                        *_COMPARISON_FIELDS[observed.name],
                        *_UNVERIFIED_SNAPSHOT_FIELDS[observed.name],
                    )
                )
                if frozenset(expected_field_order) != expected_contract_fields:
                    raise RuntimeError("Oracle surface schema is unavailable")
                actual_field_set = frozenset(actual_field_order)
                expected_field_set = frozenset(expected_field_order)
                if actual_field_set - expected_field_set:
                    mismatched.append("unexpected_fields")
                if expected_field_set - actual_field_set:
                    mismatched.append("missing_fields")
                if (
                    actual_field_set == expected_field_set
                    and actual_field_order != expected_field_order
                ):
                    mismatched.append("field_order")
            for field_name, expected_value in expected_fields.items():
                try:
                    actual_value = captured.value[field_name]
                except KeyError:
                    mismatched.append(field_name)
                else:
                    if _privacy_hash(actual_value) == _privacy_hash(expected_value):
                        matched_count += 1
                    else:
                        mismatched.append(field_name)
        if captured.source_ids == observed.source_ids:
            matched_count += 1
        else:
            mismatched.append("source_ids")
        expected_item_ids = (
            prepared_sources.memory_item_ids
            if prepared_sources is not None and observed.name == "memory_context"
            else observed.item_ids
        )
        if captured.item_ids == expected_item_ids:
            matched_count += 1
        else:
            mismatched.append("item_ids")

        mismatched = list(dict.fromkeys(mismatched))
        if mismatched:
            status: OracleSurfaceStatus = "mismatched"
        elif unverified_fields:
            status = "partial_match" if expected_fields else "unverified"
        else:
            status = "matched"
        comparisons.append(
            OracleSurfaceComparison(
                name=observed.name,
                status=status,
                matched_field_count=matched_count,
                mismatched_fields=tuple(mismatched),
                unverified_fields=unverified_fields,
            )
        )

    report_status: OracleReportStatus = (
        "mismatched"
        if any(surface.status == "mismatched" for surface in comparisons)
        else "partial_match"
    )
    observation_digest = _privacy_hash(
        {
            "final_input": [
                {
                    "name": surface.name,
                    "fields": surface.fields,
                    "source_ids": list(surface.source_ids),
                    "item_ids": list(surface.item_ids),
                }
                for surface in observation.surfaces
            ],
            "prepared_sources": (
                {
                    "memory_fields": prepared_sources.memory_fields,
                    "memory_item_ids": list(prepared_sources.memory_item_ids),
                    "attachment_manifest_message": (
                        prepared_sources.attachment_manifest_message
                    ),
                    "persona_provenance_available": (
                        prepared_sources.persona_provenance_available
                    ),
                    "persona_receipt_sha256": (
                        prepared_sources.persona_receipt_sha256
                    ),
                    "persona_subject_context_binding_sha256": (
                        prepared_sources.persona_subject_context_binding_sha256
                    ),
                    "persona_manifest_binding_sha256": (
                        prepared_sources.persona_manifest_binding_sha256
                    ),
                    "persona_system_message_binding_sha256": (
                        prepared_sources.persona_system_message_binding_sha256
                    ),
                    "persona_mismatched_fields": list(
                        prepared_sources.persona_mismatched_fields
                    ),
                    "mismatched_fields": list(
                        prepared_sources.mismatched_fields
                    ),
                }
                if prepared_sources is not None
                else None
            ),
            "request_policy_construction_receipt": (
                {
                    "schema_version": (
                        request_policy_construction_receipt.schema_version
                    ),
                    "assigned_payload_sha256": (
                        request_policy_construction_receipt.assigned_payload_sha256
                    ),
                    "top_level_field_count": (
                        request_policy_construction_receipt.top_level_field_count
                    ),
                }
                if request_policy_construction_receipt is not None
                else None
            ),
            "request_policy_effective_receipt": (
                {
                    "schema_version": request_policy_effective_receipt.schema_version,
                    "observer_sha256": request_policy_effective_receipt.observer_sha256,
                    "nonce_sha256": request_policy_effective_receipt.nonce_sha256,
                    "invocation_sha256": (
                        request_policy_effective_receipt.invocation_sha256
                    ),
                    "run_id_sha256": request_policy_effective_receipt.run_id_sha256,
                    "actual_turn_id_sha256": (
                        request_policy_effective_receipt.actual_turn_id_sha256
                    ),
                    "policy_input_sha256": (
                        request_policy_effective_receipt.policy_input_sha256
                    ),
                    "effective_payload_sha256": (
                        request_policy_effective_receipt.effective_payload_sha256
                    ),
                    "top_level_field_count": (
                        request_policy_effective_receipt.top_level_field_count
                    ),
                }
                if request_policy_effective_receipt is not None
                else None
            ),
            "request_policy_dispatch_receipt": (
                {
                    "schema_version": request_policy_dispatch_receipt.schema_version,
                    "observer_sha256": request_policy_dispatch_receipt.observer_sha256,
                    "nonce_sha256": request_policy_dispatch_receipt.nonce_sha256,
                    "invocation_sha256": (
                        request_policy_dispatch_receipt.invocation_sha256
                    ),
                    "effective_payload_sha256": (
                        request_policy_dispatch_receipt.effective_payload_sha256
                    ),
                    "evaluations_sha256": (
                        request_policy_dispatch_receipt.evaluations_sha256
                    ),
                    "evaluation_count": request_policy_dispatch_receipt.evaluation_count,
                    "middleware_dispatch_count": (
                        request_policy_dispatch_receipt.middleware_dispatch_count
                    ),
                    "all_contexts_matched": (
                        request_policy_dispatch_receipt.all_contexts_matched
                    ),
                }
                if request_policy_dispatch_receipt is not None
                else None
            ),
            "request_policy_consumption_receipt": (
                {
                    "schema_version": (
                        request_policy_consumption_receipt.schema_version
                    ),
                    "observer_sha256": (
                        request_policy_consumption_receipt.observer_sha256
                    ),
                    "nonce_sha256": (
                        request_policy_consumption_receipt.nonce_sha256
                    ),
                    "invocation_sha256": (
                        request_policy_consumption_receipt.invocation_sha256
                    ),
                    "dispatch_evaluations_sha256": (
                        request_policy_consumption_receipt
                        .dispatch_evaluations_sha256
                    ),
                    "consumptions_sha256": (
                        request_policy_consumption_receipt.consumptions_sha256
                    ),
                    "default_middleware_dispatch_count": (
                        request_policy_consumption_receipt
                        .default_middleware_dispatch_count
                    ),
                    "consumption_count": (
                        request_policy_consumption_receipt.consumption_count
                    ),
                    "live_permission_evaluation_count": (
                        request_policy_consumption_receipt
                        .live_permission_evaluation_count
                    ),
                    "live_override_applied_count": (
                        request_policy_consumption_receipt
                        .live_override_applied_count
                    ),
                    "guard_consumption_count": (
                        request_policy_consumption_receipt
                        .guard_consumption_count
                    ),
                    "all_context_chains_valid": (
                        request_policy_consumption_receipt
                        .all_context_chains_valid
                    ),
                }
                if request_policy_consumption_receipt is not None
                else None
            ),
        }
    )
    return TurnAssemblyOracleReport(
        status=report_status,
        reason_code="compared",
        surfaces=tuple(comparisons),
        observation_sha256=observation_digest,
        snapshot_sha256=snapshot.assembly_sha256,
    )


def unavailable_turn_assembly_oracle_report(
    reason_code: object,
) -> TurnAssemblyOracleReport:
    """Build a content-free unavailable report from a fixed reason code."""

    return TurnAssemblyOracleReport(
        status="unavailable",
        reason_code=_safe_reason_code(reason_code),
    )


def log_turn_assembly_oracle_report(report: TurnAssemblyOracleReport) -> None:
    """Emit only fixed statuses, hashes, and counts; diagnostics fail open."""

    if type(report) is not TurnAssemblyOracleReport:
        return
    try:
        logger.info(
            "Turn assembly input-boundary oracle evaluated",
            extra={"turn_assembly_oracle": report.privacy_evidence()},
        )
    except Exception:
        pass


__all__ = [
    "InputBoundarySurfaceObservation",
    "OracleSurfaceComparison",
    "PreparedTurnSourceObservation",
    "TurnAssemblyInputBoundaryObservation",
    "TurnAssemblyOracleReport",
    "compare_turn_assembly_snapshot",
    "log_turn_assembly_oracle_report",
    "observe_native_input_boundary",
    "observe_prepared_turn_sources",
    "unavailable_turn_assembly_oracle_report",
]
