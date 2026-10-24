"""Shutdown must drain real scheduler and manual invocation lifecycles."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lantai.app.crons.manager import CronManager
from lantai.app.crons.models import CronJobSpec, DispatchSpec, DispatchTarget, ScheduleSpec
from lantai.app.crons.repo.json_repo import JsonJobRepository
from lantai.runtime.store import RuntimeRunStore


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr('lantai.app.crons.manager.get_heartbeat_config', lambda _: SimpleNamespace(enabled=False))
    monkeypatch.setattr('lantai.app.crons.manager.get_dream_cron', lambda _: None)
    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace())
    job = CronJobSpec(id='lifecycle', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    return manager, job


@pytest.mark.asyncio
@pytest.mark.parametrize('scheduled', [False, True])
async def test_stop_waits_for_executor_cleanup_and_terminal_write(case, tmp_path, scheduled):
    manager, job = case
    entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def execute(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    manager._executor.execute = execute
    await manager._repo.upsert_job(job)
    await manager.start()
    if scheduled:
        manager._scheduler.add_job(manager._scheduled_callback, 'date', args=[job.id])
    else:
        await manager.run_job(job.id)
    await asyncio.wait_for(entered.wait(), 2)
    owner = next(iter(manager._active_runs.values()))[1]
    stop = asyncio.create_task(manager.stop())
    second = None
    try:
        await asyncio.sleep(.05)
        assert not stop.done(), 'stop returned before invocation cleanup'
        await asyncio.wait_for(cleaning.wait(), 2)
        second = asyncio.create_task(manager.stop())
        for _ in range(3):
            stop.cancel()
            await asyncio.sleep(.01)
        assert not stop.done() and not second.done()
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
        await second
        assert finished.is_set() and owner.done()
        assert not manager._scheduler.running
        assert not manager._rt[job.id].sem.locked()
        assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'cancelled'
        await manager.start()
        assert manager._scheduler.running
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, stop, *([second] if second else []), return_exceptions=True)
        await manager.stop()


@pytest.mark.asyncio
async def test_cancel_start_rolls_back_running_scheduler(case, monkeypatch):
    manager, job = case
    await manager._repo.upsert_job(job)
    entered = asyncio.Event()
    original_register = manager._register_or_update

    async def register(_):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, '_register_or_update', register)
    task = asyncio.create_task(manager.start())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not manager._scheduler.running, 'cancelled start left scheduler live'
        monkeypatch.setattr(manager, '_register_or_update', original_register)
        await manager.start()
        assert manager._scheduler.running
    finally:
        await manager.stop()
        if manager._scheduler.running:
            manager._scheduler.shutdown(wait=False)
            await asyncio.sleep(.01)


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
async def test_repository_read_cannot_submit_across_stop(case, monkeypatch, restart):
    manager, job = case
    await manager._repo.upsert_job(job)
    await manager.start()
    entered, release = asyncio.Event(), asyncio.Event()
    execute = AsyncMock()
    manager._executor.execute = execute

    async def get_job(_):
        entered.set()
        await release.wait()
        return job

    monkeypatch.setattr(manager._repo, 'get_job', get_job)
    pending = asyncio.create_task(manager.run_job(job.id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await manager.stop()
        if restart:
            await manager.start()
        release.set()
        with pytest.raises(RuntimeError, match='stopp'):
            await pending
        await asyncio.sleep(.02)
        execute.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await manager.stop()


@pytest.mark.asyncio
async def test_queued_start_cannot_reopen_admission_after_stop(case, monkeypatch):
    manager, job = case
    await manager._repo.upsert_job(job)
    entered = asyncio.Event()
    original = manager._register_or_update
    calls = 0

    async def register(spec):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        await original(spec)

    monkeypatch.setattr(manager, '_register_or_update', register)
    first = asyncio.create_task(manager.start())
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(manager.start())
    await asyncio.sleep(.01)
    await manager.stop()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], RuntimeError)
    assert not manager._accepting and not manager._scheduler.running
    with pytest.raises(RuntimeError, match='stopp'):
        await manager.run_job(job.id)


@pytest.mark.asyncio
@pytest.mark.parametrize('callback', ['heartbeat', 'dream'])
async def test_background_callback_cleanup_survives_external_cancel_during_stop(case, monkeypatch, callback):
    manager, _ = case
    entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def operation(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    if callback == 'heartbeat':
        monkeypatch.setattr('lantai.app.crons.manager.run_heartbeat_once', operation)
        manager._runner = SimpleNamespace()
    else:
        manager._runner.memory_manager = SimpleNamespace(dream=operation)
    await manager.start()
    task = asyncio.create_task(getattr(manager, f'_{callback}_callback')())
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(manager.stop())
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done() and not stop.done(), 'cleanup was interrupted'
        release.set()
        await stop
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, stop, return_exceptions=True)
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['start', 'skip', 'settle'])
async def test_stop_owns_partial_persistence_phases(case, monkeypatch, tmp_path, phase):
    from lantai.app.crons import manager as cron_module
    from lantai.app.experience import events

    manager, job = case
    await manager._repo.upsert_job(job)
    await manager.start()
    entered, release = asyncio.Event(), asyncio.Event()
    append_original = events.append_task_event
    settle_original = cron_module.record_cron_job_settled

    async def append(**kwargs):
        if phase in {'start', 'skip'} and kwargs['kind'] == 'cron.job.started':
            entered.set()
            await release.wait()
        return await append_original(**kwargs)

    async def settle(**kwargs):
        if phase == 'settle':
            entered.set()
            await release.wait()
        return await settle_original(**kwargs)

    monkeypatch.setattr(events, 'append_task_event', append)
    monkeypatch.setattr(cron_module, 'record_cron_job_settled', settle)
    manager._executor.execute = AsyncMock()
    if phase == 'skip':
        await manager._rt[job.id].sem.acquire()
    await manager.run_job(job.id)
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(manager.stop())
    try:
        await asyncio.sleep(.03)
        assert not stop.done()
        assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'running'
        release.set()
        await asyncio.wait_for(stop, 2)
        records = RuntimeRunStore(tmp_path).list_contracts()
        assert len(records) == 1
        assert records[0].status == ('succeeded' if phase == 'settle' else 'cancelled')
        assert not manager._owned_tasks and not manager._active_runs
    finally:
        release.set()
        await asyncio.gather(stop, return_exceptions=True)
        if phase == 'skip':
            manager._rt[job.id].sem.release()
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['completed', 'error', 'cancelled'])
async def test_heartbeat_terminal_write_survives_stop(case, monkeypatch, outcome):
    manager, _ = case
    entered, release, finished = (asyncio.Event() for _ in range(3))
    monkeypatch.setattr('lantai.app.crons.manager.get_heartbeat_config', lambda _: SimpleNamespace(enabled=False, target='main'))
    monkeypatch.setattr('lantai.app.crons.manager.record_heartbeat_run_started', AsyncMock())

    async def operation(**kwargs):
        if outcome == 'error':
            raise ValueError('failure')
        if outcome == 'cancelled':
            raise asyncio.CancelledError
        return {'status': 'completed'}

    async def settle(**kwargs):
        assert kwargs['final_status'] == outcome
        entered.set()
        await release.wait()
        finished.set()

    monkeypatch.setattr('lantai.app.crons.manager.run_heartbeat_once', operation)
    monkeypatch.setattr('lantai.app.crons.manager.record_heartbeat_run_settled', settle)
    await manager.start()
    task = asyncio.create_task(manager._heartbeat_callback())
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(manager.stop())
    try:
        await asyncio.sleep(.03)
        assert not stop.done(), 'stop interrupted heartbeat settlement'
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and not stop.done()
        release.set()
        await asyncio.wait_for(stop, 2)
        assert finished.is_set() and not manager._owned_tasks
    finally:
        release.set()
        await asyncio.gather(task, stop, return_exceptions=True)
        await manager.stop()


@pytest.mark.asyncio
async def test_heartbeat_start_persistence_finishes_before_cancel_settlement(case, monkeypatch):
    manager, _ = case
    entered, release = asyncio.Event(), asyncio.Event()
    order = []
    monkeypatch.setattr('lantai.app.crons.manager.get_heartbeat_config', lambda _: SimpleNamespace(enabled=False, target='main'))

    async def start(**kwargs):
        entered.set()
        await release.wait()
        order.append('started')

    async def settle(**kwargs):
        assert kwargs['final_status'] == 'cancelled'
        order.append('settled')

    execute = AsyncMock()
    monkeypatch.setattr('lantai.app.crons.manager.record_heartbeat_run_started', start)
    monkeypatch.setattr('lantai.app.crons.manager.record_heartbeat_run_settled', settle)
    monkeypatch.setattr('lantai.app.crons.manager.run_heartbeat_once', execute)
    await manager.start()
    task = asyncio.create_task(manager._heartbeat_callback())
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(manager.stop())
    try:
        await asyncio.sleep(.03)
        assert not stop.done() and not order
        release.set()
        await asyncio.wait_for(stop, 2)
        assert order == ['started', 'settled']
        execute.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(task, stop, return_exceptions=True)
        await manager.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_manual_failure_receipt(case, monkeypatch):
    manager, job = case
    entered, release, finished = (asyncio.Event() for _ in range(3))
    manager._executor.execute = AsyncMock(side_effect=ValueError('executor failed'))

    async def append(*args, **kwargs):
        assert kwargs['source_id'] == job.id
        entered.set()
        await release.wait()
        finished.set()

    monkeypatch.setattr('lantai.app.crons.manager.push_store_append', append)
    await manager._repo.upsert_job(job)
    await manager.start()
    await manager.run_job(job.id)
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(manager.stop())
    try:
        await asyncio.sleep(.03)
        assert not stop.done(), 'failure receipt detached from invocation lifetime'
        stop.cancel()
        await asyncio.sleep(.01)
        assert not stop.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
        assert finished.is_set() and not manager._owned_tasks
    finally:
        release.set()
        await asyncio.gather(stop, return_exceptions=True)
        await manager.stop()


@pytest.mark.asyncio
async def test_registry_shutdown_drains_cron_executor_and_terminal_write(case, tmp_path, monkeypatch):
    from lantai.app.runner.task_tracker import SessionRuntimeRegistry
    from lantai.app.crons import manager as cron_module

    manager, job = case
    registry = SessionRuntimeRegistry()
    manager._task_tracker = registry
    entered, cleaning, release, settling, persisted = (asyncio.Event() for _ in range(5))
    original = cron_module.record_cron_job_settled

    async def execute(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    async def settle(**kwargs):
        settling.set()
        await persisted.wait()
        return await original(**kwargs)

    manager._executor.execute = execute
    monkeypatch.setattr(cron_module, 'record_cron_job_settled', settle)
    task = asyncio.create_task(manager._execute_once(job))
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await registry.has_active_tasks(), 'Cron execution is invisible to workspace shutdown'
        contract = RuntimeRunStore(tmp_path).list_contracts()[0]
        state = registry._runs[contract.run_id]
        assert state.attempt_id == contract.attempt_id
        assert state.root_session_id == job.dispatch.target.session_id
        snapshot = await registry.get_control_snapshot(contract.run_id)
        assert snapshot["activity_kind"] == "cron"
        assert snapshot["activity_name"] == job.name
        shutdown = asyncio.create_task(registry.shutdown())
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not shutdown.done()
        release.set()
        await asyncio.wait_for(settling.wait(), 2)
        assert await registry.has_active_tasks()
        assert not shutdown.done()
        persisted.set()
        await asyncio.wait_for(shutdown, 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await registry.has_active_tasks()
        assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'cancelled'
    finally:
        release.set()
        persisted.set()
        task.cancel()
        await asyncio.gather(task, *([shutdown] if shutdown else []), return_exceptions=True)
        await manager.stop()


@pytest.mark.asyncio
async def test_closed_registry_prevents_cron_effects(case, tmp_path):
    from lantai.app.runner.task_tracker import SessionRuntimeRegistry

    manager, job = case
    registry = SessionRuntimeRegistry()
    manager._task_tracker = registry
    await registry.shutdown()
    execute = AsyncMock()
    manager._executor.execute = execute
    with pytest.raises(RuntimeError, match='shutting down'):
        await manager._execute_once(job)
    execute.assert_not_awaited()
    assert not await registry.has_active_tasks()
    assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['register', 'unregister'])
async def test_cron_registry_pairing_survives_repeated_cancellation(case, phase):
    from lantai.app.runner.task_tracker import SessionRuntimeRegistry

    manager, job = case
    registry = SessionRuntimeRegistry()
    manager._task_tracker = registry
    entered, release = asyncio.Event(), asyncio.Event()
    original = getattr(registry, f'{phase}_external_task')

    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs) if phase == 'register' else None
        entered.set()
        await release.wait()
        if phase == 'unregister':
            return await original(*args, **kwargs)
        return result

    setattr(registry, f'{phase}_external_task', delayed)
    execute = AsyncMock()
    manager._executor.execute = execute
    task = asyncio.create_task(manager._execute_once(job))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(.01)
        assert not task.done()
        assert await registry.has_active_tasks()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await registry.has_active_tasks()
        assert not manager._rt[job.id].sem.locked()
        if phase == 'register':
            execute.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.stop()
