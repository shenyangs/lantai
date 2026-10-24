# -*- coding: utf-8 -*-

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.layered_memory import AtomCandidate, LayeredMemoryStore
from lantai.agents.memory.migration import migrate_memory_governance
from lantai.agents.memory.migration import migrate_memory_recall_safety
from lantai.agents.memory.migration import migrate_memory_source_grades
from lantai.agents.memory.policy import MemoryPolicyResult
from lantai.agents.memory.structured_user_memory import StructuredUserMemoryStore
from lantai.agents.persona.user_understanding import (
    UnderstandingAtom,
    UserUnderstandingStore,
)


def _seed_legacy_memory(tmp_path):
    store = StructuredUserMemoryStore(tmp_path)
    manual = store.add_entry(
        "默认用 pytest 跑单测",
        memory_type="workflow",
        source="manual",
    )["entry"]
    role = store.add_entry(
        "请你作为一名专业取名顾问",
        memory_type="identity",
        source="auto",
        confidence=0.68,
    )["entry"]
    sensitive = store.add_entry(
        "父亲出生地点是北京，母亲出生时间是凌晨",
        memory_type="identity",
        source="auto",
        confidence=0.68,
    )["entry"]
    one_off = store.add_entry(
        "这次请生成一份日报模板",
        memory_type="instruction",
        source="auto",
        confidence=0.7,
    )["entry"]
    suggestion = store.add_entry(
        "以后回答默认更自然、克制",
        memory_type="style",
        source="auto",
        confidence=0.8,
    )["entry"]
    layered = LayeredMemoryStore(tmp_path, "agent", store)
    atoms = layered.ingest_atom_candidates(
        [
            AtomCandidate(type="identity", content="你是一名美股日报分析师"),
            AtomCandidate(type="style", content="以后回答默认更自然、克制"),
        ],
    )
    for atom in atoms:
        atom.status = "active"  # Simulate the permissive v1 format.
    layered.save_atoms(atoms)
    return {
        "manual": manual["id"],
        "role": role["id"],
        "sensitive": sensitive["id"],
        "one_off": one_off["id"],
        "suggestion": suggestion["id"],
    }


def test_migration_dry_run_does_not_mutate_files(tmp_path):
    _seed_legacy_memory(tmp_path)
    entries_path = tmp_path / "user-memory" / "entries.json"
    before = entries_path.read_bytes()

    report = migrate_memory_governance(tmp_path, apply=False, agent_id="agent")

    assert report["mode"] == "dry-run"
    assert report["structured"]["manual_preserved"] == 1
    assert report["structured"]["archived_role_or_sensitive"] >= 2
    assert entries_path.read_bytes() == before
    assert not (tmp_path / "memory-ledger" / "migration-v2.json").exists()


def test_migration_apply_is_backed_up_confirmed_only_and_idempotent(tmp_path):
    ids = _seed_legacy_memory(tmp_path)

    report = migrate_memory_governance(tmp_path, apply=True, agent_id="agent")

    assert report["backup_dir"]
    assert (tmp_path / "memory-migration-backups").exists()
    entries = {
        entry.id: entry
        for entry in StructuredUserMemoryStore(tmp_path).load_entries()
    }
    assert entries[ids["manual"]].status == "active"
    assert entries[ids["manual"]].confirmed is True
    assert entries[ids["role"]].status == "archived"
    assert entries[ids["sensitive"]].status == "archived"
    assert entries[ids["one_off"]].status == "archived"
    assert entries[ids["suggestion"]].status == "archived"

    ledger = CanonicalMemoryLedger(tmp_path)
    assert [fact.content for fact in ledger.confirmed_facts()] == [
        "默认用 pytest 跑单测。",
    ]
    assert any("自然、克制" in fact.content for fact in ledger.list_pending())
    assert all(atom.status != "active" for atom in LayeredMemoryStore(
        tmp_path,
        "agent",
        StructuredUserMemoryStore(tmp_path),
    ).load_atoms())

    second = migrate_memory_governance(tmp_path, apply=True, agent_id="agent")
    assert second["skipped"] is True
    assert second["reason"] == "already_applied"


def test_migration_labels_accepted_agent_memory_as_historical_summary(tmp_path):
    store = StructuredUserMemoryStore(tmp_path)
    store.add_entry(
        "以后回答默认用中文", memory_type="style", source="agent",
    )["entry"]
    entries = store.load_entries()
    entries[0].confirmed = True
    entries[0].canonical_fact_id = "pre_v2_fact"
    store.save_entries(entries)

    migrate_memory_governance(tmp_path, apply=True, agent_id="agent")

    ledger = CanonicalMemoryLedger(tmp_path)
    facts = ledger.confirmed_facts()
    assert len(facts) == 1
    sources = ledger.open_fact_sources(facts[0].id)["sources"]
    assert sources[0]["source_role"] == "historical_summary"
    assert sources[0]["support_status"] == "legacy_unverified"
    assert sources[0]["status"] == "source_only"


def test_source_grade_migration_repairs_legacy_model_and_summary_evidence(tmp_path):
    store = StructuredUserMemoryStore(tmp_path)
    accepted = store.add_entry(
        "以后回答默认用中文", memory_type="style", source="agent",
    )["entry"]
    ledger = CanonicalMemoryLedger(tmp_path)
    model_event = ledger.record_event(
        "用户喜欢简洁回答", source_message_id="memory-store:old-call",
        source_role="manual", event_id="event_old_model_memory",
    )
    summary_event = ledger.record_event(
        accepted["content"], source_message_id=f"migration:{accepted['id']}",
        source_role="manual", event_id=f"event_migration_{accepted['id']}",
    )
    real_manual_event = ledger.record_event(
        "默认用 pytest", source_message_id="memory-ui:1", source_role="manual",
    )
    assert model_event and summary_event and real_manual_event
    model_fact = ledger.capture_fact(
        model_event.content, memory_type="style", event=model_event,
        explicit_user_request=True,
    )
    summary_fact = ledger.capture_fact(
        summary_event.content, memory_type="style", event=summary_event,
        explicit_user_request=True,
    )
    manual_fact = ledger.capture_fact(
        real_manual_event.content, memory_type="workflow", event=real_manual_event,
        explicit_user_request=True,
    )
    assert model_fact and summary_fact and manual_fact
    assert ledger.open_fact_sources(model_fact.id)["sources"][0]["support_status"] == "user_confirmed"

    dry_run = migrate_memory_source_grades(tmp_path)
    assert dry_run["events_regraded"] == 2
    assert ledger.open_fact_sources(model_fact.id)["sources"][0]["source_role"] == "manual"

    report = migrate_memory_source_grades(tmp_path, apply=True)
    assert report["backup_dir"]
    assert report["facts_regraded"] == 2
    model_source = ledger.open_fact_sources(model_fact.id)["sources"][0]
    assert model_source["source_role"] == "derived"
    assert model_source["support_status"] == "derived_unverified"
    assert ledger.get_fact(model_fact.id).support_status == "derived_unverified"
    summary_source = ledger.open_fact_sources(summary_fact.id)["sources"][0]
    assert summary_source["source_role"] == "historical_summary"
    assert summary_source["support_status"] == "legacy_unverified"
    assert ledger.get_fact(summary_fact.id).support_status == "legacy_unverified"
    assert ledger.open_fact_sources(manual_fact.id)["sources"][0]["support_status"] == "user_confirmed"
    assert migrate_memory_source_grades(tmp_path, apply=True)["skipped"] is True


def test_recall_safety_migration_quarantines_legacy_one_case_confirmation(
    tmp_path,
):
    statement = "用户对当前输出极度不满意，需要以更高标准重新做。"
    store = StructuredUserMemoryStore(tmp_path)
    legacy_entry = store.add_entry(
        statement,
        memory_type="style",
        source="manual",
    )["entry"]
    durable_entry = store.add_entry(
        "用户负责 WPS 的公关工作。",
        memory_type="identity",
        source="manual",
    )["entry"]
    entries = store.load_entries()
    for entry in entries:
        if entry.id == legacy_entry["id"]:
            entry.scope = "global"  # Simulate a pre-scope release.
            entry.scope_id = ""
    store.save_entries(entries)

    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event(
        statement,
        session_id="old-output-case",
        event_id="event_old_output_case",
    )
    fact = ledger.capture_fact(
        statement,
        memory_type="style",
        event=event,
        explicit_user_request=True,
        policy_result=MemoryPolicyResult(
            decision="confirm",
            scope="global",
            subject="user",
            utility_score=0.98,
            reason="legacy confirmation",
        ),
    )
    assert fact is not None

    layered = LayeredMemoryStore(tmp_path, "agent", store)
    layered_atoms = layered.ingest_atom_candidates(
        [
            AtomCandidate(
                type="style",
                content=statement,
                confidence=0.95,
                explicit_user_request=True,
            ),
        ],
    )
    layered_atoms[0].status = "active"
    layered_atoms[0].metadata["scope"] = "global"
    layered.save_atoms(layered_atoms)

    understanding = UserUnderstandingStore(tmp_path)
    understanding.remember(
        UnderstandingAtom.create(
            kind="preference",
            statement=statement,
            status="confirmed",
            confidence=1.0,
            evidence_ids=(legacy_entry["id"], fact.id),
            scope="profile",  # Historical storage-layer/scope mix-up.
        ),
    )
    marker = tmp_path / "memory-ledger" / "migration-v2.json"
    marker.write_text('{"version":"memory-governance-v2"}', encoding="utf-8")

    report = migrate_memory_recall_safety(tmp_path, apply=True, agent_id="agent")

    assert report["total_repairs"] >= 4
    assert report["content_deleted"] == 0
    assert report["confirmations_revoked"] == 0
    assert report["backup_dir"]
    migrated_entries = {entry.id: entry for entry in store.load_entries()}
    assert migrated_entries[legacy_entry["id"]].confirmed is True
    assert migrated_entries[legacy_entry["id"]].scope == "thread"
    assert migrated_entries[durable_entry["id"]].scope == "global"
    migrated_fact = ledger.get_fact(fact.id)
    assert migrated_fact is not None
    assert migrated_fact.status == "confirmed"
    assert migrated_fact.scope == "thread"
    migrated_atom = layered.load_atoms(include_archived=True)[0]
    assert migrated_atom.status == "active"
    assert migrated_atom.metadata["scope"] == "thread"
    migrated_understanding = understanding.list_atoms()[0]
    assert migrated_understanding.status == "confirmed"
    assert migrated_understanding.scope == "thread"
    assert statement not in store.read_profile_summary()["summary"]

    second = migrate_memory_recall_safety(tmp_path, apply=True, agent_id="agent")
    assert second["skipped"] is True
    assert second["reason"] == "already_applied"
