"""Workspace lifecycle behavior locks; resources and config are synthetic."""
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import sys

import pytest

for dependency in ('aibot', 'lark_oapi'):
    sys.modules.setdefault(dependency, MagicMock())

import lantai.app.multi_agent_manager as mm
import lantai.app.workspace.workspace as wm
import lantai.agents.skills_manager as skills
from lantai.app.multi_agent_manager import MultiAgentManager
from lantai.app.workspace.workspace import Workspace
from lantai.app.workspace.service_manager import ServiceDescriptor, ServiceManager
from lantai.app.runner.runner import AgentRunner
from lantai.app.runner.task_tracker import SessionRuntimeRegistry
from lantai.runtime.tool_transaction import execute_owned_sync


def configured(monkeypatch, tmp_path, factory):
    monkeypatch.setattr(mm, 'load_config', lambda: SimpleNamespace(agents=SimpleNamespace(profiles={
        'default': SimpleNamespace(workspace_dir=str(tmp_path)),
    })))
    monkeypatch.setattr(mm, 'should_defer_desktop_workspace_io', lambda _: False)
    monkeypatch.setattr(mm, 'Workspace', factory)


def workspace(registry=None):
    ws = object.__new__(Workspace)
    ws.agent_id = 'default'
    ws.workspace_dir = Path('synthetic-unused')
    ws._started = True
    ws._stop_task = None
    ws._startup_cleanup_pending = False
    ws._owns_reused_services = True
    ws._recovery_task = None
    ws._task_tracker = registry or SessionRuntimeRegistry()
    ws._service_manager = ServiceManager(ws)
    return ws


@pytest.mark.asyncio
async def test_workspace_shutdown_cannot_release_running_sync_worker():
    registry = SessionRuntimeRegistry()
    ws = workspace(registry)
    runner = object.__new__(AgentRunner)
    runner._started = True
    ws._service_manager.register(ServiceDescriptor(name='runner', stop_method='stop'))
    ws._service_manager.services['runner'] = runner
    entered, release = threading.Event(), threading.Event()
    def worker():
        entered.set()
        assert release.wait(3), 'synthetic worker barrier timed out'
    async def stream(_payload):
        await execute_owned_sync(worker, {})
        yield 'synthetic-done'
    await registry.attach_or_start('synthetic-chat', {}, stream)
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(.005)
    assert entered.is_set()
    manager = MultiAgentManager()
    manager.agents['default'] = ws
    shutdown = asyncio.create_task(manager.stop_all())
    try:
        await asyncio.sleep(.03)
        assert await registry.has_unsettled_tasks()
        assert not shutdown.done() or manager.agents.get('default') is ws or id(ws) in manager._retired_instances, (
            'stop_all returned success and discarded workspace while its real registry owns a running sync worker'
        )
    finally:
        release.set()
        await asyncio.gather(shutdown, return_exceptions=True)
        assert await registry.wait_all_done(timeout=1)


@pytest.mark.asyncio
async def test_cancelled_start_with_failed_cleanup_preserves_workspace_owner(monkeypatch, tmp_path):
    ws = workspace()
    ws._started = False
    ws._migrate_legacy_weixin_data = lambda: None
    class Resource:
        failure = True
        closed = False
        async def close(self):
            if self.failure:
                raise OSError('synthetic-cleanup-failed')
            self.closed = True
    resource = Resource()
    ws._service_manager.register(ServiceDescriptor(name='resource', stop_method='close'))
    ws._service_manager.services['resource'] = resource
    async def cancel_start():
        raise asyncio.CancelledError()
    ws._service_manager.start_all = cancel_start
    monkeypatch.setattr(skills, 'ensure_skill_pool_initialized', lambda: None)
    monkeypatch.setattr(wm, 'load_agent_config', lambda _: object())
    configured(monkeypatch, tmp_path, lambda *_a, **_kw: ws)
    manager = MultiAgentManager()
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.get_agent('default')
        assert ws._startup_cleanup_pending
        assert ws in manager.agents.values() or id(ws) in manager._retired_instances, (
            'CancelledError bypassed manager retention: pending cleanup remains but all workspace owners were removed'
        )
    finally:
        resource.failure = False
        await ws.stop()
        await manager.stop_all()


class Tracker:
    async def has_unsettled_tasks(self):
        return False


class FakeWorkspace:
    def __init__(self, agent_id='default', *_args, **_kwargs):
        self.agent_id = agent_id
        self.task_tracker = Tracker()
        self._service_manager = SimpleNamespace(services={}, get_reusable_services=lambda: {})
        self.stopped = False
        self.start_entered = None
        self.start_release = None
        self.stop_entered = None
        self.stop_release = None
    async def start(self):
        if self.start_entered is not None:
            self.start_entered.set()
            await self.start_release.wait()
    async def stop(self, final=True):
        if self.stop_entered is not None:
            self.stop_entered.set()
            await self.stop_release.wait()
        self.stopped = True
    def set_manager(self, manager):
        self.manager = manager
    async def set_reusable_components(self, _components):
        pass


@pytest.mark.asyncio
async def test_stop_agent_overlapping_reload_cannot_report_success_with_live_replacement(monkeypatch, tmp_path):
    old, new = FakeWorkspace(), FakeWorkspace()
    old.stop_entered, old.stop_release = asyncio.Event(), asyncio.Event()
    new.start_entered, new.start_release = asyncio.Event(), asyncio.Event()
    configured(monkeypatch, tmp_path, lambda *_a, **_kw: new)
    manager = MultiAgentManager()
    manager.agents['default'] = old
    reloader = asyncio.create_task(manager.reload_agent('default'))
    stopper = None
    try:
        await asyncio.wait_for(new.start_entered.wait(), .5)
        stopper = asyncio.create_task(manager.stop_agent('default'))
        await asyncio.sleep(.01)
        assert not stopper.done()
        new.start_release.set()
        await asyncio.wait_for(old.stop_entered.wait(), .5)
        old.stop_release.set()
        stopped, _reload = await asyncio.gather(stopper, reloader)
        assert stopped is True
        assert 'default' not in manager.agents and new.stopped, (
            'stop_agent returned True while the overlapping reload published a running replacement'
        )
    finally:
        new.start_release.set()
        old.stop_release.set()
        await asyncio.gather(reloader, *([stopper] if stopper else []), return_exceptions=True)
        await manager.stop_all()


@pytest.mark.asyncio
async def test_retirement_timeout_does_not_stop_unsettled_healthy_instance():
    class UnsettledTracker:
        async def has_unsettled_tasks(self):
            return True
        async def wait_all_done(self, timeout):
            return False
    manager = MultiAgentManager()
    old = FakeWorkspace()
    old.task_tracker = UnsettledTracker()
    await manager._graceful_stop_old_instance(old, 'default')
    tasks = list(manager._cleanup_tasks)
    try:
        await asyncio.gather(*tasks)
        assert not old.stopped and id(old) in manager._retired_instances, (
            'retirement timeout closed and dropped an instance whose tracker explicitly remained unsettled'
        )
    finally:
        await manager.cancel_all_cleanup_tasks()
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancellation_count', [0, 2])
async def test_real_watcher_self_reload_keeps_distinct_owned_task(monkeypatch, tmp_path, cancellation_count):
    import lantai.app.agent_config_watcher as watch
    old, new = workspace(), FakeWorkspace()
    new.start_entered, new.start_release = asyncio.Event(), asyncio.Event()
    manager = MultiAgentManager()
    old._manager = manager
    manager.agents['default'] = old
    configured(monkeypatch, tmp_path, lambda *_a, **_kw: new)
    version = [0]
    class Channels:
        def model_dump(self, mode):
            return {'synthetic': version[0]}
    monkeypatch.setattr(watch, 'load_agent_config', lambda _: SimpleNamespace(channels=Channels(), heartbeat=None))
    watcher = watch.AgentConfigWatcher('default', tmp_path, old, poll_interval=.001)
    watcher._read_mtime = lambda: float(version[0])
    old._service_manager.register(ServiceDescriptor(name='agent_config_watcher', stop_method='stop'))
    old._service_manager.services['agent_config_watcher'] = watcher
    await watcher.start()
    version[0] = 1
    try:
        await asyncio.wait_for(new.start_entered.wait(), .5)
        assert watcher._disabled
        assert not watcher._task.done()
        assert manager._reload_tasks['default'] is not watcher._task
        for _ in range(cancellation_count):
            watcher._task.cancel()
            await asyncio.sleep(0)
        new.start_release.set()
        await asyncio.wait_for(asyncio.gather(watcher._task, return_exceptions=True), .5)
        assert manager.agents['default'] is new
        assert not manager._reload_tasks
        assert not old._started
        if cancellation_count:
            assert watcher._task.cancelled()
    finally:
        new.start_release.set()
        if watcher._task and not watcher._task.done():
            watcher._task.cancel()
        if watcher._task:
            await asyncio.gather(watcher._task, return_exceptions=True)
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_all', [False, True])
async def test_stopping_replacement_does_not_close_memory_used_by_retired_live_session(stop_all):
    old_registry = SessionRuntimeRegistry()
    old, new = workspace(old_registry), workspace()
    new._started = False
    class SharedMemory:
        closed = False
        close_while_unsettled = False
        async def close(self):
            self.close_while_unsettled = await old_registry.has_unsettled_tasks()
            self.closed = True
    shared = SharedMemory()
    for ws in (old, new):
        ws._service_manager.register(ServiceDescriptor(name='memory', reusable=True, stop_method='close'))
    old._service_manager.services['memory'] = shared
    await new.set_reusable_components(old._service_manager.get_reusable_services())
    new.claim_reusable_components()
    new._started = True
    assert new._service_manager.services['memory'] is shared
    release = asyncio.Event()
    entered = asyncio.Event()
    async def stream(_payload):
        entered.set()
        await release.wait()
        yield 'synthetic-done'
    await old_registry.attach_or_start('synthetic-retired-chat', {}, stream)
    await entered.wait()
    manager = MultiAgentManager()
    manager.agents['default'] = new
    await manager._graceful_stop_old_instance(old, 'default')
    try:
        if stop_all:
            await manager.stop_all()
        else:
            await manager.stop_agent('default')
        assert not shared.close_while_unsettled, 'replacement stop closed reusable memory still consumed by its retained live old session'
        assert shared.closed and not await old_registry.has_unsettled_tasks()
    finally:
        release.set()
        assert await old_registry.wait_all_done(timeout=1)
        await asyncio.gather(*list(manager._cleanup_tasks), return_exceptions=True)
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['stream_query', 'query_handler'])
async def test_real_dynamic_runner_shutdown_settles_actual_worker_before_services(entrypoint):
    from lantai.app._app import DynamicMultiAgentRunner
    registry = SessionRuntimeRegistry()
    ws = workspace(registry)
    entered, release = threading.Event(), threading.Event()
    def worker():
        entered.set()
        assert release.wait(3), 'synthetic worker barrier timed out'
    class Runner:
        stopped = False
        async def stop(self):
            self.stopped = True
        async def stream_query(self, request, *args, **kwargs):
            await execute_owned_sync(worker, {})
            yield 'synthetic-done'
        async def query_handler(self, msgs, *args, request=None, **kwargs):
            await execute_owned_sync(worker, {})
            yield 'synthetic-done'
    runner = Runner()
    ws._service_manager.register(ServiceDescriptor(name='runner', stop_method='stop'))
    ws._service_manager.services['runner'] = runner
    manager = MultiAgentManager()
    manager.agents['default'] = ws
    dynamic = DynamicMultiAgentRunner()
    dynamic.set_multi_agent_manager(manager)
    request = SimpleNamespace(input=[])
    async def consume():
        return [item async for item in getattr(dynamic, entrypoint)(request)]
    consumer = asyncio.create_task(consume())
    shutdown = None
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.005)
        assert entered.is_set()
        shutdown = asyncio.create_task(manager.stop_all())
        await asyncio.sleep(.01)
        assert await registry.has_unsettled_tasks()
        assert manager.agents.get('default') is ws
        assert not runner.stopped
        shutdown.cancel()
        await asyncio.sleep(0)
        shutdown.cancel()
        release.set()
        await asyncio.gather(consumer, return_exceptions=True)
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert runner.stopped
        assert not manager.agents
        assert not await registry.has_unsettled_tasks()
    finally:
        release.set()
        await asyncio.gather(consumer, *([shutdown] if shutdown else []), return_exceptions=True)
        await manager.stop_all()


@pytest.mark.asyncio
async def test_workspace_cron_uses_shared_registry_and_drains_before_runner_close(tmp_path):
    from lantai.app.crons.models import CronJobSpec, DispatchSpec, DispatchTarget, ScheduleSpec
    from lantai.runtime.store import RuntimeRunStore

    ws = workspace()
    ws.workspace_dir = tmp_path
    ws._register_services()
    entered, cleaning, release = (asyncio.Event() for _ in range(3))
    runner = SimpleNamespace(workspace_dir=tmp_path, closed=False)

    async def close():
        assert not await ws._task_tracker.has_unsettled_tasks()
        runner.closed = True

    runner.stop = close
    ws._service_manager.services['runner'] = runner
    descriptor = ws._service_manager.descriptors['cron_manager']
    manager = descriptor.service_class(**descriptor.init_args(ws))
    ws._service_manager.services['cron_manager'] = manager
    assert manager._task_tracker is ws._task_tracker
    job = CronJobSpec(id='workspace-cron', name='Test', schedule=ScheduleSpec(cron='0 9 * * *'), task_type='text', text='test', dispatch=DispatchSpec(channel='console', target=DispatchTarget(user_id='u', session_id='s')))

    async def execute(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            assert not runner.closed

    manager._executor.execute = execute
    await manager._repo.upsert_job(job)
    await manager.run_job(job.id)
    await asyncio.wait_for(entered.wait(), 2)
    shutdown = asyncio.create_task(ws.stop())
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not shutdown.done() and not runner.closed
        release.set()
        await asyncio.wait_for(shutdown, 2)
        assert runner.closed
        assert RuntimeRunStore(tmp_path).list_contracts()[0].status == 'cancelled'
        assert not ws._started
    finally:
        release.set()
        await asyncio.gather(shutdown, return_exceptions=True)
        await manager.stop()
