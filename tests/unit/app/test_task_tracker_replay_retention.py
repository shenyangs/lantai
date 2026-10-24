import asyncio
import tracemalloc

import pytest

from lantai.app.runner.task_tracker import SessionRuntimeRegistry


@pytest.mark.asyncio
async def test_slow_subscribers_retain_complete_replay_with_bounded_memory():
    registry = SessionRuntimeRegistry()
    ready, release = asyncio.Event(), asyncio.Event()
    count = 2048
    calls = 0

    async def source(_):
        nonlocal calls
        calls += 1
        for number in range(count):
            yield f"data: {number}:" + "x" * 8192 + "\n\n"
        ready.set()
        await release.wait()
        yield 'data: {"object":"response","status":"completed"}\n\n'

    tracemalloc.start()
    queue, _ = await registry.attach_or_start("synthetic", {}, source)
    producer = registry._runs["synthetic"].task
    try:
        await asyncio.wait_for(ready.wait(), 30)
        reconnect = await registry.attach("synthetic")
        third, started = await registry.attach_or_start(
            "synthetic", {}, source
        )
        assert not started
        _, peak = tracemalloc.get_traced_memory()
        print(f"raw_replay_peak_bytes={peak}")
        tracemalloc.stop()
        assert (
            peak < 4 * 1024 * 1024
        ), "raw backlog/slow subscribers grow in RAM"
        release.set()
        await producer
        for subscriber in (queue, reconnect, third):
            received = 0
            async for frame in registry.stream_from_queue(
                subscriber, "synthetic"
            ):
                if received < count:
                    assert frame == f"data: {received}:" + "x" * 8192 + "\n\n"
                else:
                    assert '"status":"completed"' in frame
                received += 1
            assert received == count + 1
        assert calls == 1, "reconnect must not start another model request"
    finally:
        tracemalloc.stop()
        release.set()
        await asyncio.gather(producer, return_exceptions=True)
        await registry.detach_subscriber("synthetic", queue)


@pytest.mark.asyncio
async def test_stop_during_spool_write_reaches_source_cancellation(
    monkeypatch,
):
    import threading
    from lantai.app.runner import task_tracker as tracker_module
    from lantai.app.runner.replay_transport import ReplayTransport

    monkeypatch.setattr(tracker_module, "_STOP_SETTLEMENT_WAIT_SECONDS", 0.02)
    entered, release = threading.Event(), threading.Event()
    cancelled = asyncio.Event()
    write = ReplayTransport._write

    def blocked_write(self, frame):
        entered.set()
        assert release.wait(3)
        return write(self, frame)

    monkeypatch.setattr(ReplayTransport, "_write", blocked_write)
    registry = SessionRuntimeRegistry()

    async def source(_):
        try:
            yield "data: progress\n\n"
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            yield 'data: {"object":"response","status":"interrupted"}\n\n'

    queue, _ = await registry.attach_or_start("chat", {}, source)
    state = registry._runs["chat"]
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        assert await registry.request_stop("chat")
        assert not state.task.done()
        assert await registry.has_unsettled_tasks()
        assert (await registry.get_control_snapshot("chat"))[
            "status"
        ] == "stopping"
        release.set()
        await state.task
        assert (
            cancelled.is_set()
        ), "transport cancellation must reach the source generator"
        frames = [
            frame async for frame in registry.stream_from_queue(queue, "chat")
        ]
        assert len(frames) == 1 and "interrupted" in frames[0]
        assert state.replay._closed
    finally:
        release.set()
        await asyncio.gather(state.task, return_exceptions=True)
        await registry.detach_subscriber("chat", queue)


async def _thread_entered(event):
    for _ in range(200):
        if event.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("synthetic worker did not enter")


@pytest.mark.asyncio
async def test_cancelled_read_settles_without_advancing_cursor(monkeypatch):
    import threading
    from lantai.app.runner.replay_transport import ReplayTransport

    journal = ReplayTransport()
    reader = journal.subscribe()
    await journal.append(
        "data: exact-frame\n\n", accept=lambda: True, published=lambda: None
    )
    entered, release = threading.Event(), threading.Event()
    read = journal._read

    def blocked_read(position):
        entered.set()
        assert release.wait(3)
        return read(position)

    monkeypatch.setattr(journal, "_read", blocked_read)
    task = asyncio.create_task(reader.get())
    try:
        await _thread_entered(entered)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "cancelled reader released an active worker"
        assert reader._position == 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reader._position == 0
        assert await reader.get() == "data: exact-frame\n\n"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        journal.seal()
        await reader.close()
    assert journal._closed and journal._file.closed


@pytest.mark.asyncio
async def test_last_disconnect_waits_for_reader_then_closes_file(monkeypatch):
    import threading
    from lantai.app.runner.replay_transport import ReplayTransport

    journal = ReplayTransport()
    reader = journal.subscribe()
    await journal.append(
        "data: exact-frame\n\n", accept=lambda: True, published=lambda: None
    )
    journal.seal()
    entered, release = threading.Event(), threading.Event()
    read = journal._read

    def blocked_read(position):
        entered.set()
        assert release.wait(3)
        return read(position)

    monkeypatch.setattr(journal, "_read", blocked_read)
    task = asyncio.create_task(reader.get())
    close = None
    try:
        await _thread_entered(entered)
        close = asyncio.create_task(reader.close())
        await asyncio.sleep(0.01)
        assert not close.done()
        assert not journal._file.closed
        release.set()
        assert await task is None
        await close
        assert journal._closed and journal._file.closed
        await reader.close()  # idempotent
    finally:
        release.set()
        await asyncio.gather(
            task, *([close] if close else []), return_exceptions=True
        )
        await reader.close()


@pytest.mark.asyncio
async def test_write_failure_delivers_prior_frames_then_visible_failed_terminal(
    monkeypatch,
):
    from lantai.app.runner.replay_transport import ReplayTransport

    write = ReplayTransport._write

    def fail_second(self, frame):
        if self.count == 1:
            raise OSError("synthetic disk exhausted")
        return write(self, frame)

    monkeypatch.setattr(ReplayTransport, "_write", fail_second)
    registry = SessionRuntimeRegistry()

    async def source(_):
        yield "data: prior\n\n"
        yield "data: failed-write\n\n"
        pytest.fail("write failure must not advance source")

    queue, _ = await registry.attach_or_start("chat", {}, source)
    state = registry._runs["chat"]
    await state.task
    frames = [
        frame async for frame in registry.stream_from_queue(queue, "chat")
    ]
    assert len(frames) == 2 and frames[0] == "data: prior\n\n"
    assert "response.failed" in frames[1]
    assert "synthetic disk exhausted" not in frames[1]
    assert state.replay._closed and state.replay._file.closed
    assert not await registry.has_unsettled_tasks()


@pytest.mark.asyncio
async def test_completed_terminal_not_stoppable_during_transport_write(
    monkeypatch,
):
    import threading
    from lantai.app.runner.replay_transport import ReplayTransport

    entered, release = threading.Event(), threading.Event()
    write = ReplayTransport._write

    def blocked_write(self, frame):
        entered.set()
        assert release.wait(3)
        return write(self, frame)

    monkeypatch.setattr(ReplayTransport, "_write", blocked_write)
    registry = SessionRuntimeRegistry()

    async def source(_):
        yield 'data: {"object":"response","status":"completed"}\n\n'

    queue, _ = await registry.attach_or_start("chat", {}, source)
    state = registry._runs["chat"]
    try:
        await _thread_entered(entered)
        assert await registry.get_status("chat") == "idle"
        assert not await registry.request_stop("chat")
        assert await registry.has_unsettled_tasks()
        release.set()
        await state.task
        frames = [
            frame async for frame in registry.stream_from_queue(queue, "chat")
        ]
        assert len(frames) == 1 and "completed" in frames[0]
    finally:
        release.set()
        await asyncio.gather(state.task, return_exceptions=True)
        await registry.detach_subscriber("chat", queue)


@pytest.mark.asyncio
async def test_full_snapshot_has_fixed_boundary_and_does_not_block_control(
    monkeypatch,
):
    import threading

    registry = SessionRuntimeRegistry()
    ready, more, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def source(_):
        yield "data: first\n\n"
        ready.set()
        await more.wait()
        yield "data: second\n\n"
        await finish.wait()

    queue, _ = await registry.attach_or_start("chat", {}, source)
    state = registry._runs["chat"]
    await ready.wait()
    entered, release = threading.Event(), threading.Event()
    read = state.replay._read

    def blocked_read(position):
        entered.set()
        assert release.wait(3)
        return read(position)

    monkeypatch.setattr(state.replay, "_read", blocked_read)
    snapshot_task = asyncio.create_task(registry.get_snapshot("chat"))
    try:
        await _thread_entered(entered)
        more.set()
        control = await asyncio.wait_for(
            registry.get_control_snapshot("chat"), 0.1
        )
        assert control["event_count"] == 1 and "events" not in control
        release.set()
        snapshot = await snapshot_task
        assert snapshot["event_count"] == 1
        assert snapshot["events"] == ["data: first\n\n"]
        finish.set()
        await state.task
        frames = [
            frame async for frame in registry.stream_from_queue(queue, "chat")
        ]
        assert frames == ["data: first\n\n", "data: second\n\n"]
    finally:
        release.set()
        more.set()
        finish.set()
        await asyncio.gather(snapshot_task, state.task, return_exceptions=True)
        await registry.detach_subscriber("chat", queue)


@pytest.mark.asyncio
async def test_subscriber_disconnect_does_not_drop_other_replay_or_reinvoke_source():
    registry = SessionRuntimeRegistry()
    ready, release = asyncio.Event(), asyncio.Event()
    frames = [
        'data: {"type":"approval.required"}\n\n',
        'data: {"type":"tool.result"}\n\n',
        'data: {"type":"artifact.created"}\n\n',
        'data: {"object":"response","status":"completed"}\n\n',
    ]
    calls = 0

    async def source(_):
        nonlocal calls
        calls += 1
        for frame in frames[:-1]:
            yield frame
        ready.set()
        await release.wait()
        yield frames[-1]

    first, _ = await registry.attach_or_start("chat", {}, source)
    state = registry._runs["chat"]
    await ready.wait()
    second = await registry.attach("chat")
    assert await first.get() == frames[0]
    await registry.detach_subscriber("chat", first)
    assert len(state.queues) == 1
    assert not state.replay._closed
    release.set()
    await state.task
    assert [
        frame async for frame in registry.stream_from_queue(second, "chat")
    ] == frames
    assert calls == 1
    assert state.replay._closed and state.replay._file.closed
