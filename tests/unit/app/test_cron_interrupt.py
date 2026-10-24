"""A cron invocation is visible and interruptible without a chat owner."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from lantai.app.crons.manager import CronManager
from lantai.app.crons.repo.json_repo import JsonJobRepository
from lantai.app.crons.models import CronJobSpec, ScheduleSpec, DispatchSpec, DispatchTarget
from lantai.app.routers import runtime as router
from lantai.app.runtime_tasks.projection import build_runtime_task_board
from lantai.runtime.store import RuntimeRunStore


@pytest.mark.asyncio
async def test_cron_interrupt_targets_one_invocation_and_waits_for_cleanup(tmp_path, monkeypatch):
    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace())
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    manager._executor.execute = execute
    job = CronJobSpec(id='job-a', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    task = asyncio.create_task(manager._execute_once(job))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        contract = RuntimeRunStore(tmp_path).list_contracts()[0]
        board = build_runtime_task_board([tmp_path])
        assert any(item.id == contract.run_id and item.can_interrupt for item in board.items)
        workspace = SimpleNamespace(workspace_dir=tmp_path, cron_manager=manager)
        monkeypatch.setattr(router, '_workspace_for_request', AsyncMock(return_value=workspace))
        monkeypatch.setattr(router, '_runtime_workspace_dirs', AsyncMock(return_value=[tmp_path]))
        monkeypatch.setattr(router, '_find_chat_for_run', AsyncMock(return_value=None))
        body = router.RuntimeRunControlRequest(expected_attempt_id=contract.attempt_id, expected_revision=contract.revision)
        with pytest.raises(HTTPException) as stale:
            await router.interrupt_runtime_run(None, contract.run_id, body.model_copy(update={'expected_attempt_id':'other'}))
        assert stale.value.status_code == 409
        assert not cleaning.is_set()
        result = await router.interrupt_runtime_run(None, contract.run_id, body)
        assert result['accepted'] is True and result['stopped'] is False
        await asyncio.wait_for(cleaning.wait(), 1)
        assert not task.done()
        # Repeated stop must not cancel the cleanup coroutine a second time.
        again = await router.interrupt_runtime_run(None, contract.run_id, body)
        assert again['accepted'] is True
        await asyncio.sleep(0)
        assert not task.done()
        current = await RuntimeRunStore(tmp_path).load_contract(contract.run_id)
        assert current.status == 'running'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        current = await RuntimeRunStore(tmp_path).load_contract(contract.run_id)
        assert current.status == 'cancelled'
        result = await router.interrupt_runtime_run(None, contract.run_id, router.RuntimeRunControlRequest())
        assert not result['accepted']
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_cannot_cancel_a_different_cron_invocation(tmp_path):
    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace())
    entered = asyncio.Queue()

    async def execute(job):
        await entered.put(job.id)
        await asyncio.Event().wait()

    manager._executor.execute = execute
    tasks = []
    try:
        for name in ['a', 'b']:
            job = CronJobSpec(id=name, name=name, schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='shared')))
            tasks.append(asyncio.create_task(manager._execute_once(job)))
            await asyncio.wait_for(entered.get(), 2)
        contracts = RuntimeRunStore(tmp_path).list_contracts()
        first = next(c for c in contracts if c.owner.session_id == 'cron:a')
        assert not manager.request_stop_run(first.run_id, expected_attempt_id='stale')
        assert not manager.request_stop_run('missing', expected_attempt_id=first.attempt_id)
        assert manager.request_stop_run(first.run_id, expected_attempt_id=first.attempt_id)
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert not tasks[1].done()
        assert len(manager._active_runs) == 1
        assert first.run_id not in manager._stop_requested_runs
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_first_stop_during_settlement_cannot_cancel_terminal_write(tmp_path, monkeypatch):
    from lantai.app.crons import manager as cron_module

    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace())
    settling, release = asyncio.Event(), asyncio.Event()
    original = cron_module.record_cron_job_settled

    async def settle(**kwargs):
        settling.set()
        await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(cron_module, 'record_cron_job_settled', settle)
    manager._executor.execute = AsyncMock()
    job = CronJobSpec(id='settling', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    task = asyncio.create_task(manager._execute_once(job))
    try:
        await asyncio.wait_for(settling.wait(), 2)
        contract = RuntimeRunStore(tmp_path).list_contracts()[0]
        assert not manager.request_stop_run(contract.run_id, expected_attempt_id=contract.attempt_id)
        release.set()
        await task
        current = await RuntimeRunStore(tmp_path).load_contract(contract.run_id)
        assert current.status == 'succeeded'
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('execution_outcome', ['success', 'cancelled', 'error'])
async def test_external_repeat_cancel_drains_terminal_write(tmp_path, monkeypatch, execution_outcome):
    from lantai.app.crons import manager as cron_module

    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace())
    settling, release = asyncio.Event(), asyncio.Event()
    original = cron_module.record_cron_job_settled
    written = []

    async def execute(job):
        if execution_outcome == 'cancelled':
            raise asyncio.CancelledError
        if execution_outcome == 'error':
            raise ValueError('execution failed')

    async def settle(**kwargs):
        settling.set()
        await release.wait()
        result = await original(**kwargs)
        written.append(kwargs['final_status'])
        return result

    monkeypatch.setattr(cron_module, 'record_cron_job_settled', settle)
    manager._executor.execute = execute
    job = CronJobSpec(id='external-stop', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    task = asyncio.create_task(manager._execute_once(job))
    try:
        await asyncio.wait_for(settling.wait(), 2)
        contract = RuntimeRunStore(tmp_path).list_contracts()[0]
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
        assert not task.done(), 'caller must own terminal persistence until it settles'
        assert manager._rt[job.id].sem.locked()
        assert contract.run_id not in manager._active_runs
        assert (await RuntimeRunStore(tmp_path).load_contract(contract.run_id)).status == 'running'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert written == [execution_outcome]
        expected = {'success': 'succeeded', 'cancelled': 'cancelled', 'error': 'failed'}[execution_outcome]
        assert (await RuntimeRunStore(tmp_path).load_contract(contract.run_id)).status == expected
        assert not manager._rt[job.id].sem.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_caller', [False, True])
async def test_settlement_failure_is_observable_and_releases_slot(tmp_path, monkeypatch, caplog, cancel_caller):
    from lantai.app.crons import manager as cron_module

    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace())
    entered, release = asyncio.Event(), asyncio.Event()

    async def settle(**kwargs):
        entered.set()
        await release.wait()
        raise OSError('private persistence contents')

    monkeypatch.setattr(cron_module, 'record_cron_job_settled', settle)
    manager._executor.execute = AsyncMock()
    job = CronJobSpec(id='failed-settle', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    task = asyncio.create_task(manager._execute_once(job))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cancel_caller:
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0.01)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel_caller else OSError):
            await task
        assert 'cron settlement failed' in caplog.text
        assert 'error_type=OSError' in caplog.text
        assert 'private persistence contents' not in caplog.text
        assert not manager._rt[job.id].sem.locked()
        assert not manager._active_runs
        assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'running'
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_execution_cancel_survives_failed_settlement(tmp_path, monkeypatch, caplog):
    from lantai.app.crons import manager as cron_module

    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace())
    executing = asyncio.Event()

    async def execute(job):
        executing.set()
        await asyncio.Event().wait()

    async def settle(**kwargs):
        raise OSError('private persistence contents')

    monkeypatch.setattr(cron_module, 'record_cron_job_settled', settle)
    manager._executor.execute = execute
    job = CronJobSpec(id='cancel-then-failure', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    task = asyncio.create_task(manager._execute_once(job))
    await asyncio.wait_for(executing.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 'error_type=OSError' in caplog.text
    assert 'private persistence contents' not in caplog.text
    assert not manager._rt[job.id].sem.locked()
    assert not manager._active_runs


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup_fails', [False, True])
async def test_repeated_external_cancel_drains_actual_executor_cleanup(tmp_path, caplog, cleanup_fails):
    executing, cleaning, release, finished = (asyncio.Event() for _ in range(4))
    sends = []

    async def send_text(**kwargs):
        sends.append(kwargs['text'])
        executing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()
            if cleanup_fails:
                raise OSError('private cleanup contents')

    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace(send_text=send_text))
    job = CronJobSpec(id='executor-cleanup', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    task = asyncio.create_task(manager._execute_once(job))
    try:
        await asyncio.wait_for(executing.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
        assert not task.done(), 'actual executor cleanup must stay owned'
        assert manager._rt[job.id].sem.locked()
        assert not finished.is_set()
        assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'running'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert sends == ['test']
        assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'cancelled'
        assert not manager._rt[job.id].sem.locked()
        if cleanup_fails:
            assert 'cron executor cleanup failed' in caplog.text
            assert 'error_type=OSError' in caplog.text
            assert 'private cleanup contents' not in caplog.text
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_agent_timeout_cleanup_stays_owned_when_invocation_cancelled(tmp_path):
    from lantai.app.crons.models import CronJobRequest, JobRuntimeSpec

    cleaning, release, finished = (asyncio.Event() for _ in range(3))

    async def stream_query(request):
        try:
            await asyncio.Event().wait()
            yield None
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path, stream_query=stream_query), channel_manager=SimpleNamespace(send_event=AsyncMock()))
    job = CronJobSpec(id='timeout-cleanup', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='agent', request=CronJobRequest(input=[]), runtime=JobRuntimeSpec(timeout_seconds=1), dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    task = asyncio.create_task(manager._execute_once(job))
    try:
        await asyncio.wait_for(cleaning.wait(), 3)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
        assert not task.done(), 'timed agent cleanup must stay owned'
        assert manager._rt[job.id].sem.locked()
        assert not finished.is_set()
        assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'running'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'cancelled'
        assert not manager._rt[job.id].sem.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('start_fails', [False, True])
async def test_cancel_during_start_settles_same_invocation_without_execution(tmp_path, monkeypatch, start_fails):
    from lantai.app.experience import events

    entered, release = asyncio.Event(), asyncio.Event()
    original = events.append_task_event

    async def delayed_start(**kwargs):
        if kwargs['kind'] == 'cron.job.started':
            entered.set()
            await release.wait()
            if start_fails:
                raise OSError('private start contents')
        return await original(**kwargs)

    monkeypatch.setattr(events, 'append_task_event', delayed_start)
    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace())
    execute = AsyncMock()
    manager._executor.execute = execute
    job = CronJobSpec(id='starting-stop', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    task = asyncio.create_task(manager._execute_once(job))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        before = RuntimeRunStore(tmp_path).list_contracts()
        assert len(before) == 1 and before[0].status == 'running'
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
        assert not task.done(), 'start persistence must remain owned'
        assert manager._rt[job.id].sem.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        after = RuntimeRunStore(tmp_path).list_contracts()
        assert len(after) == 1 and after[0].run_id == before[0].run_id
        assert after[0].status == 'cancelled'
        execute.assert_not_awaited()
        assert not manager._rt[job.id].sem.locked()
        assert not manager._active_runs
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['cancel_start', 'fail_start', 'cancel_settle'])
async def test_skipped_invocation_owns_partial_persistence(tmp_path, monkeypatch, mode):
    from lantai.app.experience import events
    from lantai.app.crons import manager as cron_module

    entered, release = asyncio.Event(), asyncio.Event()
    original_append = events.append_task_event
    original_settle = cron_module.record_cron_job_settled

    async def append(**kwargs):
        if kwargs['kind'] == 'cron.job.started' and mode != 'cancel_settle':
            entered.set()
            await release.wait()
            if mode == 'fail_start':
                raise OSError('synthetic partial write')
        return await original_append(**kwargs)

    async def settle(**kwargs):
        if mode == 'cancel_settle':
            entered.set()
            await release.wait()
        return await original_settle(**kwargs)

    monkeypatch.setattr(events, 'append_task_event', append)
    monkeypatch.setattr(cron_module, 'record_cron_job_settled', settle)
    manager = CronManager(repo=JsonJobRepository(tmp_path / 'jobs.json'), runner=SimpleNamespace(workspace_dir=tmp_path), channel_manager=SimpleNamespace())
    job = CronJobSpec(id='skip-persist', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))
    task = asyncio.create_task(manager._record_skipped_invocation(job, reason='max_concurrency_reached'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        before = RuntimeRunStore(tmp_path).list_contracts()
        assert len(before) == 1 and before[0].status == 'running'
        if mode.startswith('cancel'):
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0.01)
            assert not task.done(), 'skipped persistence must stay owned'
        release.set()
        with pytest.raises(OSError if mode == 'fail_start' else asyncio.CancelledError):
            await task
        after = RuntimeRunStore(tmp_path).list_contracts()
        assert len(after) == 1 and after[0].run_id == before[0].run_id
        assert after[0].status == ('failed' if mode == 'fail_start' else 'cancelled')
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
