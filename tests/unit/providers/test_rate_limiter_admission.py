"""Dispatch-time quota checks, with no live provider traffic."""
import asyncio
from types import SimpleNamespace

import pytest

from lantai.providers import rate_limiter as module


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0, sleeps=[], yield_control=asyncio.sleep)

    async def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    return clock


@pytest.mark.asyncio
async def test_queued_request_checks_new_cooldown_and_records_dispatch_time(clock):
    limiter = module.LLMRateLimiter(max_concurrent=1, max_qpm=10, jitter_range=0)
    await limiter.acquire()
    waiting = asyncio.create_task(limiter.acquire())
    # Let the second request reach the concurrency queue without advancing time.
    await clock.yield_control(0)
    clock.now = 110.0
    await limiter.report_rate_limit(retry_after=20)
    limiter.release()
    await waiting
    assert clock.sleeps == [20]
    assert list(limiter._request_times) == [100, 130]
    limiter.release()


@pytest.mark.asyncio
async def test_cancellation_while_throttled_does_not_leak_concurrency(monkeypatch):
    limiter = module.LLMRateLimiter(max_concurrent=1, jitter_range=0)
    await limiter.report_rate_limit(retry_after=60)
    sleeping = asyncio.Event()

    async def sleep(delay):
        sleeping.set()
        await asyncio.Future()

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    waiter = asyncio.create_task(limiter.acquire())
    await sleeping.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    limiter._pause_until = 0
    await asyncio.wait_for(limiter.acquire(), timeout=0.1)
    assert limiter.stats()["current_in_flight"] == 1
    limiter.release()


@pytest.mark.asyncio
async def test_cooldown_arriving_during_rpm_wait_is_respected(monkeypatch, clock):
    limiter = module.LLMRateLimiter(max_concurrent=2, max_qpm=1, jitter_range=0)
    await limiter.acquire()
    limiter.release()

    async def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay
        if len(clock.sleeps) == 1:
            await limiter.report_rate_limit(retry_after=20)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    await limiter.acquire()
    assert len(clock.sleeps) == 2
    assert clock.sleeps[1] == 20
    limiter.release()
