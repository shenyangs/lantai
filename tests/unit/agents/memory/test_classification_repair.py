import hashlib
from dataclasses import replace

import pytest

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.structured_user_memory import StructuredUserMemoryStore, UserMemoryEntry
from lantai.agents.memory.classification_repair import apply_classification_repair, rollback_classification_repair
from lantai.agents.memory.layered_memory import LayeredMemoryStore
from lantai.agents.memory import classification_repair


def fixture(tmp_path):
    text = "用户的长期偏好：用户担任编辑；Atlas 项目提供资料检索接口。"
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event(text, source_role="reviewed_import")
    fact = ledger.capture_fact(text, memory_type="style", event=event, explicit_user_request=True)
    store = StructuredUserMemoryStore(tmp_path)
    store.save_entries([UserMemoryEntry(id="entry", type="style", content=text, confirmed=True, canonical_fact_id=fact.id, source_kind="import")])
    ledger.link_legacy_entry(fact.id, "entry")
    pivot = text.index("Atlas")
    plan = {"version": 1, "batch_id": "classification-test", "repairs": [{"entry_id": "entry", "content_sha256": hashlib.sha256(text.encode()).hexdigest(), "parts": [
        {"start": len("用户的长期偏好："), "end": pivot, "type": "identity", "subject": "user", "scope": "global", "scope_id": ""},
        {"start": pivot, "end": len(text), "type": "identity", "subject": "project", "scope": "project", "scope_id": "atlas"},
    ]}]}
    return store, ledger, plan


def test_reviewed_split_preserves_history_and_is_idempotent(tmp_path):
    store, ledger, plan = fixture(tmp_path)
    result = apply_classification_repair(tmp_path, plan, apply=True, offline=True)
    assert result["created_entries"] == 2
    entries = store.load_entries()
    assert next(entry for entry in entries if entry.id == "entry").status == "archived"
    active = [entry for entry in entries if entry.status == "active"]
    assert {(entry.type, entry.subject, entry.scope) for entry in active} == {("identity", "user", "global"), ("identity", "project", "project")}
    assert "Atlas" not in store.read_profile_summary()["summary"]
    assert (tmp_path / "memory-classification-backups" / plan["batch_id"] / "user-memory" / "entries.json").exists()
    assert apply_classification_repair(tmp_path, plan, apply=True, offline=True)["already_applied"]


def test_stale_or_incomplete_plan_does_not_write(tmp_path):
    store, ledger, plan = fixture(tmp_path)
    before = store.entries_path.read_bytes()
    plan["repairs"][0]["parts"] = plan["repairs"][0]["parts"][:1]
    with pytest.raises(ValueError, match="uncovered"):
        apply_classification_repair(tmp_path, plan, apply=True, offline=True)
    assert store.entries_path.read_bytes() == before


def test_apply_requires_offline_and_preview_is_read_only(tmp_path):
    store, ledger, plan = fixture(tmp_path)
    before = store.entries_path.read_bytes()
    assert apply_classification_repair(tmp_path, plan)["created_entries"] == 2
    assert store.entries_path.read_bytes() == before
    with pytest.raises(ValueError, match="offline"):
        apply_classification_repair(tmp_path, plan, apply=True)


def test_failed_refresh_restores_original_bytes(tmp_path, monkeypatch):
    store, ledger, plan = fixture(tmp_path)
    before_entries = store.entries_path.read_bytes()
    before_facts = ledger.facts_path.read_bytes()
    def fail(*args, **kwargs):
        raise RuntimeError("injected cache failure")
    monkeypatch.setattr(LayeredMemoryStore, "refresh_scenes_and_persona", fail)
    with pytest.raises(RuntimeError, match="injected"):
        apply_classification_repair(tmp_path, plan, apply=True, offline=True)
    assert store.entries_path.read_bytes() == before_entries
    assert ledger.facts_path.read_bytes() == before_facts


def test_stale_hash_is_rejected_before_backup(tmp_path):
    store, ledger, plan = fixture(tmp_path)
    plan["repairs"][0]["content_sha256"] = "stale"
    with pytest.raises(ValueError, match="changed"):
        apply_classification_repair(tmp_path, plan, apply=True, offline=True)
    assert not (tmp_path / "memory-classification-backups").exists()


def test_shared_atom_reference_blocks_partial_repair(tmp_path):
    store, ledger, plan = fixture(tmp_path)
    fact = ledger.list_facts()[0]
    fact.atom_ids = ["shared"]
    ledger._save_facts([fact, replace(fact, id="other", content="其他有效事实", legacy_entry_ids=[])])
    before = ledger.facts_path.read_bytes()
    with pytest.raises(ValueError, match="shared"):
        apply_classification_repair(tmp_path, plan, apply=True, offline=True)
    assert ledger.facts_path.read_bytes() == before


def test_verified_rollback_restores_sources_and_refuses_newer_writes(tmp_path):
    store, ledger, plan = fixture(tmp_path)
    before = store.entries_path.read_bytes()
    apply_classification_repair(tmp_path, plan, apply=True, offline=True)
    assert rollback_classification_repair(tmp_path, plan["batch_id"])["status"] == "ready_to_rollback"
    rollback_classification_repair(tmp_path, plan["batch_id"], apply=True, offline=True)
    assert store.entries_path.read_bytes() == before
    plan["batch_id"] = "classification-second"
    apply_classification_repair(tmp_path, plan, apply=True, offline=True)
    store.add_entry("用户后来补充了新的事实。", "identity")
    with pytest.raises(ValueError, match="newer evidence"):
        rollback_classification_repair(tmp_path, plan["batch_id"], apply=True, offline=True)


def test_failed_rollback_restores_the_applied_state(tmp_path, monkeypatch):
    store, ledger, plan = fixture(tmp_path)
    apply_classification_repair(tmp_path, plan, apply=True, offline=True)
    applied_entries = store.entries_path.read_bytes()
    original_copy = classification_repair.shutil.copy2
    failed = False
    def fail_once(source, destination, *args, **kwargs):
        nonlocal failed
        if not failed and str(destination).endswith("user-memory/entries.json"):
            failed = True
            raise OSError("injected rollback failure")
        return original_copy(source, destination, *args, **kwargs)
    monkeypatch.setattr(classification_repair.shutil, "copy2", fail_once)
    with pytest.raises(OSError, match="injected"):
        rollback_classification_repair(tmp_path, plan["batch_id"], apply=True, offline=True)
    assert store.entries_path.read_bytes() == applied_entries
    assert rollback_classification_repair(tmp_path, plan["batch_id"])["status"] == "ready_to_rollback"
