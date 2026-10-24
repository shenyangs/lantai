# -*- coding: utf-8 -*-
"""Model-visible surface contracts and durable source receipts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from lantai.security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
    write_text_atomic_no_follow,
)

from ..models import ModelRequest
from .contracts import ExecutionFence
from .integrity import canonical_json_bytes, canonical_sha256


InstructionTrust = Literal[
    "trusted_system",
    "direct_user",
    "model_output",
    "untrusted_tool_evidence",
    "untrusted_external_evidence",
]
SourceKind = Literal[
    "tool_result",
    "runtime_event",
    "memory_fact",
    "wiki_evidence",
    "artifact",
    "external_evidence",
]
CompactionMode = Literal["truncated", "omitted", "unchanged"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _non_empty(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    return text


class SourceReference(BaseModel):
    """Content-addressed pointer to raw evidence outside the model window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source_id: str
    kind: SourceKind
    content_sha256: str
    byte_length: int = Field(ge=0)
    scope: str
    retention: Literal["turn", "run", "workspace", "permanent"] = "run"
    retrievable: bool = False
    storage_ref: str | None = None
    instruction_trust: InstructionTrust = "untrusted_tool_evidence"

    @field_validator("source_id", "content_sha256", "scope")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("storage_ref")
    @classmethod
    def normalize_storage_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_empty(value, "storage_ref")

    @model_validator(mode="after")
    def validate_retrieval(self) -> "SourceReference":
        if self.retrievable != bool(self.storage_ref):
            raise ValueError("retrievable and storage_ref must agree")
        if len(self.content_sha256) != 64:
            raise ValueError("content_sha256 must be a SHA-256 hex digest")
        return self


class SurfaceMessageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    role: str
    content_sha256: str
    byte_length: int = Field(ge=0)
    instruction_trust: InstructionTrust
    instruction_origin: str | None = None
    source_ids: tuple[str, ...] = ()
    client_turn_id: str | None = None


class SurfaceToolEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    name: str
    enabled: bool
    spec_sha256: str


class ContextBudgetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effective_window: int = Field(ge=0)
    usable_input_tokens: int = Field(ge=0)
    output_reserve: int = Field(ge=0)
    safety_margin: int = Field(ge=0)
    before_upper_bound: int = Field(ge=0)
    after_upper_bound: int = Field(ge=0)
    profile_status: str
    used_unknown_fallback: bool = False


class SurfaceCompactionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_ref: str
    tool_call_id: str
    mode: CompactionMode
    original_sha256: str
    projected_sha256: str
    original_bytes: int = Field(ge=0)
    projected_bytes: int = Field(ge=0)
    source: SourceReference


class ProgressWitness(BaseModel):
    """Deterministic proof that compaction/recovery advanced the run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    witness_id: str
    request_sha256: str
    before_upper_bound: int = Field(ge=0)
    after_upper_bound: int = Field(ge=0)
    message_count: int = Field(ge=0)
    tool_call_ids: tuple[str, ...] = ()
    tool_result_ids: tuple[str, ...] = ()
    unpaired_tool_call_ids: tuple[str, ...] = ()
    unpaired_tool_result_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    previous_manifest_sha256: str | None = None

    def demonstrates_progress_from(self, previous: "ProgressWitness") -> bool:
        """Require a measurable change before an automatic retry."""

        return bool(
            self.after_upper_bound < previous.after_upper_bound
            or set(self.tool_result_ids) > set(previous.tool_result_ids)
            or set(self.source_ids) > set(previous.source_ids)
            or self.request_sha256 != previous.request_sha256
        )


class ModelRequestManifest(BaseModel):
    """Verifiable description of exactly what one model request could see."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    manifest_id: str
    run_id: str
    attempt_id: str
    turn_id: str
    model_request_id: str
    created_at: datetime
    provider_id: str
    model_id: str
    fence: ExecutionFence
    request_sha256: str
    message_entries: tuple[SurfaceMessageEntry, ...]
    tool_entries: tuple[SurfaceToolEntry, ...]
    tool_order_sha256: str
    budget: ContextBudgetSnapshot
    compactions: tuple[SurfaceCompactionRecord, ...] = ()
    admitted_input_ids: tuple[str, ...] = ()
    progress_witness: ProgressWitness
    integrity_sha256: str

    @field_validator(
        "manifest_id",
        "run_id",
        "attempt_id",
        "turn_id",
        "model_request_id",
        "provider_id",
        "model_id",
        "request_sha256",
        "tool_order_sha256",
        "integrity_sha256",
    )
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)

    def verify_integrity(self) -> bool:
        payload = self.model_dump(
            mode="json",
            exclude={"integrity_sha256"},
        )
        return canonical_sha256(payload) == self.integrity_sha256


class ModelRequestManifestConflictError(RuntimeError):
    """Raised when an immutable manifest ID resolves to different content."""


class SourceReferenceStore:
    """Content-addressed spill store under the existing Runtime v1 root."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
        )

    def persist(
        self,
        value: Any,
        *,
        run_id: str,
        attempt_id: str,
        kind: SourceKind,
        scope: str,
        retention: Literal["turn", "run", "workspace", "permanent"] = "run",
        instruction_trust: InstructionTrust = "untrusted_tool_evidence",
    ) -> SourceReference:
        raw = canonical_json_bytes(value)
        digest = canonical_sha256(value)
        run_key = safe_storage_key("runtime-run", run_id, suffix="")
        attempt_key = safe_storage_key(
            "runtime-attempt", attempt_id, suffix=""
        )
        source_key = safe_storage_key("runtime-source", digest, suffix=".json")
        path = contained_storage_path(
            self.root,
            "runs",
            run_key,
            "attempts",
            attempt_key,
            "sources",
            source_key,
        )
        envelope = {
            "schema_version": 1,
            "content_sha256": digest,
            "encoding": "canonical-json",
            "payload": json.loads(raw.decode("utf-8")),
        }
        serialized = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if path.is_file():
            existing = json.loads(read_text_no_follow(path))
            if existing != envelope:
                raise ModelRequestManifestConflictError(
                    "content-addressed source receipt changed",
                )
        else:
            write_text_atomic_no_follow(path, serialized)
        storage_ref = str(path.relative_to(self.root))
        return SourceReference(
            source_id=f"source:{digest}",
            kind=kind,
            content_sha256=digest,
            byte_length=len(raw),
            scope=scope,
            retention=retention,
            retrievable=True,
            storage_ref=storage_ref,
            instruction_trust=instruction_trust,
        )

    def retrieve(
        self,
        source: SourceReference,
        *,
        allowed_scopes: Sequence[str],
    ) -> Any:
        if not source.retrievable or source.storage_ref is None:
            raise PermissionError("source is not retrievable")
        if source.scope not in set(allowed_scopes):
            raise PermissionError("source scope is not authorized")
        path = contained_storage_path(
            self.root, *Path(source.storage_ref).parts
        )
        envelope = json.loads(read_text_no_follow(path))
        payload = envelope.get("payload")
        if canonical_sha256(payload) != source.content_sha256:
            raise ModelRequestManifestConflictError(
                "source receipt checksum mismatch",
            )
        return payload


class ModelRequestManifestStore:
    """Immutable manifest store flushed before provider dispatch."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
        )

    def _path(self, manifest: ModelRequestManifest) -> Path:
        run_key = safe_storage_key("runtime-run", manifest.run_id, suffix="")
        attempt_key = safe_storage_key(
            "runtime-attempt",
            manifest.attempt_id,
            suffix="",
        )
        manifest_key = safe_storage_key(
            "model-request-manifest",
            manifest.manifest_id,
            suffix=".json",
        )
        return contained_storage_path(
            self.root,
            "runs",
            run_key,
            "attempts",
            attempt_key,
            "model-requests",
            manifest_key,
        )

    def save(self, manifest: ModelRequestManifest) -> Path:
        if not manifest.verify_integrity():
            raise ModelRequestManifestConflictError(
                "model request manifest integrity is invalid",
            )
        path = self._path(manifest)
        serialized = manifest.model_dump_json(indent=2)
        if path.is_file():
            existing = ModelRequestManifest.model_validate_json(
                read_text_no_follow(path),
            )
            if existing != manifest:
                raise ModelRequestManifestConflictError(
                    "immutable model request manifest changed",
                )
            return path
        write_text_atomic_no_follow(path, serialized)
        return path

    def load_from_path(self, path: str | Path) -> ModelRequestManifest:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = contained_storage_path(self.root, *candidate.parts)
        manifest = ModelRequestManifest.model_validate_json(
            read_text_no_follow(candidate),
        )
        if not manifest.verify_integrity():
            raise ModelRequestManifestConflictError(
                "stored model request manifest integrity is invalid",
            )
        return manifest

    def list_for_attempt(
        self,
        *,
        run_id: str,
        attempt_id: str,
    ) -> list[ModelRequestManifest]:
        run_key = safe_storage_key("runtime-run", run_id, suffix="")
        attempt_key = safe_storage_key(
            "runtime-attempt",
            attempt_id,
            suffix="",
        )
        directory = contained_storage_path(
            self.root,
            "runs",
            run_key,
            "attempts",
            attempt_key,
            "model-requests",
        )
        if not directory.is_dir():
            return []
        manifests: list[ModelRequestManifest] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.suffix != ".json":
                continue
            manifests.append(self.load_from_path(path))
        return manifests


def _instruction_trust(
    role: str, metadata: dict[str, Any]
) -> InstructionTrust:
    declared = str(metadata.get("instruction_trust") or "").strip()
    if declared in {
        "trusted_system",
        "direct_user",
        "model_output",
        "untrusted_tool_evidence",
        "untrusted_external_evidence",
    }:
        return declared  # type: ignore[return-value]
    return {
        "system": "trusted_system",
        "user": "direct_user",
        "assistant": "model_output",
        "tool": "untrusted_tool_evidence",
    }.get(
        role, "untrusted_external_evidence"
    )  # type: ignore[return-value]


def _instruction_origin(metadata: dict[str, Any]) -> str | None:
    """Project only bounded machine provenance, never instruction content."""

    origin = str(metadata.get("instruction_origin") or "").strip()
    if not origin or len(origin) > 96:
        return None
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789._:-"
    if any(char not in allowed for char in origin):
        return None
    return origin


def build_model_request_manifest(
    request: ModelRequest,
    *,
    provider_id: str,
    model_id: str,
    budget: ContextBudgetSnapshot,
    compactions: Sequence[SurfaceCompactionRecord] = (),
) -> ModelRequestManifest:
    """Build a deterministic manifest for the final neutral request."""

    payload = {
        "messages": [
            message.model_dump(mode="json") for message in request.messages
        ],
        "tools": [tool.model_dump(mode="json") for tool in request.tools],
        "tool_choice": request.tool_choice,
        "generation": request.generation,
    }
    request_sha256 = canonical_sha256(payload)
    metadata = dict(request.metadata or {})
    turn_id = str(metadata.get("turn_id") or "standalone").strip()
    run_id = str(metadata.get("run_id") or turn_id).strip()
    attempt_id = str(metadata.get("attempt_id") or run_id).strip()
    request_base_id = str(
        metadata.get("model_request_id") or f"{turn_id}:surface"
    ).strip()
    model_request_id = f"{request_base_id}:{request_sha256[:12]}"
    manifest_id = f"manifest:{model_request_id}"
    created_raw = metadata.get("model_request_created_at")
    if isinstance(created_raw, datetime):
        created_at = created_raw
    elif created_raw:
        created_at = datetime.fromisoformat(
            str(created_raw).replace("Z", "+00:00"),
        )
    else:
        created_at = datetime.fromtimestamp(0, timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    fence = ExecutionFence(
        attempt_id=attempt_id,
        generation=max(0, int(metadata.get("runtime_generation") or 0)),
        expected_revision=(
            max(0, int(metadata["runtime_revision"]))
            if metadata.get("runtime_revision") is not None
            else None
        ),
        lease_owner=str(metadata.get("runtime_lease_owner") or "").strip()
        or None,
    )
    source_ids_by_block: dict[str, tuple[str, ...]] = {
        row.block_ref: (row.source.source_id,) for row in compactions
    }
    message_entries: list[SurfaceMessageEntry] = []
    admitted_input_ids: list[str] = []
    tool_call_ids: list[str] = []
    tool_result_ids: list[str] = []
    for message_index, message in enumerate(request.messages):
        content_payload = [
            block.model_dump(mode="json") for block in message.content
        ]
        source_ids = tuple(
            source_id
            for block_index, _block in enumerate(message.content)
            for source_id in source_ids_by_block.get(
                f"message:{message_index}:block:{block_index}",
                (),
            )
        )
        message_client_turn_id = str(
            message.metadata.get("client_turn_id") or "",
        ).strip()
        if (
            message_client_turn_id
            and message_client_turn_id not in admitted_input_ids
        ):
            admitted_input_ids.append(message_client_turn_id)
        message_entries.append(
            SurfaceMessageEntry(
                index=message_index,
                role=message.role,
                content_sha256=canonical_sha256(content_payload),
                byte_length=len(canonical_json_bytes(content_payload)),
                instruction_trust=_instruction_trust(
                    message.role,
                    dict(message.metadata or {}),
                ),
                instruction_origin=_instruction_origin(
                    dict(message.metadata or {}),
                ),
                source_ids=source_ids,
                client_turn_id=message_client_turn_id or None,
            ),
        )
        for block in message.content:
            if block.type == "tool_call":
                call_id = str(block.data.get("id") or "").strip()
                if call_id and call_id not in tool_call_ids:
                    tool_call_ids.append(call_id)
            if block.type == "tool_result":
                result_id = str(
                    block.data.get("id") or message.tool_call_id or ""
                ).strip()
                if result_id and result_id not in tool_result_ids:
                    tool_result_ids.append(result_id)
    tool_entries = tuple(
        SurfaceToolEntry(
            index=index,
            name=tool.name,
            enabled=tool.enabled,
            spec_sha256=canonical_sha256(tool.model_dump(mode="json")),
        )
        for index, tool in enumerate(request.tools)
    )
    source_ids = tuple(
        dict.fromkeys(row.source.source_id for row in compactions),
    )
    request_client_turn_id = str(
        metadata.get("client_turn_id") or "",
    ).strip()
    if (
        request_client_turn_id
        and request_client_turn_id not in admitted_input_ids
    ):
        admitted_input_ids.append(request_client_turn_id)
    witness_payload = {
        "request_sha256": request_sha256,
        "before": budget.before_upper_bound,
        "after": budget.after_upper_bound,
        "tool_call_ids": tool_call_ids,
        "tool_result_ids": tool_result_ids,
        "source_ids": source_ids,
    }
    witness = ProgressWitness(
        witness_id=f"witness:{canonical_sha256(witness_payload)[:24]}",
        request_sha256=request_sha256,
        before_upper_bound=budget.before_upper_bound,
        after_upper_bound=budget.after_upper_bound,
        message_count=len(message_entries),
        tool_call_ids=tuple(tool_call_ids),
        tool_result_ids=tuple(tool_result_ids),
        unpaired_tool_call_ids=tuple(
            item for item in tool_call_ids if item not in tool_result_ids
        ),
        unpaired_tool_result_ids=tuple(
            item for item in tool_result_ids if item not in tool_call_ids
        ),
        source_ids=source_ids,
        previous_manifest_sha256=str(
            metadata.get("previous_manifest_sha256") or "",
        ).strip()
        or None,
    )
    tool_order_sha256 = canonical_sha256(
        [entry.name for entry in tool_entries],
    )
    manifest_payload = {
        "schema_version": 1,
        "manifest_id": manifest_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "turn_id": turn_id,
        "model_request_id": model_request_id,
        "created_at": created_at,
        "provider_id": provider_id,
        "model_id": model_id,
        "fence": fence,
        "request_sha256": request_sha256,
        "message_entries": tuple(message_entries),
        "tool_entries": tool_entries,
        "tool_order_sha256": tool_order_sha256,
        "budget": budget,
        "compactions": tuple(compactions),
        "admitted_input_ids": tuple(admitted_input_ids),
        "progress_witness": witness,
    }
    return ModelRequestManifest(
        **manifest_payload,
        integrity_sha256=canonical_sha256(manifest_payload),
    )


__all__ = [
    "CompactionMode",
    "ContextBudgetSnapshot",
    "InstructionTrust",
    "ModelRequestManifest",
    "ModelRequestManifestConflictError",
    "ModelRequestManifestStore",
    "ProgressWitness",
    "SourceKind",
    "SourceReference",
    "SourceReferenceStore",
    "SurfaceCompactionRecord",
    "SurfaceMessageEntry",
    "SurfaceToolEntry",
    "build_model_request_manifest",
]
