import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lantai.app.runner import task_tracker as tracker_module
from lantai.app.runner.task_tracker import SessionRuntimeRegistry
from lantai.app.runner.control_commands.base import ControlContext
from lantai.app.runner.control_commands.stop_handler import StopCommandHandler


def context(tracker, cleared=0):
    return ControlContext(
        workspace=SimpleNamespace(
            task_tracker=tracker,
            chat_manager=SimpleNamespace(get_chat_id_by_session=AsyncMock(return_value='chat')),
            channel_manager=SimpleNamespace(clear_queue=AsyncMock(return_value=cleared)),
        ), payload=None, channel=SimpleNamespace(channel='feishu'),
        session_id='session', user_id='u', agent_id='default', args={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup_fails', [False, True])
async def test_im_stop_does_not_claim_settled_while_cleanup_owns_attempt(monkeypatch, cleanup_fails):
    monkeypatch.setattr(tracker_module, '_STOP_SETTLEMENT_WAIT_SECONDS', 0.01)
    registry = SessionRuntimeRegistry()
    entered, release = asyncio.Event(), asyncio.Event()
    async def cleanup():
        entered.set()
        if cleanup_fails:
            raise RuntimeError('fixture cleanup failed')
        await release.wait()
    await registry.register_external_task('chat', cleanup=cleanup)
    cleanup_task = asyncio.create_task(registry.unregister_external_task('chat'))
    await entered.wait()
    if cleanup_fails:
        await cleanup_task
    try:
        reply = await StopCommandHandler().handle(context(registry, 2))
        assert '**Task Stopped**' not in reply
        assert 'running task stopped' not in reply
        assert ('Cleanup Failed' if cleanup_fails else 'Stopping') in reply
        assert '2 queued message(s) cleared' in reply
        assert await registry.has_unsettled_tasks()
    finally:
        release.set()
        await cleanup_task


@pytest.mark.asyncio
async def test_im_stop_acknowledges_success_only_after_owner_settles(monkeypatch):
    monkeypatch.setattr(tracker_module, '_STOP_SETTLEMENT_WAIT_SECONDS', 0.1)
    registry = SessionRuntimeRegistry()
    started = asyncio.Event()
    async def producer():
        await registry.register_external_task('chat', owner_task=asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await registry.unregister_external_task('chat')
    task = asyncio.create_task(producer())
    await started.wait()
    try:
        reply = await StopCommandHandler().handle(context(registry))
        assert '**Task Stopped**' in reply
        assert not await registry.has_unsettled_tasks()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_im_stop_fences_observed_attempt_and_does_not_stop_replacement():
    tracker = SimpleNamespace(
        get_settlement_snapshot=AsyncMock(side_effect=[
            {'attempt_id': 'old', 'generation': 1, 'status': 'running'},
            {'attempt_id': 'new', 'generation': 2, 'status': 'running'},
        ]),
        request_stop_if_attempt=AsyncMock(return_value=False),
        request_stop=AsyncMock(return_value=True),
    )
    reply = await StopCommandHandler().handle(context(tracker))
    tracker.request_stop_if_attempt.assert_awaited_once_with('chat', attempt_id='old', generation=1)
    tracker.request_stop.assert_not_awaited()
    assert '**Task Stopped**' not in reply
    assert 'changed' in reply.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('cleared', [0, 3])
async def test_im_stop_empty_session_reports_queue_separately(cleared):
    registry = SessionRuntimeRegistry()
    reply = await StopCommandHandler().handle(context(registry, cleared))
    assert '**Task Stopped**' not in reply
    assert ('Queue Cleared' if cleared else 'Task Not Running') in reply


@pytest.mark.asyncio
async def test_im_stop_terminal_frame_does_not_prove_cleanup_finished():
    registry = SessionRuntimeRegistry()
    entered, release = asyncio.Event(), asyncio.Event()
    async def cleanup():
        entered.set()
        await release.wait()
    async def stream(_):
        yield 'data: {"object":"response","status":"completed","type":"response.completed"}\n\n'
    await registry.attach_or_start('chat', {}, stream, cleanup=cleanup)
    task = registry._runs['chat'].task
    await entered.wait()
    try:
        assert await registry.get_status('chat') == 'idle'
        reply = await StopCommandHandler().handle(context(registry))
        assert '**Stopping**' in reply
        assert '**Task Stopped**' not in reply
    finally:
        release.set()
        await task
