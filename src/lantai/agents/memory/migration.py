# -*- coding: utf-8 -*-
"""One-time, reversible migration from permissive memory capture to governance v2."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical_ledger import CanonicalMemoryLedger
from .layered_memory import LayeredMemoryStore, now_iso
from .policy import MemoryPolicyEngine, is_explicitly_one_off_memory
from .structured_user_memory import StructuredUserMemoryStore
from ..persona.user_understanding import UserUnderstandingStore


MIGRATION_VERSION = "memory-governance-v2"
RECALL_SAFETY_MIGRATION_VERSION = "memory-recall-safety-v1"
SOURCE_GRADE_MIGRATION_VERSION = "memory-source-grades-v1"


def _normalize(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", (value or "").casefold(), flags=re.UNICODE)


def _load_existing_entries(store: StructuredUserMemoryStore):
    return store.load_entries() if store.entries_path.exists() else []


def migrate_memory_source_grades(
    working_dir: str | Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Correct legacy model summaries that were labeled as direct user evidence.

    The old memory_store path recorded its rewritten tool argument as a manual
    event. Older governance migrations did the same for accepted agent entries.
    Neither contains the user's original words. Preserve the accepted memory,
    while making its source grade and any downstream review metadata honest.
    """

    root = Path(working_dir)
    ledger = CanonicalMemoryLedger(root)
    marker_path = ledger.root / "source-grades-v1.json"
    with ledger._locked():  # pylint: disable=protected-access
        if apply and marker_path.exists():
            try:
                previous = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            if previous.get("version") == SOURCE_GRADE_MIGRATION_VERSION:
                return {**previous, "skipped": True, "reason": "already_applied"}

        events = ledger._load_events()  # pylint: disable=protected-access
        facts = ledger._load_facts()  # pylint: disable=protected-access
        entries = {
            entry.id: entry
            for entry in _load_existing_entries(StructuredUserMemoryStore(root))
        }
        expected_roles: dict[str, str] = {}
        corrected_events = []
        changed_events = 0
        for event in events:
            source_id = event.source_message_id
            expected_role = ""
            if source_id.startswith("memory-store:"):
                expected_role = "derived"
            elif source_id.startswith("migration:"):
                entry = entries.get(source_id.removeprefix("migration:"))
                expected_role = (
                    "manual" if entry is not None and entry.source == "manual"
                    else "historical_summary"
                )
            if expected_role:
                expected_roles[event.id] = expected_role
            if expected_role and event.source_role != expected_role:
                corrected_events.append(replace(event, source_role=expected_role))
                changed_events += 1
            else:
                corrected_events.append(event)

        changed_facts = 0
        for fact in facts:
            changed = False
            unverified_event_ids = {
                event_id for event_id in fact.event_ids
                if expected_roles.get(event_id) in {"derived", "historical_summary"}
            }
            for ref in fact.evidence_refs:
                role = expected_roles.get(str(ref.get("event_id") or ""))
                if role not in {"derived", "historical_summary"}:
                    continue
                grade = (
                    "derived_unverified" if role == "derived"
                    else "legacy_unverified"
                )
                if ref.get("source_role") != role or ref.get("support_status") != grade:
                    ref["source_role"] = role
                    ref["support_status"] = grade
                    changed = True
            if not fact.evidence_refs and unverified_event_ids and fact.support_status in {
                "supported", "user_confirmed", "explicit_request",
            }:
                fact.support_status = "legacy_unverified"
                changed = True
            if not changed:
                continue
            grades = {str(ref.get("support_status") or "") for ref in fact.evidence_refs}
            if fact.evidence_refs and fact.support_status not in grades:
                fact.support_status = next((
                    grade for grade in (
                        "supported", "user_confirmed", "explicit_request",
                        "derived_unverified", "legacy_unverified",
                    ) if grade in grades
                ), "legacy_unverified")
            if fact.support_status in {"derived_unverified", "legacy_unverified"}:
                fact.support_reason = ""
                fact.support_model = ""
                fact.support_reviewed_at = ""
            if any(event_id in unverified_event_ids for event_id in fact.dream_source_event_ids):
                fact.dream_score = 0.0
                fact.dream_run_id = ""
                fact.dream_reason = ""
                fact.dream_source_event_ids = []
                fact.last_verified_at = ""
            fact.updated_at = now_iso()
            changed_facts += 1

        report: dict[str, Any] = {
            "version": SOURCE_GRADE_MIGRATION_VERSION,
            "mode": "apply" if apply else "dry-run",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "events_regraded": changed_events,
            "facts_regraded": changed_facts,
            "backup_dir": "",
            "skipped": False,
        }
        if not apply:
            return report
        if changed_events or changed_facts:
            backup_dir = root / "memory-migration-backups" / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + f"-source-grades-{uuid.uuid4().hex[:8]}"
            )
            _copy_recall_safety_backup(
                root, backup_dir,
                [ledger.events_path, ledger.facts_path, ledger.projections_path],
            )
            report["backup_dir"] = str(backup_dir)
            if changed_events:
                ledger._write_lines_atomic(  # pylint: disable=protected-access
                    ledger.events_path, [asdict(event) for event in corrected_events],
                )
            if changed_facts:
                ledger._save_facts(facts)  # pylint: disable=protected-access
                ledger._rebuild_projections_locked(facts)  # pylint: disable=protected-access
        ledger._write_atomic(marker_path, report)  # pylint: disable=protected-access
        return report


def migrate_memory_governance(
    working_dir: str | Path,
    *,
    apply: bool = False,
    agent_id: str = "default",
) -> dict[str, Any]:
    """Plan or apply the v2 cleanup. Apply always creates a local backup first."""

    root = Path(working_dir)
    marker_path = root / "memory-ledger" / "migration-v2.json"
    if apply and marker_path.exists():
        try:
            previous = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if previous.get("version") == MIGRATION_VERSION:
            return {**previous, "skipped": True, "reason": "already_applied"}

    user_store = StructuredUserMemoryStore(root)
    layered = LayeredMemoryStore(root, agent_id=agent_id, user_memory_store=user_store)
    ledger = CanonicalMemoryLedger(root)
    policy = MemoryPolicyEngine()
    entries = _load_existing_entries(user_store)
    atoms = layered.load_atoms(include_archived=True)
    confirmed_norms = {
        _normalize(fact.content)
        for fact in ledger.confirmed_facts()
    }

    report: dict[str, Any] = {
        "version": MIGRATION_VERSION,
        "mode": "apply" if apply else "dry-run",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "structured": {
            "total": len(entries),
            "manual_preserved": 0,
            "confirmed_preserved": 0,
            "archived_role_or_sensitive": 0,
            "archived_one_off": 0,
            "archived_duplicate": 0,
            "suggestions_created": 0,
        },
        "atoms": {
            "total": len(atoms),
            "active": 0,
            "pending": 0,
            "observed": 0,
            "archived": 0,
        },
        "backup_dir": "",
        "skipped": False,
    }

    seen_confirmed: set[str] = set()
    suggestion_entries = []
    for entry in sorted(entries, key=lambda item: item.updated_at, reverse=True):
        norm = _normalize(entry.content)
        if entry.source == "manual":
            entry.confirmed = True
            report["structured"]["manual_preserved"] += 1
            if norm in seen_confirmed:
                entry.status = "archived"
                entry.archived_at = entry.archived_at or now_iso()
                report["structured"]["archived_duplicate"] += 1
            else:
                seen_confirmed.add(norm)
            continue
        if entry.confirmed and (
            entry.canonical_fact_id
            or norm in confirmed_norms
        ):
            report["structured"]["confirmed_preserved"] += 1
            seen_confirmed.add(norm)
            continue

        result = policy.evaluate(entry.content, memory_type=entry.type)
        entry.confirmed = False
        entry.status = "archived"
        entry.archived_at = entry.archived_at or now_iso()
        entry.evolution_status = "archived"
        if result.decision == "reject" or result.sensitivity == "sensitive":
            report["structured"]["archived_role_or_sensitive"] += 1
        elif result.decision == "suggest" and norm not in seen_confirmed:
            suggestion_entries.append((entry, result))
            seen_confirmed.add(norm)
            report["structured"]["suggestions_created"] += 1
        else:
            report["structured"]["archived_one_off"] += 1

    atom_actions: list[tuple[Any, str, Any]] = []
    for atom in atoms:
        norm = _normalize(atom.content)
        if norm in confirmed_norms:
            status = "active"
            result = policy.evaluate(
                atom.content,
                memory_type=atom.type,
                explicit_user_request=True,
            )
        else:
            result = policy.evaluate(atom.content, memory_type=atom.type)
            status = {
                "confirm": "active",
                "suggest": "pending",
                "observe": "observed",
                "reject": "archived",
            }[result.decision]
            if atom.type == "episodic" and status == "active":
                status = "observed"
        atom_actions.append((atom, status, result))
        report["atoms"][status] += 1

    if not apply:
        return report

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = root / "memory-migration-backups" / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in (
        user_store.entries_path,
        user_store.summary_path,
        layered.atoms_path,
        layered.persona_path,
        ledger.events_path,
        ledger.facts_path,
        ledger.projections_path,
    ):
        if path.exists() and path.is_file():
            relative = path.relative_to(root)
            destination = backup_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    report["backup_dir"] = str(backup_dir)

    # Preserve accepted entries, while keeping the provenance grade honest.
    for entry in entries:
        if entry.status != "active" or not entry.confirmed:
            continue
        event = ledger.record_event(
            entry.content,
            source_message_id=f"migration:{entry.id}",
            source_role="manual" if entry.source == "manual" else "historical_summary",
            event_id=f"event_migration_{entry.id}",
        )
        if event is None:
            continue
        fact = ledger.capture_fact(
            entry.content,
            memory_type=entry.type,
            event=event,
            explicit_user_request=True,
            confidence=max(entry.confidence, 0.95),
        )
        if fact is not None:
            ledger.link_legacy_entry(fact.id, entry.id)
            entry.canonical_fact_id = fact.id

    # Valuable automatic memories become suggestions, never active projections.
    for entry, result in suggestion_entries:
        event = ledger.record_event(
            entry.content,
            source_message_id=f"migration-suggestion:{entry.id}",
            source_role="unknown",
            event_id=f"event_migration_suggestion_{entry.id}",
        )
        if event is not None:
            ledger.capture_fact(
                entry.content,
                memory_type=entry.type,
                event=event,
                confidence=entry.confidence,
                policy_result=result,
            )

    for atom, status, result in atom_actions:
        atom.status = status
        atom.metadata.update(
            {
                "scope": result.scope,
                "subject": result.subject,
                "utility_score": result.utility_score,
                "decision_reason": result.reason,
                "sensitivity": result.sensitivity,
                "migrated_by": MIGRATION_VERSION,
            },
        )
        atom.updated_at = now_iso()

    user_store.save_entries(entries)
    layered.save_atoms(atoms)
    layered.refresh_scenes_and_persona()
    layered.system2_consolidate(reason="memory-governance-migration", force=True)
    ledger.rebuild_projections()
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _legacy_thread_scope_id(prefix: str, record_id: str, candidates: list[str]) -> str:
    source = next((str(item).strip() for item in candidates if str(item).strip()), "")
    return f"legacy-thread:{source or prefix + ':' + record_id}"[:240]


def _copy_recall_safety_backup(root: Path, backup_dir: Path, paths: list[Path]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in paths:
        if not path.exists() or path.is_symlink():
            continue
        target = backup_dir / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)


def migrate_memory_recall_safety(
    working_dir: str | Path,
    *,
    apply: bool = False,
    agent_id: str = "default",
) -> dict[str, Any]:
    """Quarantine legacy one-case memories without deleting user knowledge.

    Older releases persisted confirmed entries without an applicability
    contract.  Their missing scope was read back as global and derived user
    projections could keep that interpretation indefinitely.  This migration
    preserves confirmation and content while assigning clearly one-off items
    to an isolated historical thread, then rebuilds every derived projection.
    """

    root = Path(working_dir)
    marker_path = root / "memory-ledger" / "recall-safety-v1.json"
    if apply and marker_path.exists():
        try:
            previous = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if previous.get("version") == RECALL_SAFETY_MIGRATION_VERSION:
            return {**previous, "skipped": True, "reason": "already_applied"}

    user_store = StructuredUserMemoryStore(root)
    ledger = CanonicalMemoryLedger(root)
    layered = LayeredMemoryStore(root, agent_id=agent_id, user_memory_store=user_store)
    understanding_store = UserUnderstandingStore(root)

    entries = _load_existing_entries(user_store)
    facts = ledger.list_facts()
    atoms = layered.load_atoms(include_archived=True)
    understandings = understanding_store.list_atoms()
    entry_repairs: list[Any] = []
    fact_repairs: list[Any] = []
    atom_repairs: list[Any] = []
    understanding_repairs: list[Any] = []
    normalized_understanding_scopes = 0

    for entry in entries:
        if (
            entry.status == "active"
            and entry.confirmed
            and entry.scope != "thread"
            and is_explicitly_one_off_memory(entry.content)
        ):
            entry.scope = "thread"
            entry.scope_id = _legacy_thread_scope_id(
                "entry",
                entry.id,
                [entry.source_session_id, entry.source_message_id],
            )
            entry.updated_at = now_iso()
            entry_repairs.append(entry)

    for fact in facts:
        if (
            fact.status == "confirmed"
            and fact.scope != "thread"
            and is_explicitly_one_off_memory(fact.content)
        ):
            fact.scope = "thread"
            fact.scope_id = _legacy_thread_scope_id(
                "fact",
                fact.id,
                list(fact.session_ids),
            )
            fact.updated_at = now_iso()
            fact_repairs.append(fact)

    for atom in atoms:
        scope = str(atom.metadata.get("scope") or "global")
        if (
            atom.status == "active"
            and scope != "thread"
            and is_explicitly_one_off_memory(atom.content)
        ):
            atom.metadata["scope"] = "thread"
            atom.metadata["scope_id"] = _legacy_thread_scope_id(
                "atom",
                atom.id,
                list(atom.source_record_ids) + list(atom.source_message_ids),
            )
            atom.metadata["recall_safety_migration"] = RECALL_SAFETY_MIGRATION_VERSION
            atom.updated_at = now_iso()
            atom_repairs.append(atom)

    active_understanding_statuses = {
        "observed",
        "working_hypothesis",
        "confirmed",
    }
    for atom in understandings:
        if atom.scope in {"profile", "workflow", "principle", "intention"}:
            atom.scope = "global"
            atom.scope_id = ""
            normalized_understanding_scopes += 1
        if (
            atom.status in active_understanding_statuses
            and atom.scope != "thread"
            and is_explicitly_one_off_memory(atom.statement)
        ):
            atom.scope = "thread"
            atom.scope_id = _legacy_thread_scope_id(
                "understanding",
                atom.id,
                list(atom.evidence_ids),
            )
            atom.updated_at = now_iso()
            understanding_repairs.append(atom)

    repair_count = (
        len(entry_repairs)
        + len(fact_repairs)
        + len(atom_repairs)
        + len(understanding_repairs)
        + normalized_understanding_scopes
    )
    report: dict[str, Any] = {
        "version": RECALL_SAFETY_MIGRATION_VERSION,
        "mode": "apply" if apply else "dry-run",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "structured_rescoped": len(entry_repairs),
        "canonical_rescoped": len(fact_repairs),
        "layered_rescoped": len(atom_repairs),
        "understanding_rescoped": len(understanding_repairs),
        "understanding_scopes_normalized": normalized_understanding_scopes,
        "total_repairs": repair_count,
        "content_deleted": 0,
        "confirmations_revoked": 0,
        "backup_dir": "",
        "skipped": False,
    }
    if not apply:
        return report

    if repair_count:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = root / "memory-migration-backups" / (
            f"{timestamp}-recall-safety-{uuid.uuid4().hex[:8]}"
        )
        _copy_recall_safety_backup(
            root,
            backup_dir,
            [
                user_store.entries_path,
                ledger.facts_path,
                ledger.projections_path,
                layered.atoms_path,
                layered.scenes_dir,
                layered.system2_dir,
                layered.persona_path,
                understanding_store.path,
            ],
        )
        report["backup_dir"] = str(backup_dir)

        user_store.save_entries(entries)
        # The migration owns the same workspace lock domain as the ledger and
        # rewrites the complete fact set once so projections cannot observe a
        # half-rescoped state.
        with ledger._locked():  # pylint: disable=protected-access
            ledger._save_facts(facts)  # pylint: disable=protected-access
            ledger._rebuild_projections_locked(facts)  # pylint: disable=protected-access
        layered.save_atoms(atoms)
        understanding_store.replace_atoms(understandings)
        layered.refresh_scenes_and_persona()
        layered.system2_consolidate(
            reason="memory-recall-safety-migration",
            force=True,
        )

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report
