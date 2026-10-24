# -*- coding: utf-8 -*-
"""Canonical tool value, model projection, and UI intent contracts."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..tools import ToolResult
from .integrity import canonical_json_bytes, canonical_sha256, json_safe


ToolValueKind = Literal["structured", "content", "error", "empty"]
ToolUIIntentKind = Literal[
    "activity",
    "artifact",
    "media",
    "approval",
    "error",
]


def _non_empty(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    return text


class CanonicalToolValue(BaseModel):
    """Provider- and UI-independent truth returned by a tool execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    call_id: str
    tool_name: str
    ok: bool
    kind: ToolValueKind
    value: Any = None
    content: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    value_sha256: str
    byte_length: int = Field(ge=0)

    @field_validator("call_id", "tool_name", "value_sha256")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)


class ModelToolProjection(BaseModel):
    """The bounded, instruction-inert representation exposed to the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    blocks: tuple[dict[str, Any], ...] = ()
    projection_sha256: str
    instruction_trust: Literal[
        "untrusted_tool_evidence"
    ] = "untrusted_tool_evidence"


class ToolUIIntent(BaseModel):
    """Semantic display intent; renderers remain free to choose components."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ToolUIIntentKind
    title: str
    summary: str = ""
    severity: Literal["info", "success", "warning", "error"] = "info"
    media_count: int = Field(default=0, ge=0)
    artifact_count: int = Field(default=0, ge=0)


class CanonicalToolEnvelope(BaseModel):
    """One checked envelope joining value, model surface, UI and Effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    envelope_id: str
    canonical: CanonicalToolValue
    model_projection: ModelToolProjection
    ui_intent: ToolUIIntent
    effect_id: str | None = None
    effect_attempt_id: str | None = None
    effect_revision: int | None = Field(default=None, ge=0)
    effect_status: str | None = None
    integrity_sha256: str

    def verify_integrity(self) -> bool:
        payload = self.model_dump(
            mode="json",
            exclude={"integrity_sha256"},
        )
        return canonical_sha256(payload) == self.integrity_sha256


def _model_blocks(result: ToolResult) -> list[dict[str, Any]]:
    if not result.ok:
        return [{"type": "text", "text": result.error or ""}]
    output = [dict(block) for block in result.content]
    if "structured_content" in result.metadata and not output:
        output.append(
            {
                "type": "structured",
                "data": json_safe(result.metadata["structured_content"]),
            },
        )
    return output


def _canonical_value(result: ToolResult) -> tuple[ToolValueKind, Any]:
    if not result.ok:
        return "error", {"error": result.error or "Tool call failed."}
    if "structured_content" in result.metadata:
        return "structured", json_safe(result.metadata["structured_content"])
    if result.content:
        return "content", [json_safe(block) for block in result.content]
    return "empty", None


def _ui_intent(
    result: ToolResult, blocks: list[dict[str, Any]]
) -> ToolUIIntent:
    media_types = {"image", "audio", "video"}
    artifact_types = {"file", "resource", "resource_link"}
    media_count = sum(
        1 for block in blocks if str(block.get("type") or "") in media_types
    )
    artifact_count = sum(
        1 for block in blocks if str(block.get("type") or "") in artifact_types
    )
    if not result.ok:
        kind: ToolUIIntentKind = "error"
    elif artifact_count:
        kind = "artifact"
    elif media_count:
        kind = "media"
    elif result.metadata.get("approval"):
        kind = "approval"
    else:
        kind = "activity"
    text_parts = [
        str(block.get("text") or "").strip()
        for block in blocks
        if str(block.get("type") or "") == "text"
        and str(block.get("text") or "").strip()
    ]
    summary = (result.error or "\n".join(text_parts)).strip()[:240]
    return ToolUIIntent(
        kind=kind,
        title=result.name,
        summary=summary,
        severity="error" if not result.ok else "success",
        media_count=media_count,
        artifact_count=artifact_count,
    )


def build_canonical_tool_envelope(result: ToolResult) -> CanonicalToolEnvelope:
    """Build a deterministic envelope without mutating ``ToolResult``."""

    blocks = _model_blocks(result)
    kind, value = _canonical_value(result)
    value_payload = {
        "call_id": result.call_id,
        "tool_name": result.name,
        "ok": result.ok,
        "kind": kind,
        "value": value,
        "content": [json_safe(block) for block in result.content],
        "error": result.error,
    }
    canonical = CanonicalToolValue(
        call_id=result.call_id,
        tool_name=result.name,
        ok=result.ok,
        kind=kind,
        value=value,
        content=tuple(dict(json_safe(block)) for block in result.content),
        error=result.error,
        value_sha256=canonical_sha256(value_payload),
        byte_length=len(canonical_json_bytes(value_payload)),
    )
    model_projection = ModelToolProjection(
        blocks=tuple(blocks),
        projection_sha256=canonical_sha256(blocks),
    )
    ui_intent = _ui_intent(result, blocks)
    effect_id = str(result.metadata.get("effect_id") or "").strip() or None
    effect_attempt_id = (
        str(result.metadata.get("attempt_id") or "").strip() or None
    )
    raw_effect_revision = result.metadata.get("effect_revision")
    effect_revision = (
        max(0, int(raw_effect_revision))
        if raw_effect_revision is not None
        else None
    )
    effect_status = (
        str(result.metadata.get("effect_status") or "").strip() or None
    )
    envelope_payload = {
        "schema_version": 1,
        "envelope_id": (
            f"tool-envelope:{result.call_id}:{canonical.value_sha256[:16]}"
        ),
        "canonical": canonical,
        "model_projection": model_projection,
        "ui_intent": ui_intent,
        "effect_id": effect_id,
        "effect_attempt_id": effect_attempt_id,
        "effect_revision": effect_revision,
        "effect_status": effect_status,
    }
    return CanonicalToolEnvelope(
        **envelope_payload,
        integrity_sha256=canonical_sha256(envelope_payload),
    )


def parse_or_build_tool_envelope(result: ToolResult) -> CanonicalToolEnvelope:
    existing = result.metadata.get("canonical_tool_envelope")
    if isinstance(existing, dict):
        try:
            envelope = CanonicalToolEnvelope.model_validate(existing)
        except Exception:
            envelope = build_canonical_tool_envelope(result)
        else:
            if envelope.verify_integrity():
                return envelope
    return build_canonical_tool_envelope(result)


__all__ = [
    "CanonicalToolEnvelope",
    "CanonicalToolValue",
    "ModelToolProjection",
    "ToolUIIntent",
    "build_canonical_tool_envelope",
    "parse_or_build_tool_envelope",
]
