import json
import hashlib

import pytest

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.utils.authoritative_store import StoreCorruptionError


@pytest.mark.parametrize('component', ['facts', 'events', 'preferences', 'deleted_sources'])
@pytest.mark.parametrize('fault', ['truncated', 'wrong_shape', 'unsupported_schema'])
def test_corrupt_ledger_never_accepts_mutation(tmp_path, component, fault):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event('记住默认使用 pytest', event_id='original', session_id='original-session')
    ledger.capture_fact('默认使用 pytest', memory_type='workflow', event=event, explicit_user_request=True)
    path = getattr(ledger, component + '_path')
    path.parent.mkdir(parents=True, exist_ok=True)
    if fault == 'truncated':
        damaged = b'{"private_fixture":'
    elif fault == 'wrong_shape':
        damaged = b'[]'
    else:
        damaged = json.dumps({'version': 999, 'facts': [], 'event_ids': []}).encode()
    path.write_bytes(damaged)
    restarted = CanonicalMemoryLedger(tmp_path)
    with pytest.raises(RuntimeError):
        restarted.record_event('用户说默认用中文', event_id='later', session_id='later-session')
    assert path.read_bytes() == damaged
    assert b'later' not in ledger.events_path.read_bytes()


def test_unreadable_fact_store_is_not_empty(tmp_path, monkeypatch):
    from pathlib import Path
    ledger = CanonicalMemoryLedger(tmp_path)
    ledger.root.mkdir()
    ledger.facts_path.write_bytes(b'{"version":2,"facts":[]}')
    original = Path.read_bytes
    def read(path, *args, **kwargs):
        if path == ledger.facts_path:
            raise PermissionError('private fixture detail')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_bytes', read)
    with pytest.raises(RuntimeError):
        ledger.list_facts()


def test_explicit_recovery_preserves_decisions_and_source_identity(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    first = ledger.record_event('旧默认用 pytest', event_id='old-source', session_id='old-session')
    old = ledger.capture_fact('旧默认用 pytest', memory_type='workflow', event=first, explicit_user_request=True)
    second = ledger.record_event('新默认用 unittest', event_id='new-source', session_id='new-session')
    new = ledger.capture_fact('新默认用 unittest', memory_type='workflow', event=second, explicit_user_request=True)
    assert ledger.supersede(old.id, new.id)
    assert ledger.forget(new.id)
    damaged = b'{"truncated":'
    ledger.facts_path.write_bytes(damaged)
    with pytest.raises(RuntimeError):
        ledger.confirmed_facts()
    restarted = CanonicalMemoryLedger(tmp_path)
    recovered = restarted.recover_last_good('facts', expected_sha256=hashlib.sha256(damaged).hexdigest())
    assert recovered['recovered'] is True
    facts = {fact.id: fact for fact in restarted.list_facts()}
    assert facts[old.id].status == 'superseded'
    assert facts[old.id].superseded_by_id == new.id
    assert facts[new.id].status == 'forgotten'
    assert facts[new.id].event_ids == ['new-source']
    assert restarted.confirmed_facts() == []
    assert restarted.list_events(event_ids={'new-source'})[0].session_id == 'new-session'
    assert any(path.read_bytes() == damaged for path in ledger.root.glob('*.corrupt-*.bin'))


def test_detected_corruption_cannot_be_bypassed_by_removing_file(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event('默认用中文', event_id='source')
    ledger.capture_fact('默认用中文', memory_type='style', event=event, explicit_user_request=True)
    ledger.facts_path.write_bytes(b'{')
    with pytest.raises(RuntimeError):
        ledger.list_facts()
    ledger.facts_path.unlink()
    with pytest.raises(RuntimeError):
        CanonicalMemoryLedger(tmp_path).record_event('后续证据', event_id='later')
    assert b'later' not in ledger.events_path.read_bytes()


def test_recovery_rejects_changed_damage_and_invalid_backup(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    ledger.set_preferences(capture_enabled=False)
    damaged = b'{'
    ledger.preferences_path.write_bytes(damaged)
    with pytest.raises(RuntimeError):
        ledger.recover_last_good('preferences', expected_sha256='0' * 64)
    assert ledger.preferences_path.read_bytes() == damaged
    ledger.preferences_path.with_name(ledger.preferences_path.name + '.last-good').write_bytes(b'[]')
    with pytest.raises(RuntimeError):
        ledger.recover_last_good('preferences', expected_sha256=hashlib.sha256(damaged).hexdigest())
    assert ledger.preferences_path.read_bytes() == damaged


def test_interrupted_recovery_receipt_cannot_revive_forgotten_fact(tmp_path, monkeypatch):
    from lantai.utils import authoritative_store
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event('默认用中文', event_id='source')
    fact = ledger.capture_fact('默认用中文', memory_type='style', event=event, explicit_user_request=True)
    original = authoritative_store.atomic_write_bytes
    receipt = ledger.facts_path.with_name(ledger.facts_path.name + '.last-good.receipt.json')
    def fail_receipt(path, data):
        if path == receipt:
            raise OSError('fixture failure')
        return original(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(authoritative_store, 'atomic_write_bytes', fail_receipt)
        with pytest.raises(OSError):
            ledger.forget(fact.id)
    # A missing publication receipt blocks ordinary readers, including when
    # the new primary contains the intended forgotten decision.
    with pytest.raises(authoritative_store.StoreCorruptionError):
        ledger.get_fact(fact.id)
    persisted = json.loads(ledger.facts_path.read_bytes())
    assert next(item for item in persisted['facts'] if item['id'] == fact.id)['status'] == 'forgotten'
    damaged = b'{'
    ledger.facts_path.write_bytes(damaged)
    with pytest.raises(RuntimeError):
        ledger.recover_last_good('facts', expected_sha256=hashlib.sha256(damaged).hexdigest())
    assert ledger.facts_path.read_bytes() == damaged


def test_corrupt_middle_event_blocks_capture_and_keeps_all_bytes(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event('默认用中文', event_id='first')
    ledger.record_event('后续用 pytest', event_id='last')
    lines = ledger.events_path.read_bytes().splitlines(keepends=True)
    damaged = lines[0] + b'{broken}\n' + lines[1]
    ledger.events_path.write_bytes(damaged)
    with pytest.raises(RuntimeError):
        ledger.capture_fact('默认用中文', memory_type='style', event=event, explicit_user_request=True)
    assert ledger.events_path.read_bytes() == damaged
    assert not ledger.facts_path.exists()


def test_projection_interruption_keeps_fact_decision_and_source(tmp_path, monkeypatch):
    import lantai.agents.memory.canonical_ledger as canonical
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event('默认用中文', event_id='source', session_id='session')
    original = canonical.atomic_write_bytes
    def fail_projection(path, value):
        if path == ledger.projections_path:
            raise OSError('fixture projection failure')
        return original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(canonical, 'atomic_write_bytes', fail_projection)
        with pytest.raises(OSError):
            ledger.capture_fact('默认用中文', memory_type='style', event=event, explicit_user_request=True)
    restarted = CanonicalMemoryLedger(tmp_path)
    facts = restarted.confirmed_facts()
    assert len(facts) == 1 and facts[0].event_ids == ['source']
    assert restarted.rebuild_projections()[0]['fact_id'] == facts[0].id


def test_concurrent_ledger_writers_preserve_source_ids(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    def write(index):
        CanonicalMemoryLedger(tmp_path).record_event(f'fixture {index}', event_id=f'e{index}', session_id=f's{index}')
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(write, range(10)))
    events = CanonicalMemoryLedger(tmp_path).list_events()
    assert {event.id for event in events} == {f'e{index}' for index in range(10)}
    assert {event.session_id for event in events} == {f's{index}' for index in range(10)}
def test_corrupt_ledger_blocks_legacy_projection_bootstrap(tmp_path):
    from lantai.agents.memory.structured_user_memory import StructuredUserMemoryStore

    ledger = CanonicalMemoryLedger(tmp_path)
    ledger.record_event("synthetic prior source", session_id="fixture")
    ledger.facts_path.write_bytes(b"{broken")
    (tmp_path / "MEMORY.md").write_text("# Memory\n用户偏好简洁表达。\n", encoding="utf-8")
    store = StructuredUserMemoryStore(tmp_path)
    with pytest.raises(StoreCorruptionError):
        store.load_entries()
    assert not store.entries_path.exists(), "bootstrap rewrote projections after authority was damaged"
