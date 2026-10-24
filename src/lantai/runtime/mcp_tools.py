# -*- coding: utf-8 -*-
"""Deep MCP-to-Runtime projection contracts.

MCP annotations are server-supplied hints, not an authorization boundary.  A
connector must be official or explicitly trusted before those hints may relax
Lantai's fail-closed effect and recovery defaults.
"""

from __future__ import annotations

from typing import Any

from .effects import RuntimeEffectRecoveryCapability
from .tools import McpClientSpec, ToolSpec


def _field(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json", by_alias=False)
        except TypeError:
            return value.model_dump()
    return str(value)


def _annotation_payload(raw_tool: Any) -> dict[str, Any]:
    annotations = _field(raw_tool, "annotations")
    if annotations is None:
        return {}
    aliases = {
        "title": ("title",),
        "read_only_hint": ("read_only_hint", "readOnlyHint"),
        "destructive_hint": ("destructive_hint", "destructiveHint"),
        "idempotent_hint": ("idempotent_hint", "idempotentHint"),
        "open_world_hint": ("open_world_hint", "openWorldHint"),
    }
    payload: dict[str, Any] = {}
    for canonical, names in aliases.items():
        value = _field(annotations, *names)
        if value is not None:
            payload[canonical] = _json_value(value)
    return payload


def _trusted_annotations(client_spec: McpClientSpec) -> bool:
    if client_spec.metadata.get("official_integration") == "chrome-devtools":
        from ..app.mcp.chrome_devtools import (
            is_official_chrome_devtools_metadata,
        )

        return is_official_chrome_devtools_metadata(client_spec.metadata)
    return bool(
        client_spec.metadata.get("official_integration")
        or client_spec.metadata.get("trust_tool_annotations") is True
    )


def _recovery_contract(
    annotations: dict[str, Any],
    *,
    trusted: bool,
) -> tuple[bool, bool, bool, RuntimeEffectRecoveryCapability]:
    if trusted and annotations.get("read_only_hint") is True:
        return (
            True,
            True,
            False,
            RuntimeEffectRecoveryCapability(
                external_side_effect=False,
                idempotency="read_only",
                probe="not_required",
                compensation="not_required",
                on_ambiguity="safe_to_retry",
                notes="Trusted MCP annotation declares a read-only tool.",
            ),
        )

    destructive = True
    idempotency = "not_idempotent"
    on_ambiguity = "manual_disambiguation"
    notes = (
        "MCP tool annotations are absent or untrusted; external effects "
        "fail closed and ambiguous calls require human disambiguation."
    )
    if trusted:
        destructive = annotations.get("destructive_hint") is not False
        if annotations.get("idempotent_hint") is True:
            idempotency = "natural_key"
            on_ambiguity = "safe_to_retry"
        notes = "Recovery semantics projected from trusted MCP annotations."
    return (
        False,
        False,
        destructive,
        RuntimeEffectRecoveryCapability(
            external_side_effect=True,
            idempotency=idempotency,
            probe="none",
            compensation="none",
            on_ambiguity=on_ambiguity,
            notes=notes,
        ),
    )


def project_mcp_tool_spec(
    raw_tool: Any,
    client_spec: McpClientSpec,
) -> ToolSpec:
    """Project one MCP SDK v1/v2 tool into the stable Runtime contract."""

    name = str(_field(raw_tool, "name") or "").strip()
    if not name:
        raise ValueError("MCP tool has no name")
    input_schema = _field(raw_tool, "input_schema", "inputSchema")
    output_schema = _field(raw_tool, "output_schema", "outputSchema")
    annotations = _annotation_payload(raw_tool)
    trusted = _trusted_annotations(client_spec)
    read_only, concurrency_safe, destructive, recovery = _recovery_contract(
        annotations,
        trusted=trusted,
    )
    chrome_effect = None
    if client_spec.metadata.get("official_integration") == "chrome-devtools":
        from ..app.mcp.chrome_devtools import (
            CHROME_DEVTOOLS_PATH_FIELDS,
            chrome_devtools_tool_effect,
            is_official_chrome_devtools_metadata,
        )

        if is_official_chrome_devtools_metadata(client_spec.metadata):
            chrome_effect = chrome_devtools_tool_effect(name)
            read_only = chrome_effect.read_only
            concurrency_safe = chrome_effect.read_only
            destructive = chrome_effect.destructive
            recovery = RuntimeEffectRecoveryCapability(
                external_side_effect=chrome_effect.external_side_effect,
                idempotency=chrome_effect.idempotency,
                probe=chrome_effect.probe,
                compensation=chrome_effect.compensation,
                on_ambiguity=chrome_effect.on_ambiguity,
                notes=(
                    "Host-reviewed Chrome DevTools MCP effect contract; "
                    "upstream annotations do not grant authority."
                ),
            )

    metadata: dict[str, Any] = {
        "mcp_client": client_spec.name,
        "annotation_trust": "trusted" if trusted else "untrusted_hint_only",
    }
    if annotations:
        metadata["annotations"] = annotations
    if isinstance(output_schema, dict):
        metadata["output_schema"] = _json_value(output_schema)
    if chrome_effect is not None:
        metadata["chrome_devtools_effect"] = {
            "category": chrome_effect.category,
            "approval": chrome_effect.approval,
            "populated_path_fields": list(chrome_effect.populated_path_fields),
            "path_fields": dict(CHROME_DEVTOOLS_PATH_FIELDS.get(name, {})),
        }
        metadata["progressive_group"] = "browser"
        if chrome_effect.category == "unknown":
            metadata["unavailable_reason"] = "chrome_devtools_tool_unreviewed"
    execution = _field(raw_tool, "execution")
    task_support = _field(execution, "task_support", "taskSupport")
    if task_support is not None:
        metadata["task_support"] = _json_value(task_support)
        if str(task_support) == "required":
            metadata["unavailable_reason"] = "mcp_tasks_extension_unsupported"
    for key in (
        "official_integration",
        "integration_policy_version",
        "capability_matrix_id",
        "capability_matrix_mode",
        "capability_matrix_reason_code",
    ):
        if key in client_spec.metadata:
            metadata[key] = client_spec.metadata[key]

    task_supported = str(task_support or "forbidden") != "required"
    chrome_reviewed = chrome_effect is None or chrome_effect.category != "unknown"
    return ToolSpec(
        name=name,
        description=str(_field(raw_tool, "description") or ""),
        input_schema=(
            _json_value(input_schema)
            if isinstance(input_schema, dict)
            else {"type": "object", "properties": {}}
        ),
        origin="mcp",
        enabled=bool(client_spec.enabled and task_supported and chrome_reviewed),
        read_only=read_only,
        concurrency_safe=concurrency_safe,
        destructive=destructive,
        effect_recovery=recovery,
        metadata=metadata,
    )


def mcp_structured_result_metadata(raw_result: Any) -> dict[str, Any]:
    """Extract stable MCP result fields without retaining transport objects."""

    metadata: dict[str, Any] = {}
    structured = _field(raw_result, "structured_content", "structuredContent")
    if structured is not None:
        metadata["structured_content"] = _json_value(structured)
    mcp_meta = _field(raw_result, "meta", "_meta")
    if isinstance(mcp_meta, dict):
        metadata["mcp_meta"] = _json_value(mcp_meta)
    result_type = _field(raw_result, "result_type", "resultType")
    if result_type is not None:
        metadata["mcp_result_type"] = _json_value(result_type)
    task = _field(raw_result, "task")
    if task is not None:
        metadata["mcp_task"] = _json_value(task)
    return metadata


__all__ = ["mcp_structured_result_metadata", "project_mcp_tool_spec"]
