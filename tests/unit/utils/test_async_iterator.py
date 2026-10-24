import asyncio
import contextvars

import pytest


@pytest.mark.asyncio
async def test_cancel_at_yield_crosses_all_layers_and_preserves_terminal():
    from lantai.utils.async_iterator import DelegatedAsyncIterator

    marker = contextvars.ContextVar("owned_marker", default=False)
    trace = []

    async def source():
        token = marker.set(True)
        try:
            yield "progress"
            trace.append("unwanted-side-effect")
        except asyncio.CancelledError:
            trace.append("interrupted-checkpoint")
            yield "interrupted"
            raise
        finally:
            assert marker.get()
            marker.reset(token)
            trace.append("source-closed")

    async def bridge(child, name):
        async with DelegatedAsyncIterator(child) as events:
            async for item in events:
                trace.append((name, item))
                try:
                    yield item
                except asyncio.CancelledError as exc:
                    events.request_cancel(exc)

    chain = source()
    for name in ("runtime", "runner", "channel", "route"):
        chain = bridge(chain, name)
    assert await anext(chain) == "progress"
    assert await chain.athrow(asyncio.CancelledError()) == "interrupted"
    with pytest.raises(asyncio.CancelledError):
        await anext(chain)
    assert "unwanted-side-effect" not in trace
    assert trace.count("interrupted-checkpoint") == 1
    assert trace.count("source-closed") == 1
    assert all(
        (name, "interrupted") in trace
        for name in ("runtime", "runner", "channel", "route")
    )
    assert not marker.get()


@pytest.mark.asyncio
async def test_body_failure_cancels_and_closes_child_before_return():
    from lantai.utils.async_iterator import DelegatedAsyncIterator

    trace = []

    async def source():
        try:
            yield 1
            trace.append("unwanted-side-effect")
        except asyncio.CancelledError:
            trace.append("cancelled")
            yield "interrupted"
            raise
        finally:
            trace.append("closed")

    with pytest.raises(ValueError, match="transport failed"):
        async with DelegatedAsyncIterator(source()) as events:
            async for _ in events:
                raise ValueError("transport failed")
    assert trace == ["cancelled", "closed"]


@pytest.mark.asyncio
async def test_child_consuming_cancel_cannot_resume_outer_work():
    from lantai.utils.async_iterator import DelegatedAsyncIterator

    trace = []

    async def source():
        try:
            yield "partial"
        except asyncio.CancelledError:
            yield "interrupted"
        finally:
            trace.append("closed")

    async def bridge():
        async with DelegatedAsyncIterator(source()) as events:
            async for event in events:
                try:
                    yield event
                except asyncio.CancelledError as exc:
                    events.request_cancel(exc)
        trace.append("unwanted-next-phase")

    stream = bridge()
    assert await anext(stream) == "partial"
    assert await stream.athrow(asyncio.CancelledError()) == "interrupted"
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)
    assert trace == ["closed"]
