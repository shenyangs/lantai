"""Public clear and user-understanding writers adjacent to memory adoption."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import threading
import pytest
from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory import memory_pack
from lantai.agents.memory.maintenance import clear_all_long_term_memory, MEMORY_CLEAR_CONFIRMATION
from lantai.agents.persona.user_understanding import UserUnderstandingStore


def test_public_clear_keeps_initialized_authoritative_ledger_healthy(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event('synthetic source evidence', event_id='source', session_id='fixture-session')
    fact = ledger.capture_fact('synthetic source evidence', memory_type='style', event=event, explicit_user_request=True)
    assert fact is not None and fact.status == 'confirmed'
    report = clear_all_long_term_memory(tmp_path, confirmation=MEMORY_CLEAR_CONFIRMATION, agent_id='fixture')
    assert report['cleared'] is True
    restarted = CanonicalMemoryLedger(tmp_path)
    assert restarted.confirmed_facts() == []
    assert restarted.list_events() == []
    assert restarted.get_preferences()['capture_enabled'] is True
    new_event = restarted.record_event('new synthetic evidence', event_id='new-source', session_id='new-session')
    assert new_event is not None


@pytest.mark.parametrize('decision', ['corrected', 'source_invalidated'])
def test_adoption_does_not_overwrite_completed_user_understanding_negative_feedback(tmp_path, monkeypatch, decision):
    target = tmp_path / 'target'
    target.mkdir()
    store = UserUnderstandingStore(target)
    atom = store.observe(kind='preference', statement='synthetic preference evidence', confidence=0.7, evidence_id='source')
    source = tmp_path / 'source'
    shutil.copytree(target, source)
    pack = tmp_path / 'old-memory.zip'
    memory_pack.export_memory_pack(source, pack, agent_id='fixture')
    entered = threading.Event()
    resume = threading.Event()
    export = memory_pack.export_memory_pack
    def held_export(working_dir, destination, *, agent_id):
        if Path(working_dir).resolve() == target.resolve():
            entered.set()
            assert resume.wait(5), 'test release barrier timed out'
        return export(working_dir, destination, agent_id=agent_id)
    monkeypatch.setattr(memory_pack, 'export_memory_pack', held_export)
    def adopt():
        return memory_pack.adopt_memory_pack(pack, target, agent_id='fixture', dry_run=False, confirmation=memory_pack.MEMORY_PACK_ADOPT_CONFIRMATION)
    def negative():
        current = UserUnderstandingStore(target)
        if decision == 'corrected':
            changed = current.apply_feedback(atom.id, 'corrected')
            assert changed is not None and changed.status == 'contradicted'
        else:
            assert current.invalidate_source('source') == 1
    with ThreadPoolExecutor(max_workers=2) as workers:
        adoption = workers.submit(adopt)
        try:
            assert entered.wait(5), 'adoption did not reach real recovery export'
            feedback = workers.submit(negative)
            threading.Event().wait(0.2)
        finally:
            resume.set()
        report = adoption.result(timeout=8)
        feedback.result(timeout=8)
    assert report.adopted is True
    current = UserUnderstandingStore(target).get_atom(atom.id)
    assert current is not None
    if decision == 'corrected':
        assert current.status == 'contradicted', 'adoption revived an understanding that explicit completed feedback corrected'
        assert current.corrected_hits == 1
    else:
        assert current.status == 'superseded', 'adoption revived a retired source-dependent understanding'
        assert current.evidence_ids == ()
