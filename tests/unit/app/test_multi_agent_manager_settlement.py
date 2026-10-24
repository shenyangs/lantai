"""Read-only source repros; all workspaces/services are disposable fakes."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import lantai.app.multi_agent_manager as manager_module
from lantai.app.multi_agent_manager import MultiAgentManager
from lantai.app.workspace.service_manager import ServiceDescriptor, ServiceManager
from lantai.app.workspace.workspace import Workspace


class Tracker:
    def __init__(self, *, active=False, unsettled=False):
        self.active = active
        self.unsettled = unsettled or active
        self.done = asyncio.Event()
        if not self.unsettled:
            self.done.set()

    async def has_active_tasks(self):
        return self.active

    async def has_unsettled_tasks(self):
        return self.unsettled

    async def list_active_tasks(self):
        return ["attempt-old"] if self.active else []

    async def wait_all_done(self, timeout=60.0):
        await self.done.wait()
        return True

    def settle(self):
        self.active = self.unsettled = False
        self.done.set()


class FakeWorkspace:
    def __init__(self, agent_id="default", workspace_dir="unused", **_kwargs):
        self.agent_id = agent_id
        self.workspace_dir = Path(workspace_dir)
        self.task_tracker = Tracker()
        self._service_manager = SimpleNamespace(
            services={}, get_reusable_services=lambda: {}
        )
        self.started = False
        self.stopped = False
        self.stop_error = None
        self.stop_calls = []

    async def start(self):
        self.started = True

    async def stop(self, final=True):
        self.stop_calls.append(final)
        if self.stop_error:
            raise self.stop_error
        self.stopped = True
        self.started = False

    def set_manager(self, manager):
        self.manager = manager

    async def set_reusable_components(self, components):
        self.reusable = components


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_module, "load_config", lambda: SimpleNamespace(
        agents=SimpleNamespace(profiles={
            "default": SimpleNamespace(workspace_dir=str(tmp_path))
        })
    ))
    monkeypatch.setattr(manager_module, "should_defer_desktop_workspace_io", lambda _: False)
    monkeypatch.setattr(manager_module, "Workspace", FakeWorkspace)
    return tmp_path


async def wait_thread_event(event):
    for _ in range(100):
        if event.is_set():
            return
        await asyncio.sleep(0.005)
    pytest.fail("controlled constructor did not reach its barrier")


@pytest.mark.asyncio
async def test_constructor_failure_releases_start_claim_and_wakes_waiters(configured, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def broken(*_args, **_kwargs):
        entered.set()
        if not release.wait(2):
            raise AssertionError("fixture barrier timed out")
        raise OSError("fixture-construction-failure")
    monkeypatch.setattr(manager_module, "Workspace", broken)
    manager = MultiAgentManager()
    first = asyncio.create_task(manager.get_agent("default"))
    waiter = None
    try:
        await wait_thread_event(entered)
        waiter = asyncio.create_task(manager.get_agent("default"))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(OSError):
            await first
        assert "default" not in manager._pending_starts, "failed construction retains an orphan startup claim"
        with pytest.raises(Exception):
            await asyncio.wait_for(waiter, timeout=0.1)
        assert not manager.agents
    finally:
        release.set()
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_constructor_cancellation_retains_or_settles_created_instance(configured, monkeypatch):
    entered, release, constructed = threading.Event(), threading.Event(), threading.Event()
    made = []
    def slow(*args, **kwargs):
        entered.set()
        if not release.wait(2):
            raise AssertionError("fixture barrier timed out")
        instance = FakeWorkspace(*args, **kwargs)
        made.append(instance)
        constructed.set()
        return instance
    monkeypatch.setattr(manager_module, "Workspace", slow)
    manager = MultiAgentManager()
    first = asyncio.create_task(manager.get_agent("default"))
    try:
        await wait_thread_event(entered)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        release.set()
        await wait_thread_event(constructed)
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0)
        assert made[0].stopped or made[0] in manager.agents.values(), "cancelled constructor's result has no retained owner or cleanup"
        assert "default" not in manager._pending_starts
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_does_not_allow_pending_start_to_repopulate_agent_table(configured, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    made = []
    def slow(*args, **kwargs):
        entered.set()
        if not release.wait(2):
            raise AssertionError("fixture barrier timed out")
        instance = FakeWorkspace(*args, **kwargs)
        made.append(instance)
        return instance
    monkeypatch.setattr(manager_module, "Workspace", slow)
    manager = MultiAgentManager()
    starter = asyncio.create_task(manager.get_agent("default"))
    shutdown = None
    try:
        await wait_thread_event(entered)
        shutdown = asyncio.create_task(manager.stop_all())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(starter, shutdown, return_exceptions=True)
        assert not manager.agents, "late constructor restarted a manager after shutdown"
        assert made[0].stopped, "shutdown did not settle its pending constructor"
    finally:
        release.set()
        await asyncio.gather(starter, *( [shutdown] if shutdown else [] ), return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_failure_retains_failed_workspace_owner(configured):
    manager = MultiAgentManager()
    instance = FakeWorkspace(workspace_dir=configured)
    instance.started = True
    instance.stop_error = OSError("fixture-resource-remains-open")
    manager.agents["default"] = instance
    try:
        await manager.stop_all()
    except Exception:
        pass
    assert manager.agents.get("default") is instance, "failed stop was erased from ownership registry"
    assert not instance.stopped


@pytest.mark.asyncio
async def test_terminal_response_with_pending_resources_delays_old_instance_stop(configured):
    manager = MultiAgentManager()
    old = FakeWorkspace(workspace_dir=configured)
    old.started = True
    old.task_tracker = Tracker(active=False, unsettled=True)
    try:
        await manager._graceful_stop_old_instance(old, "default")
        assert not old.stopped, "public terminal state was treated as resource settlement"
    finally:
        old.task_tracker.settle()
        await asyncio.gather(*list(manager._cleanup_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_settles_replaced_instance_after_cancelling_delayed_cleanup(configured):
    manager = MultiAgentManager()
    old = FakeWorkspace(workspace_dir=configured)
    old.started = True
    old.task_tracker = Tracker(active=True)
    manager.agents["default"] = old
    try:
        assert await manager.reload_agent("default")
        current = manager.agents["default"]
        assert current is not old
        assert not old.stopped
        await asyncio.sleep(0)
        await manager.stop_all()
        assert old.stopped, "replaced workspace lost its only cleanup owner during shutdown"
        assert current.stopped
    finally:
        old.task_tracker.settle()
        await manager.cancel_all_cleanup_tasks()


@pytest.mark.parametrize("failure", [OSError("fixture-stop-failure"), asyncio.CancelledError()])
@pytest.mark.asyncio
async def test_workspace_stop_does_not_report_success_when_a_service_fails(failure):
    class Service:
        async def stop(self):
            raise failure
    workspace = object.__new__(Workspace)
    workspace.agent_id = "fixture"
    workspace._started = True
    workspace._recovery_task = None
    services = ServiceManager(workspace)
    services.register(ServiceDescriptor(name="fixture-owned-resource", stop_method="stop"))
    services.services["fixture-owned-resource"] = Service()
    workspace._service_manager = services
    with pytest.raises((OSError, RuntimeError, asyncio.CancelledError)):
        await workspace.stop()
    assert workspace._started is True


@pytest.mark.asyncio
async def test_stop_agent_failure_already_retains_owner(configured):
    manager = MultiAgentManager()
    instance = FakeWorkspace(workspace_dir=configured)
    instance.stop_error = OSError("fixture")
    manager.agents["default"] = instance
    with pytest.raises(OSError):
        await manager.stop_agent("default")
    assert manager.agents["default"] is instance


@pytest.mark.asyncio
async def test_cooperative_reload_preserves_inflight_task_and_then_cleans_old_instance(configured):
    manager = MultiAgentManager()
    old = FakeWorkspace(workspace_dir=configured)
    old.started = True
    old.task_tracker = Tracker(active=True)
    manager.agents["default"] = old
    try:
        assert await manager.reload_agent("default")
        assert manager.agents["default"] is not old
        assert not old.stopped
        old.task_tracker.settle()
        await asyncio.gather(*list(manager._cleanup_tasks), return_exceptions=True)
        assert old.stopped
    finally:
        old.task_tracker.settle()
        await manager.cancel_all_cleanup_tasks()


@pytest.mark.asyncio
async def test_successful_concurrent_start_is_deduplicated(configured, monkeypatch):
    made = []
    def construct(*args, **kwargs):
        instance = FakeWorkspace(*args, **kwargs)
        made.append(instance)
        return instance
    monkeypatch.setattr(manager_module, "Workspace", construct)
    manager = MultiAgentManager()
    first, second = await asyncio.gather(manager.get_agent("default"), manager.get_agent("default"))
    assert first is second
    assert made == [first]
    assert not manager._pending_starts


@pytest.mark.asyncio
async def test_cancelled_service_constructor_is_registered_before_cleanup():
    entered, release = threading.Event(), threading.Event()

    class Resource:
        def __init__(self):
            entered.set()
            if not release.wait(2):
                raise AssertionError("fixture barrier timed out")
            self.stopped = False

        async def stop(self):
            self.stopped = True

    services = ServiceManager(SimpleNamespace(agent_id="fixture"))
    descriptor = ServiceDescriptor(name="resource", service_class=Resource, stop_method="stop")
    services.register(descriptor)
    constructor = asyncio.create_task(services._get_or_create_service(descriptor, False))
    try:
        await wait_thread_event(entered)
        constructor.cancel()
        await asyncio.sleep(0)
        constructor.cancel()
        release.set()
        await asyncio.gather(constructor, return_exceptions=True)
        assert "resource" in services.services, "cancelled constructor lost the resource returned by its worker"
        await services.stop_all(final=True)
        assert services.services["resource"].stopped
    finally:
        release.set()
        await asyncio.gather(constructor, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_start_waits_for_sibling_before_partial_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow:
        async def start(self):
            entered.set()
            await release.wait()

    class Broken:
        async def start(self):
            await entered.wait()
            raise OSError("fixture-start-failure")

    services = ServiceManager(SimpleNamespace(agent_id="fixture"))
    for name, service in (("slow", Slow()), ("broken", Broken())):
        services.register(ServiceDescriptor(name=name, start_method="start"))
        services.services[name] = service
        services.reused_services.add(name)
    # Use normal startup methods without constructors, including start on
    # these synthetic instances rather than the reuse startup bypass.
    async def start_service(descriptor):
        await services.services[descriptor.name].start()
    services._start_service = start_service
    startup = asyncio.create_task(services.start_all())
    try:
        await entered.wait()
        await asyncio.sleep(0.01)
        assert not startup.done(), "one failure let startup cleanup race a still-starting sibling"
        release.set()
        with pytest.raises(OSError):
            await startup
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancellation_settles_owned_stop_before_propagating(configured):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(FakeWorkspace):
        async def stop(self, final=True):
            entered.set()
            await release.wait()
            await super().stop(final=final)

    instance = Slow(workspace_dir=configured)
    manager = MultiAgentManager()
    manager.agents["default"] = instance
    shutdown = asyncio.create_task(manager.stop_all())
    try:
        await entered.wait()
        shutdown.cancel()
        await asyncio.sleep(0)
        shutdown.cancel()
        assert not instance.stopped
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert instance.stopped, "caller cancellation orphaned the owned shutdown"
        assert not manager.agents
    finally:
        release.set()
        await asyncio.gather(shutdown, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_shutdown_reports_failure_after_stopping_other_instances(configured):
    manager = MultiAgentManager()
    broken = FakeWorkspace(workspace_dir=configured)
    broken.stop_error = OSError("fixture-resource-remains-open")
    healthy = FakeWorkspace("healthy", configured)
    manager.agents.update(default=broken, healthy=healthy)
    with pytest.raises(RuntimeError):
        await manager.stop_all()
    assert healthy.stopped
    assert manager.agents == {"default": broken}


@pytest.mark.asyncio
async def test_shutdown_fences_pending_reload_and_settles_both_instances(configured, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    made = []

    def slow(*args, **kwargs):
        entered.set()
        if not release.wait(2):
            raise AssertionError("fixture barrier timed out")
        instance = FakeWorkspace(*args, **kwargs)
        made.append(instance)
        return instance

    monkeypatch.setattr(manager_module, "Workspace", slow)
    manager = MultiAgentManager()
    old = FakeWorkspace(workspace_dir=configured)
    manager.agents["default"] = old
    reload_task = asyncio.create_task(manager.reload_agent("default"))
    shutdown = None
    try:
        await wait_thread_event(entered)
        shutdown = asyncio.create_task(manager.stop_all())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(reload_task, shutdown, return_exceptions=True)
        assert old.stopped and made[0].stopped
        assert not manager.agents
    finally:
        release.set()
        await asyncio.gather(reload_task, *([shutdown] if shutdown else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_service_stop_failure_settles_siblings_and_retries_only_failure():
    class Resource:
        def __init__(self, failure=None):
            self.failure = failure
            self.calls = 0

        async def stop(self):
            self.calls += 1
            if self.failure is not None:
                raise self.failure

    services = ServiceManager(SimpleNamespace(agent_id="fixture"))
    broken, healthy = Resource(asyncio.CancelledError()), Resource()
    for name, service in (("broken", broken), ("healthy", healthy)):
        services.register(ServiceDescriptor(name=name, stop_method="stop"))
        services.services[name] = service
    with pytest.raises(RuntimeError):
        await services.stop_all(final=True)
    assert healthy.calls == 1 and broken.calls == 1
    broken.failure = None
    await services.stop_all(final=True)
    assert healthy.calls == 1 and broken.calls == 2


@pytest.mark.asyncio
async def test_failed_replacement_start_keeps_borrowed_service_running(monkeypatch):
    import lantai.agents.skills_manager as skills_module
    import lantai.app.workspace.workspace as workspace_module

    class Resource:
        def __init__(self):
            self.calls = 0

        async def close(self):
            self.calls += 1

    workspace = object.__new__(Workspace)
    workspace.agent_id = "fixture"
    workspace._started = False
    workspace._recovery_task = None
    workspace._migrate_legacy_weixin_data = lambda: None
    services = ServiceManager(workspace)
    services.register(ServiceDescriptor(name="borrowed", reusable=True, stop_method="close"))
    borrowed = Resource()
    workspace._service_manager = services
    await workspace.set_reusable_components({"borrowed": borrowed})

    async def fail_start():
        raise OSError("fixture-new-workspace-failure")

    services.start_all = fail_start
    monkeypatch.setattr(skills_module, "ensure_skill_pool_initialized", lambda: None)
    monkeypatch.setattr(workspace_module, "load_agent_config", lambda _: object())
    with pytest.raises(OSError):
        await workspace.start()
    assert borrowed.calls == 0, "failed replacement closed a resource still owned by the serving workspace"


@pytest.mark.asyncio
async def test_startup_cleanup_failure_remains_owned_and_can_be_retried(monkeypatch):
    import lantai.agents.skills_manager as skills_module
    import lantai.app.workspace.workspace as workspace_module

    class Resource:
        failure = True
        stopped = False

        async def close(self):
            if self.failure:
                raise OSError("fixture-resource-still-open")
            self.stopped = True

    workspace = object.__new__(Workspace)
    workspace.agent_id = "fixture"
    workspace._started = False
    workspace._recovery_task = None
    workspace._migrate_legacy_weixin_data = lambda: None
    services = ServiceManager(workspace)
    services.register(ServiceDescriptor(name="owned", stop_method="close"))
    owned = Resource()
    services.services["owned"] = owned
    workspace._service_manager = services

    async def fail_start():
        raise OSError("fixture-start-failure")

    services.start_all = fail_start
    monkeypatch.setattr(skills_module, "ensure_skill_pool_initialized", lambda: None)
    monkeypatch.setattr(workspace_module, "load_agent_config", lambda _: object())
    with pytest.raises(OSError):
        await workspace.start()
    owned.failure = False
    await workspace.stop()
    assert owned.stopped, "unstarted workspace silently skipped cleanup of its partial resource"
