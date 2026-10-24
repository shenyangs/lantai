import asyncio
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from lantai.app.runner import task_tracker as module
from lantai.app.runner.task_tracker import ActiveRunRequestConflict, SessionRuntimeRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize('acknowledges', [True, False])
async def test_external_stop_waits_for_unregister_acknowledgement(monkeypatch, acknowledges):
    monkeypatch.setattr(module, '_STOP_SETTLEMENT_WAIT_SECONDS', 0.02)
    registry = SessionRuntimeRegistry()
    entered = asyncio.Event()

    async def producer():
        await registry.register_external_task(
            'external', owner_task=asyncio.current_task(),
        )
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            if acknowledges:
                await registry.unregister_external_task('external')

    owner = asyncio.create_task(producer())
    await entered.wait()
    placeholder = registry._runs['external'].task
    try:
        assert await registry.request_stop('external') is True
        if acknowledges:
            assert await registry.get_status('external') == 'idle'
            assert not await registry.has_unsettled_tasks()
        else:
            assert owner.done(), 'owner exit alone is not a cleanup acknowledgement'
            assert not placeholder.done(), 'stop must not cancel the placeholder on timeout'
            assert await registry.has_unsettled_tasks()
            assert (await registry.get_settlement_snapshot('external'))['settlement_pending']
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        await registry.unregister_external_task('external')


@pytest.mark.asyncio
@pytest.mark.parametrize('cancellation_count', [0, 2])
async def test_sync_failure_preserves_requested_cancellation(cancellation_count):
    from lantai.runtime.tool_transaction import execute_owned_sync
    entered, release = threading.Event(), threading.Event()
    def worker():
        entered.set()
        assert release.wait(3), 'synthetic worker barrier timed out'
        raise OSError('synthetic-worker-failure')
    owned = asyncio.create_task(execute_owned_sync(worker, {}))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.005)
        assert entered.is_set()
        for _ in range(cancellation_count):
            owned.cancel()
            await asyncio.sleep(0)
        if cancellation_count:
            assert not owned.done()
        release.set()
        expected = asyncio.CancelledError if cancellation_count else OSError
        with pytest.raises(expected):
            await owned
    finally:
        release.set()
        await asyncio.gather(owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_sync_worker_retains_attempt_until_actual_completion(monkeypatch):
    from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
    monkeypatch.setattr(module, '_STOP_SETTLEMENT_WAIT_SECONDS', 0.01)
    registry = SessionRuntimeRegistry()
    loop = asyncio.get_running_loop()
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    writes = []
    def worker():
        loop.call_soon_threadsafe(started.set)
        release.wait()
        writes.append('old completed')
        loop.call_soon_threadsafe(finished.set)
    async def stream(_):
        await NativeLantaiRuntime._execute_callable(None, worker, {})
        yield 'data: old-result\n\n'
    queue, _ = await registry.attach_or_start('chat', {}, stream, request_id='first')
    producer = registry._runs['chat'].task
    await started.wait()
    try:
        assert await registry.request_stop('chat')
        assert not await registry.wait_all_done(timeout=0.01)
        with pytest.raises(ActiveRunRequestConflict):
            await registry.attach_or_start('chat', {}, stream, request_id='next', reject_conflicting_request=True)
    finally:
        release.set()
        await finished.wait()
        await producer
    assert writes == ['old completed']
    assert all('old-result' not in item for item in [frame async for frame in registry.stream_from_queue(queue, 'chat')])


@pytest.mark.asyncio
async def test_mcp_close_retains_attempt_until_actual_completion(monkeypatch):
    from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
    from lantai.runtime.tools import ToolCall
    monkeypatch.setattr(module, '_STOP_SETTLEMENT_WAIT_SECONDS', 0.01)
    registry = SessionRuntimeRegistry()
    started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Client:
        async def call_tool(self, *_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await release.wait()
    async def stream(_):
        await NativeLantaiRuntime._execute_mcp_tool(None,
            SimpleNamespace(client=Client(), execution_timeout=1),
            ToolCall(id='mcp-1', name='fake', arguments={}))
        yield 'data: late-mcp\n\n'
    await registry.attach_or_start('chat', {}, stream, request_id='first')
    producer = registry._runs['chat'].task
    await started.wait()
    try:
        assert await registry.request_stop('chat')
        await closing.wait()
        assert not await registry.wait_all_done(timeout=0.01)
        assert (await registry.get_snapshot('chat'))['status'] == 'stopping'
    finally:
        release.set()
        await producer


@pytest.mark.asyncio
async def test_cancelled_unregister_still_settles_successful_cleanup():
    registry = SessionRuntimeRegistry()
    started, release = asyncio.Event(), asyncio.Event()
    async def cleanup():
        started.set()
        await release.wait()
    await registry.register_external_task('chat', cleanup=cleanup)
    unregister = asyncio.create_task(registry.unregister_external_task('chat'))
    await started.wait()
    unregister.cancel()
    with pytest.raises(asyncio.CancelledError):
        await unregister
    release.set()
    assert await registry.wait_all_done(timeout=0.1)
    assert await registry.get_status('chat') == 'idle'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['orphan_stop', 'reconcile', 'refresh'])
async def test_terminal_cleanup_is_never_reconciled_as_orphan(tmp_path, operation):
    from lantai.app.runner.manager import ChatManager
    from lantai.app.runner.models import ChatSpec
    from lantai.app.runner.repo.json_repo import JsonChatRepository
    from lantai.app.runner import stall_watchdog
    registry = SessionRuntimeRegistry()
    manager = ChatManager(repo=JsonChatRepository(tmp_path / 'chats.json'))
    chat = await manager.create_chat(ChatSpec(
        id='chat', session_id='console:chat', user_id='default', channel='console',
        meta={'topic_workspace_dir': str(tmp_path), 'pending_turn': {
            'text': 'fixture task', 'run_id': 'run-1', 'status': 'running',
            'created_at': datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
        }},
    ))
    workspace = SimpleNamespace(workspace_dir=tmp_path, agent_id='default',
                                task_tracker=registry, chat_manager=manager)
    started, release = asyncio.Event(), asyncio.Event()
    async def cleanup():
        started.set()
        await release.wait()
    async def stream(_):
        yield 'data: {"object":"response","type":"response.completed","status":"completed"}\n\n'
    await registry.attach_or_start('chat', {}, stream, cleanup=cleanup)
    producer = registry._runs['chat'].task
    await started.wait()
    try:
        assert await registry.get_status('chat') == 'idle'
        if operation == 'orphan_stop':
            updated, changed = await stall_watchdog.settle_orphaned_pending_turn_on_stop(
                mgr=manager, workspace=workspace, chat_spec=chat, requested_chat_id='chat')
        elif operation == 'reconcile':
            updated, changed = await stall_watchdog.reconcile_pending_chat_stall(
                mgr=manager, workspace=workspace, chat_spec=chat, force=True)
        else:
            updated = (await stall_watchdog.refresh_stalled_chats(workspace, [chat]))[0]
            changed = updated.meta != chat.meta
        assert changed is False
        assert updated.meta['pending_turn']['status'] == 'running'
    finally:
        release.set()
        await producer


@pytest.mark.asyncio
async def test_stop_timeout_keeps_slot_and_fences_old_events(monkeypatch):
    monkeypatch.setattr(module, '_STOP_SETTLEMENT_WAIT_SECONDS', 0.01)
    registry = SessionRuntimeRegistry()
    started, release = asyncio.Event(), asyncio.Event()
    async def stream(_):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            yield 'data: late-old-generation\n\n'
    queue, _ = await registry.attach_or_start('chat', {}, stream, request_id='first')
    task = registry._runs['chat'].task
    await started.wait()
    try:
        assert await registry.request_stop('chat')
        snapshot = await registry.get_snapshot('chat')
        assert snapshot is not None and snapshot['status'] == 'stopping'
        with pytest.raises(ActiveRunRequestConflict):
            await registry.attach_or_start('chat', {}, stream, request_id='second', reject_conflicting_request=True)
    finally:
        release.set()
        await task
    assert all('late-old-generation' not in item for item in [frame async for frame in registry.stream_from_queue(queue, 'chat')])


@pytest.mark.asyncio
async def test_stop_waits_for_existing_external_cleanup(monkeypatch):
    monkeypatch.setattr(module, '_STOP_SETTLEMENT_WAIT_SECONDS', 0.01)
    registry = SessionRuntimeRegistry()
    started, release = asyncio.Event(), asyncio.Event()
    async def cleanup():
        started.set()
        await release.wait()
    await registry.register_external_task('chat', cleanup=cleanup)
    unregister = asyncio.create_task(registry.unregister_external_task('chat'))
    await started.wait()
    try:
        assert await registry.request_stop('chat')
        assert not await registry.wait_all_done(timeout=0.01)
        assert await registry.get_snapshot('chat') is not None
    finally:
        release.set()
        await unregister
    assert await registry.wait_all_done(timeout=0.1)


@pytest.mark.asyncio
async def test_cleanup_failure_retains_unsettled_owner():
    registry = SessionRuntimeRegistry()
    async def cleanup():
        raise OSError('private cleanup detail')
    await registry.register_external_task('chat', cleanup=cleanup)
    await registry.unregister_external_task('chat')
    snapshot = await registry.get_snapshot('chat')
    assert snapshot is not None and snapshot['status'] == 'cleanup_failed'
    assert not await registry.wait_all_done(timeout=0.01)
    assert 'private cleanup detail' not in str(snapshot)


@pytest.mark.asyncio
async def test_terminal_cleanup_remains_a_resource_wait():
    registry = SessionRuntimeRegistry()
    started, release = asyncio.Event(), asyncio.Event()
    async def cleanup():
        started.set()
        await release.wait()
    async def stream(_):
        yield 'data: {"object":"response","type":"response.completed","status":"completed"}\n\n'
    _, _ = await registry.attach_or_start('chat', {}, stream, cleanup=cleanup)
    task = registry._runs['chat'].task
    await started.wait()
    try:
        assert not await registry.has_active_tasks()
        assert not await registry.wait_all_done(timeout=0.01)
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_second_stop_does_not_cancel_running_cleanup(monkeypatch):
    monkeypatch.setattr(module, '_STOP_SETTLEMENT_WAIT_SECONDS', 0.01)
    registry = SessionRuntimeRegistry()
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def stream(_):
        started.set()
        await asyncio.Event().wait()
        yield 'unreachable'

    async def cleanup():
        calls.append('cleanup')
        cleaning.set()
        await release.wait()

    await registry.attach_or_start('chat', {}, stream, cleanup=cleanup)
    task = registry._runs['chat'].task
    await started.wait()
    try:
        assert await registry.request_stop('chat')
        await cleaning.wait()
        assert await registry.request_stop('chat')
        assert calls == ['cleanup']
        assert not registry._runs['chat'].cleanup_task.cancelled()
        assert not await registry.wait_all_done(timeout=0.01)
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_stop_before_producer_starts_settles_cleanup():
    registry = SessionRuntimeRegistry()
    calls = []

    async def stream(_):
        pytest.fail('cancelled producer must not invoke the stream')
        yield 'unreachable'

    async def cleanup():
        calls.append('cleanup')

    await registry.attach_or_start('chat', {}, stream, cleanup=cleanup)
    assert await registry.request_stop('chat')
    assert calls == ['cleanup']
    assert await registry.wait_all_done(timeout=0.1)


@pytest.mark.parametrize('status', ['stopping', 'cleanup_failed'])
def test_unsettled_attempt_is_not_an_orphan(status):
    from lantai.app.runner.stall_watchdog import _has_active_run
    assert _has_active_run(status, {'status': status})


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['console', 'turn'])
async def test_stop_api_reports_pending_settlement(route, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from lantai.app.runner.api import interrupt_chat_turn
    from lantai.app.routers import console as console_router
    from lantai.app.runner.manager import ChatManager
    from lantai.app.runner.models import ChatSpec
    from lantai.app.runner.repo.json_repo import JsonChatRepository

    monkeypatch.setattr(module, '_STOP_SETTLEMENT_WAIT_SECONDS', 0.01)
    registry = SessionRuntimeRegistry()
    release = asyncio.Event()

    async def cleanup():
        await release.wait()

    manager = ChatManager(repo=JsonChatRepository(tmp_path / 'chats.json'))
    chat = await manager.create_chat(ChatSpec(
        id='chat', name='Stop', session_id='session', user_id='user',
        channel='console', meta={'topic_workspace_dir': str(tmp_path / 'topic')},
    ))
    await registry.register_external_task(chat.id, attempt_id='turn', cleanup=cleanup)
    workspace = SimpleNamespace(
        agent_id='default', task_tracker=registry, workspace_dir=tmp_path,
        runner=SimpleNamespace(_chat_manager=manager),
    )

    async def get_workspace(_):
        return workspace

    monkeypatch.setattr(console_router, 'get_agent_for_request', get_workspace)
    try:
        if route == 'console':
            result = await console_router.post_console_chat_stop(
                SimpleNamespace(), chat_id=chat.id, turn_id='turn',
            )
        else:
            result = await interrupt_chat_turn(
                chat.id, 'turn', mgr=manager, workspace=workspace,
            )
        assert result['stopped'] is True
        assert result['settlement_pending'] is True
        assert result['settlement_status'] == 'stopping'
        assert not await registry.wait_all_done(timeout=0.01)
    finally:
        release.set()
        await registry.unregister_external_task(chat.id)


@pytest.mark.asyncio
async def test_shutdown_does_not_resurrect_public_terminal_while_cleanup_is_unsettled():
    registry = SessionRuntimeRegistry()
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    async def cleanup():
        cleanup_entered.set()
        await cleanup_release.wait()
    async def stream(_payload):
        yield 'data: {"object":"response","status":"completed","type":"response.completed"}\n\n'
    queue, _ = await registry.attach_or_start('terminal', {}, stream, cleanup=cleanup)
    await asyncio.wait_for(queue.get(), .5)
    await asyncio.wait_for(cleanup_entered.wait(), .5)
    assert await registry.get_status('terminal') == 'idle'
    assert await registry.get_snapshot('terminal') is None
    assert await registry.has_unsettled_tasks()
    shutdown = asyncio.create_task(registry.shutdown(timeout=.2))
    try:
        await asyncio.sleep(.01)
        assert await registry.get_status('terminal') == 'idle', 'shutdown resurrected an already terminal response as public stopping'
        assert await registry.get_snapshot('terminal') is None
        assert (await registry.get_global_status())['status'] == 'idle'
        assert await registry.has_unsettled_tasks()
    finally:
        cleanup_release.set()
        await asyncio.gather(shutdown, return_exceptions=True)
        assert await registry.wait_all_done(timeout=1)


@pytest.mark.asyncio
async def test_request_stop_cannot_release_external_placeholder_before_owner_finishes():
    registry = SessionRuntimeRegistry()
    entered, release = asyncio.Event(), asyncio.Event()
    async def external_producer():
        await registry.register_external_task('external')
        entered.set()
        try:
            await release.wait()
        finally:
            await registry.unregister_external_task('external')
    producer = asyncio.create_task(external_producer())
    await entered.wait()
    placeholder = registry._runs['external'].task
    try:
        assert await registry.request_stop('external') is True
        assert not producer.done(), 'the real owner is intentionally still running'
        assert await registry.has_unsettled_tasks(), 'request_stop discarded ownership without acknowledgement from the external producer'
        assert not placeholder.done(), 'a placeholder Future is not an external producer cancellation acknowledgement'
        assert (await registry.get_settlement_snapshot('external'))['settlement_pending'] is True
        assert await registry.request_stop('external') is True
        assert await registry.has_unsettled_tasks()
    finally:
        release.set()
        await asyncio.gather(producer, return_exceptions=True)
        assert await registry.wait_all_done(timeout=1)


@pytest.mark.asyncio
async def test_shutdown_fences_admission_but_retains_external_owner_through_timeout():
    registry = SessionRuntimeRegistry()
    await registry.register_external_task('external')
    placeholder = registry._runs['external'].task
    async def stream(_payload):
        yield 'unused'
    with pytest.raises(RuntimeError, match='remain unsettled'):
        await registry.shutdown(timeout=.02)
    assert await registry.has_unsettled_tasks()
    assert not placeholder.done()
    with pytest.raises(RuntimeError, match='shutting down'):
        await registry.register_external_task('fresh-external')
    with pytest.raises(RuntimeError, match='shutting down'):
        await registry.attach_or_start('fresh-internal', {}, stream)
    await registry.unregister_external_task('external')
    await registry.shutdown(timeout=.02)
    assert not await registry.has_unsettled_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_timeout', [None, 60])
@pytest.mark.parametrize('cancellation_count', [1, 2])
async def test_native_runtime_stop_then_sync_failure_cannot_advance_model(tmp_path, tool_timeout, cancellation_count):
    from lantai.runtime.adapters.native_runtime import NativeLantaiRuntime
    from lantai.runtime.input import RuntimeInput, RuntimeRequestContext
    from lantai.runtime.messages import RuntimeMessage
    from lantai.runtime.models import ModelStreamEvent, ModelMessage, ModelResponse
    from lantai.runtime.tools import LantaiToolRegistry, ToolSpec, ToolCall
    entered, release = threading.Event(), threading.Event()
    def worker():
        entered.set()
        assert release.wait(3), 'synthetic worker barrier timed out'
        raise OSError('synthetic-worker-failure')
    class Model:
        provider_id = 'synthetic'
        model_name = 'synthetic'
        def __init__(self):
            self.requests = []
        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelStreamEvent(kind='tool_call_completed', tool_call=ToolCall(id='synthetic-call', name='synthetic_sync_failure'))
                yield ModelStreamEvent(kind='message_completed', response=ModelResponse(message=ModelMessage(role='assistant')))
            else:
                yield ModelStreamEvent(kind='message_completed', response=ModelResponse(message=ModelMessage.text('assistant', 'must not advance after stop')))
        async def complete(self, request):
            raise AssertionError('must not start an auxiliary request after stop')
    model = Model()
    registry = LantaiToolRegistry()
    registry.register_tool(ToolSpec(name='synthetic_sync_failure', read_only=True), worker)
    runtime = NativeLantaiRuntime(model_client_factory=lambda _: model, tool_registry=registry, tool_timeout_seconds=tool_timeout, max_iters=3)
    runtime_input = RuntimeInput(request=RuntimeRequestContext(agent_id='default', session_id='synthetic-native-stop', user_id='synthetic', run_id='synthetic-turn', workspace_dir=str(tmp_path)), messages=[RuntimeMessage.text('user', 'synthetic verification')])
    events = []
    async def consume():
        async for event in runtime.run_turn(runtime_input):
            events.append(event)
    consumer = asyncio.create_task(consume())
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(.005)
        assert entered.is_set()
        for _ in range(cancellation_count):
            consumer.cancel()
            await asyncio.sleep(0)
        await asyncio.sleep(.01)
        assert not consumer.done(), 'full Runtime must retain the attempt while its real sync worker runs'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert len(model.requests) == 1, 'stop followed by tool failure must not advance another model request'
        assert events[-1].type == 'turn.interrupted'
        assert not any(event.type == 'turn.completed' for event in events)
    finally:
        release.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_startup_wait_is_attempt_owned_and_does_not_cancel_producer():
    registry = SessionRuntimeRegistry()
    entered, release, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stream(_):
        entered.set()
        await release.wait()
        yield 'data: ready\n\n'
        await finish.wait()

    queue, _ = await registry.attach_or_start(
        'chat', {}, stream, attempt_id='first',
    )
    await entered.wait()
    producer = registry._runs['chat'].task
    try:
        with pytest.raises(asyncio.TimeoutError):
            await registry.wait_for_first_event('chat', 'first', timeout=.01)
        assert not producer.done()
        waiter = asyncio.create_task(
            registry.wait_for_first_event('chat', 'first', timeout=1),
        )
        await asyncio.sleep(0)
        assert not waiter.done()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert not producer.done()
        assert not await registry.wait_for_first_event('chat', 'other', timeout=.01)
        release.set()
        assert await registry.wait_for_first_event('chat', 'first', timeout=1)
    finally:
        release.set()
        finish.set()
        await producer
        await registry.detach_subscriber('chat', queue)


@pytest.mark.asyncio
async def test_startup_wait_wakes_when_source_exits_without_a_frame():
    registry = SessionRuntimeRegistry()
    release = asyncio.Event()

    async def stream(_):
        await release.wait()
        if False:
            yield ''

    queue, _ = await registry.attach_or_start('chat', {}, stream, attempt_id='first')
    producer = registry._runs['chat'].task
    waiter = asyncio.create_task(
        registry.wait_for_first_event('chat', 'first', timeout=1),
    )
    await asyncio.sleep(0)
    release.set()
    assert not await waiter
    await producer
    await registry.detach_subscriber('chat', queue)
