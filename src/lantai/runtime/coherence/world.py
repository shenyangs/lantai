# -*- coding: utf-8 -*-
"""Read-only work-reality ledger and least-context view compiler."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from lantai.security.storage_keys import (
    contained_storage_path,
    safe_storage_key,
    write_text_atomic_no_follow,
)

from ..runs import RuntimeEffectRecord
from .contracts import PermissionEnvelope, merge_permission_decisions
from .integrity import canonical_sha256


WorldSourceKind = Literal["memory", "wiki", "runtime", "effect", "artifact"]
WorldClaimKind = Literal[
    "declared",
    "observed",
    "inferred",
    "decision",
    "conflict",
    "unknown",
]
WorldClaimStatus = Literal[
    "current",
    "tentative",
    "contested",
    "superseded",
    "retracted",
    "expired",
    "unknown",
]
ContextViewCategory = Literal[
    "goal",
    "current_state",
    "decision",
    "open_item",
    "artifact",
    "recent_change",
    "conflict",
    "unknown",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return (
        result
        if result.tzinfo is not None
        else result.replace(tzinfo=timezone.utc)
    )


class WorldEvidence(BaseModel):
    """A source pointer that supports a claim without becoming one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    source_kind: WorldSourceKind
    source_ref: str
    content_sha256: str
    scopes: tuple[str, ...]
    learned_at: datetime
    event_time: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    trust: float = Field(default=1.0, ge=0.0, le=1.0)
    instruction_trust: Literal[
        "untrusted_external_evidence"
    ] = "untrusted_external_evidence"


class WorldClaim(BaseModel):
    """Typed statement with explicit epistemic state and source links."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    kind: WorldClaimKind
    status: WorldClaimStatus
    subject: str
    predicate: str
    object_value: Any
    scopes: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()
    supersedes_claim_ids: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    learned_at: datetime
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    sensitivity: str = "normal"


class WorldConflict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conflict_id: str
    subject: str
    predicate: str
    claim_ids: tuple[str, ...]
    object_hashes: tuple[str, ...]


class WorldUnknown(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unknown_id: str
    subject: str
    question: str
    reason: str
    scopes: tuple[str, ...]
    source_refs: tuple[str, ...] = ()


class WorldChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    change_id: str
    kind: Literal["state_transition", "supersession", "effect", "artifact"]
    subject: str
    before_claim_ids: tuple[str, ...] = ()
    after_claim_ids: tuple[str, ...] = ()
    cause_refs: tuple[str, ...] = ()
    occurred_at: datetime | None = None


class WorldLedgerSnapshot(BaseModel):
    """Disposable, source-addressed projection of current work reality."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    snapshot_id: str
    workspace_id: str
    compiled_at: datetime
    source_state_sha256: str
    evidence: tuple[WorldEvidence, ...]
    claims: tuple[WorldClaim, ...]
    conflicts: tuple[WorldConflict, ...] = ()
    unknowns: tuple[WorldUnknown, ...] = ()
    changes: tuple[WorldChange, ...] = ()
    integrity_sha256: str

    def verify_integrity(self) -> bool:
        return (
            canonical_sha256(
                self.model_dump(mode="json", exclude={"integrity_sha256"}),
            )
            == self.integrity_sha256
        )


class ContextViewRequest(BaseModel):
    """Explicit boundary for least-context compilation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    objective: str
    allowed_scopes: tuple[str, ...]
    allowed_source_kinds: tuple[WorldSourceKind, ...] = (
        "memory",
        "wiki",
        "runtime",
        "effect",
        "artifact",
    )
    permission_envelope: PermissionEnvelope | None = None
    include_sensitive: bool = False
    max_items: int = Field(default=80, ge=1, le=400)
    temporary: bool = False


class ContextViewItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: ContextViewCategory
    claim_id: str
    statement: str
    status: WorldClaimStatus
    evidence_refs: tuple[str, ...]
    instruction_trust: Literal[
        "untrusted_external_evidence"
    ] = "untrusted_external_evidence"


class ContextViewManifest(BaseModel):
    """Hashed, permission-cut worksite view consumable by any model/Agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    view_id: str
    task_id: str
    objective: str
    world_snapshot_id: str
    world_snapshot_sha256: str
    compiled_at: datetime
    allowed_scopes: tuple[str, ...]
    items: tuple[ContextViewItem, ...]
    withheld_by_scope: int = Field(ge=0)
    withheld_sensitive: int = Field(ge=0)
    conflicts: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    temporary: bool = False
    integrity_sha256: str

    def verify_integrity(self) -> bool:
        return (
            canonical_sha256(
                self.model_dump(mode="json", exclude={"integrity_sha256"}),
            )
            == self.integrity_sha256
        )


def _claim_status_from_memory(status: str) -> WorldClaimStatus:
    return {
        "confirmed": "current",
        "observed": "tentative",
        "pending": "tentative",
        "conflict": "contested",
        "rejected": "retracted",
        "superseded": "superseded",
        "expired": "expired",
        "forgotten": "retracted",
    }.get(
        status, "unknown"
    )  # type: ignore[return-value]


class WorldLedgerCompiler:
    """Compile authorities without taking write ownership from them."""

    def __init__(
        self, workspace_dir: str | Path, *, workspace_id: str
    ) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.workspace_id = workspace_id.strip() or "default"
        self.runtime_root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
        )

    def compile_workspace(
        self,
        *,
        run_ids: Sequence[str] | None = None,
        persist: bool = True,
    ) -> WorldLedgerSnapshot:
        evidence: list[WorldEvidence] = []
        claims: list[WorldClaim] = []
        unknowns: list[WorldUnknown] = []
        changes: list[WorldChange] = []
        source_state: dict[str, Any] = {}
        now = _utc_now()

        self._project_memory(evidence, claims, source_state, now=now)
        self._project_wiki(evidence, claims, source_state, now=now)
        self._project_runtime(
            evidence,
            claims,
            unknowns,
            changes,
            source_state,
            run_ids=set(run_ids) if run_ids is not None else None,
            now=now,
        )
        self._project_artifacts(
            evidence,
            claims,
            changes,
            source_state,
            now=now,
        )
        conflicts = self._detect_conflicts(claims)
        source_state_sha256 = canonical_sha256(source_state)
        snapshot_payload = {
            "schema_version": 1,
            "snapshot_id": (
                f"world:{self.workspace_id}:{source_state_sha256[:16]}:"
                f"{canonical_sha256(now)[:8]}"
            ),
            "workspace_id": self.workspace_id,
            "compiled_at": now,
            "source_state_sha256": source_state_sha256,
            "evidence": tuple(evidence),
            "claims": tuple(claims),
            "conflicts": tuple(conflicts),
            "unknowns": tuple(unknowns),
            "changes": tuple(changes),
        }
        snapshot = WorldLedgerSnapshot(
            **snapshot_payload,
            integrity_sha256=canonical_sha256(snapshot_payload),
        )
        if persist:
            self._persist_world_snapshot(snapshot)
        return snapshot

    def _project_memory(
        self,
        evidence: list[WorldEvidence],
        claims: list[WorldClaim],
        source_state: dict[str, Any],
        *,
        now: datetime,
    ) -> None:
        from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger

        ledger = CanonicalMemoryLedger(self.workspace_dir)
        facts = ledger.list_facts()
        events_by_id = {event.id: event for event in ledger.list_events()}
        source_state["memory"] = [fact.to_dict() for fact in facts]
        for fact in facts:
            scope = (
                "global"
                if fact.scope == "global"
                else f"{fact.scope}:{fact.scope_id or self.workspace_id}"
            )
            evidence_ids: list[str] = []
            for event_id in fact.event_ids:
                event = events_by_id.get(event_id)
                if event is None:
                    continue
                evidence_id = f"world-evidence:memory:{event.id}"
                evidence_ids.append(evidence_id)
                evidence.append(
                    WorldEvidence(
                        evidence_id=evidence_id,
                        source_kind="memory",
                        source_ref=f"memory-event:{event.id}",
                        content_sha256=canonical_sha256(event.content),
                        scopes=(scope,),
                        learned_at=_parse_datetime(event.created_at) or now,
                        event_time=_parse_datetime(event.created_at),
                        trust=1.0 if event.source_role == "user" else 0.7,
                    ),
                )
            claims.append(
                WorldClaim(
                    claim_id=f"world-claim:memory:{fact.id}",
                    kind="declared"
                    if fact.status == "confirmed"
                    else "inferred",
                    status=_claim_status_from_memory(fact.status),
                    subject=fact.subject,
                    predicate=f"memory.{fact.memory_type}",
                    object_value=fact.content,
                    scopes=(scope,),
                    evidence_ids=tuple(evidence_ids),
                    supersedes_claim_ids=(
                        (f"world-claim:memory:{fact.supersedes_id}",)
                        if fact.supersedes_id
                        else ()
                    ),
                    valid_from=_parse_datetime(fact.created_at),
                    valid_to=_parse_datetime(fact.expires_at),
                    learned_at=_parse_datetime(fact.updated_at) or now,
                    confidence=fact.confidence,
                    sensitivity=fact.sensitivity,
                ),
            )

    def _project_wiki(
        self,
        evidence: list[WorldEvidence],
        claims: list[WorldClaim],
        source_state: dict[str, Any],
        *,
        now: datetime,
    ) -> None:
        from lantai.app.wiki import core as wiki_core
        from lantai.app.wiki.fact_graph import EvidenceGraphIndex

        graph_path = (
            wiki_core.wiki_root(self.workspace_dir)
            / wiki_core.WIKI_FACT_GRAPH_FILE
        )
        if not graph_path.is_file():
            source_state["wiki"] = []
            return
        facts = EvidenceGraphIndex(graph_path).list_facts(limit=2_000)
        source_state["wiki"] = [fact.model_dump(mode="json") for fact in facts]
        scope = f"workspace:{self.workspace_id}"
        for fact in facts:
            evidence_ids: list[str] = []
            for item in fact.evidence:
                evidence_id = (
                    f"world-evidence:wiki:{fact.relation_id}:"
                    f"{item.source_id}:{item.span_start}-{item.span_end}"
                )
                evidence_ids.append(evidence_id)
                evidence.append(
                    WorldEvidence(
                        evidence_id=evidence_id,
                        source_kind="wiki",
                        source_ref=(
                            f"wiki:{item.source_path}#char="
                            f"{item.span_start}-{item.span_end}"
                        ),
                        content_sha256=item.source_sha256,
                        scopes=(scope,),
                        learned_at=_parse_datetime(item.extracted_at) or now,
                        event_time=_parse_datetime(item.extracted_at),
                        trust=max(0.0, min(1.0, item.trust * item.confidence)),
                    ),
                )
            claims.append(
                WorldClaim(
                    claim_id=f"world-claim:wiki:{fact.relation_id}",
                    kind="observed",
                    status=(
                        "current"
                        if fact.polarity == "positive"
                        else "contested"
                    ),
                    subject=fact.subject_name,
                    predicate=fact.predicate,
                    object_value=fact.object_name,
                    scopes=(scope,),
                    evidence_ids=tuple(evidence_ids),
                    learned_at=max(
                        (
                            _parse_datetime(item.extracted_at) or now
                            for item in fact.evidence
                        ),
                        default=now,
                    ),
                    confidence=max(
                        (item.confidence for item in fact.evidence),
                        default=None,
                    ),
                ),
            )

    def _project_runtime(
        self,
        evidence: list[WorldEvidence],
        claims: list[WorldClaim],
        unknowns: list[WorldUnknown],
        changes: list[WorldChange],
        source_state: dict[str, Any],
        *,
        run_ids: set[str] | None,
        now: datetime,
    ) -> None:
        from ..effects import RuntimeEffectJournal
        from ..store import RuntimeRunStore

        store = RuntimeRunStore(self.workspace_dir)
        contracts = [
            contract
            for contract in store.list_contracts()
            if run_ids is None or contract.run_id in run_ids
        ]
        journal = RuntimeEffectJournal(self.workspace_dir)
        source_state["runtime"] = [
            contract.model_dump(mode="json") for contract in contracts
        ]
        for contract in contracts:
            scope = f"run:{contract.run_id}"
            workspace_scope = f"workspace:{self.workspace_id}"
            evidence_id = f"world-evidence:runtime:{contract.run_id}"
            evidence.append(
                WorldEvidence(
                    evidence_id=evidence_id,
                    source_kind="runtime",
                    source_ref=f"runtime-run:{contract.run_id}",
                    content_sha256=canonical_sha256(
                        contract.model_dump(mode="json")
                    ),
                    scopes=(workspace_scope, scope),
                    learned_at=contract.heartbeat_at or contract.created_at,
                    event_time=contract.settled_at or contract.started_at,
                    valid_from=contract.started_at or contract.created_at,
                    valid_to=contract.settled_at,
                ),
            )
            status_claim_id = f"world-claim:runtime:{contract.run_id}:status"
            claims.append(
                WorldClaim(
                    claim_id=status_claim_id,
                    kind="observed",
                    status="current",
                    subject=f"run:{contract.run_id}",
                    predicate="runtime.status",
                    object_value=contract.status,
                    scopes=(workspace_scope, scope),
                    evidence_ids=(evidence_id,),
                    valid_from=contract.started_at or contract.created_at,
                    valid_to=contract.settled_at,
                    learned_at=contract.heartbeat_at or contract.created_at,
                ),
            )
            if contract.task_contract is not None:
                claims.append(
                    WorldClaim(
                        claim_id=(
                            f"world-claim:runtime:{contract.run_id}:objective"
                        ),
                        kind="declared",
                        status="current",
                        subject=f"run:{contract.run_id}",
                        predicate="runtime.objective",
                        object_value=contract.task_contract.objective,
                        scopes=(workspace_scope, scope),
                        evidence_ids=(evidence_id,),
                        learned_at=contract.created_at,
                    ),
                )
            if contract.error is not None:
                unknowns.append(
                    WorldUnknown(
                        unknown_id=(
                            "world-unknown:runtime:" f"{contract.run_id}:error"
                        ),
                        subject=f"run:{contract.run_id}",
                        question="Can the failed run complete its objective?",
                        reason=contract.error.code,
                        scopes=(workspace_scope, scope),
                        source_refs=(f"runtime-run:{contract.run_id}",),
                    ),
                )
            effects = journal.list_effects(
                contract.run_id,
                contract.attempt_id,
            )
            latest_effects: dict[str, RuntimeEffectRecord] = {}
            for effect in effects:
                current = latest_effects.get(effect.effect_id)
                if current is None or effect.revision > current.revision:
                    latest_effects[effect.effect_id] = effect
            source_state.setdefault("effects", {})[contract.run_id] = [
                effect.model_dump(mode="json") for effect in effects
            ]
            for effect in latest_effects.values():
                effect_evidence_id = (
                    f"world-evidence:effect:{effect.effect_id}:"
                    f"{effect.revision}"
                )
                evidence.append(
                    WorldEvidence(
                        evidence_id=effect_evidence_id,
                        source_kind="effect",
                        source_ref=f"runtime-effect:{effect.effect_id}",
                        content_sha256=canonical_sha256(
                            effect.model_dump(mode="json")
                        ),
                        scopes=(workspace_scope, scope),
                        learned_at=effect.settled_at
                        or effect.started_at
                        or effect.created_at,
                        event_time=effect.settled_at or effect.started_at,
                    ),
                )
                effect_claim_id = f"world-claim:effect:{effect.effect_id}"
                claims.append(
                    WorldClaim(
                        claim_id=effect_claim_id,
                        kind="observed",
                        status=(
                            "contested"
                            if effect.status == "ambiguous"
                            else "current"
                        ),
                        subject=f"run:{contract.run_id}",
                        predicate=f"effect.{effect.tool_name}.status",
                        object_value=effect.status,
                        scopes=(workspace_scope, scope),
                        evidence_ids=(effect_evidence_id,),
                        learned_at=(
                            effect.settled_at
                            or effect.started_at
                            or effect.created_at
                        ),
                    ),
                )
                changes.append(
                    WorldChange(
                        change_id=(
                            f"world-change:effect:{effect.effect_id}:"
                            f"{effect.revision}"
                        ),
                        kind="effect",
                        subject=f"run:{contract.run_id}",
                        after_claim_ids=(effect_claim_id,),
                        cause_refs=(f"runtime-effect:{effect.effect_id}",),
                        occurred_at=effect.settled_at or effect.started_at,
                    ),
                )

    def _project_artifacts(
        self,
        evidence: list[WorldEvidence],
        claims: list[WorldClaim],
        changes: list[WorldChange],
        source_state: dict[str, Any],
        *,
        now: datetime,
    ) -> None:
        from lantai.app.capabilities.artifacts import load_artifact_manifest

        manifest = load_artifact_manifest(self.workspace_dir)
        artifacts = manifest.get("artifacts") or []
        source_state["artifacts"] = artifacts
        workspace_scope = f"workspace:{self.workspace_id}"
        for raw in artifacts:
            if not isinstance(raw, dict):
                continue
            artifact_id = str(
                raw.get("id") or raw.get("path") or raw.get("url") or ""
            ).strip()
            if not artifact_id:
                continue
            run_id = str(raw.get("run_id") or "").strip()
            scopes = (
                (workspace_scope, f"run:{run_id}")
                if run_id
                else (workspace_scope,)
            )
            evidence_id = f"world-evidence:artifact:{artifact_id}"
            created_at = _parse_datetime(raw.get("created_at")) or now
            evidence.append(
                WorldEvidence(
                    evidence_id=evidence_id,
                    source_kind="artifact",
                    source_ref=f"artifact:{artifact_id}",
                    content_sha256=str(raw.get("sha256") or "").strip()
                    or canonical_sha256(raw),
                    scopes=scopes,
                    learned_at=created_at,
                    event_time=created_at,
                ),
            )
            claim_id = f"world-claim:artifact:{artifact_id}"
            claims.append(
                WorldClaim(
                    claim_id=claim_id,
                    kind="observed",
                    status="current",
                    subject=f"artifact:{artifact_id}",
                    predicate="artifact.current_version",
                    object_value={
                        key: raw.get(key)
                        for key in (
                            "title",
                            "type",
                            "path",
                            "url",
                            "mime_type",
                            "sha256",
                            "run_id",
                            "attempt_id",
                            "step_id",
                            "effect_id",
                        )
                        if raw.get(key) not in (None, "")
                    },
                    scopes=scopes,
                    evidence_ids=(evidence_id,),
                    learned_at=created_at,
                ),
            )
            changes.append(
                WorldChange(
                    change_id=f"world-change:artifact:{artifact_id}",
                    kind="artifact",
                    subject=f"artifact:{artifact_id}",
                    after_claim_ids=(claim_id,),
                    cause_refs=tuple(
                        f"runtime-{kind}:{raw[kind + '_id']}"
                        for kind in ("run", "attempt", "step", "effect")
                        if raw.get(kind + "_id")
                    ),
                    occurred_at=created_at,
                ),
            )

    @staticmethod
    def _detect_conflicts(claims: Sequence[WorldClaim]) -> list[WorldConflict]:
        grouped: dict[tuple[str, str], list[WorldClaim]] = {}
        for claim in claims:
            if claim.status not in {"current", "tentative", "contested"}:
                continue
            grouped.setdefault((claim.subject, claim.predicate), []).append(
                claim
            )
        conflicts: list[WorldConflict] = []
        for (subject, predicate), rows in grouped.items():
            object_hashes = tuple(
                sorted({canonical_sha256(row.object_value) for row in rows})
            )
            if len(object_hashes) <= 1:
                continue
            claim_ids = tuple(sorted(row.claim_id for row in rows))
            identity = (subject, predicate, claim_ids)
            conflicts.append(
                WorldConflict(
                    conflict_id=(
                        "world-conflict:" f"{canonical_sha256(identity)[:24]}"
                    ),
                    subject=subject,
                    predicate=predicate,
                    claim_ids=claim_ids,
                    object_hashes=object_hashes,
                ),
            )
        return conflicts

    def _persist_world_snapshot(self, snapshot: WorldLedgerSnapshot) -> None:
        filename = safe_storage_key(
            "world-ledger-snapshot",
            snapshot.snapshot_id,
            suffix=".json",
        )
        path = contained_storage_path(
            self.runtime_root,
            "world-ledger",
            "snapshots",
            filename,
        )
        write_text_atomic_no_follow(path, snapshot.model_dump_json(indent=2))


class ContextViewCompiler:
    """Cut a minimal, source-aware context view by task and permissions."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.runtime_root = contained_storage_path(
            self.workspace_dir,
            ".lantai",
            "runtime",
            "v1",
        )

    def compile(
        self,
        snapshot: WorldLedgerSnapshot,
        request: ContextViewRequest,
        *,
        persist: bool = True,
    ) -> ContextViewManifest:
        if not snapshot.verify_integrity():
            raise ValueError("World Ledger snapshot integrity is invalid")
        evidence_by_id = {item.evidence_id: item for item in snapshot.evidence}
        allowed_scopes = set(request.allowed_scopes)
        allowed_sources = set(request.allowed_source_kinds)
        withheld_by_scope = 0
        withheld_sensitive = 0
        candidates: list[tuple[int, datetime, ContextViewItem]] = []
        conflict_claim_ids = {
            claim_id
            for conflict in snapshot.conflicts
            for claim_id in conflict.claim_ids
        }
        for claim in snapshot.claims:
            scope_matches = bool(allowed_scopes.intersection(claim.scopes))
            if request.permission_envelope is not None and scope_matches:
                scope_matches = (
                    merge_permission_decisions(
                        [
                            request.permission_envelope.resolve(
                                capability="context.read",
                                scope=scope,
                            )
                            for scope in allowed_scopes.intersection(
                                claim.scopes
                            )
                        ],
                    )
                    != "deny"
                )
            if not scope_matches:
                withheld_by_scope += 1
                continue
            if claim.sensitivity != "normal" and not request.include_sensitive:
                withheld_sensitive += 1
                continue
            evidence_rows = [
                evidence_by_id[evidence_id]
                for evidence_id in claim.evidence_ids
                if evidence_id in evidence_by_id
            ]
            if evidence_rows and not any(
                row.source_kind in allowed_sources for row in evidence_rows
            ):
                withheld_by_scope += 1
                continue
            category = self._category_for_claim(
                claim,
                is_conflict=claim.claim_id in conflict_claim_ids,
            )
            object_text = json.dumps(
                claim.object_value,
                ensure_ascii=False,
                sort_keys=True,
            )
            statement = f"{claim.subject} · {claim.predicate} = {object_text}"
            priority = {
                "conflict": 0,
                "goal": 1,
                "open_item": 2,
                "current_state": 3,
                "decision": 4,
                "artifact": 5,
                "recent_change": 6,
                "unknown": 7,
            }[category]
            candidates.append(
                (
                    priority,
                    claim.learned_at,
                    ContextViewItem(
                        category=category,
                        claim_id=claim.claim_id,
                        statement=statement,
                        status=claim.status,
                        evidence_refs=tuple(
                            row.source_ref for row in evidence_rows
                        ),
                    ),
                ),
            )
        candidates.sort(
            key=lambda row: (row[0], -row[1].timestamp(), row[2].claim_id)
        )
        items = tuple(row[2] for row in candidates[: request.max_items])
        compiled_at = _utc_now()
        payload = {
            "schema_version": 1,
            "view_id": (
                f"context-view:{request.task_id}:"
                f"{canonical_sha256((snapshot.integrity_sha256, request.model_dump(mode='json'), compiled_at))[:20]}"  # noqa: E501
            ),
            "task_id": request.task_id,
            "objective": request.objective,
            "world_snapshot_id": snapshot.snapshot_id,
            "world_snapshot_sha256": snapshot.integrity_sha256,
            "compiled_at": compiled_at,
            "allowed_scopes": request.allowed_scopes,
            "items": items,
            "withheld_by_scope": withheld_by_scope,
            "withheld_sensitive": withheld_sensitive,
            "conflicts": tuple(
                conflict.conflict_id
                for conflict in snapshot.conflicts
                if any(
                    claim_id in {item.claim_id for item in items}
                    for claim_id in conflict.claim_ids
                )
            ),
            "unknowns": tuple(
                unknown.unknown_id
                for unknown in snapshot.unknowns
                if allowed_scopes.intersection(unknown.scopes)
            ),
            "temporary": request.temporary,
        }
        view = ContextViewManifest(
            **payload,
            integrity_sha256=canonical_sha256(payload),
        )
        if persist and not request.temporary:
            self._persist_view(view)
        return view

    @staticmethod
    def _category_for_claim(
        claim: WorldClaim,
        *,
        is_conflict: bool,
    ) -> ContextViewCategory:
        if is_conflict or claim.status == "contested":
            return "conflict"
        if claim.predicate == "runtime.objective":
            return "goal"
        if claim.predicate == "runtime.status" and str(claim.object_value) in {
            "queued",
            "claimed",
            "running",
            "failed",
            "stalled",
            "interrupted",
        }:
            return "open_item"
        if claim.predicate.startswith("artifact."):
            return "artifact"
        if claim.kind == "decision":
            return "decision"
        if claim.kind == "unknown" or claim.status == "unknown":
            return "unknown"
        return "current_state"

    def _persist_view(self, view: ContextViewManifest) -> None:
        filename = safe_storage_key(
            "context-view-manifest",
            view.view_id,
            suffix=".json",
        )
        path = contained_storage_path(
            self.runtime_root,
            "context-views",
            filename,
        )
        write_text_atomic_no_follow(path, view.model_dump_json(indent=2))


__all__ = [
    "ContextViewCompiler",
    "ContextViewItem",
    "ContextViewManifest",
    "ContextViewRequest",
    "WorldChange",
    "WorldClaim",
    "WorldConflict",
    "WorldEvidence",
    "WorldLedgerCompiler",
    "WorldLedgerSnapshot",
    "WorldUnknown",
]
