import json
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from lantai.agents.persona.subject_continuity import SubjectContinuityStore, SubjectReflection


@pytest.mark.parametrize('fault', ['truncated', 'wrong_shape', 'unsupported_schema', 'invalid_turn_index'])
@pytest.mark.parametrize('operation', ['observe', 'reflect'])
def test_corrupt_subject_store_never_overwrites_history(tmp_path, fault, operation):
    store = SubjectContinuityStore(tmp_path, agent_id='main')
    store.observe_messages([{'id': 'u1', 'role': 'user', 'content': 'fixture'}], session_id='original-session')
    good = json.loads(store.path.read_text())
    damaged = {
        'truncated': b'{"private_fixture":',
        'wrong_shape': b'[]',
        'unsupported_schema': json.dumps({**good, 'version': 999}).encode(),
        'invalid_turn_index': json.dumps({**good, 'turn_index': []}).encode(),
    }[fault]
    store.path.write_bytes(damaged)
    restarted = SubjectContinuityStore(tmp_path, agent_id='main')
    with pytest.raises(RuntimeError):
        if operation == 'observe':
            restarted.observe_messages([{'id': 'u2', 'role': 'user', 'content': 'later'}], session_id='later-session')
        else:
            restarted.record_reflection(SubjectReflection(source_session_id='later-session'))
    assert store.path.read_bytes() == damaged


def test_explicit_recovery_keeps_turn_and_reflection_sources(tmp_path):
    store = SubjectContinuityStore(tmp_path, agent_id='main')
    store.observe_messages([{'id': 'u1', 'role': 'user', 'content': 'fixture'}], session_id='original-session')
    store.record_reflection(SubjectReflection(causal_anchor='fixture consequence', source_turn_id='turn-1', source_session_id='original-session'))
    damaged = b'{'
    store.path.write_bytes(damaged)
    with pytest.raises(RuntimeError):
        store.load()
    restarted = SubjectContinuityStore(tmp_path, agent_id='main')
    restored = restarted.recover_last_good(expected_sha256=hashlib.sha256(damaged).hexdigest())
    assert restored.turn_index == 1
    assert restored.recent_events[0].session_id == 'original-session'
    assert restored.reflection.source_turn_id == 'turn-1'
    assert restored.reflection.source_session_id == 'original-session'
    assert restored.reflection.causal_anchor == 'fixture consequence'
    assert SubjectContinuityStore(tmp_path, agent_id='main').load() == restored
    assert any(path.read_bytes() == damaged for path in store.root.glob('*.corrupt-*.bin'))


def test_missing_store_is_new_but_detected_corruption_is_not(tmp_path):
    store = SubjectContinuityStore(tmp_path, agent_id='main')
    assert store.load().turn_index == 0
    store.observe_messages([{'id': 'u1', 'role': 'user', 'content': 'fixture'}])
    store.path.write_bytes(b'{')
    with pytest.raises(RuntimeError):
        store.load()
    store.path.unlink()
    with pytest.raises(RuntimeError):
        SubjectContinuityStore(tmp_path, agent_id='main').record_reflection(SubjectReflection())
    assert not store.path.exists()


def test_concurrent_observations_do_not_lose_real_turns(tmp_path, monkeypatch):
    original = SubjectContinuityStore._save
    def slow_save(store, state):
        time.sleep(0.05)
        return original(store, state)
    monkeypatch.setattr(SubjectContinuityStore, '_save', slow_save)
    def observe(index):
        SubjectContinuityStore(tmp_path, agent_id='main').observe_messages(
            [{'id': f'u{index}', 'role': 'user', 'content': f'fixture {index}'}], session_id=f's{index}',
        )
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(observe, range(10)))
    restored = SubjectContinuityStore(tmp_path, agent_id='main').load()
    assert restored.turn_index == 10
    assert {event.message_id for event in restored.recent_events} == {f'u{index}' for index in range(10)}


def test_unreadable_subject_store_is_observable_and_preserved(tmp_path, monkeypatch):
    from pathlib import Path
    store = SubjectContinuityStore(tmp_path, agent_id='main')
    store.observe_messages([{'id': 'u1', 'role': 'user', 'content': 'fixture'}])
    original = Path.read_bytes
    def unreadable(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError('private fixture detail')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_bytes', unreadable)
    with pytest.raises(RuntimeError) as failure:
        store.record_reflection(SubjectReflection())
    assert 'private fixture detail' not in str(failure.value)


def test_explicit_recovery_of_missing_subject_preserves_real_history(tmp_path):
    store = SubjectContinuityStore(tmp_path, agent_id='main')
    state = store.observe_messages([{'id': 'u1', 'role': 'user', 'content': 'fixture'}], session_id='original-session')
    store.path.unlink()
    with pytest.raises(RuntimeError):
        store.load()
    restored = store.recover_last_good(expected_missing=True)
    assert restored == state
    assert store.load() == state
