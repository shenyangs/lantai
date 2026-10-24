"""Model-aware last-line protection for provider request payloads."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..agents.context.context_budget import ModelAwareTokenCounter
from ..exceptions import ModelContextLengthExceededException
from ..model_context_profile import ModelContextProfile
from ..providers.model_capability_registry import resolve_model_capability
from ..providers.model_endpoint import ProviderModelEndpoint
from .models import ModelContentBlock, ModelRequest
from .coherence.integrity import canonical_json_bytes, canonical_sha256
from .coherence.surface import (
    ContextBudgetSnapshot,
    ModelRequestManifestStore,
    SourceReference,
    SourceReferenceStore,
    SurfaceCompactionRecord,
    build_model_request_manifest,
)


logger = logging.getLogger(__name__)

_DEFAULT_TOOL_EVIDENCE_BUDGET_TOKENS = 32_768
_MIN_TOOL_EVIDENCE_BUDGET_TOKENS = 8_192
_MAX_TOOL_EVIDENCE_BUDGET_TOKENS = 65_536
_MAX_SINGLE_TOOL_RESULT_TOKENS = 12_288


def _request_payload(request: ModelRequest) -> dict[str, Any]:
    return {
        "messages": [message.model_dump(mode="json") for message in request.messages],
        "tools": [tool.model_dump(mode="json") for tool in request.tools],
        "tool_choice": request.tool_choice,
        "generation": request.generation,
    }


def _context_profile(
    request: ModelRequest,
    endpoint: ProviderModelEndpoint,
) -> ModelContextProfile:
    model_id = request.model or endpoint.model_id
    provider_id = request.provider_id or endpoint.provider_id
    provider_metadata: dict[str, Any] = {}
    registered = resolve_model_capability(provider_id, model_id)
    if registered is not None:
        provider_metadata.update(registered.as_metadata())
    endpoint_metadata = dict(endpoint.metadata or {})
    route_metadata = endpoint_metadata.get("route_metadata")
    if not isinstance(route_metadata, dict):
        route_metadata = {}
    route_metadata = {
        **endpoint_metadata,
        **route_metadata,
    }
    user_window = endpoint_metadata.get("user_context_window")
    try:
        user_window = int(user_window)
    except (TypeError, ValueError):
        user_window = None
    return ModelContextProfile.from_mapping(
        provider_id=provider_id,
        model_id=model_id,
        user_window=user_window,
        provider_metadata=provider_metadata,
        route_metadata=route_metadata,
    )


def _tool_result_blocks(request: ModelRequest) -> list[ModelContentBlock]:
    return [
        block
        for message in request.messages
        for block in message.content
        if block.type == "tool_result" and "output" in block.data
    ]


def _tool_result_entries(
    request: ModelRequest,
) -> list[tuple[int, int, ModelContentBlock]]:
    return [
        (message_index, block_index, block)
        for message_index, message in enumerate(request.messages)
        for block_index, block in enumerate(message.content)
        if block.type == "tool_result" and "output" in block.data
    ]


def _serialized(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = (
        f"\n...[truncated by Lantai context guard; original_bytes={len(encoded)}]...\n"
    ).encode("utf-8")
    room = max(0, max_bytes - len(marker))
    head_size = int(room * 0.75)
    tail_size = room - head_size
    head = encoded[:head_size].decode("utf-8", errors="ignore")
    tail = encoded[-tail_size:].decode("utf-8", errors="ignore") if tail_size else ""
    return head + marker.decode("utf-8") + tail


def _compact_tool_output(output: Any, *, max_bytes: int) -> list[dict[str, Any]]:
    media: list[dict[str, Any]] = []
    if isinstance(output, list):
        media = [
            dict(item)
            for item in output
            if isinstance(item, dict)
            and item.get("type") in {"image", "audio", "video", "file"}
        ]
    text = _truncate_utf8(_serialized(output), max_bytes)
    return [{"type": "text", "text": text}, *media]


def _fallback_source_reference(
    value: Any,
    *,
    scope: str,
) -> SourceReference:
    raw = canonical_json_bytes(value)
    digest = canonical_sha256(value)
    return SourceReference(
        source_id=f"source:{digest}",
        kind="tool_result",
        content_sha256=digest,
        byte_length=len(raw),
        scope=scope,
        retrievable=False,
        storage_ref=None,
        instruction_trust="untrusted_tool_evidence",
    )


def prepare_model_request_for_context(
    request: ModelRequest,
    endpoint: ProviderModelEndpoint,
) -> ModelRequest:
    """Fit tool evidence to the exact route and reject unsafe payloads locally.

    Conversation text is never silently deleted. Oversized tool results are
    compacted first because their complete raw form remains in runtime events
    and audit metadata; if the remaining required conversation still cannot
    fit, the call fails locally with inspectable budget details.
    """

    profile = _context_profile(request, endpoint)
    policy = profile.policy("max")
    requested_output = 0
    for key in ("max_completion_tokens", "max_tokens"):
        try:
            requested_output = max(
                requested_output,
                int(request.generation.get(key) or 0),
            )
        except (TypeError, ValueError):
            continue
    effective_requested_output = profile.output_reserve(requested_output)
    output_reserve = min(
        policy.hard_window // 2,
        max(policy.output_reserve, effective_requested_output),
    )
    usable_input_tokens = max(
        0,
        policy.hard_window - output_reserve - policy.safety_margin,
    )
    counter = ModelAwareTokenCounter()
    before = counter.count(_request_payload(request)).upper_bound
    prepared = request.model_copy(deep=True)
    if effective_requested_output < requested_output:
        for key in ("max_completion_tokens", "max_tokens"):
            try:
                value = int(prepared.generation.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value > effective_requested_output:
                prepared.generation[key] = effective_requested_output
    entries = _tool_result_entries(prepared)
    blocks = [entry[2] for entry in entries]
    compacted = 0
    metadata = dict(prepared.metadata or {})
    harness_mode = str(
        metadata.get("harness_mode") or "shadow"
    ).strip().lower()
    workspace_dir = str(metadata.get("state_workspace_dir") or "").strip()
    run_id = str(
        metadata.get("run_id") or metadata.get("turn_id") or ""
    ).strip()
    attempt_id = str(metadata.get("attempt_id") or run_id).strip()
    source_scope = str(
        metadata.get("context_scope")
        or (f"run:{run_id}" if run_id else "standalone"),
    ).strip()
    source_store = (
        SourceReferenceStore(workspace_dir)
        if harness_mode != "off" and workspace_dir and run_id and attempt_id
        else None
    )
    compaction_records: dict[str, SurfaceCompactionRecord] = {}

    def record_compaction(
        *,
        message_index: int,
        block_index: int,
        block: ModelContentBlock,
        original: Any,
        projected: Any,
        mode: str,
    ) -> None:
        block_ref = f"message:{message_index}:block:{block_index}"
        existing = compaction_records.get(block_ref)
        source = existing.source if existing is not None else None
        if source is None and source_store is not None:
            try:
                source = source_store.persist(
                    original,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    kind="tool_result",
                    scope=source_scope,
                    retention="run",
                    instruction_trust="untrusted_tool_evidence",
                )
            except Exception:
                if harness_mode == "enforced":
                    raise
                logger.warning(
                    "Coherence Harness could not persist a raw tool source",
                    exc_info=True,
                )
        if source is None:
            source = _fallback_source_reference(original, scope=source_scope)
        original_sha256 = (
            existing.original_sha256
            if existing is not None
            else canonical_sha256(original)
        )
        original_bytes = (
            existing.original_bytes
            if existing is not None
            else len(canonical_json_bytes(original))
        )
        compaction_records[block_ref] = SurfaceCompactionRecord(
            block_ref=block_ref,
            tool_call_id=str(block.data.get("id") or "unknown"),
            mode=mode,
            original_sha256=original_sha256,
            projected_sha256=canonical_sha256(projected),
            original_bytes=original_bytes,
            projected_bytes=len(canonical_json_bytes(projected)),
            source=source,
        )
    try:
        requested_tool_budget = int(
            metadata.get("runtime_tool_evidence_budget_tokens")
            or _DEFAULT_TOOL_EVIDENCE_BUDGET_TOKENS
        )
    except (TypeError, ValueError):
        requested_tool_budget = _DEFAULT_TOOL_EVIDENCE_BUDGET_TOKENS
    tool_evidence_budget = min(
        _MAX_TOOL_EVIDENCE_BUDGET_TOKENS,
        max(_MIN_TOOL_EVIDENCE_BUDGET_TOKENS, requested_tool_budget),
        usable_input_tokens,
    )
    per_result_tokens = min(
        _MAX_SINGLE_TOOL_RESULT_TOKENS,
        max(4_096, tool_evidence_budget // max(2, min(8, len(blocks) or 1))),
    )
    per_result_bytes = max(16_384, int(per_result_tokens * 3.0))

    for message_index, block_index, block in entries:
        output = block.data.get("output")
        if counter.count(output).upper_bound <= per_result_tokens:
            continue
        projected = _compact_tool_output(
            output,
            max_bytes=per_result_bytes,
        )
        block.data["output"] = projected
        if harness_mode != "off":
            record_compaction(
                message_index=message_index,
                block_index=block_index,
                block=block,
                original=output,
                projected=projected,
                mode="truncated",
            )
        compacted += 1

    after = counter.count(_request_payload(prepared)).upper_bound
    without_tool_outputs = prepared.model_copy(deep=True)
    for block in _tool_result_blocks(without_tool_outputs):
        block.data["output"] = [{"type": "text", "text": "[tool evidence]"}]
    non_tool_upper_bound = counter.count(
        _request_payload(without_tool_outputs),
    ).upper_bound
    operating_target_tokens = min(
        usable_input_tokens,
        non_tool_upper_bound + tool_evidence_budget,
    )
    # Older tool outputs are recoverable from the run ledger. Prefer omitting
    # them once this run's operating evidence budget is full instead of
    # repeatedly resending an ever-growing transcript until the hard provider
    # window is reached. Persona, direct user input, and ordinary conversation
    # text are not removed by this projection.
    for message_index, block_index, block in entries[:-1]:
        if after <= operating_target_tokens:
            break
        output = block.data.get("output")
        existing_record = compaction_records.get(
            f"message:{message_index}:block:{block_index}",
        )
        source_id = (
            existing_record.source.source_id
            if existing_record is not None
            else f"source:{canonical_sha256(output)}"
        )
        projected = [
            {
                "type": "text",
                "text": (
                    "[Earlier tool result omitted by Lantai context guard.]"
                    if harness_mode == "off"
                    else (
                        "[Earlier tool result omitted by Lantai context "
                        "guard; "
                        f"source_ref={source_id}.]"
                    )
                ),
            },
        ]
        block.data["output"] = projected
        if harness_mode != "off":
            record_compaction(
                message_index=message_index,
                block_index=block_index,
                block=block,
                original=output,
                projected=projected,
                mode="omitted",
            )
        compacted += 1
        after = counter.count(_request_payload(prepared)).upper_bound

    manifest = {
        "provider_id": profile.provider_id,
        "model_id": profile.model_id,
        "effective_window": policy.hard_window,
        "usable_input_tokens": usable_input_tokens,
        "output_reserve": output_reserve,
        "requested_output_tokens": requested_output,
        "effective_output_tokens": effective_requested_output,
        "safety_margin": policy.safety_margin,
        "profile_status": profile.status,
        "used_unknown_fallback": policy.used_unknown_fallback,
        "before_upper_bound": before,
        "after_upper_bound": after,
        "compacted_tool_results": compacted,
        "tool_evidence_budget_tokens": tool_evidence_budget,
        "non_tool_upper_bound": non_tool_upper_bound,
        "operating_target_tokens": operating_target_tokens,
    }
    if after > usable_input_tokens:
        raise ModelContextLengthExceededException(
            profile.model_id or endpoint.model_id,
            details=manifest,
        )
    if harness_mode == "off":
        prepared.metadata = {**prepared.metadata, "context_guard": manifest}
        return prepared
    surface_manifest = build_model_request_manifest(
        prepared,
        provider_id=profile.provider_id or endpoint.provider_id,
        model_id=profile.model_id or endpoint.model_id,
        budget=ContextBudgetSnapshot(
            effective_window=policy.hard_window,
            usable_input_tokens=usable_input_tokens,
            output_reserve=output_reserve,
            safety_margin=policy.safety_margin,
            before_upper_bound=before,
            after_upper_bound=after,
            profile_status=profile.status,
            used_unknown_fallback=policy.used_unknown_fallback,
        ),
        compactions=tuple(compaction_records.values()),
    )
    persistence_status = "memory_only"
    manifest_ref: str | None = None
    if workspace_dir and run_id and attempt_id:
        try:
            surface_store = ModelRequestManifestStore(workspace_dir)
            manifest_path = surface_store.save(surface_manifest)
            manifest_ref = str(manifest_path.relative_to(surface_store.root))
            persistence_status = "durable"
        except Exception:
            if harness_mode == "enforced":
                raise
            persistence_status = "failed_open_shadow"
            logger.warning(
                "Coherence Harness could not persist model surface manifest",
                exc_info=True,
            )
    prepared.metadata = {
        **prepared.metadata,
        "context_guard": manifest,
        "model_context_surface": surface_manifest.model_dump(mode="json"),
        "model_request_manifest_hash": surface_manifest.integrity_sha256,
        "model_request_manifest_ref": manifest_ref,
        "model_request_manifest_persistence": persistence_status,
    }
    return prepared


__all__ = ["prepare_model_request_for_context"]
