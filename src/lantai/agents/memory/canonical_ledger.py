# -*- coding: utf-8 -*-
"""Canonical memory ledger: events are evidence, facts are explicit state."""

from __future__ import annotations

import json
import hashlib
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from .policy import MemoryPolicyEngine, MemoryPolicyResult
from .ledger_validation import object_payload, record_payload, string_list
from .locking import memory_state_lock, memory_state_lock_path
from ...utils.authoritative_store import (
    StoreCorruptionError, atomic_write_bytes, exclusive_store_lock, fsync_directory, load_validated,
    load_interrupted_publication, recover_last_good, resume_interrupted_publication, write_authoritative,
)


FactStatus = Literal[
    "observed",
    "pending",
    "confirmed",
    "conflict",
    "rejected",
    "superseded",
    "expired",
    "forgotten",
]
_SENSITIVE = re.compile(
    r"(?:api[_ -]?key|secret|password|token|系统提示|system prompt|身份证|护照|"
    r"银行卡|出生(?:日期|时间|地点|地|年月|时辰)|生辰|八字|家庭住址|病史|诊断)",
    re.I,
)
_RUNTIME = re.compile(r"(?:todo|下一步|待办|任务进度|run state|运行状态)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", value.casefold(), flags=re.UNICODE)


@dataclass(frozen=True)
class MemoryEvent:
    id: str
    content: str
    source_message_id: str = ""
    session_id: str = ""
    source_role: str = "unknown"
    content_hash: str = ""
    source_created_at: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class ConfirmedFact:
    """A canonical fact candidate; the historical class name is API-compatible."""

    id: str
    content: str
    memory_type: str
    status: FactStatus
    event_ids: list[str]
    confidence: float
    evidence_refs: list[dict] = field(default_factory=list)
    support_status: str = "legacy_unverified"
    support_reason: str = ""
    support_model: str = ""
    support_reviewed_at: str = ""
    scope: str = "global"
    scope_id: str = ""
    subject: str = "user"
    utility_score: float = 0.0
    decision_reason: str = ""
    sensitivity: str = "normal"
    session_ids: list[str] = field(default_factory=list)
    occurrence_count: int = 1
    legacy_entry_ids: list[str] = field(default_factory=list)
    atom_ids: list[str] = field(default_factory=list)
    supersedes_id: str = ""
    superseded_by_id: str = ""
    rejection_reason: str = ""
    expires_at: str = ""
    dream_score: float = 0.0
    dream_run_id: str = ""
    dream_reason: str = ""
    dream_source_event_ids: list[str] = field(default_factory=list)
    last_verified_at: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


class CanonicalMemoryLedger:
    """Auditable facts with projections that can be discarded and rebuilt."""

    def __init__(
        self,
        working_dir: str | Path,
        *,
        index_delete: Callable[[str], None] | None = None,
        policy: MemoryPolicyEngine | None = None,
        ledger_dir: str | Path | None = None,
    ) -> None:
        self.root = Path(ledger_dir) if ledger_dir is not None else Path(working_dir) / "memory-ledger"
        self.working_dir = Path(working_dir)
        self.events_path = self.root / "events.jsonl"
        self.facts_path = self.root / "facts.json"
        self.projections_path = self.root / "projections.json"
        self.preferences_path = self.root / "preferences.json"
        self.deleted_sources_path = self.root / "deleted-sources.json"
        # Adoption replaces the ledger directory. Its lock must survive that
        # rename so writers waiting in another process still share one inode.
        self.lock_path = memory_state_lock_path(working_dir)
        self.deletion_pending_path = self.root / ".source-deletion.pending.json"
        self.index_delete = index_delete
        self.policy = policy or MemoryPolicyEngine()

    def get_preferences(self) -> dict[str, bool]:
        with self._locked() as snapshot:
            return snapshot["preferences"]

    def read_snapshot(self) -> dict:
        """Return one validated view for recall and portable memory decisions."""
        with self._locked() as snapshot:
            return {
                "facts": [asdict(fact) for fact in snapshot["facts"]],
                "events": [asdict(event) for event in snapshot["events"]],
                "deleted_sources": snapshot["deleted_sources"],
                "preferences": snapshot["preferences"],
            }

    def _load_preferences(self) -> dict[str, bool]:
        return load_validated(
            self.preferences_path, self._decode_preferences,
            missing=lambda: {"runtime_enabled": True, "capture_enabled": True},
        )

    @staticmethod
    def _decode_preferences(value: str) -> dict[str, bool]:
        payload = object_payload(value, versions={1, 2})
        keys = ("runtime_enabled", "capture_enabled")
        if any(type(payload.get(key)) is not bool for key in keys):
            raise ValueError("invalid preferences")
        return {key: payload[key] for key in keys}

    def set_preferences(
        self,
        *,
        runtime_enabled: bool | None = None,
        capture_enabled: bool | None = None,
    ) -> dict[str, bool]:
        with self._locked():
            preferences = self.get_preferences()
            if runtime_enabled is not None:
                preferences["runtime_enabled"] = bool(runtime_enabled)
            if capture_enabled is not None:
                preferences["capture_enabled"] = bool(capture_enabled)
            self._write_atomic(self.preferences_path, preferences)
            return preferences

    @contextmanager
    def _locked(self, *, validate: bool = True, allow_pending: bool = False):
        with memory_state_lock(self.working_dir):
            if not allow_pending and self.deletion_pending_path.exists():
                raise StoreCorruptionError(self.deletion_pending_path, "interrupted source deletion")
            snapshot = None
            if validate:
                snapshot = {
                    "facts": self._load_facts(), "events": self._load_events(),
                    "deleted_sources": self._load_deleted_sources(),
                    "preferences": self._load_preferences(),
                }
                self._validate_sources(snapshot["facts"], snapshot["events"], snapshot["deleted_sources"])
            yield snapshot

    def _validate_sources(self, facts, events, deleted) -> None:
        event_ids = set(deleted["event_ids"])
        fact_hashes = set(deleted["fact_hashes"])
        if any(event.id in event_ids for event in events):
            raise StoreCorruptionError(self.events_path, "source deletion is inconsistent")
        for fact in facts:
            sources = set(fact.event_ids) | set(fact.dream_source_event_ids) | {
                str(ref.get("event_id") or "") for ref in fact.evidence_refs
            }
            content_hash = hashlib.sha256(_normalize(fact.content).encode("utf-8")).hexdigest()
            if sources & event_ids or (content_hash in fact_hashes and fact.status not in {
                "forgotten", "rejected", "superseded", "expired",
            }):
                raise StoreCorruptionError(self.facts_path, "fact conflicts with source deletion")

    @staticmethod
    def is_eligible(content: str) -> bool:
        """Keep secrets, sensitive personal data and run state out of the ledger."""

        clean = content.strip()
        return bool(clean) and not _SENSITIVE.search(clean) and not _RUNTIME.search(clean)

    def record_event(
        self,
        content: str,
        *,
        source_message_id: str = "",
        session_id: str = "",
        source_role: str = "unknown",
        source_created_at: str = "",
        event_id: str = "",
    ) -> MemoryEvent | None:
        if not self.is_eligible(content):
            return None
        event = MemoryEvent(
            id=event_id or f"event_{uuid.uuid4().hex}",
            content=content,
            source_message_id=source_message_id,
            session_id=session_id,
            source_role=source_role,
            source_created_at=source_created_at,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        with self._locked():
            if event.id in self._load_deleted_sources()["event_ids"]:
                return None
            events = self._load_events()
            for existing in events:
                if existing.id == event.id:
                    return existing
            self._write_lines_atomic(self.events_path, [asdict(item) for item in [*events, event]])
        return event

    def list_events(
        self,
        *,
        event_ids: set[str] | None = None,
    ) -> list[MemoryEvent]:
        """Read immutable source evidence through a stable public interface."""

        with self._locked() as snapshot:
            events = snapshot["events"]
            if event_ids is None:
                return events
            return [event for event in events if event.id in event_ids]

    def capture_fact(
        self,
        content: str,
        *,
        memory_type: str,
        event: MemoryEvent,
        explicit_user_request: bool = False,
        confidence: float = 0.6,
        policy_result: MemoryPolicyResult | None = None,
        scope_id: str = "",
        atom_id: str = "",
        evidence_quote: str = "",
        support_status: str = "unreviewed",
        support_reason: str = "",
        support_model: str = "",
        support_reviewed_at: str = "",
        support_group: str = "",
    ) -> ConfirmedFact | None:
        """Capture evidence; only explicit requests or confirmation become injectable."""

        clean = content.strip()
        if not self.is_eligible(clean):
            return None
        admission = policy_result or self.policy.evaluate(
            clean, memory_type=memory_type,
            explicit_user_request=explicit_user_request,
            recurrence_count=1, distinct_session_count=1,
        )
        if admission.decision == "reject":
            return None
        with self._locked():
            deleted = self._load_deleted_sources()
            if event.id in deleted["event_ids"]:
                return None
            stored_event = next(
                (item for item in self._load_events() if item.id == event.id), None
            )
            if stored_event is None or stored_event.content != event.content:
                # A stale or fabricated event object cannot become evidence.
                return None
            event = stored_event
            # Explicit memory requests retain their existing admission path,
            # even when the tool stored a model-written statement. Its source
            # grade remains derived and is never presented as user wording.
            effective_explicit = explicit_user_request
            if (
                admission.decision == "confirm"
                and not effective_explicit
                and (
                    support_status != "supported"
                    or event.source_role in {"derived", "historical_summary"}
                )
            ):
                admission = MemoryPolicyResult(
                    decision="suggest",
                    scope=admission.scope,
                    subject=admission.subject,
                    utility_score=admission.utility_score,
                    reason="等待原文语义复核",
                    sensitivity=admission.sensitivity,
                )
            if (
                hashlib.sha256(_normalize(clean).encode("utf-8")).hexdigest()
                in deleted["fact_hashes"]
                and not (explicit_user_request and event.source_role == "manual")
            ):
                return None
            facts = self._load_facts()
            normalized = _normalize(clean)
            evidence_ref = self._evidence_ref(event, evidence_quote)
            if support_group:
                # One semantic review can require several originals together.
                # Each member must be invalidated if any member is deleted.
                evidence_ref["support_group"] = support_group
            source_support = self._source_support_status(
                event.source_role, effective_explicit, support_status,
            )
            evidence_ref["support_status"] = source_support
            same_owner = []
            for fact in facts:
                if (fact.subject, fact.scope_id) != (admission.subject, scope_id):
                    continue
                if fact.scope == admission.scope:
                    same_owner.append(fact)
                elif policy_result is None and not scope_id:
                    # A previously observed legacy statement may have earned
                    # a broader scope through recurrence. Re-evaluate that
                    # same evidence history, never override explicit ownership.
                    repeated = self.policy.evaluate(
                        clean, memory_type=memory_type,
                        explicit_user_request=effective_explicit,
                        recurrence_count=fact.occurrence_count + (event.id not in fact.event_ids),
                        distinct_session_count=max(1, len(set(fact.session_ids) | {event.session_id})),
                    )
                    if (repeated.subject, repeated.scope) == (fact.subject, fact.scope):
                        same_owner.append(fact)
            existing = self._find_duplicate(
                same_owner,
                clean, memory_type,
            )
            if existing is None and any(
                _normalize(fact.content) == normalized
                and fact.status in {"forgotten", "rejected", "superseded"}
                and not (explicit_user_request and event.source_role == "manual")
                for fact in facts
            ):
                # An old source cannot undo deletion or correction. A new
                # manual decision can restore it, then receive new evidence.
                return None
            session_ids = {event.session_id} if event.session_id else set()
            if existing is not None:
                if existing.status == "confirmed" and source_support not in {
                    "supported", "user_confirmed", "explicit_request",
                } and not effective_explicit:
                    # An unreviewed recapture may not attach a new source to
                    # an already injectable understanding.
                    return existing
                if event.id not in existing.event_ids:
                    existing.event_ids.append(event.id)
                    existing.occurrence_count += 1
                if evidence_ref and evidence_ref not in existing.evidence_refs:
                    existing.evidence_refs.append(evidence_ref)
                existing.session_ids = list(dict.fromkeys([*existing.session_ids, *session_ids]))
                if atom_id and atom_id not in existing.atom_ids:
                    existing.atom_ids.append(atom_id)
                result = policy_result or self.policy.evaluate(
                    existing.content,
                    memory_type=memory_type,
                    explicit_user_request=effective_explicit,
                    recurrence_count=existing.occurrence_count,
                    distinct_session_count=max(1, len(existing.session_ids)),
                )
                self._apply_policy(existing, result, effective_explicit)
                existing.confidence = max(existing.confidence, max(0.0, min(1.0, confidence)))
                if source_support == "supported":
                    existing.support_status = source_support
                    existing.support_reason = support_reason[:500]
                    existing.support_model = support_model
                    existing.support_reviewed_at = support_reviewed_at
                elif effective_explicit and existing.support_status not in {
                    "supported", "user_confirmed",
                }:
                    existing.support_status = source_support
                existing.updated_at = _now()
                self._save_facts(facts)
                self._rebuild_projections_locked(facts)
                return existing

            result = admission
            if result.decision == "reject":
                return None
            status: FactStatus = {
                "observe": "observed",
                "suggest": "pending",
                "confirm": "confirmed",
            }.get(result.decision, "observed")  # type: ignore[assignment]
            fact = ConfirmedFact(
                id=f"fact_{uuid.uuid4().hex}",
                content=clean,
                memory_type=memory_type,
                status=status,
                event_ids=[event.id],
                confidence=max(0.0, min(1.0, confidence)),
                evidence_refs=[evidence_ref] if evidence_ref else [],
                support_status=source_support,
                support_reason=support_reason[:500],
                support_model=support_model,
                support_reviewed_at=support_reviewed_at,
                scope=result.scope,
                scope_id=scope_id,
                subject=result.subject,
                utility_score=result.utility_score,
                decision_reason=result.reason,
                sensitivity=result.sensitivity,
                session_ids=list(session_ids),
                atom_ids=[atom_id] if atom_id else [],
            )
            facts.append(fact)
            self._save_facts(facts)
            self._rebuild_projections_locked(facts)
            return fact

    @staticmethod
    def _source_support_status(
        source_role: str, explicit_user_request: bool, support_status: str,
    ) -> str:
        if source_role == "derived":
            return "derived_unverified"
        if source_role == "historical_summary":
            return "legacy_unverified"
        if not explicit_user_request:
            return support_status
        return {
            "manual": "user_confirmed",
            "user": "explicit_request",
            "derived": "derived_unverified",
            "historical_summary": "legacy_unverified",
        }.get(source_role, "legacy_unverified")

    @staticmethod
    def _evidence_ref(event: MemoryEvent, quote: str) -> dict:
        """Offsets are Unicode code points into this immutable event version."""

        start = event.content.find(quote) if quote else -1
        return {
            "event_id": event.id,
            "source_message_id": event.source_message_id,
            "session_id": event.session_id,
            "source_role": event.source_role,
            "source_created_at": event.source_created_at,
            "content_hash": event.content_hash or hashlib.sha256(
                event.content.encode("utf-8"),
            ).hexdigest(),
            "quote": quote if start >= 0 else "",
            "start_codepoint": start,
            "end_codepoint": start + len(quote) if start >= 0 else -1,
            "status": "located" if start >= 0 else "source_only",
        }

    def open_fact_sources(self, fact_id: str) -> dict:
        """Resolve only this ledger's fact/event IDs; never accept a file path."""

        with self._locked():
            return self._open_fact_sources_locked(fact_id)

    def _open_fact_sources_locked(self, fact_id: str) -> dict:

        fact = self.get_fact(fact_id)
        if fact is None:
            return {"found": False, "fact_id": fact_id, "sources": []}
        events = {event.id: event for event in self.list_events(event_ids=set(fact.event_ids))}
        refs = fact.evidence_refs or [
            self._evidence_ref(events[event_id], "")
            for event_id in fact.event_ids if event_id in events
        ]
        sources = []
        for ref in refs:
            event = events.get(str(ref.get("event_id") or ""))
            if event is None:
                sources.append({**ref, "status": "missing"})
                continue
            content_hash = hashlib.sha256(event.content.encode("utf-8")).hexdigest()
            if ref.get("content_hash") != content_hash:
                sources.append({**ref, "status": "version_mismatch"})
                continue
            try:
                start = int(ref.get("start_codepoint", -1))
                end = int(ref.get("end_codepoint", -1))
            except (TypeError, ValueError):
                start = end = -1
            quote = str(ref.get("quote") or "")
            status = (
                "located" if quote and 0 <= start < end <= len(event.content)
                and event.content[start:end] == quote else "source_only"
            )
            sources.append({
                **ref,
                "status": status,
                "content": event.content,
                "created_at": event.created_at,
                "source_created_at": event.source_created_at,
                "before": event.content[max(0, start - 120):start] if status == "located" else "",
                "after": event.content[end:end + 120] if status == "located" else "",
            })
        return {"found": True, "fact_id": fact_id, "fact": fact.to_dict(), "sources": sources}

    def _load_deleted_sources(self) -> dict[str, list[str]]:
        return load_validated(
            self.deleted_sources_path, self._decode_deleted_sources,
            missing=lambda: {"event_ids": [], "fact_hashes": []},
        )

    @staticmethod
    def _decode_deleted_sources(value: str) -> dict[str, list[str]]:
        payload = object_payload(value, versions={1, 2})
        keys = ("event_ids", "fact_hashes")
        if any(not string_list(payload.get(key)) for key in keys):
            raise ValueError("invalid deletion records")
        return {key: payload[key] for key in keys}

    def delete_source_event(self, event_id: str) -> dict:
        """Delete one original snapshot and invalidate facts without independent support.

        Tombstones are written first so a delayed extraction cannot recreate
        the source after a crash. IDs and fact hashes do not retain raw text.
        """

        with self._locked():
            return self._delete_source_event_locked(event_id)

    def resume_source_deletion(self, *, expected_event_id: str) -> dict:
        """Explicitly finish the identified deletion; never reinstate its source."""
        with self._locked(validate=False, allow_pending=True):
            try:
                journal = self.deletion_pending_path.read_bytes()
                pending = json.loads(journal.decode("utf-8"))
                if (not isinstance(pending, dict) or type(pending.get("version")) is not int
                        or pending["version"] != 1 or not expected_event_id
                        or pending.get("event_id") != expected_event_id
                        or not isinstance(pending.get("affected_facts"), list)
                        or not isinstance(pending.get("publications"), dict)
                        or set(pending["publications"]) != {"facts", "events", "deleted_sources"}):
                    raise ValueError("unidentified deletion")
                for item in pending["publications"].values():
                    if (not isinstance(item, dict) or not self._is_sha256(item.get("sha256"))
                            or "previous_sha256" not in item
                            or (item["previous_sha256"] is not None
                                and not self._is_sha256(item["previous_sha256"]))):
                        raise ValueError("invalid publication identity")
                for item in pending["affected_facts"]:
                    if (not isinstance(item, dict) or not isinstance(item.get("fact_id"), str)
                            or not item["fact_id"] or item.get("status") not in {
                                "observed", "pending", "confirmed", "conflict", "rejected",
                                "superseded", "expired", "forgotten",
                            } or not string_list(item.get("atom_ids"))
                            or not string_list(item.get("legacy_entry_ids"))):
                        raise ValueError("invalid affected fact")
                if len({item["fact_id"] for item in pending["affected_facts"]}) != len(pending["affected_facts"]):
                    raise ValueError("duplicate affected fact")
            except (OSError, UnicodeError, ValueError, TypeError, KeyError):
                raise StoreCorruptionError(self.deletion_pending_path, "deletion changed since inspection") from None
            self._load_preferences()
            components = self._deletion_components()
            targets = {}
            decoded = {}
            for name, (path, decode, missing) in components.items():
                identity = pending["publications"][name]
                if path.with_name(path.name + ".pending.json").exists():
                    load_interrupted_publication(path, decode, missing=missing, **{
                        "expected_sha256": identity["sha256"],
                        "previous_sha256": identity["previous_sha256"],
                    })
                else:
                    load_validated(path, decode, missing=missing)
                self._check_deletion_primary(path, identity)
                try:
                    value = self._deletion_target_path(name, identity["sha256"]).read_bytes()
                    if hashlib.sha256(value).hexdigest() != identity["sha256"]:
                        raise ValueError("target changed")
                    decoded[name] = decode(value.decode("utf-8"))
                    targets[name] = value
                except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    raise StoreCorruptionError(self.deletion_pending_path, "no validated source deletion target") from None
            facts, events, deleted = decoded["facts"], decoded["events"], decoded["deleted_sources"]
            self._validate_sources(facts, events, deleted)
            fact_by_id = {fact.id: fact for fact in facts}
            if (expected_event_id not in deleted["event_ids"]
                    or any(event.id == expected_event_id for event in events)
                    or any(item["fact_id"] not in fact_by_id or {
                        "fact_id": item["fact_id"], "status": fact_by_id[item["fact_id"]].status,
                        "atom_ids": fact_by_id[item["fact_id"]].atom_ids,
                        "legacy_entry_ids": fact_by_id[item["fact_id"]].legacy_entry_ids,
                    } != item for item in pending["affected_facts"])):
                raise StoreCorruptionError(self.deletion_pending_path, "source deletion target is inconsistent")
            if self.deletion_pending_path.read_bytes() != journal:
                raise StoreCorruptionError(self.deletion_pending_path, "deletion changed during validation")
            return self._publish_source_deletion(pending, targets, facts, events, deleted)

    @staticmethod
    def _is_sha256(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    def _deletion_target_path(self, name: str, digest: str) -> Path:
        return self.root / f".source-deletion-{name}-{digest}.target"

    def _deletion_components(self) -> dict:
        return {
            "deleted_sources": (self.deleted_sources_path, self._decode_deleted_sources,
                                lambda: {"event_ids": [], "fact_hashes": []}),
            "events": (self.events_path, self._decode_events, list),
            "facts": (self.facts_path, self._decode_facts, list),
        }

    @staticmethod
    def _check_deletion_primary(path: Path, identity: dict) -> str | None:
        try:
            try:
                current = hashlib.sha256(path.read_bytes()).hexdigest()
            except FileNotFoundError:
                current = None
            if current not in {identity["previous_sha256"], identity["sha256"]}:
                raise ValueError("data changed")
        except (OSError, ValueError):
            raise StoreCorruptionError(path, "source deletion data changed since inspection") from None
        return current

    def _delete_source_event_locked(self, event_id: str) -> dict:
        events = self._load_events()
        event = next((item for item in events if item.id == event_id), None)
        if event is None:
            return {"deleted": False, "affected_facts": []}
        deleted = self._load_deleted_sources()
        deleted["event_ids"] = list(dict.fromkeys([*deleted["event_ids"], event_id]))
        facts = self._load_facts()
        affected: list[dict] = []
        updated_at = _now()
        for fact in facts:
            if event_id not in (
                set(fact.event_ids) | set(fact.dream_source_event_ids)
                | {ref.get("event_id") for ref in fact.evidence_refs}
            ):
                continue
            deleted_groups = {
                str(ref.get("support_group"))
                for ref in fact.evidence_refs
                if ref.get("event_id") == event_id and ref.get("support_group")
            }
            fact.event_ids = [value for value in fact.event_ids if value != event_id]
            fact.evidence_refs = [ref for ref in fact.evidence_refs if ref.get("event_id") != event_id]
            for ref in fact.evidence_refs:
                if ref.get("support_group") in deleted_groups:
                    ref["support_status"] = "unreviewed"
            fact.dream_source_event_ids = [value for value in fact.dream_source_event_ids if value != event_id]
            if not fact.event_ids:
                deleted["fact_hashes"].append(hashlib.sha256(_normalize(fact.content).encode("utf-8")).hexdigest())
                fact.status = "forgotten"
                fact.content = "[来源已删除]"
                fact.support_reason = ""
                fact.decision_reason = ""
                fact.dream_reason = ""
                fact.rejection_reason = ""
                fact.session_ids = []
            elif not any(
                ref.get("support_status") in {"supported", "user_confirmed", "explicit_request"}
                for ref in fact.evidence_refs
            ):
                if fact.status == "confirmed":
                    fact.status = "pending"
                fact.support_status = "unreviewed"
                fact.support_reason = ""
            fact.updated_at = updated_at
            affected.append({
                "fact_id": fact.id, "status": fact.status,
                "atom_ids": list(fact.atom_ids), "legacy_entry_ids": list(fact.legacy_entry_ids),
            })
        deleted["fact_hashes"] = list(dict.fromkeys(deleted["fact_hashes"]))
        events = [item for item in events if item.id != event_id]
        self._validate_sources(facts, events, deleted)
        targets = {
            "deleted_sources": json.dumps(deleted, ensure_ascii=False, indent=2).encode("utf-8"),
            "events": "".join(json.dumps(asdict(item), ensure_ascii=False) + "\n" for item in events).encode("utf-8"),
            "facts": json.dumps({"version": 2, "facts": [asdict(fact) for fact in facts]},
                                ensure_ascii=False, indent=2).encode("utf-8"),
        }
        publications = {}
        for name, (path, _, _) in self._deletion_components().items():
            try:
                previous_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            except FileNotFoundError:
                previous_sha256 = None
            digest = hashlib.sha256(targets[name]).hexdigest()
            publications[name] = {"previous_sha256": previous_sha256, "sha256": digest}
            atomic_write_bytes(self._deletion_target_path(name, digest), targets[name])
        pending = {"version": 1, "event_id": event_id, "affected_facts": affected, "publications": publications}
        atomic_write_bytes(self.deletion_pending_path, json.dumps(pending).encode("utf-8"))
        return self._publish_source_deletion(pending, targets, facts, events, deleted)

    def _publish_source_deletion(self, pending, targets, facts, events, deleted) -> dict:
        for name, (path, _, _) in self._deletion_components().items():
            identity = pending["publications"][name]
            current_sha256 = self._check_deletion_primary(path, identity)
            if path.with_name(path.name + ".pending.json").exists():
                resume_interrupted_publication(path, targets[name], expected_sha256=identity["sha256"],
                                               previous_sha256=identity["previous_sha256"])
            elif current_sha256 == identity["sha256"]:
                continue
            elif name == "deleted_sources":
                self._write_atomic(path, deleted)
            elif name == "events":
                self._write_lines_atomic(path, [asdict(item) for item in events])
            else:
                self._save_facts(facts)
        self._validate_sources(facts, events, deleted)
        for item in pending["affected_facts"]:
            if item["status"] != "confirmed":
                self._delete_index(item["fact_id"])
        self._rebuild_projections_locked(facts)
        self.deletion_pending_path.unlink()
        fsync_directory(self.root)
        for name, identity in pending["publications"].items():
            self._deletion_target_path(name, identity["sha256"]).unlink(missing_ok=True)
        fsync_directory(self.root)
        return {"deleted": True, "affected_facts": pending["affected_facts"]}

    @staticmethod
    def _write_lines_atomic(path: Path, values: list[dict]) -> None:
        write_authoritative(path, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in values).encode("utf-8"))

    @staticmethod
    def _apply_policy(
        fact: ConfirmedFact,
        result: MemoryPolicyResult,
        explicit_user_request: bool,
    ) -> None:
        fact.scope = result.scope
        fact.subject = result.subject
        fact.utility_score = max(fact.utility_score, result.utility_score)
        fact.decision_reason = result.reason
        fact.sensitivity = result.sensitivity
        if fact.status in {"rejected", "forgotten", "superseded", "expired"}:
            return
        if explicit_user_request and result.decision == "confirm":
            fact.status = "confirmed"
        elif fact.status == "observed" and result.decision == "suggest":
            fact.status = "pending"

    def list_facts(
        self,
        *,
        statuses: set[str] | None = None,
        limit: int | None = None,
    ) -> list[ConfirmedFact]:
        with self._locked() as snapshot:
            facts = snapshot["facts"]
        if statuses is not None:
            facts = [fact for fact in facts if fact.status in statuses]
        facts.sort(
            key=lambda fact: (
                bool(fact.dream_run_id),
                fact.dream_score,
                fact.utility_score,
                fact.occurrence_count,
                fact.updated_at,
            ),
            reverse=True,
        )
        return facts[:limit] if limit is not None else facts

    def list_pending(self, *, limit: int = 5) -> list[ConfirmedFact]:
        """Return a bounded, de-duplicated suggestion queue."""

        return self.list_facts(statuses={"pending", "conflict"}, limit=max(0, min(limit, 5)))

    def correction_fingerprint(self) -> str:
        """Revision guard for async interpretations of mutable user decisions."""

        with self._locked() as snapshot:
            decisions = [
                (fact.id, fact.status, fact.updated_at, fact.superseded_by_id)
                for fact in snapshot["facts"]
                if fact.status in {"forgotten", "rejected", "superseded"}
            ]
            decisions.sort()
            return hashlib.sha256(
                json.dumps([decisions, snapshot["deleted_sources"]], ensure_ascii=False).encode("utf-8"),
            ).hexdigest()

    def get_fact(self, fact_id: str) -> ConfirmedFact | None:
        with self._locked() as snapshot:
            return next((fact for fact in snapshot["facts"] if fact.id == fact_id), None)

    def stage_for_review(
        self,
        fact_id: str,
        *,
        run_id: str,
        score: float,
        reason: str,
        source_event_ids: list[str],
    ) -> ConfirmedFact | None:
        """Annotate a grounded inference and make it reviewable, never confirmed."""

        with self._locked():
            facts = self._load_facts()
            for fact in facts:
                if fact.id != fact_id or fact.status not in {
                    "observed",
                    "pending",
                    "conflict",
                }:
                    continue
                requested = list(dict.fromkeys(source_event_ids))
                if not requested or any(event_id not in fact.event_ids for event_id in requested):
                    return None
                events = {event.id: event for event in self._load_events() if event.id in requested}
                if len(events) != len(requested):
                    return None
                if any(not self._is_verified_evidence_ref(fact, events[event_id]) for event_id in requested):
                    return None
                if fact.status == "observed":
                    fact.status = "pending"
                fact.dream_score = max(0.0, min(1.0, score))
                fact.dream_run_id = run_id
                fact.dream_reason = reason[:500]
                fact.dream_source_event_ids = list(
                    requested,
                )
                fact.last_verified_at = _now()
                fact.updated_at = fact.last_verified_at
                self._save_facts(facts)
                self._rebuild_projections_locked(facts)
                return fact
        return None

    @staticmethod
    def _is_verified_evidence_ref(fact: ConfirmedFact, event: MemoryEvent) -> bool:
        if event.source_role not in {"user", "manual", "reviewed_import"}:
            return False
        content_hash = hashlib.sha256(event.content.encode("utf-8")).hexdigest()
        for ref in fact.evidence_refs:
            if ref.get("event_id") != event.id or ref.get("support_status") not in {
                "supported", "user_confirmed", "explicit_request",
            }:
                continue
            if ref.get("content_hash") != content_hash:
                continue
            quote = str(ref.get("quote") or "")
            if quote:
                try:
                    start = int(ref.get("start_codepoint", -1))
                    end = int(ref.get("end_codepoint", -1))
                except (TypeError, ValueError):
                    continue
                if not (0 <= start < end <= len(event.content) and event.content[start:end] == quote):
                    continue
            return True
        return False

    def rollback_dream_stage(
        self,
        fact_id: str,
        *,
        run_id: str,
        before: dict,
        after: dict,
    ) -> ConfirmedFact | None:
        """Undo untouched Dreaming metadata without reverting user decisions."""

        previous_status = str(before.get("status") or "")
        if previous_status not in {"observed", "pending", "conflict"}:
            return None
        with self._locked():
            facts = self._load_facts()
            for fact in facts:
                if fact.id != fact_id:
                    continue
                if (
                    fact.status != str(after.get("status") or "")
                    or fact.status == "confirmed"
                    or fact.dream_run_id != run_id
                    or fact.content != str(after.get("content") or "")
                ):
                    return None
                fact.status = previous_status  # type: ignore[assignment]
                fact.dream_score = float(before.get("dream_score") or 0.0)
                fact.dream_run_id = str(before.get("dream_run_id") or "")
                fact.dream_reason = str(before.get("dream_reason") or "")
                fact.dream_source_event_ids = [
                    str(value)
                    for value in before.get("dream_source_event_ids", [])
                    if value
                ]
                fact.last_verified_at = str(before.get("last_verified_at") or "")
                fact.updated_at = _now()
                self._save_facts(facts)
                self._rebuild_projections_locked(facts)
                return fact
        return None

    def confirm(
        self, fact_id: str, *, scope: str | None = None, scope_id: str = "",
        required_event_ids: set[str] | None = None,
    ) -> ConfirmedFact | None:
        with self._locked():
            facts = self._load_facts()
            for fact in facts:
                if fact.id != fact_id or fact.status in {"forgotten", "rejected", "superseded"}:
                    continue
                if required_event_ids is not None:
                    if not required_event_ids.issubset(set(fact.event_ids)):
                        return None
                    live_event_ids = {event.id for event in self._load_events()}
                    supported_event_ids = {
                        str(ref.get("event_id")) for ref in fact.evidence_refs
                        if ref.get("support_status") in {
                            "supported", "user_confirmed", "explicit_request",
                        }
                    }
                    if not required_event_ids.issubset(live_event_ids & supported_event_ids):
                        return None
                fact.status = "confirmed"
                fact.confidence = max(fact.confidence, 0.95)
                if scope in {"thread", "project", "global"}:
                    fact.scope = scope
                if scope_id:
                    fact.scope_id = scope_id
                fact.updated_at = _now()
                self._save_facts(facts)
                self._rebuild_projections_locked(facts)
                return fact
        return None

    def reject(self, fact_id: str, *, reason: str = "用户忽略") -> ConfirmedFact | None:
        return self._transition(fact_id, "rejected", rejection_reason=reason)

    def supersede(self, fact_id: str, replacement_id: str) -> bool:
        with self._locked():
            facts = self._load_facts()
            old = next((fact for fact in facts if fact.id == fact_id), None)
            new = next((fact for fact in facts if fact.id == replacement_id), None)
            if old is None or new is None or old.id == new.id:
                return False
            old.status = "superseded"
            old.superseded_by_id = new.id
            old.updated_at = _now()
            new.supersedes_id = old.id
            new.updated_at = _now()
            self._delete_index(old.id)
            self._save_facts(facts)
            self._rebuild_projections_locked(facts)
            return True

    def expire_stale(self, *, days: int = 30, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(1, days))
        changed = 0
        with self._locked():
            facts = self._load_facts()
            for fact in facts:
                if fact.status not in {"observed", "pending"}:
                    continue
                try:
                    updated = datetime.fromisoformat(fact.updated_at)
                except ValueError:
                    continue
                if updated <= cutoff:
                    fact.status = "expired"
                    fact.expires_at = (now or datetime.now(timezone.utc)).isoformat()
                    fact.updated_at = fact.expires_at
                    changed += 1
                    self._delete_index(fact.id)
            if changed:
                self._save_facts(facts)
                self._rebuild_projections_locked(facts)
        return changed

    def forget(self, fact_id: str) -> bool:
        return self._transition(fact_id, "forgotten") is not None

    def _transition(
        self,
        fact_id: str,
        status: FactStatus,
        *,
        rejection_reason: str = "",
    ) -> ConfirmedFact | None:
        with self._locked():
            facts = self._load_facts()
            for fact in facts:
                if fact.id != fact_id or fact.status == status:
                    continue
                fact.status = status
                fact.rejection_reason = rejection_reason
                fact.updated_at = _now()
                self._delete_index(fact.id)
                self._save_facts(facts)
                self._rebuild_projections_locked(facts)
                return fact
        return None

    def link_legacy_entry(self, fact_id: str, entry_id: str) -> bool:
        return self._link_id(fact_id, entry_id, "legacy_entry_ids")

    def link_atom(self, fact_id: str, atom_id: str) -> bool:
        return self._link_id(fact_id, atom_id, "atom_ids")

    def _link_id(self, fact_id: str, value: str, field_name: str) -> bool:
        if not value:
            return False
        with self._locked():
            facts = self._load_facts()
            for fact in facts:
                if fact.id == fact_id:
                    values = getattr(fact, field_name)
                    if value not in values:
                        values.append(value)
                        fact.updated_at = _now()
                        self._save_facts(facts)
                    return True
        return False

    def forget_matching_content(self, content: str, *, legacy_entry_id: str = "") -> int:
        normalized = _normalize(content)
        if not normalized and not legacy_entry_id:
            return 0
        with self._locked():
            facts = self._load_facts()
            matches = [
                fact for fact in facts
                if (
                    _normalize(fact.content) == normalized
                    or legacy_entry_id in fact.legacy_entry_ids
                )
                and fact.status != "forgotten"
            ]
            for fact in matches:
                fact.status = "forgotten"
                fact.updated_at = _now()
                self._delete_index(fact.id)
            if matches:
                self._save_facts(facts)
                self._rebuild_projections_locked(facts)
            return len(matches)

    def rebuild_projections(self) -> list[dict[str, str]]:
        with self._locked():
            return self._rebuild_projections_locked(self._load_facts())

    def confirmed_facts(self) -> list[ConfirmedFact]:
        return self.list_facts(statuses={"confirmed"})

    def _rebuild_projections_locked(self, facts: list[ConfirmedFact]) -> list[dict[str, str]]:
        projections = [
            {
                "fact_id": fact.id,
                "type": fact.memory_type,
                "content": fact.content,
                "scope": fact.scope,
                "scope_id": fact.scope_id,
            }
            for fact in facts
            if fact.status == "confirmed"
        ]
        self._write_atomic(self.projections_path, {"version": 2, "facts": projections})
        return projections

    @staticmethod
    def _find_duplicate(
        facts: list[ConfirmedFact],
        content: str,
        memory_type: str,
    ) -> ConfirmedFact | None:
        normalized = _normalize(content)
        candidates = [
            fact for fact in facts
            if fact.status not in {"forgotten", "rejected", "superseded", "expired"}
            and fact.memory_type == memory_type
        ]
        for fact in candidates:
            existing = _normalize(fact.content)
            if existing == normalized:
                return fact
        return None

    def _delete_index(self, fact_id: str) -> None:
        if self.index_delete:
            self.index_delete(f"memory:{fact_id}")

    def _load_events(self) -> list[MemoryEvent]:
        return load_validated(self.events_path, self._decode_events, missing=list)

    @staticmethod
    def _decode_events(value: str) -> list[MemoryEvent]:
        events: list[MemoryEvent] = []
        seen: set[str] = set()
        for line in value.splitlines():
            if not line.strip():
                continue
            event = MemoryEvent(**record_payload(json.loads(line), MemoryEvent))
            if event.id in seen or (event.content_hash and event.content_hash != hashlib.sha256(event.content.encode("utf-8")).hexdigest()):
                raise ValueError("invalid event identity or content hash")
            seen.add(event.id)
            events.append(event)
        return events

    def _load_facts(self) -> list[ConfirmedFact]:
        return load_validated(self.facts_path, self._decode_facts, missing=list)

    @staticmethod
    def _decode_facts(value: str) -> list[ConfirmedFact]:
        raw = object_payload(value, versions={1, 2})
        if not isinstance(raw.get("facts"), list):
            raise ValueError("invalid facts collection")
        facts: list[ConfirmedFact] = []
        seen: set[str] = set()
        for item in raw["facts"]:
            if isinstance(item, dict) and item.get("status") == "active":
                item = {**item, "status": "confirmed"}
            fact = ConfirmedFact(**record_payload(item, ConfirmedFact))
            if fact.id in seen or not 0 <= fact.confidence <= 1:
                raise ValueError("invalid fact identity or confidence")
            seen.add(fact.id)
            facts.append(fact)
        return facts

    def recover_last_good(
        self, component: str, *, expected_sha256: str | None = None, expected_missing: bool = False,
    ) -> dict:
        """Explicit local recovery; never reinterpret events as new facts."""
        components = {
            "facts": (self.facts_path, self._decode_facts, self._load_facts),
            "events": (self.events_path, self._decode_events, self._load_events),
            "preferences": (self.preferences_path, self._decode_preferences, self._load_preferences),
            "deleted_sources": (self.deleted_sources_path, self._decode_deleted_sources, self._load_deleted_sources),
        }
        if component not in components:
            raise ValueError("unknown authoritative ledger component")
        with self._locked(validate=False):
            for name, (_, _, load) in components.items():
                if name != component:
                    load()
            path, decode, _ = components[component]
            def validate_candidate(value):
                candidate = decode(value)
                facts = candidate if component == "facts" else self._load_facts()
                events = candidate if component == "events" else self._load_events()
                deleted = candidate if component == "deleted_sources" else self._load_deleted_sources()
                self._validate_sources(facts, events, deleted)
                return candidate
            recover_last_good(path, validate_candidate, expected_sha256=expected_sha256, expected_missing=expected_missing)
            self._rebuild_projections_locked(self._load_facts())
        return {"recovered": True, "component": component}

    def _save_facts(self, facts: list[ConfirmedFact]) -> None:
        self._write_atomic(
            self.facts_path,
            {"version": 2, "facts": [asdict(fact) for fact in facts]},
        )

    @staticmethod
    def _write_atomic(path: Path, value: dict) -> None:
        encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        if path.name == "projections.json":
            atomic_write_bytes(path, encoded)
        else:
            write_authoritative(path, encoded)
