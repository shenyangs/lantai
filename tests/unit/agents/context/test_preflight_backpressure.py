import asyncio
from types import SimpleNamespace

import pytest

from lantai.app.runner.runner import AgentRunner


async def test_slow_preflight_consumer_bounds_pending_rows_without_loss():
    produced = 0

    async def pre_reasoning(_agent, kwargs):
        nonlocal produced
        for index in range(2048):
            await kwargs["_context_status_sink"](index, index == 2047)
            produced += 1

    stream = AgentRunner._stream_native_context_preflight(
        SimpleNamespace(
            context_manager=SimpleNamespace(pre_reasoning=pre_reasoning)
        ),
        [],
    )
    try:
        first = await anext(stream)
        for _ in range(10):
            await asyncio.sleep(0)
        assert (
            produced <= 65
        ), "slow consumer retained an unbounded status backlog"
        rows = [first] + [row async for row in stream]
        assert rows == [(i, i == 2047) for i in range(2048)]
    finally:
        await stream.aclose()


async def test_close_full_preflight_queue_settles_producer():
    settled = asyncio.Event()

    async def pre_reasoning(_agent, kwargs):
        try:
            for index in range(2048):
                await kwargs["_context_status_sink"](index, False)
            await asyncio.Event().wait()
        finally:
            settled.set()

    stream = AgentRunner._stream_native_context_preflight(
        SimpleNamespace(
            context_manager=SimpleNamespace(pre_reasoning=pre_reasoning)
        ),
        [],
    )
    await anext(stream)
    for _ in range(10):
        await asyncio.sleep(0)
    await asyncio.wait_for(stream.aclose(), 1)
    assert settled.is_set()


async def test_preflight_close_repeated_cancel_keeps_cleanup_owned():
    cleaning = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    async def pre_reasoning(_agent, kwargs):
        try:
            await kwargs["_context_status_sink"]("progress", False)
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            settled.set()

    stream = AgentRunner._stream_native_context_preflight(
        SimpleNamespace(
            context_manager=SimpleNamespace(pre_reasoning=pre_reasoning)
        ),
        [],
    )
    await anext(stream)
    closer = asyncio.create_task(stream.aclose())
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        closer.cancel()
        await asyncio.sleep(0)
        closer.cancel()
        await asyncio.sleep(0)
        assert (
            not closer.done()
        ), "closing must retain producer cleanup ownership"
    finally:
        release.set()
        await asyncio.gather(closer, return_exceptions=True)
    assert settled.is_set()


async def test_preflight_failure_after_backlog_preserves_rows_and_error():
    async def pre_reasoning(_agent, kwargs):
        for index in range(256):
            await kwargs["_context_status_sink"](index, False)
        raise ValueError("preflight failure")

    stream = AgentRunner._stream_native_context_preflight(
        SimpleNamespace(
            context_manager=SimpleNamespace(pre_reasoning=pre_reasoning)
        ),
        [],
    )
    rows = []
    with pytest.raises(ValueError, match="preflight failure"):
        async for row in stream:
            rows.append(row)
    assert rows == [(i, False) for i in range(256)]


async def test_close_full_preflight_queue_allows_cleanup_status():
    settled = asyncio.Event()

    async def pre_reasoning(_agent, kwargs):
        sink = kwargs["_context_status_sink"]
        try:
            for index in range(2048):
                await sink(index, False)
            await asyncio.Event().wait()
        finally:
            await sink("cancelled", True)
            settled.set()

    stream = AgentRunner._stream_native_context_preflight(
        SimpleNamespace(
            context_manager=SimpleNamespace(pre_reasoning=pre_reasoning)
        ),
        [],
    )
    await anext(stream)
    for _ in range(10):
        await asyncio.sleep(0)
    closer = asyncio.create_task(stream.aclose())
    done, _ = await asyncio.wait({closer}, timeout=0.1)
    if not done:
        # Force the isolated failed probe to settle without stranding pytest.
        for task in asyncio.all_tasks():
            if getattr(task.get_coro(), "__qualname__", "").endswith(
                "run_preflight"
            ):
                task.cancel()
        await asyncio.gather(closer, return_exceptions=True)
    assert done, "cleanup status blocked forever behind the closed consumer"
    assert settled.is_set()
