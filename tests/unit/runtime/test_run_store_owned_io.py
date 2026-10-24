"""Slow durable reads must stay owned and keep the event loop responsive."""
import asyncio
import threading
import time
from contextlib import contextmanager
import os

import pytest

from lantai.runtime import store as store_module
from lantai.runtime.store import RuntimeRunStore
from datetime import datetime, timezone
from lantai.runtime.runs import RuntimeRunContract, RuntimeRunOwner


def contract():
    return RuntimeRunContract(
        run_id="contended-run", attempt_id="attempt-1", source="chat",
        owner=RuntimeRunOwner(user_id="user", agent_id="default", session_id="s", workspace_id="w", channel="console"),
        status="running", created_at=datetime.now(timezone.utc),
        release_edition="official",
    )


@contextmanager
def nonblocking_lock(path):
    fd = os.open(path, os.O_RDWR)
    try:
        with store_module.locked_file_descriptor(fd, blocking=False):
            yield
    finally:
        os.close(fd)


@pytest.mark.asyncio
@pytest.mark.parametrize('listing', [False, True])
async def test_slow_read_keeps_event_loop_responsive(tmp_path, monkeypatch, listing):
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(contract(), expected_revision=None)
    finished = threading.Event()
    original = store._load_contract_unlocked

    def slow(*args):
        time.sleep(0.2)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(store, '_load_contract_unlocked', slow)
    read = asyncio.create_task(store.list_contracts_async() if listing else store.load_contract('contended-run'))
    try:
        await asyncio.sleep(0.03)
        responsive = not finished.is_set()
        assert await read
        assert responsive, 'disk read blocked the event loop'
    finally:
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_drains_corruption_quarantine_before_unlock(tmp_path, monkeypatch):
    store = RuntimeRunStore(tmp_path)
    path = store.contract_path('broken')
    path.parent.mkdir(parents=True)
    path.write_text('broken json')
    started, finished = threading.Event(), threading.Event()
    original = store._load_contract_unlocked

    def slow(*args):
        started.set()
        time.sleep(0.2)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(store, '_load_contract_unlocked', slow)
    read = asyncio.create_task(store.list_contracts_async())
    try:
        while not started.is_set():
            await asyncio.sleep(0.005)
        read.cancel()
        await asyncio.sleep(0.02)
        read.cancel()  # repeated cancellation must not abandon the worker
        assert not read.done(), 'cancel reported settled while disk work was active'
        with pytest.raises(BlockingIOError):
            with nonblocking_lock(store._lock_path()):
                pass
        with pytest.raises(asyncio.CancelledError):
            await read
        assert finished.is_set()
        assert not path.exists()
        assert len(list((store.root / 'corrupt').glob('*.bad'))) == 1
        assert await RuntimeRunStore(tmp_path).list_contracts_async() == []
    finally:
        await asyncio.gather(read, return_exceptions=True)


@pytest.mark.asyncio
async def test_io_admission_is_bounded_and_cancelled_waiters_never_start(tmp_path, monkeypatch):
    stores = [RuntimeRunStore(tmp_path / str(i)) for i in range(8)]
    for store in stores:
        await store.save_contract(contract(), expected_revision=None)
    release = threading.Event()
    started = []
    guard = threading.Lock()
    def operation(index, original, *args):
        with guard:
            started.append(index)
        assert release.wait(3)
        return original(*args)
    for index, store in enumerate(stores):
        original = store._load_contract_unlocked
        monkeypatch.setattr(store, '_load_contract_unlocked', lambda *args, i=index, f=original: operation(i, f, *args))
    reads = [asyncio.create_task(store.load_contract('contended-run')) for store in stores]
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while len(started) < 4 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        assert len(started) == 4
        await asyncio.sleep(0.03)
        assert len(started) == 4
        admitted = set(started)
        for i, task in enumerate(reads):
            if i not in admitted:
                task.cancel()
        for i, task in enumerate(reads):
            if i not in admitted:
                with pytest.raises(asyncio.CancelledError):
                    await task
        release.set()
        results = await asyncio.gather(*reads, return_exceptions=True)
        assert sum(isinstance(r, RuntimeRunContract) for r in results) == 4
        assert set(started) == admitted
        # All four permits and file locks remain usable after cancellation.
        assert len(await asyncio.gather(*(store.list_contracts_async() for store in stores))) == 8
    finally:
        release.set()
        await asyncio.gather(*reads, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_failure_releases_capacity_and_keeps_context(tmp_path, monkeypatch):
    import contextvars
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(contract(), expected_revision=None)
    original = store._load_contract_unlocked
    marker = contextvars.ContextVar('store_io_test', default='absent')
    marker.set('request-scope')
    def fail(*args):
        assert marker.get() == 'request-scope'
        raise OSError('disk unavailable')
    monkeypatch.setattr(store, '_load_contract_unlocked', fail)
    for _ in range(6):
        with pytest.raises(OSError, match='disk unavailable'):
            await store.load_contract('contended-run')
    monkeypatch.setattr(store, '_load_contract_unlocked', original)
    assert await store.load_contract('contended-run')


@pytest.mark.parametrize('listing', [False, True])
def test_sync_reader_cannot_deadlock_worker_unlock(tmp_path, listing):
    # Isolate the event loop so an actual deadlock fails with a bounded timeout.
    import subprocess
    import sys
    script = '''
import asyncio, json, sys, threading, time
from lantai.runtime.store import RuntimeRunStore
from lantai.runtime.runs import RuntimeRunContract
async def run():
    store = RuntimeRunStore(sys.argv[1])
    await store.save_contract(RuntimeRunContract.model_validate_json(sys.argv[2]), expected_revision=None)
    original = store._load_contract_unlocked
    started = threading.Event()
    def slow(*args):
        started.set()
        time.sleep(0.15)
        return original(*args)
    store._load_contract_unlocked = slow
    task = asyncio.create_task(store.list_contracts_async() if sys.argv[3] == 'True' else store.load_contract('contended-run'))
    while not started.is_set():
        await asyncio.sleep(0.005)
    assert RuntimeRunStore(sys.argv[1]).list_contracts()
    assert await task
asyncio.run(run())
'''
    result = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path), contract().model_dump_json(), str(listing)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


def sidecar_reader(store, kind):
    """Use malformed real files to include quarantine writes in slow reads."""
    run_id = 'slow-sidecar'
    if kind == 'history':
        path, read = store.contract_path(run_id), lambda: store.list_contract_history(run_id)
    elif kind == 'progress':
        path, read = store.loop_progress_path(run_id), lambda: store.load_loop_progress_snapshot(run_id)
    elif kind == 'loop':
        path, read = store.loop_contract_path(run_id), lambda: store.load_loop_contract_snapshot(run_id)
    elif kind in ('evaluation', 'evaluations'):
        path = store.loop_evaluation_path(run_id, 'report')
        read = (lambda: store.load_loop_evaluation_report(run_id, 'report')) if kind == 'evaluation' else (lambda: store.list_loop_evaluation_reports(run_id))
    else:
        path = store.step_path(run_id, 'step')
        read = (lambda: store.load_step(run_id, 'step')) if kind == 'step' else (lambda: store.list_steps(run_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('broken JSON', encoding='utf-8')
    return path, read


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['history', 'progress', 'loop', 'evaluation', 'evaluations', 'step', 'steps'])
@pytest.mark.parametrize('cancel', [False, True])
async def test_sidecar_reads_remain_responsive_and_own_quarantine(tmp_path, monkeypatch, kind, cancel):
    store = RuntimeRunStore(tmp_path)
    path, read = sidecar_reader(store, kind)
    started, finished = threading.Event(), threading.Event()
    original = store_module.read_text_no_follow

    def slow(*args, **kwargs):
        started.set()
        time.sleep(0.2)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(store_module, 'read_text_no_follow', slow)
    task = asyncio.create_task(read())
    try:
        while not started.is_set():
            await asyncio.sleep(0.005)
        assert not finished.is_set(), 'sidecar disk read blocked the event loop'
        if cancel:
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            assert not task.done(), 'quarantine ownership abandoned on cancellation'
            with pytest.raises(BlockingIOError):
                with nonblocking_lock(store._lock_path()):
                    pass
            with pytest.raises(asyncio.CancelledError):
                await task
        elif kind in ('evaluations', 'steps'):
            assert await task == []
        else:
            with pytest.raises(store_module.RuntimeRunStoreCorruptionError):
                await task
        assert finished.is_set()
        assert not path.exists()
        assert len(list((store.root / 'corrupt').glob('*.bad'))) == 1
        with nonblocking_lock(store._lock_path()):
            pass
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_history_retains_revisions_and_deduplicates_active_archive(tmp_path):
    store = RuntimeRunStore(tmp_path)
    original = contract()
    await store.save_contract(original, expected_revision=None)
    await store.archive_contract(original.run_id, expected_revision=0)
    assert await store.list_contract_history(original.run_id) == (original,)
    revised = original.model_copy(update={'revision': 1})
    await store.save_contract(revised, expected_revision=0)
    history = await store.list_contract_history(original.run_id)
    assert [item.revision for item in history] == [1, 0]


@pytest.mark.asyncio
async def test_progress_roundtrip_keeps_attempt_filter(tmp_path):
    from lantai.runtime.loops import LoopProgressWindow
    store = RuntimeRunStore(tmp_path)
    progress = LoopProgressWindow(consecutive_failures=2)
    snapshot = await store.save_loop_progress_window('run', attempt_id='a', progress=progress)
    assert await store.load_loop_progress_snapshot('run') == snapshot
    assert await store.load_loop_progress_window('run', expected_attempt_id='a') == progress
    assert await store.load_loop_progress_window('run', expected_attempt_id='b') is None


@pytest.mark.asyncio
async def test_steps_roundtrip_keeps_attempt_filter_and_stable_order(tmp_path):
    from lantai.runtime.runs import RuntimeStepContract
    store = RuntimeRunStore(tmp_path)
    parent = contract()
    steps = []
    for step_id, index, attempt in [('z', 2, 'a'), ('b', 1, 'a'), ('a', 1, 'a'), ('other', 0, 'b')]:
        step = RuntimeStepContract(
            run_id=parent.run_id, attempt_id=attempt, step_id=step_id,
            source='chat', owner=parent.owner, index=index, kind='tool',
            release_edition='official',
        )
        await store.save_step(step)
        assert await store.load_step(parent.run_id, step_id) == step
        steps.append(step)
    assert [s.step_id for s in await store.list_steps(parent.run_id)] == ['other', 'a', 'b', 'z']
    assert [s.step_id for s in await store.list_steps(parent.run_id, expected_attempt_id='a')] == ['a', 'b', 'z']
    assert await store.list_steps(parent.run_id, expected_attempt_id='missing') == []


def sidecar_writer(store, kind):
    from lantai.runtime.loops import (
        LoopContract, LoopDeliverable, LoopDeterministicGate, LoopRubric,
        LoopRubricDimension, LoopProgressWindow, evaluate_loop_contract,
    )
    from lantai.runtime.runs import RuntimeStepContract
    if kind == 'progress':
        return '_write_loop_progress_snapshot_unlocked', lambda: store.save_loop_progress_window(
            'run', attempt_id='a', progress=LoopProgressWindow(consecutive_failures=2)), lambda: store.load_loop_progress_snapshot('run')
    if kind == 'step':
        step = RuntimeStepContract(run_id='run', attempt_id='a', step_id='step', source='chat',
                                   owner=contract().owner, index=0, kind='tool', release_edition='official')
        return '_write_step_unlocked', lambda: store.save_step(step), lambda: store.load_step('run', 'step')
    loop = LoopContract(
        contract_id='loop', objective='Persist evidence', scope=['runtime'],
        deliverables=[LoopDeliverable(deliverable_id='evidence', title='Evidence', acceptance=['durable'])],
        acceptance=['durable'], deterministic_gates=[LoopDeterministicGate(gate_id='test', name='Test', kind='test', command='pytest')],
        rubric=LoopRubric(rubric_id='quality', passing_score=4, dimensions=[LoopRubricDimension(dimension_id='correctness', name='Correctness', weight=1, target_score=4)]),
    )
    if kind == 'loop':
        return '_write_loop_contract_snapshot_unlocked', lambda: store.save_loop_contract('run', attempt_id='a', contract=loop), lambda: store.load_loop_contract_snapshot('run')
    report = evaluate_loop_contract(loop, gate_results=[], rubric_scores=[])
    return '_write_loop_evaluation_snapshot_unlocked', lambda: store.save_loop_evaluation_report('run', attempt_id='a', report=report), lambda: store.load_latest_loop_evaluation_report('run')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['progress', 'loop', 'evaluation', 'step'])
@pytest.mark.parametrize('cancel', [False, True])
async def test_sidecar_write_is_responsive_and_cancel_drains_commit(tmp_path, monkeypatch, kind, cancel):
    store = RuntimeRunStore(tmp_path)
    method, write, read = sidecar_writer(store, kind)
    original = getattr(store, method)
    started, finished = threading.Event(), threading.Event()

    def slow(*args):
        started.set()
        time.sleep(0.2)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(store, method, slow)
    task = asyncio.create_task(write())
    try:
        while not started.is_set():
            await asyncio.sleep(0.005)
        assert not finished.is_set(), 'sidecar disk write blocked the event loop'
        if cancel:
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            assert not task.done(), 'write ownership abandoned before durable commit'
            with pytest.raises(BlockingIOError):
                with nonblocking_lock(store._lock_path()):
                    pass
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert finished.is_set()
        assert await read() is not None
        with nonblocking_lock(store._lock_path()):
            pass
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_step_writes_keep_attempt_and_revision_fences(tmp_path, monkeypatch):
    store = RuntimeRunStore(tmp_path)
    _, write, read = sidecar_writer(store, 'step')
    original_step = await write()
    started, release = threading.Event(), threading.Event()
    original_write = store._write_step_unlocked

    def held_write(path, step):
        if step.revision == 2:
            started.set()
            assert release.wait(3)
        return original_write(path, step)

    monkeypatch.setattr(store, '_write_step_unlocked', held_write)
    newer = original_step.model_copy(update={'revision': 2})
    first = asyncio.create_task(store.save_step(newer))
    tasks = [first]
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while not started.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        assert started.is_set()
        stale = asyncio.create_task(store.save_step(original_step.model_copy(update={'revision': 1})))
        wrong_attempt = asyncio.create_task(store.save_step(original_step.model_copy(update={'attempt_id': 'b', 'revision': 3})))
        tasks.extend([stale, wrong_attempt])
        await asyncio.sleep(0.02)
        assert not stale.done() and not wrong_attempt.done()
        release.set()
        assert await first == newer
        with pytest.raises(store_module.RuntimeRunStoreConflictError, match='revision'):
            await stale
        with pytest.raises(store_module.RuntimeRunStoreConflictError, match='attempt'):
            await wrong_attempt
        assert await read() == newer
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["save", "archive", "delete", "migrate"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_contract_disk_transaction_is_responsive_and_owned(
    tmp_path, monkeypatch, kind, cancel
):
    store = RuntimeRunStore(tmp_path)
    initial = contract().model_copy(update={"status": "succeeded"})
    await store.save_contract(initial, expected_revision=None)
    if kind == "save":
        operation = lambda: store.save_contract(
            initial.model_copy(update={"revision": 1}), expected_revision=0
        )
        hook = "_write_contract_unlocked"
    elif kind == "archive":
        operation = lambda: store.archive_contract(
            initial.run_id, expected_revision=0
        )
        hook = "_archive_contract_unlocked"
    elif kind == "delete":
        operation = lambda: store.delete_contract(
            initial.run_id, expected_revision=0
        )
        hook = "_archive_contract_unlocked"
    else:
        raw = {
            "schema_version": 0,
            "id": "legacy-run",
            "source": "chat",
            "agent_id": "default",
            "session_id": "s",
            "status": "completed",
            "started_at": initial.created_at.isoformat(),
            "ended_at": initial.created_at.isoformat(),
            "release_edition": "official",
        }
        operation = lambda: store.migrate_legacy_contract(raw, dry_run=False)
        hook = "_write_contract_unlocked"
    original = getattr(store, hook)
    started, finished = threading.Event(), threading.Event()

    def slow(*args):
        started.set()
        time.sleep(0.2)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(store, hook, slow)
    task = asyncio.create_task(operation())
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while (
            not started.is_set()
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.005)
        assert started.is_set()
        assert (
            not finished.is_set()
        ), "contract transaction blocked the event loop"
        if cancel:
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            assert not task.done(), "cancel abandoned the transaction"
            with pytest.raises(BlockingIOError):
                with nonblocking_lock(store._lock_path()):
                    pass
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert finished.is_set()
        reopened = RuntimeRunStore(tmp_path)
        persisted = await reopened.load_contract(initial.run_id)
        if kind == "save":
            assert persisted.revision == 1
        elif kind == "archive":
            assert persisted == initial
            assert await reopened.list_contract_history(initial.run_id) == (
                initial,
            )
            assert list((reopened.root / "archives").rglob("*.json"))
        elif kind == "delete":
            assert persisted is None
            assert await reopened.list_contract_history(initial.run_id) == (
                initial,
            )
        else:
            assert await reopened.load_contract("legacy-run") is not None
            assert store._backup_path("legacy-run", raw).exists()
        with nonblocking_lock(store._lock_path()):
            pass
    finally:
        await asyncio.gather(task, return_exceptions=True)
