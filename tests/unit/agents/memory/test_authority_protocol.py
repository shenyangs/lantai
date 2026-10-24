"""Authoritative storage publication, deletion and explicit recovery behavior locks."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.utils import authoritative_store as store


def _sidecar(path, suffix):
    return path.with_name(path.name + suffix)


def _load(path):
    return store.load_validated(path, json.loads, missing=dict)


@pytest.mark.parametrize("replacement", [b'{"decision":"older"}', b'{}'])
def test_schema_valid_primary_replacement_does_not_bypass_receipt(tmp_path, replacement):
    path = tmp_path / "state.json"
    store.write_authoritative(path, b'{"decision":"forgotten"}')
    path.write_bytes(replacement)
    with pytest.raises(store.StoreCorruptionError):
        _load(path)
    assert path.read_bytes() == replacement


@pytest.mark.parametrize("fault_point", ["primary", "receipt"])
def test_interrupted_publication_blocks_further_reads(tmp_path, monkeypatch, fault_point):
    path = tmp_path / "state.json"
    old = b'{"decision":"confirmed"}'
    new = b'{"decision":"forgotten"}'
    store.write_authoritative(path, old)
    target = path if fault_point == "primary" else _sidecar(path, ".last-good.receipt.json")
    write = store.atomic_write_bytes

    def fail_publication(candidate, value):
        if candidate == target:
            raise OSError("injected publication interruption")
        return write(candidate, value)

    with monkeypatch.context() as patch:
        patch.setattr(store, "atomic_write_bytes", fail_publication)
        with pytest.raises(OSError):
            store.write_authoritative(path, new)
    expected = old if fault_point == "primary" else new
    assert path.read_bytes() == expected
    assert _sidecar(path, ".last-good").read_bytes() == new
    with pytest.raises(store.StoreCorruptionError):
        _load(path)


@pytest.mark.parametrize("retained_lines", [0, 1])
def test_complete_jsonl_line_loss_is_not_a_new_empty_store(tmp_path, retained_lines):
    ledger = CanonicalMemoryLedger(tmp_path)
    ledger.record_event("fixture first", event_id="first")
    ledger.record_event("fixture second", event_id="second")
    original = ledger.events_path.read_bytes()
    damaged = b"".join(original.splitlines(keepends=True)[:retained_lines])
    ledger.events_path.write_bytes(damaged)
    with pytest.raises(store.StoreCorruptionError):
        CanonicalMemoryLedger(tmp_path).record_event("fixture third", event_id="third")
    assert ledger.events_path.read_bytes() == damaged


def test_first_validated_legacy_read_establishes_byte_identity(tmp_path):
    path = tmp_path / "legacy.json"
    original = b'{"decision":"forgotten"}\n'
    path.write_bytes(original)
    assert _load(path) == {"decision": "forgotten"}
    assert path.read_bytes() == original
    assert _sidecar(path, ".last-good").read_bytes() == original
    receipt = json.loads(_sidecar(path, ".last-good.receipt.json").read_text())
    assert receipt["sha256"] == hashlib.sha256(original).hexdigest()


def test_reader_waits_for_an_entire_publication(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store.write_authoritative(path, b'{"value":1}')
    backup_published = threading.Event()
    finish_publication = threading.Event()
    write = store.atomic_write_bytes

    def pause_after_backup(candidate, value):
        write(candidate, value)
        if candidate == _sidecar(path, ".last-good"):
            backup_published.set()
            assert finish_publication.wait(3)

    monkeypatch.setattr(store, "atomic_write_bytes", pause_after_backup)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(store.write_authoritative, path, b'{"value":2}')
        assert backup_published.wait(3)
        reader_started = threading.Event()

        def read():
            reader_started.set()
            return _load(path)

        reader = executor.submit(read)
        assert reader_started.wait(3)
        try:
            assert not reader.done(), "reader bypassed the publication lock"
        finally:
            finish_publication.set()
        writer.result(timeout=3)
        assert reader.result(timeout=3) == {"value": 2}


def test_same_thread_can_reenter_its_store_lock(tmp_path):
    code = (
        "import sys; from pathlib import Path; "
        "from lantai.utils.authoritative_store import exclusive_store_lock; "
        "p=Path(sys.argv[1]); "
        "exec('with exclusive_store_lock(p):\\n with exclusive_store_lock(p):\\n  pass')"
    )
    try:
        completed = subprocess.run([sys.executable, "-c", code, str(tmp_path / "nested.lock")],
                                   capture_output=True, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail("nested store lock deadlocked")
    assert completed.returncode == 0


def test_missing_initialized_primary_has_explicit_recovery(tmp_path):
    path = tmp_path / "state.json"
    store.write_authoritative(path, b'{"decision":"forgotten"}')
    path.unlink()
    with pytest.raises(store.StoreCorruptionError):
        _load(path)
    restored = store.recover_last_good(path, json.loads, expected_missing=True)
    assert restored == {"decision": "forgotten"}
    assert _load(path) == restored


def test_recovery_rechecks_damage_after_candidate_validation(tmp_path):
    path = tmp_path / "state.json"
    store.write_authoritative(path, b'{"decision":"forgotten"}')
    damage = b"{"
    path.write_bytes(damage)
    newer = b'{"decision":"newly_changed"}'

    def decode(value):
        path.write_bytes(newer)
        return json.loads(value)

    with pytest.raises(store.StoreCorruptionError):
        store.recover_last_good(path, decode, expected_sha256=hashlib.sha256(damage).hexdigest())
    assert path.read_bytes() == newer


@pytest.mark.parametrize("fault_point", ["events", "facts"])
def test_deleted_source_cannot_escape_through_ordinary_readers(tmp_path, monkeypatch, fault_point):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("fixture source content", event_id="source")
    fact = ledger.capture_fact("fixture source content", memory_type="style", event=event,
                               explicit_user_request=True)

    def fail(*args, **kwargs):
        raise OSError("injected source deletion interruption")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_write_lines_atomic" if fault_point == "events" else "_save_facts", fail)
        with pytest.raises(OSError):
            ledger.delete_source_event("source")
    restarted = CanonicalMemoryLedger(tmp_path)
    leaked = []
    readers = {
        "get_fact": lambda: restarted.get_fact(fact.id),
        "list_facts": restarted.list_facts,
        "confirmed_facts": restarted.confirmed_facts,
        "open_fact_sources": lambda: restarted.open_fact_sources(fact.id),
        "list_events": restarted.list_events,
    }
    for name, read in readers.items():
        try:
            read()
        except store.StoreCorruptionError:
            continue
        leaked.append(name)
    assert leaked == [], f"readers bypassed pending deletion: {leaked}"


def test_recovery_cannot_publish_a_fact_with_a_tombstoned_source(tmp_path, monkeypatch):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("fixture source content", event_id="source")
    ledger.capture_fact("fixture source content", memory_type="style", event=event,
                        explicit_user_request=True)
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_save_facts", lambda *args: (_ for _ in ()).throw(OSError("fixture")))
        with pytest.raises(OSError):
            ledger.delete_source_event("source")
    damaged = b"{"
    ledger.facts_path.write_bytes(damaged)
    with pytest.raises(store.StoreCorruptionError):
        ledger.recover_last_good("facts", expected_sha256=hashlib.sha256(damaged).hexdigest())
    assert ledger.facts_path.read_bytes() == damaged


def test_layered_recall_cannot_bypass_damaged_canonical_store(tmp_path):
    from lantai.agents.memory.layered_memory import LayeredMemoryStore

    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("fixture source", event_id="source")
    ledger.capture_fact("fixture source", memory_type="style", event=event, explicit_user_request=True)
    ledger.facts_path.write_bytes(b"{")
    with pytest.raises(store.StoreCorruptionError):
        LayeredMemoryStore(tmp_path, agent_id="fixture").load_atoms()


def test_memory_pack_decisions_cannot_bypass_pending_source_deletion(tmp_path, monkeypatch):
    from lantai.agents.memory.memory_pack import _local_decisions

    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("fixture source", event_id="source")
    ledger.capture_fact("fixture source", memory_type="style", event=event, explicit_user_request=True)
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_save_facts", lambda *args: (_ for _ in ()).throw(OSError("fixture")))
        with pytest.raises(OSError):
            ledger.delete_source_event("source")
    with pytest.raises(store.StoreCorruptionError):
        _local_decisions(tmp_path)


def test_carrying_pack_decisions_keeps_the_authority_receipt_valid(tmp_path):
    from lantai.agents.memory.memory_pack import _carry_local_decisions

    working = tmp_path / "working"
    ledger = CanonicalMemoryLedger(working)
    ledger.record_event("fixture source", event_id="source")
    ledger.delete_source_event("source")
    stage = tmp_path / "stage"
    target = stage / "memory_ledger" / "deleted-sources.json"
    store.write_authoritative(target, b'{"version":2,"event_ids":["other"],"fact_hashes":[]}')
    _carry_local_decisions(working, stage)
    deleted = store.load_validated(target, ledger._decode_deleted_sources, missing=dict)
    assert set(deleted["event_ids"]) == {"source", "other"}


@pytest.mark.parametrize("component", ["deleted_sources", "events", "facts"])
@pytest.mark.parametrize("fault_point", ["backup", "primary", "receipt"])
def test_explicit_source_deletion_resume_finishes_an_interrupted_publication(
    tmp_path, monkeypatch, component, fault_point,
):
    ledger = CanonicalMemoryLedger(tmp_path)
    deleted_event = ledger.record_event("fixture deleted source", event_id="deleted")
    deleted_fact = ledger.capture_fact("fixture deleted source", memory_type="style", event=deleted_event,
                                       explicit_user_request=True)
    kept_event = ledger.record_event("fixture kept source", event_id="kept")
    kept_fact = ledger.capture_fact("fixture kept source", memory_type="style", event=kept_event,
                                    explicit_user_request=True)
    path = getattr(ledger, component + "_path")
    target = path if fault_point == "primary" else _sidecar(
        path, ".last-good" if fault_point == "backup" else ".last-good.receipt.json",
    )
    write = store.atomic_write_bytes

    def fail(candidate, value):
        if candidate == target:
            raise OSError("fixture interrupted deletion publication")
        return write(candidate, value)

    with monkeypatch.context() as patch:
        patch.setattr(store, "atomic_write_bytes", fail)
        with pytest.raises(OSError):
            ledger.delete_source_event("deleted")
    restarted = CanonicalMemoryLedger(tmp_path)
    with pytest.raises(store.StoreCorruptionError):
        restarted.read_snapshot()
    resumed = restarted.resume_source_deletion(expected_event_id="deleted")
    assert resumed["deleted"] is True
    assert restarted.get_fact(deleted_fact.id).status == "forgotten"
    assert restarted.get_fact(deleted_fact.id).event_ids == []
    assert restarted.get_fact(kept_fact.id).status == "confirmed"
    assert {event.id for event in restarted.list_events()} == {"kept"}
    assert not restarted.deletion_pending_path.exists()
    assert {fact.id for fact in restarted.confirmed_facts()} == {kept_fact.id}


def test_source_deletion_resume_rejects_changed_transaction_state(tmp_path, monkeypatch):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("fixture source", event_id="source")
    ledger.capture_fact("fixture source", memory_type="style", event=event, explicit_user_request=True)
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_save_facts", lambda *args: (_ for _ in ()).throw(OSError("fixture")))
        with pytest.raises(OSError):
            ledger.delete_source_event("source")
    newer = {"version": 2, "facts": []}
    store.write_authoritative(ledger.facts_path, json.dumps(newer).encode())
    before = ledger.facts_path.read_bytes()
    with pytest.raises(store.StoreCorruptionError):
        ledger.resume_source_deletion(expected_event_id="source")
    assert ledger.facts_path.read_bytes() == before
    assert ledger.deletion_pending_path.exists()


@pytest.mark.parametrize("fault_point", ["backup", "receipt"])
def test_legacy_attestation_interruption_has_an_explicit_forward_recovery(tmp_path, monkeypatch, fault_point):
    path = tmp_path / "legacy.json"
    original = b'{"decision":"forgotten"}\n'
    path.write_bytes(original)
    target = _sidecar(path, ".last-good" if fault_point == "backup" else ".last-good.receipt.json")
    write = store.atomic_write_bytes

    def fail(candidate, value):
        if candidate == target:
            raise OSError("fixture interrupted legacy attestation")
        return write(candidate, value)

    with monkeypatch.context() as patch:
        patch.setattr(store, "atomic_write_bytes", fail)
        with pytest.raises(OSError):
            _load(path)
    with pytest.raises(store.StoreCorruptionError):
        _load(path)
    assert store.recover_last_good(path, json.loads, expected_sha256=hashlib.sha256(original).hexdigest()) == {
        "decision": "forgotten",
    }
    assert path.read_bytes() == original
    assert _load(path) == {"decision": "forgotten"}


def test_source_deletion_can_resume_after_a_second_publication_interruption(tmp_path, monkeypatch):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("fixture source", event_id="source")
    fact = ledger.capture_fact("fixture source", memory_type="style", event=event, explicit_user_request=True)
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_save_facts", lambda *args: (_ for _ in ()).throw(OSError("fixture")))
        with pytest.raises(OSError):
            ledger.delete_source_event("source")
    write = store.atomic_write_bytes

    def fail(candidate, value):
        if candidate == ledger.deleted_sources_path:
            raise OSError("fixture interrupted retry")
        return write(candidate, value)

    with monkeypatch.context() as patch:
        patch.setattr(store, "atomic_write_bytes", fail)
        try:
            ledger.resume_source_deletion(expected_event_id="source")
        except OSError:
            pass
    if ledger.deletion_pending_path.exists():
        ledger.resume_source_deletion(expected_event_id="source")
    assert ledger.get_fact(fact.id).status == "forgotten"
    assert ledger.list_events() == []


@pytest.mark.parametrize("reference", ["evidence", "dream"])
def test_source_deletion_removes_secondary_source_references(tmp_path, reference):
    ledger = CanonicalMemoryLedger(tmp_path)
    ledger.record_event("fixture removed source", event_id="removed")
    kept = ledger.record_event("fixture kept source", event_id="kept", source_role="user")
    fact = ledger.capture_fact("fixture kept source", memory_type="style", event=kept,
                               explicit_user_request=True)
    facts = ledger.list_facts()
    if reference == "evidence":
        facts[0].evidence_refs.append({"event_id": "removed", "support_status": "supported"})
    else:
        facts[0].dream_source_event_ids.append("removed")
    ledger._save_facts(facts)
    assert ledger.delete_source_event("removed")["deleted"] is True
    remaining = ledger.get_fact(fact.id)
    assert remaining.event_ids == ["kept"]
    assert "removed" not in remaining.dream_source_event_ids
    assert all(ref.get("event_id") != "removed" for ref in remaining.evidence_refs)
    assert remaining.status == "confirmed"
