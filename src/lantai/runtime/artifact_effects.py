# -*- coding: utf-8 -*-
"""Durable, content-free artifact evidence derived from tool semantics."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from lantai.utils.file_lock import locked_file_descriptor
from lantai.security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    safe_storage_key,
)

from .effects import RuntimeEffectJournal
from .file_delivery import resolve_workspace_deliverable_path
from .runs import RuntimeEffectRecord
from .tools import ToolCall, ToolOutputSemantics, ToolResult, ToolSpec


ArtifactRole = Literal["candidate", "primary", "delivery"]
ArtifactEffectStatus = Literal[
    "intent",
    "committed",
    "delivered",
    "failed",
    "missing",
    "ambiguous",
]


class RuntimeArtifactEffectRecord(BaseModel):
    """One versioned artifact fact linked to a Runtime EffectRecord."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    output_semantics_version: Literal[1] = 1
    run_id: str
    attempt_id: str
    source_effect_id: str
    tool_call_id: str
    tool_name: str
    artifact_effect_id: str
    artifact_id: str
    artifact_role: ArtifactRole
    status: ArtifactEffectStatus = "intent"
    revision: int = 0
    relative_path: str
    research_export_id: str | None = None
    delivery_idempotency_key: str
    created_at: datetime
    settled_at: datetime | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    media_type: str | None = None
    content_media_types: tuple[
        Literal["file", "image", "audio", "video"],
        ...,
    ] = ()
    source_adapter: Literal["declared", "legacy_tool_adapter"] = "declared"


class RuntimeArtifactEffectEnvelope(BaseModel):
    """Checksum envelope kept separate from the legacy effect journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    writer_version: str = "lantai-runtime-artifact-effect-journal/1"
    checksum: str
    payload: dict[str, object]


class RuntimeArtifactEffectJournalError(ValueError):
    """Base error for artifact evidence persistence."""


class RuntimeArtifactEffectJournalConflictError(
    RuntimeArtifactEffectJournalError,
):
    """Raised when an artifact effect revision is stale or duplicated."""


class RuntimeArtifactEffectJournalCorruptionError(
    RuntimeArtifactEffectJournalError,
):
    """Raised when persisted artifact evidence cannot be trusted."""


def _stable_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _locked_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with locked_file_descriptor(fd):
            yield
    finally:
        os.close(fd)


class RuntimeArtifactEffectJournal:
    """Append-only artifact evidence sidecar ignored by pre-S6 readers."""

    def __init__(self, workspace_dir: Path | str) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
        )

    def ledger_path(self, run_id: str, attempt_id: str) -> Path:
        run_key = safe_storage_key("runtime-run", run_id, suffix="")
        attempt_key = safe_storage_key(
            "runtime-attempt",
            attempt_id,
            suffix="",
        )
        return contained_storage_path(
            self.root,
            "runs",
            run_key,
            "attempts",
            attempt_key,
            "artifact-effects.jsonl",
        )

    def _lock_path(self, run_id: str, attempt_id: str) -> Path:
        return self.ledger_path(run_id, attempt_id).with_name(
            ".artifact-effects.lock",
        )

    def append(
        self,
        record: RuntimeArtifactEffectRecord,
    ) -> RuntimeArtifactEffectRecord:
        path = self.ledger_path(record.run_id, record.attempt_id)
        with _locked_file(self._lock_path(record.run_id, record.attempt_id)):
            latest = self.latest(
                record.run_id,
                record.attempt_id,
                record.artifact_effect_id,
            )
            if latest is not None and record.revision <= latest.revision:
                raise RuntimeArtifactEffectJournalConflictError(
                    "artifact effect revision conflict: "
                    f"{record.revision} <= {latest.revision}",
                )
            payload = record.model_dump(mode="json")
            envelope = RuntimeArtifactEffectEnvelope(
                checksum=_stable_digest(payload),
                payload=payload,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise RuntimeArtifactEffectJournalError(
                    "Artifact effect journal path is a symlink",
                )
            fd = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    fd = -1
                    handle.write(
                        json.dumps(
                            envelope.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n",
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if fd >= 0:
                    os.close(fd)
        return record

    def list_records(
        self,
        run_id: str,
        attempt_id: str,
    ) -> list[RuntimeArtifactEffectRecord]:
        path = self.ledger_path(run_id, attempt_id)
        if not path.exists():
            return []
        records: list[RuntimeArtifactEffectRecord] = []
        revisions: dict[str, int] = {}
        for line_number, line in enumerate(
            read_text_no_follow(path).splitlines(),
            1,
        ):
            if not line.strip():
                continue
            try:
                envelope = RuntimeArtifactEffectEnvelope.model_validate(
                    json.loads(line),
                )
                if envelope.checksum != _stable_digest(envelope.payload):
                    raise ValueError("checksum mismatch")
                record = RuntimeArtifactEffectRecord.model_validate(
                    envelope.payload,
                )
                if record.run_id != run_id or record.attempt_id != attempt_id:
                    raise ValueError("artifact effect identity mismatch")
                previous = revisions.get(record.artifact_effect_id)
                if previous is not None and record.revision <= previous:
                    raise ValueError(
                        "artifact effect revision is not increasing",
                    )
                revisions[record.artifact_effect_id] = record.revision
                records.append(record)
            except Exception as exc:
                raise RuntimeArtifactEffectJournalCorruptionError(
                    (
                        "artifact effect journal line "
                        f"{line_number}"
                        " is corrupt: "
                        f"{exc}"
                    ),
                ) from exc
        return records

    def latest(
        self,
        run_id: str,
        attempt_id: str,
        artifact_effect_id: str,
    ) -> RuntimeArtifactEffectRecord | None:
        matches = [
            record
            for record in self.list_records(run_id, attempt_id)
            if record.artifact_effect_id == artifact_effect_id
        ]
        return (
            max(matches, key=lambda item: item.revision) if matches else None
        )


@dataclass(frozen=True, slots=True)
class ArtifactDeliveryRecovery:
    """In-memory projection used to resume delivery without duplication."""

    pending_paths: tuple[str, ...]
    idempotency_keys: dict[str, str]
    completed: bool
    invalid_artifact_ids: tuple[str, ...] = ()
    ambiguous_artifact_ids: tuple[str, ...] = ()


def artifact_effect_intents(
    *,
    source_effect: RuntimeEffectRecord,
    tool_call: ToolCall,
    tool_spec: ToolSpec,
    workspace_dir: Path | str,
) -> tuple[RuntimeArtifactEffectRecord, ...]:
    """Project safe, deterministic artifact intents before tool execution."""

    semantics = tool_spec.output_semantics
    if semantics.artifact_role == "none":
        return ()
    root = Path(workspace_dir).expanduser().resolve(strict=False)
    records: list[RuntimeArtifactEffectRecord] = []
    seen: set[str] = set()
    for field_name in semantics.argument_path_fields:
        resolved = resolve_workspace_deliverable_path(
            str(root),
            tool_call.arguments.get(field_name),
            require_file=False,
        )
        if not resolved:
            continue
        path = Path(resolved)
        if path.exists() and not path.is_file():
            continue
        try:
            relative_path = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative_path in seen:
            continue
        seen.add(relative_path)
        identity = _stable_digest(
            {
                "run_id": source_effect.run_id,
                "attempt_id": source_effect.attempt_id,
                "relative_path": relative_path,
            },
        )
        artifact_id = f"artifact:sha256:{identity}"
        artifact_effect_id = "artifact-effect:sha256:" + _stable_digest(
            {
                "source_effect_id": source_effect.effect_id,
                "artifact_id": artifact_id,
                "artifact_role": semantics.artifact_role,
            },
        )
        records.append(
            RuntimeArtifactEffectRecord(
                run_id=source_effect.run_id,
                attempt_id=source_effect.attempt_id,
                source_effect_id=source_effect.effect_id,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                artifact_effect_id=artifact_effect_id,
                artifact_id=artifact_id,
                artifact_role=semantics.artifact_role,
                relative_path=relative_path,
                delivery_idempotency_key=f"artifact-delivery:{artifact_id}",
                created_at=source_effect.created_at,
                content_media_types=semantics.content_media_types,
                source_adapter=semantics.source_adapter,
            ),
        )
    return tuple(records)


def artifact_effect_intents_from_result(
    *,
    source_effect: RuntimeEffectRecord,
    tool_call: ToolCall,
    tool_spec: ToolSpec,
    result: ToolResult,
    workspace_dir: Path | str,
) -> tuple[RuntimeArtifactEffectRecord, ...]:
    """Project paths explicitly declared by a structured tool result."""

    semantics = tool_spec.output_semantics
    if semantics.artifact_role == "none" or not semantics.result_path_fields:
        return ()
    if tool_spec.name == "research_export":
        # Only this built-in may reference the application-owned export store.
        # The receipt is checked against the current agent's store; no root
        # supplied by a model, plugin or tool result is accepted.
        if tool_spec.origin != "builtin" or not result.ok:
            return ()
        export_id = result.metadata.get("research_export_id")
        managed = _managed_research_export(export_id)
        if managed is None:
            return ()
        root, path, receipt = managed
        if (result.metadata.get("research_artifact_path") != str(path)
                or result.metadata.get("artifact_hash") != receipt["artifact_hash"]):
            return ()
        managed_call = tool_call.model_copy(update={"arguments": {"research_artifact_path": str(path)}})
        managed_spec = tool_spec.model_copy(update={"output_semantics": semantics.model_copy(update={
            "argument_path_fields": ("research_artifact_path",), "result_path_fields": (),
        })})
        return tuple(record.model_copy(update={"research_export_id": export_id}) for record in
            artifact_effect_intents(source_effect=source_effect, tool_call=managed_call,
                tool_spec=managed_spec, workspace_dir=root))
    raw = result.metadata.get("raw")
    raw_mapping = raw if isinstance(raw, dict) else {}
    arguments: dict[str, object] = {}
    for field_name in semantics.result_path_fields:
        value: object | None = None
        if field_name in result.metadata:
            value = result.metadata[field_name]
        elif field_name in raw_mapping:
            value = raw_mapping[field_name]
        values = value if isinstance(value, (list, tuple)) else (value,)
        for index, path_value in enumerate(values):
            if path_value is None:
                continue
            key = field_name if len(values) == 1 else f"{field_name}:{index}"
            arguments[key] = path_value
    if not arguments:
        return ()
    result_call = tool_call.model_copy(update={"arguments": arguments})
    result_spec = tool_spec.model_copy(
        update={
            "output_semantics": semantics.model_copy(
                update={
                    "argument_path_fields": tuple(arguments),
                    "result_path_fields": (),
                },
            ),
        },
    )
    return artifact_effect_intents(
        source_effect=source_effect,
        tool_call=result_call,
        tool_spec=result_spec,
        workspace_dir=workspace_dir,
    )


def _managed_research_export(export_id: object) -> tuple[Path, Path, dict] | None:
    from ..config.context import get_current_state_root, get_current_workspace_context
    from ..research.exports import export_file
    from ..research.projects import ResearchService

    context = get_current_workspace_context()
    root = context.agent_home if context is not None else get_current_state_root()
    if not isinstance(export_id, str) or not export_id.startswith("export-") or root is None:
        return None
    root = Path(root).resolve(strict=False)
    # Do not create another agent's store merely to check a stale receipt.
    if not (root / ".lantai/research/index.sqlite3").is_file():
        return None
    try:
        service = ResearchService(root)
        receipt = service.store.get(export_id, kind="export")
        return root, export_file(service, export_id), receipt
    except (ValueError, FileNotFoundError, KeyError, OSError):
        return None


def resolve_artifact_effect_path(record: RuntimeArtifactEffectRecord, workspace_dir: Path | str) -> str | None:
    """Resolve workspace files or receipt-bound exports within the current agent."""
    if record.research_export_id is not None:
        if record.tool_name != "research_export":
            return None
        managed = _managed_research_export(record.research_export_id)
        if managed is None:
            return None
        root, path, _receipt = managed
        return str(path) if path.relative_to(root).as_posix() == record.relative_path else None
    return resolve_workspace_deliverable_path(str(workspace_dir), record.relative_path, require_file=True)


def register_artifact_candidate(
    journal: RuntimeArtifactEffectJournal,
    *,
    source_effect: RuntimeEffectRecord,
    tool_call: ToolCall,
    candidate_path: Path | str,
    result: ToolResult,
    workspace_dir: Path | str,
) -> tuple[RuntimeArtifactEffectRecord, ...]:
    """Persist a legacy generator output as a candidate, never a primary.

    Some established generators expose a file block but predate declared
    output semantics.  The adapter may recognize those narrow shapes, but the
    resulting file still needs a durable lifecycle record before any later
    verification can promote it.  Replays are idempotent by the source effect
    and artifact identities.
    """

    candidate_call = tool_call.model_copy(
        update={
            "arguments": {
                **tool_call.arguments,
                "__runtime_candidate_path": str(candidate_path),
            },
        },
    )
    candidate_spec = ToolSpec(
        name=tool_call.name,
        input_schema={"type": "object"},
        read_only=True,
        output_semantics=ToolOutputSemantics(
            artifact_role="candidate",
            argument_path_fields=("__runtime_candidate_path",),
            source_adapter="legacy_tool_adapter",
        ),
    )
    intents = artifact_effect_intents(
        source_effect=source_effect,
        tool_call=candidate_call,
        tool_spec=candidate_spec,
        workspace_dir=workspace_dir,
    )
    registered: list[RuntimeArtifactEffectRecord] = []
    for intent in intents:
        latest = journal.latest(
            intent.run_id,
            intent.attempt_id,
            intent.artifact_effect_id,
        )
        if latest is None:
            journal.append(intent)
            latest = intent
        if latest.status == "intent":
            latest = settle_artifact_effects(
                (latest,),
                result=result,
                workspace_dir=workspace_dir,
            )[0]
            journal.append(latest)
        registered.append(latest)
    return tuple(registered)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _delivery_media_kind(
    path: Path,
) -> Literal["file", "image", "audio", "video"]:
    media_type = (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    for prefix, kind in (
        ("image/", "image"),
        ("audio/", "audio"),
        ("video/", "video"),
    ):
        if media_type.startswith(prefix):
            return cast(Literal["file", "image", "audio", "video"], kind)
    return "file"


# pylint: disable=R0912
def recover_artifact_delivery(
    journal: RuntimeArtifactEffectJournal,
    *,
    effect_journal: RuntimeEffectJournal | None = None,
    run_id: str,
    attempt_id: str,
    workspace_dir: Path | str,
) -> ArtifactDeliveryRecovery:
    """Project committed primaries that still need one delivery effect."""

    latest: dict[str, RuntimeArtifactEffectRecord] = {}
    for record in journal.list_records(run_id, attempt_id):
        current = latest.get(record.artifact_effect_id)
        if current is None or record.revision > current.revision:
            latest[record.artifact_effect_id] = record
    latest_source_effect_status: dict[str, str] = {}
    if effect_journal is not None:
        latest_source_effects: dict[str, RuntimeEffectRecord] = {}
        for effect in effect_journal.list_effects(run_id, attempt_id):
            current = latest_source_effects.get(effect.effect_id)
            if current is None or effect.revision > current.revision:
                latest_source_effects[effect.effect_id] = effect
        latest_source_effect_status = {
            effect_id: effect.status
            for effect_id, effect in latest_source_effects.items()
        }
    # A successful tool may crash after its base EffectRecord commits but
    # before the artifact sidecar settles. Reconcile that narrow window from
    # the explicit intent and the real workspace file; never re-execute the
    # tool or infer a path from mtime/prose.
    for artifact_effect_id, record in tuple(latest.items()):
        if (
            record.status != "intent"
            or record.artifact_role == "delivery"
            or latest_source_effect_status.get(record.source_effect_id)
            not in {"committed", "probed_committed"}
        ):
            continue
        reconciled = settle_artifact_effects(
            (record,),
            result=ToolResult(
                call_id=record.tool_call_id,
                name=record.tool_name,
                ok=True,
            ),
            workspace_dir=workspace_dir,
        )[0]
        journal.append(reconciled)
        latest[artifact_effect_id] = reconciled
    delivered_versions = {
        (record.artifact_id, record.sha256)
        for record in latest.values()
        if record.artifact_role == "delivery" and record.status == "delivered"
    }
    ambiguous_artifact_ids = {
        record.artifact_id
        for record in latest.values()
        if record.artifact_role == "delivery"
        and record.status == "intent"
        and latest_source_effect_status.get(record.source_effect_id)
        in {"running", "committed", "probed_committed"}
    }
    root = Path(workspace_dir).expanduser().resolve(strict=False)
    pending: list[str] = []
    idempotency_keys: dict[str, str] = {}
    invalid: list[str] = []
    primary_count = 0
    # A committed revision supersedes earlier bytes at the same artifact path.
    # Failed revisions do not replace the last committed primary.
    primary_versions: dict[str, RuntimeArtifactEffectRecord] = {}
    for record in sorted(
        latest.values(),
        key=lambda item: (
            item.settled_at or item.created_at,
            item.created_at,
            item.artifact_effect_id,
        ),
    ):
        if record.artifact_role == "primary" and record.status in {
            "committed",
            "delivered",
        }:
            primary_versions[record.artifact_id] = record
    for record in primary_versions.values():
        primary_count += 1
        resolved = resolve_workspace_deliverable_path(
            str(root),
            record.relative_path,
            require_file=True,
        )
        if (
            not resolved
            or not record.sha256
            or _file_sha256(Path(resolved)) != record.sha256
        ):
            invalid.append(record.artifact_id)
            continue
        if (record.artifact_id, record.sha256) in delivered_versions:
            continue
        if record.artifact_id in ambiguous_artifact_ids:
            continue
        if resolved not in pending:
            pending.append(resolved)
            idempotency_keys[resolved] = record.delivery_idempotency_key
    return ArtifactDeliveryRecovery(
        pending_paths=tuple(pending),
        idempotency_keys=idempotency_keys,
        completed=(
            bool(primary_count)
            and not pending
            and not invalid
            and not ambiguous_artifact_ids
        ),
        invalid_artifact_ids=tuple(sorted(set(invalid))),
        ambiguous_artifact_ids=tuple(sorted(ambiguous_artifact_ids)),
    )


# pylint: enable=R0912


def project_committed_primary_delivery(
    metadata: dict[str, object],
    *,
    workspace_dir: Path | str,
    legacy_primary_path: object = None,
) -> ArtifactDeliveryRecovery:
    """Project one completed tool result into safe primary delivery paths."""

    rows = metadata.get("artifact_effects")
    artifact_rows = rows if isinstance(rows, list) else []
    primary_rows = [
        row
        for row in artifact_rows
        if isinstance(row, dict)
        and row.get("artifact_role") == "primary"
        and row.get("status") == "committed"
    ]
    if not primary_rows and legacy_primary_path:
        primary_rows = [{"relative_path": legacy_primary_path}]
    paths: list[str] = []
    keys: dict[str, str] = {}
    for row in primary_rows:
        resolved = resolve_workspace_deliverable_path(
            str(workspace_dir),
            row.get("relative_path"),
            require_file=True,
        )
        if not resolved or resolved in paths:
            continue
        paths.append(resolved)
        key = str(row.get("delivery_idempotency_key") or "").strip()
        if key:
            keys[resolved] = key
    return ArtifactDeliveryRecovery(
        pending_paths=tuple(paths),
        idempotency_keys=keys,
        completed=False,
    )


def settle_artifact_effects(
    intents: Iterable[RuntimeArtifactEffectRecord],
    *,
    result: object,
    workspace_dir: Path | str,
    settled_at: datetime | None = None,
) -> tuple[RuntimeArtifactEffectRecord, ...]:
    (
        "Settle artifact intents from the real file and normalize"
        "d tool result."
    )

    ok = bool(getattr(result, "ok", False))
    root = Path(workspace_dir).expanduser().resolve(strict=False)
    now = settled_at or datetime.now(timezone.utc)
    settled: list[RuntimeArtifactEffectRecord] = []
    for intent in intents:
        update: dict[str, object] = {
            "revision": intent.revision + 1,
            "settled_at": now,
        }
        if not ok:
            # A stopped write may have changed the file without returning its
            # receipt. Keep it distinct from a confirmed rejection/failure.
            metadata = getattr(result, "metadata", {})
            update["status"] = (
                "ambiguous"
                if metadata.get("side_effect_outcome") == "unknown"
                else "failed"
            )
            settled.append(intent.model_copy(update=update))
            continue
        resolved = resolve_artifact_effect_path(intent, root)
        if not resolved:
            update["status"] = "missing"
            settled.append(intent.model_copy(update=update))
            continue
        path = Path(resolved)
        if intent.artifact_role == "delivery" and intent.content_media_types:
            expected_kind = _delivery_media_kind(path)
            projected_kinds = {
                str(block.get("type") or "")
                for block in getattr(result, "content", ())
                if isinstance(block, dict)
            }
            if (
                expected_kind not in intent.content_media_types
                or expected_kind not in projected_kinds
            ):
                update["status"] = "failed"
                settled.append(intent.model_copy(update=update))
                continue
        content_hash = _file_sha256(path)
        update.update(
            {
                "status": (
                    "delivered"
                    if intent.artifact_role == "delivery"
                    else "committed"
                ),
                "size_bytes": path.stat().st_size,
                "sha256": content_hash,
                "delivery_idempotency_key": (
                    f"artifact-delivery:{intent.artifact_id}:{content_hash}"
                ),
                "media_type": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            },
        )
        settled.append(intent.model_copy(update=update))
    return tuple(settled)


def artifact_effect_public_payload(
    record: RuntimeArtifactEffectRecord,
) -> dict[str, object]:
    """Return the content-free evidence safe for runtime/UI projection."""

    payload = {
        "artifact_effect_id": record.artifact_effect_id,
        "artifact_id": record.artifact_id,
        "artifact_role": record.artifact_role,
        "status": record.status,
        "relative_path": record.relative_path,
        "delivery_idempotency_key": record.delivery_idempotency_key,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "media_type": record.media_type,
    }
    if record.research_export_id is not None:
        payload["research_export_id"] = record.research_export_id
    return payload


__all__ = [
    "ArtifactEffectStatus",
    "ArtifactDeliveryRecovery",
    "ArtifactRole",
    "RuntimeArtifactEffectRecord",
    "RuntimeArtifactEffectEnvelope",
    "RuntimeArtifactEffectJournal",
    "RuntimeArtifactEffectJournalConflictError",
    "RuntimeArtifactEffectJournalCorruptionError",
    "RuntimeArtifactEffectJournalError",
    "artifact_effect_public_payload",
    "artifact_effect_intents",
    "artifact_effect_intents_from_result",
    "recover_artifact_delivery",
    "project_committed_primary_delivery",
    "register_artifact_candidate",
    "settle_artifact_effects",
]
