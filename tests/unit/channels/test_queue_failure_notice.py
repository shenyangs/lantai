"""Queue rejection remains visible without replaying uncertain notices."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lantai.app.channels.manager import ChannelManager


def channel():
    return SimpleNamespace(
        channel="telegram",
        _payload_to_request=lambda payload: SimpleNamespace(user_id="u-1", channel_meta={"thread_id": "topic-1"}),
        get_to_handle_from_request=lambda request: request.user_id,
        send_content_parts=AsyncMock(),
        _is_native_payload=lambda value: isinstance(value, dict),
        _on_consume_error=AsyncMock(side_effect=AssertionError("must not alter active consumer")),
    )


def payload():
    return {"sender_id": "u-1", "meta": {"message_id": "m-queue-1"}}


@pytest.mark.asyncio
async def test_queue_timeout_notifies_once_and_preserves_pending_record(tmp_path):
    ch = channel()
    path = tmp_path / "deliveries.sqlite3"
    manager = ChannelManager([ch], delivery_ledger_path=path)
    manager._queue_manager = SimpleNamespace(enqueue=AsyncMock(side_effect=asyncio.TimeoutError))
    await manager._enqueue_with_timeout("telegram", "s-1", 20, payload(), "")
    assert ch.send_content_parts.await_count == 1
    assert ch.send_content_parts.call_args.args[0] == "u-1"
    assert ch.send_content_parts.call_args.args[2] == {"thread_id": "topic-1"}
    pending = manager._delivery_ledger.list_pending()
    assert len(pending) == 1
    assert pending[0].attempts == 1
    assert pending[0].last_error == "enqueue timeout"
    assert manager._delivery_ledger.notice_status(pending[0].delivery_id) == "unconfirmed"
    reopened = ChannelManager([ch], delivery_ledger_path=path)
    reopened._queue_manager = manager._queue_manager
    await reopened._enqueue_with_timeout("telegram", "s-1", 20, payload(), "")
    assert ch.send_content_parts.await_count == 1
    assert manager._queue_manager.enqueue.await_count == 1


@pytest.mark.asyncio
async def test_failed_notice_does_not_erase_delivery_failure_or_retry_send(tmp_path):
    ch = channel()
    ch.send_content_parts.side_effect = ConnectionError("uncertain response")
    manager = ChannelManager([ch], delivery_ledger_path=tmp_path / "deliveries.sqlite3")
    manager._queue_manager = SimpleNamespace(enqueue=AsyncMock(side_effect=RuntimeError("queue full")))
    await manager._enqueue_with_timeout("telegram", "s-1", 20, payload(), "")
    assert ch.send_content_parts.await_count == 1
    pending = manager._delivery_ledger.list_pending()
    assert len(pending) == 1 and pending[0].last_error == "queue full"
    assert manager._delivery_ledger.notice_status(pending[0].delivery_id) == "unconfirmed"
    await manager._enqueue_with_timeout("telegram", "s-1", 20, payload(), "")
    assert ch.send_content_parts.await_count == 1


@pytest.mark.asyncio
async def test_cancellation_during_notice_is_durable_and_not_replayed(tmp_path):
    ch = channel()
    entered = asyncio.Event()

    async def stalled(*args):
        entered.set()
        await asyncio.Event().wait()

    ch.send_content_parts.side_effect = stalled
    path = tmp_path / "deliveries.sqlite3"
    manager = ChannelManager([ch], delivery_ledger_path=path)
    manager._queue_manager = SimpleNamespace(enqueue=AsyncMock(side_effect=asyncio.TimeoutError))
    task = asyncio.create_task(manager._enqueue_with_timeout("telegram", "s-1", 20, payload(), ""))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = manager._delivery_ledger.list_pending()
    assert pending[0].last_error == "enqueue timeout"
    reopened = ChannelManager([ch], delivery_ledger_path=path)
    assert reopened._delivery_ledger.notice_status(pending[0].delivery_id) == "unconfirmed"
    await reopened._notify_enqueue_failure("telegram", payload(), pending[0].delivery_id)
    assert ch.send_content_parts.await_count == 1


@pytest.mark.asyncio
async def test_successful_enqueue_does_not_send_failure_notice(tmp_path):
    ch = channel()
    manager = ChannelManager([ch], delivery_ledger_path=tmp_path / "deliveries.sqlite3")
    manager._queue_manager = SimpleNamespace(enqueue=AsyncMock())
    await manager._enqueue_with_timeout("telegram", "s-1", 20, payload(), "")
    ch.send_content_parts.assert_not_awaited()
    assert len(manager._delivery_ledger.list_pending()) == 1


def test_notice_claim_is_unique_across_connections_and_pruned(tmp_path):
    from lantai.app.channels.delivery import ChannelDeliveryLedger
    path = tmp_path / "deliveries.sqlite3"
    first, second = ChannelDeliveryLedger(path), ChannelDeliveryLedger(path)
    registration = first.register("telegram", "s-1", 20, payload())
    delivery_id = registration.delivery_id
    assert first.claim_notice(delivery_id)
    assert not second.claim_notice(delivery_id)
    assert second.notice_status(delivery_id) == "unconfirmed"
    first.mark_delivered(delivery_id)
    assert first.prune_delivered(0) == 1
    assert second.notice_status(delivery_id) is None
    assert not second.claim_notice(delivery_id)


@pytest.mark.asyncio
async def test_notice_timeout_preserves_failure_and_does_not_retry(tmp_path, monkeypatch):
    import lantai.app.channels.manager as manager_module
    monkeypatch.setattr(manager_module, "_CHANNEL_FAILURE_NOTICE_TIMEOUT_SECONDS", 0.01)
    ch = channel()

    async def stalled(*args):
        await asyncio.Event().wait()

    ch.send_content_parts.side_effect = stalled
    manager = ChannelManager([ch], delivery_ledger_path=tmp_path / "deliveries.sqlite3")
    manager._queue_manager = SimpleNamespace(enqueue=AsyncMock(side_effect=asyncio.TimeoutError))
    await asyncio.wait_for(manager._enqueue_with_timeout("telegram", "s-1", 20, payload(), ""), 1)
    record = manager._delivery_ledger.list_pending()[0]
    assert record.last_error == "enqueue timeout"
    assert manager._delivery_ledger.notice_status(record.delivery_id) == "unconfirmed"
    await manager._notify_enqueue_failure("telegram", payload(), record.delivery_id)
    assert ch.send_content_parts.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_notice_timeout_retains_cleanup_during_repeated_cancellation(
    tmp_path, monkeypatch, cleanup_fails,
):
    import lantai.app.channels.manager as manager_module
    monkeypatch.setattr(manager_module, "_CHANNEL_FAILURE_NOTICE_TIMEOUT_SECONDS", 0.01)
    ch = channel()
    cleanup_started, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    children = []

    async def send(*args):
        children.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release.wait()
            settled.set()
            if cleanup_fails:
                raise ConnectionError("synthetic cleanup failure")

    ch.send_content_parts.side_effect = send
    path = tmp_path / "deliveries.sqlite3"
    manager = ChannelManager([ch], delivery_ledger_path=path)
    manager._queue_manager = SimpleNamespace(enqueue=AsyncMock(side_effect=asyncio.TimeoutError))
    task = asyncio.create_task(manager._enqueue_with_timeout("telegram", "s-1", 20, payload(), ""))
    manager._enqueue_tasks.add(task)
    task.add_done_callback(manager._enqueue_tasks.discard)
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done(), "enqueue owner detached live notice cleanup"
            assert task in manager._enqueue_tasks
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert settled.is_set()
        assert all(child.done() for child in children)
        assert task not in manager._enqueue_tasks
        record = manager._delivery_ledger.list_pending()[0]
        assert record.last_error == "enqueue timeout"
        reopened = ChannelManager([ch], delivery_ledger_path=path)
        assert reopened._delivery_ledger.notice_status(record.delivery_id) == "unconfirmed"
        await reopened._notify_enqueue_failure("telegram", payload(), record.delivery_id)
        assert ch.send_content_parts.await_count == 1
    finally:
        release.set()
        await asyncio.gather(task, *children, return_exceptions=True)
