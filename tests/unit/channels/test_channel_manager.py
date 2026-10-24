# -*- coding: utf-8 -*-
"""Behavior locks for ChannelManager startup lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from lantai.app.channels import manager as manager_module
from lantai.app.channels.manager import ChannelManager


class _FakeChannel:
    def __init__(self, channel: str, *, start_event: asyncio.Event | None = None):
        self.channel = channel
        self.uses_manager_queue = True
        self.enqueue = None
        self.start_event = start_event
        self.started = False
        self.stopped = False

    def set_enqueue(self, callback):
        self.enqueue = callback

    def _extract_query_from_payload(self, payload):
        return str(payload)

    def get_debounce_key(self, _payload):
        return f"{self.channel}:session"

    async def start(self) -> None:
        self.started = True
        if self.start_event is not None:
            await self.start_event.wait()

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_start_all_does_not_block_on_slow_external_channel(monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "_CHANNEL_START_TIMEOUT_SECONDS", 0.01)
    slow_start = asyncio.Event()
    console = _FakeChannel("console")
    slow_external = _FakeChannel("wechat", start_event=slow_start)
    manager = ChannelManager([console, slow_external])

    await asyncio.wait_for(manager.start_all(), timeout=0.2)

    assert console.started is True
    assert slow_external.started is True
    assert len(manager._startup_tasks) == 1

    await manager.stop_all()

    assert console.stopped is True
    assert slow_external.stopped is True
    assert manager._startup_tasks == set()


@pytest.mark.asyncio
async def test_enqueue_tasks_are_rejected_at_bounded_limit(monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "_CHANNEL_MAX_PENDING_ENQUEUE_TASKS", 1)
    manager = ChannelManager([_FakeChannel("console")])
    manager._queue_manager = object()
    pending = asyncio.create_task(asyncio.sleep(60))
    manager._enqueue_tasks.add(pending)

    manager._enqueue_one("console", "hello")

    assert manager._rejected_enqueue_count == 1
    assert manager._enqueue_tasks == {pending}
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)


class _CapturingQueueManager:
    def __init__(self):
        self.items = []

    async def enqueue(self, channel_id, session_id, priority_level, payload):
        self.items.append((channel_id, session_id, priority_level, payload))


@pytest.mark.asyncio
async def test_manager_suppresses_duplicate_external_message_ids(tmp_path) -> None:
    manager = ChannelManager(
        [_FakeChannel("telegram")],
        delivery_ledger_path=tmp_path / "deliveries.sqlite3",
    )
    queue_manager = _CapturingQueueManager()
    manager._queue_manager = queue_manager
    payload = {
        "sender_id": "u1",
        "content_parts": [{"type": "text", "text": "hello"}],
        "meta": {"message_id": "telegram-1"},
    }

    await manager._enqueue_with_timeout(
        "telegram",
        "telegram:u1",
        20,
        payload,
        "hello",
    )
    await manager._enqueue_with_timeout(
        "telegram",
        "telegram:u1",
        20,
        payload,
        "hello",
    )

    assert len(queue_manager.items) == 1


@pytest.mark.asyncio
async def test_manager_restores_pending_serializable_deliveries(tmp_path) -> None:
    manager = ChannelManager(
        [_FakeChannel("discord")],
        delivery_ledger_path=tmp_path / "deliveries.sqlite3",
    )
    manager._delivery_ledger.register(
        "discord",
        "discord:u1",
        20,
        {
            "sender_id": "u1",
            "content_parts": [{"type": "text", "text": "resume"}],
            "meta": {"message_id": "discord-1"},
        },
    )
    queue_manager = _CapturingQueueManager()
    manager._queue_manager = queue_manager

    await manager._restore_pending_deliveries()

    assert len(queue_manager.items) == 1
    assert queue_manager.items[0][3].payload["sender_id"] == "u1"


async def test_replace_does_not_hold_manager_lock_during_retired_stop():
    stopping = asyncio.Event()
    finish = asyncio.Event()
    old = _FakeChannel("feishu")
    new = _FakeChannel("feishu")
    other = _FakeChannel("console")

    async def slow_stop():
        stopping.set()
        await finish.wait()
    old.stop = slow_stop
    manager = ChannelManager([old, other])
    replacement = asyncio.create_task(manager.replace_channel(new))
    try:
        await stopping.wait()
        assert await asyncio.wait_for(manager.get_channel("console"), 0.1) is other
        assert await manager.get_channel("feishu") is new
    finally:
        finish.set()
        await replacement


async def test_cancelled_channel_replacement_cleans_unpublished_instance():
    from contextlib import suppress
    starting = asyncio.Event()
    old = _FakeChannel("feishu")
    new = _FakeChannel("feishu")

    async def blocked_start():
        starting.set()
        await asyncio.Event().wait()
    new.start = blocked_start
    manager = ChannelManager([old])
    replacement = asyncio.create_task(manager.replace_channel(new))
    await starting.wait()
    replacement.cancel()
    with suppress(asyncio.CancelledError):
        await replacement
    assert new.stopped
    assert await manager.get_channel("feishu") is old


async def test_channel_replacement_times_out_without_losing_old(monkeypatch):
    monkeypatch.setattr(manager_module, "_CHANNEL_REPLACE_TIMEOUT_SECONDS", 0.02, raising=False)
    old = _FakeChannel("feishu")
    new = _FakeChannel("feishu", start_event=asyncio.Event())
    manager = ChannelManager([old])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(manager.replace_channel(new), 0.2)
    assert new.stopped
    assert await manager.get_channel("feishu") is old


async def test_cancelled_start_all_cleans_shielded_channel_start():
    from contextlib import suppress
    starting = asyncio.Event()
    stopped = asyncio.Event()
    ch = _FakeChannel("console")

    async def start():
        starting.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    ch.start = start
    manager = ChannelManager([ch])
    startup = asyncio.create_task(manager.start_all())
    try:
        await starting.wait()
        startup.cancel()
        with suppress(asyncio.CancelledError):
            await startup
        await manager.stop_all()
        assert stopped.is_set(), "shielded startup must stay owned through cancellation"
    finally:
        # Clean only this fixture's coroutine if the original ownership bug hit.
        for task in asyncio.all_tasks():
            if task.get_coro().__qualname__ == start.__qualname__:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await manager.stop_all()


async def test_slow_retired_stop_retains_cleanup_ownership(monkeypatch):
    monkeypatch.setattr(manager_module, "_CHANNEL_STOP_TIMEOUT_SECONDS", 0.01)
    finish = asyncio.Event()
    old, new = _FakeChannel("feishu"), _FakeChannel("feishu")

    async def stop():
        await finish.wait()
        old.stopped = True
    old.stop = stop
    manager = ChannelManager([old])
    await manager.replace_channel(new)
    assert not old.stopped
    assert id(old) in manager._channel_stop_tasks
    cleanup = manager._channel_stop_tasks[id(old)]
    assert not cleanup.cancelled()
    finish.set()
    await cleanup
    assert old.stopped
    assert not manager._channel_stop_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_status", [None, "pending", "delivered"])
async def test_overload_is_durable_without_tasks_or_duplicate_mutation(tmp_path, monkeypatch, previous_status):
    from lantai.app.channels.delivery import ChannelDeliveryLedger

    monkeypatch.setattr(manager_module, "_CHANNEL_MAX_PENDING_ENQUEUE_TASKS", 1)
    path = tmp_path / "deliveries.sqlite3"
    manager = ChannelManager([_FakeChannel("telegram")], delivery_ledger_path=path)
    manager._queue_manager = _CapturingQueueManager()
    payload = {"sender_id": "u1", "meta": {"message_id": "overload-1"}}
    if previous_status:
        registration = manager._delivery_ledger.register("telegram", "telegram:session", 20, payload)
        if previous_status == "delivered":
            manager._delivery_ledger.mark_delivered(registration.delivery_id)
    pending = asyncio.create_task(asyncio.Event().wait())
    manager._enqueue_tasks.add(pending)
    try:
        manager._enqueue_one("telegram", payload)
        manager._enqueue_one("telegram", payload)
        assert manager._enqueue_tasks == {pending}
        assert manager._queue_manager.items == []
        reopened = ChannelDeliveryLedger(path)
        if previous_status:
            assert reopened.list_dead_letters() == []
            assert len(reopened.list_pending()) == (previous_status == "pending")
        else:
            dead = reopened.list_dead_letters()
            assert len(dead) == 1
            assert dead[0].attempts == 0
            assert dead[0].last_error == "enqueue pending task limit reached"
            assert dead[0].payload == payload
            assert reopened.list_pending() == []
            assert manager.list_dead_letters()[0]["deliveryId"] == dead[0].delivery_id
            replay = await manager.replay_dead_letter(dead[0].delivery_id)
            assert len(manager._queue_manager.items) == 1
            assert reopened.list_dead_letters() == []
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_overload_duplicate_cannot_reject_already_scheduled_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "_CHANNEL_MAX_PENDING_ENQUEUE_TASKS", 1)
    manager = ChannelManager([_FakeChannel("telegram")], delivery_ledger_path=tmp_path / "deliveries.sqlite3")
    manager._queue_manager = _CapturingQueueManager()
    payload = {"sender_id": "u1", "meta": {"message_id": "same-turn"}}
    manager._enqueue_one("telegram", payload)
    manager._enqueue_one("telegram", payload)
    await asyncio.gather(*manager._enqueue_tasks)
    assert len(manager._queue_manager.items) == 1
    assert manager._delivery_ledger.list_dead_letters() == []
    assert len(manager._delivery_ledger.list_pending()) == 1


@pytest.mark.asyncio
async def test_shutdown_retains_slow_enqueue_cleanup_without_recancelling(tmp_path):
    from unittest.mock import AsyncMock

    entered = asyncio.Event()
    cleaning = asyncio.Event()
    finish = asyncio.Event()
    cleaned = asyncio.Event()

    class SlowQueue:
        stop_all = AsyncMock()

        async def enqueue(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await finish.wait()
                cleaned.set()
                raise

    manager = ChannelManager(
        [_FakeChannel("telegram")], delivery_ledger_path=tmp_path / "deliveries.sqlite3",
    )
    queue = SlowQueue()
    manager._queue_manager = queue
    manager._enqueue_one("telegram", {"sender_id": "u1", "meta": {"message_id": "shutdown-1"}})
    task = next(iter(manager._enqueue_tasks))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(manager.stop_all(), 3)
        assert cleaning.is_set()
        assert not task.done()
        assert manager._enqueue_tasks == {task}
        await asyncio.wait_for(manager.stop_all(), 3)
        assert not task.done()
        assert manager._enqueue_tasks == {task}
        queue.stop_all.assert_awaited_once()
    finally:
        finish.set()
        if not cleaning.is_set():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    assert cleaned.is_set()
    assert manager._enqueue_tasks == set()
    assert manager._enqueue_cancellation_requested == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_start_caller", [False, True])
async def test_shutdown_preserves_pending_start_cleanup(
    tmp_path, monkeypatch, cancel_start_caller,
):
    monkeypatch.setattr(manager_module, "_CHANNEL_START_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(manager_module, "_CHANNEL_STOP_TIMEOUT_SECONDS", 0.01)
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
    slow, console = _FakeChannel("slow"), _FakeChannel("console")
    order = []

    async def start():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()
            order.append("start-cleaned")

    async def stop():
        order.append("transport-stopped")
        slow.stopped = True

    slow.start, slow.stop = start, stop
    manager = ChannelManager([slow, console], delivery_ledger_path=tmp_path / "ledger.db")
    caller = asyncio.create_task(manager._start_channel_without_blocking(slow))
    shutdowns = []
    try:
        await entered.wait()
        if cancel_start_caller:
            caller.cancel()
        else:
            await caller
        shutdowns.append(asyncio.create_task(manager.stop_all()))
        await cleaning.wait()
        done, _ = await asyncio.wait(shutdowns, timeout=0.2)
        assert done, "slow startup cleanup must not block manager shutdown"
        assert console.stopped
        assert not slow.stopped, "transport must stop after startup settles"
        assert len(manager._startup_tasks) == 1
        shutdowns.append(asyncio.create_task(manager.stop_all()))
        done, _ = await asyncio.wait(shutdowns, timeout=0.2)
        assert len(done) == 2
        assert len(manager._startup_tasks) == 1
        assert not cleaned.is_set()
        cleanup = list(manager._channel_stop_tasks.values())
        release.set()
        await asyncio.gather(*manager._startup_tasks, *cleanup, return_exceptions=True)
        assert order == ["start-cleaned", "transport-stopped"]
        assert not manager._startup_tasks
        assert not manager._channel_stop_tasks
    finally:
        release.set()
        await asyncio.gather(caller, *shutdowns, return_exceptions=True)
        await manager.stop_all()
