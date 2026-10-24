"""Real offload may not overwrite an acknowledged memory tombstone."""
from concurrent.futures import ThreadPoolExecutor
import threading

from lantai.agents.memory.layered_memory import LayeredMemoryStore
from lantai.runtime.converters import LegacyMsg as Msg


def test_tool_offload_cannot_overwrite_acknowledged_forget(tmp_path, monkeypatch):
    store = LayeredMemoryStore(tmp_path, 'fixture')
    forgotten = '仅限本项目使用 pytest 执行单元测试'
    store.record_messages([
        Msg(name='user', role='user', content=forgotten),
    ], session_id='synthetic')
    assert store.search_conversations(forgotten), 'fixture source must initially be recallable'
    entered, release = threading.Event(), threading.Event()
    original_load = store._load_state
    held = False

    def hold_first_offload_state():
        nonlocal held
        state = original_load()
        if not held:
            held = True
            entered.set()
            assert release.wait(5), 'fixture offload release timed out'
        return state

    monkeypatch.setattr(store, '_load_state', hold_first_offload_state)
    messages = [
        Msg(name='assistant', role='assistant', content=[{
            'type': 'tool_use', 'id': 'synthetic-call', 'name': 'read_file',
            'input': {'path': 'synthetic.txt'},
        }]),
        Msg(name='assistant', role='system', content=[{
            'type': 'tool_result', 'id': 'synthetic-call', 'name': 'read_file',
            'output': [{'type': 'text', 'text': 'synthetic tool line\n' * 100}],
        }]),
    ]
    with ThreadPoolExecutor(max_workers=2) as workers:
        offload = workers.submit(store.offload_tool_results, messages, min_bytes=1000)
        try:
            assert entered.wait(5), 'fixture did not reach actual offload state read'
            forget = workers.submit(store.forget_projection_content, forgotten)
            # Correct fencing leaves the second operation pending until the
            # first writer finishes. Both legal serial orders preserve forget.
            threading.Event().wait(0.2)
        finally:
            release.set()
        assert offload.result(timeout=8) == {'created': 1, 'replaced': 1}
        assert forget.result(timeout=8)['tombstones'] == 1
    fresh = LayeredMemoryStore(tmp_path, 'fixture')
    assert fresh.search_conversations(forgotten) == [], (
        'tool offload overwrote acknowledged forgotten_content and revived raw L0 recall'
    )
