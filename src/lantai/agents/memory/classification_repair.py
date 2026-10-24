"""Offline application of reviewed memory classification spans.

This is not a classifier: a reviewer supplies the subject/type/scope and exact
source spans. It never asks a model to rewrite facts, accepts no replacement
prose, retains superseded sources, and rejects stale or incomplete plans.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import os
import uuid
from typing import Any

from .canonical_ledger import CanonicalMemoryLedger
from .layered_memory import LayeredMemoryStore
from .structured_user_memory import StructuredUserMemoryStore, TYPE_LAYER, now_iso


_DIRECTORIES = ("user-memory", "memory-ledger", "layered-memory", "user-understanding")
_WRAPPERS = ("用户的长期偏好：", "用户的长期偏好:")


def _read(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else deepcopy(default)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _restore_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.restore")
    temporary.write_bytes(data)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _files(root: Path) -> dict[Path, bytes]:
    result = {}
    for name in _DIRECTORIES:
        directory = root / name
        if directory.is_symlink():
            raise ValueError("Memory directories must not be symlinks")
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("Memory source symlinks are not supported")
            if path.is_file() and not path.name.endswith(".lock"):
                result[path.relative_to(root)] = path.read_bytes()
    return result


def _prepare(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    entries_payload = _read(root / "user-memory" / "entries.json", {})
    facts_payload = _read(root / "memory-ledger" / "facts.json", {})
    entries = entries_payload.get("entries", [])
    facts = facts_payload.get("facts", [])
    by_entry = {item["id"]: item for item in entries}
    by_fact = {item["id"]: item for item in facts}
    operations = plan.get("repairs", [])
    if not operations and not plan.get("duplicates"):
        raise ValueError("A repair plan must contain reviewed source entries")
    used = set()
    retired_sources = set()
    retired_atoms = set()
    added_entries = []
    added_facts = []
    now = now_iso()
    for operation in operations:
        entry_id = str(operation.get("entry_id") or "")
        if entry_id in used:
            raise ValueError("Duplicate source entry in repair plan")
        used.add(entry_id)
        source = by_entry.get(entry_id)
        if source is None or source.get("status") != "active" or not source.get("confirmed"):
            raise ValueError("Repair source is not an active confirmed entry")
        content = str(source.get("content") or "")
        if _hash(content) != operation.get("content_sha256"):
            raise ValueError("Repair source changed; review a fresh plan")
        fact = by_fact.get(source.get("canonical_fact_id"))
        if fact is None or fact.get("status") != "confirmed":
            raise ValueError("Repair requires a confirmed canonical source")
        normalize = lambda value: re.sub(r"\s+", " ", str(value)).strip().rstrip("。")
        if normalize(fact.get("content", "")) != normalize(content):
            raise ValueError("Canonical and compatibility content differ; review provenance first")
        if fact["id"] in retired_sources:
            raise ValueError("One plan cannot repair the same canonical fact twice")
        coverage = [False] * len(content)
        for wrapper in _WRAPPERS:
            if content.startswith(wrapper):
                coverage[:len(wrapper)] = [True] * len(wrapper)
        parts = operation.get("parts", [])
        if not 1 <= len(parts) <= 30:
            raise ValueError("Each source needs between 1 and 30 reviewed parts")
        if str(source.get("reason") or "").startswith("用户纠正主题:") and len(parts) != 1:
            raise ValueError("A topic correction cannot be split into competing corrections")
        successors = []
        for index, part in enumerate(parts):
            start, end = part.get("start"), part.get("end")
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(content):
                raise ValueError("Invalid source span")
            if any(coverage[start:end]):
                raise ValueError("Overlapping source spans")
            coverage[start:end] = [True] * (end - start)
            fragment = content[start:end].strip().strip("；;，,\n ")
            if not fragment:
                raise ValueError("Empty source span")
            memory_type, subject, scope = part.get("type"), part.get("subject"), part.get("scope")
            if memory_type not in TYPE_LAYER or subject not in {"user", "project", "third_party", "unknown"} or scope not in {"global", "project", "thread"}:
                raise ValueError("Invalid reviewed classification")
            if subject != "user" and scope == "global":
                raise ValueError("Non-personal repaired facts require a bounded scope")
            scope_id = str(part.get("scope_id") or "")
            if scope != "global" and not scope_id:
                raise ValueError("Scoped repaired facts require a scope identifier")
            suffix = _hash(f"{plan['batch_id']}:{entry_id}:{index}")[:24]
            new_entry_id, new_fact_id = f"repair_{suffix}", f"fact_repair_{suffix}"
            if new_entry_id in by_entry or new_fact_id in by_fact:
                raise ValueError("Repair successor already exists without a completed batch")
            new_entry = {**source, "id": new_entry_id, "content": fragment,
                         "type": memory_type, "subject": subject, "scope": scope, "scope_id": scope_id,
                         "canonical_fact_id": new_fact_id, "canonical_atom_ids": [],
                         "memory_layer": TYPE_LAYER[memory_type], "status": "active",
                         "updated_at": now, "supersedes_id": entry_id, "superseded_by_id": "",
                         "evolution_status": "current", "archived_at": "", "valid_to": "",
                         "related_ids": [entry_id], "evolution_reason": "已复核分类；原始内容保留在历史中"}
            new_fact = {**fact, "id": new_fact_id, "content": fragment,
                        "memory_type": memory_type, "subject": subject, "scope": scope, "scope_id": scope_id,
                        "status": "confirmed", "updated_at": now, "atom_ids": [],
                        "legacy_entry_ids": [new_entry_id], "supersedes_id": fact["id"], "superseded_by_id": "",
                        "decision_reason": "对已确认来源做分类修复，不新增事实"}
            added_entries.append(new_entry)
            added_facts.append(new_fact)
            successors.append((new_entry_id, new_fact_id))
        if any(not covered and not (char.isspace() or char in "；;。-•") for char, covered in zip(content, coverage)):
            raise ValueError("Repair plan has uncovered source content")
        for linked in entries:
            if linked.get("canonical_fact_id") != fact["id"] or linked.get("status") == "archived":
                continue
            if normalize(linked.get("content", "")) != normalize(content):
                raise ValueError("Linked projection has distinct content; review provenance first")
            linked.update(status="archived", archived_at=now, updated_at=now,
                          evolution_status="superseded", superseded_by_id=successors[0][0],
                          related_ids=[value[0] for value in successors], valid_to=now,
                          evolution_reason="分类修复已生成可独立理解的条目，原文保留")
            retired_sources.add(linked["id"])
        fact.update(status="superseded", superseded_by_id=successors[0][1], updated_at=now)
        retired_sources.add(fact["id"])
        retired_atoms.update(fact.get("atom_ids", []))
    entries_payload["entries"] = entries + added_entries
    facts_payload["facts"] = facts + added_facts
    by_entry.update({item["id"]: item for item in added_entries})
    by_fact.update({item["id"]: item for item in added_facts})
    for operation in plan.get("duplicates", []):
        source = by_entry.get(str(operation.get("entry_id") or ""))
        target = by_entry.get(str(operation.get("keep_entry_id") or ""))
        if source is None or target is None or source["id"] == target["id"]:
            raise ValueError("Invalid reviewed duplicate pair")
        if source.get("status") != "active" or target.get("status") != "active":
            raise ValueError("Duplicate sources must still be active")
        if _hash(str(source.get("content") or "")) != operation.get("content_sha256") or _hash(str(target.get("content") or "")) != operation.get("keep_content_sha256"):
            raise ValueError("Duplicate source changed; review a fresh plan")
        source_fact = by_fact.get(source.get("canonical_fact_id"))
        target_fact = by_fact.get(target.get("canonical_fact_id"))
        if not source_fact or not target_fact or source_fact.get("status") != "confirmed" or target_fact.get("status") != "confirmed":
            raise ValueError("Duplicates need confirmed canonical evidence")
        if (source_fact.get("subject"), source_fact.get("scope"), source_fact.get("scope_id")) != (target_fact.get("subject"), target_fact.get("scope"), target_fact.get("scope_id")):
            raise ValueError("Duplicate repair cannot cross ownership boundaries")
        source.update(status="archived", archived_at=now, updated_at=now, evolution_status="superseded",
                      superseded_by_id=target["id"], related_ids=[target["id"]], valid_to=now,
                      evolution_reason="复核为重复表述，原文保留在历史中")
        retired_sources.add(source["id"])
        if source_fact["id"] != target_fact["id"]:
            remaining = [entry for entry in entries_payload["entries"] if entry.get("canonical_fact_id") == source_fact["id"] and entry.get("status") == "active"]
            if remaining:
                raise ValueError("Duplicate canonical source has other active projections")
            source_fact.update(status="superseded", superseded_by_id=target_fact["id"], updated_at=now)
            target_fact["event_ids"] = list(dict.fromkeys([*target_fact.get("event_ids", []), *source_fact.get("event_ids", [])]))
            target_fact["session_ids"] = list(dict.fromkeys([*target_fact.get("session_ids", []), *source_fact.get("session_ids", [])]))
            target_fact["occurrence_count"] = max(int(target_fact.get("occurrence_count") or 1), len(target_fact["event_ids"]))
            retired_sources.add(source_fact["id"])
            retired_atoms.update(source_fact.get("atom_ids", []))
    for fact in facts_payload["facts"]:
        if fact["id"] not in retired_sources and fact.get("status") not in {"superseded", "forgotten", "expired", "rejected"} and set(fact.get("atom_ids", [])) & retired_atoms:
            raise ValueError("A retired atom is shared with another current canonical fact; review associations first")
    return {"entries": entries_payload, "facts": facts_payload,
            "retired_sources": retired_sources, "retired_atoms": retired_atoms,
            "created_entries": len(added_entries), "repaired_sources": len(operations) + len(plan.get("duplicates", []))}


def apply_classification_repair(
    working_dir: str | Path, plan: dict[str, Any], *, apply: bool = False,
    offline: bool = False, agent_id: str = "default",
) -> dict[str, Any]:
    root = Path(working_dir).resolve()
    for name in _DIRECTORIES:
        if (root / name).is_symlink():
            raise ValueError("Memory directories must not be symlinks")
    for name in ("user-memory/entries.json", "memory-ledger/facts.json"):
        if (root / name).is_symlink():
            raise ValueError("Memory source symlinks are not supported")
    batch_id = str(plan.get("batch_id") or "")
    if plan.get("version") != 1 or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", batch_id):
        raise ValueError("Invalid repair plan identity")
    if apply and not offline:
        raise ValueError("Applying classification repair requires an offline, quiesced workspace")
    backup = root / "memory-classification-backups" / batch_id
    if backup.is_symlink() or backup.parent.is_symlink():
        raise ValueError("Backup locations must not be symlinks")
    marker = backup / "repair.json"
    plan_hash = _hash(json.dumps(plan, sort_keys=True, ensure_ascii=False))
    if marker.exists():
        previous = _read(marker, {})
        if previous.get("status") == "applied" and previous.get("plan_hash") == plan_hash:
            return {**previous, "already_applied": True}
        raise ValueError("Batch identity was already used; inspect the preserved backup")
    prepared = _prepare(root, plan)
    result = {"created_entries": prepared["created_entries"], "repaired_sources": prepared["repaired_sources"],
              "dry_run": not apply, "already_applied": False, "plan_hash": plan_hash,
              "backup_dir": str(backup), "requires_runtime_reload": True}
    if not apply:
        return result
    store, ledger = StructuredUserMemoryStore(root), CanonicalMemoryLedger(root)
    layered = LayeredMemoryStore(root, agent_id, store)
    before = None
    sources_changed = False
    try:
        with ledger._locked(), store._locked(), layered._locked():
            prepared = _prepare(root, plan)
            before = _files(root)
            backup.mkdir(parents=True, mode=0o700)
            for relative in before:
                target = backup / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / relative, target)
            sources_changed = True
            store._write_json_atomic(store.entries_path, prepared["entries"])
            ledger._write_atomic(ledger.facts_path, prepared["facts"])
            atoms_payload = _read(layered.atoms_path, {"atoms": []})
            for atom in atoms_payload.get("atoms", []):
                if atom.get("id") in prepared["retired_atoms"]:
                    atom.update(status="archived", updated_at=now_iso())
            store._write_json_atomic(layered.atoms_path, atoms_payload)
            understanding_path = root / "user-understanding" / "projection.json"
            if understanding_path.exists():
                understanding = _read(understanding_path, {})
                for atom in understanding.get("atoms", []):
                    if set(atom.get("evidence_ids", [])) & prepared["retired_sources"]:
                        atom.update(status="superseded", updated_at=now_iso())
                store._write_json_atomic(understanding_path, understanding)
        ledger.rebuild_projections()
        store._write_profile_summary(store.load_entries())
        layered.refresh_scenes_and_persona()
        layered.system2_consolidate(force=True, reason="reviewed-classification-repair")
        result.update(status="applied", dry_run=False,
                      backup_hashes={str(path): hashlib.sha256(data).hexdigest() for path, data in before.items()},
                      applied_hashes={str(path): hashlib.sha256(data).hexdigest() for path, data in _files(root).items()})
        store._write_json_atomic(marker, result)
    except Exception:
        # Only files created by this offline operation are removed. Every
        # pre-existing byte is restored; the recovery copy remains available.
        if before is not None and sources_changed:
            for relative in _files(root).keys() - before.keys():
                (root / relative).unlink()
            for relative in before:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup / relative, path)
        raise
    return result


def rollback_classification_repair(
    working_dir: str | Path, batch_id: str, *, apply: bool = False, offline: bool = False,
) -> dict[str, Any]:
    """Restore a verified backup only if no subsequent memory write occurred."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", batch_id):
        raise ValueError("Invalid repair batch identity")
    if apply and not offline:
        raise ValueError("Rollback requires an offline, quiesced workspace")
    root = Path(working_dir).resolve()
    backup = root / "memory-classification-backups" / batch_id
    if backup.is_symlink() or backup.parent.is_symlink():
        raise ValueError("Backup locations must not be symlinks")
    marker_path = backup / "repair.json"
    marker = _read(marker_path, {})
    if marker.get("status") == "rolled_back":
        return {"status": "rolled_back", "already_rolled_back": True, "dry_run": not apply}
    if marker.get("status") != "applied" or not marker.get("applied_hashes"):
        raise ValueError("No verifiable applied repair exists for this batch")
    current, original = _files(root), _files(backup)
    hashes = lambda files: {str(path): hashlib.sha256(data).hexdigest() for path, data in files.items()}
    if hashes(current) != marker["applied_hashes"]:
        raise ValueError("Memory changed after repair; rollback would overwrite newer evidence")
    if hashes(original) != marker.get("backup_hashes"):
        raise ValueError("Backup integrity check failed")
    result = {"status": "ready_to_rollback", "dry_run": not apply, "restored_files": len(original)}
    if not apply:
        return result
    store, ledger = StructuredUserMemoryStore(root), CanonicalMemoryLedger(root)
    layered = LayeredMemoryStore(root, "default", store)
    with ledger._locked(), store._locked(), layered._locked():
        if hashes(_files(root)) != marker["applied_hashes"]:
            raise ValueError("Memory changed while preparing rollback")
        modes = {relative: (root / relative).stat().st_mode & 0o777 for relative in current}
        marker_bytes = marker_path.read_bytes()
        try:
            for relative in current.keys() - original.keys():
                (root / relative).unlink()
            for relative in original:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup / relative, path)
            marker.update(status="rolled_back", rolled_back_at=now_iso())
            store._write_json_atomic(marker_path, marker)
        except Exception:
            for relative in _files(root).keys() - current.keys():
                (root / relative).unlink()
            for relative, data in current.items():
                _restore_bytes(root / relative, data, modes[relative])
            _restore_bytes(marker_path, marker_bytes, 0o600)
            raise
    return {**result, "status": "rolled_back", "dry_run": False}
