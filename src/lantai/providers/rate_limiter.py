# -*- coding: utf-8 -*-
"""Provider-scoped LLM request rate limiters.

How it works:
1. QPM sliding window: tracks request timestamps in a 60-second window.
   Before each call, if the window is full, the caller waits until the
   oldest timestamp slides out — proactively preventing 429s.
2. asyncio.Semaphore caps the number of concurrent in-flight LLM calls.
3. A global pause timestamp: when a 429 is received every subsequent
   acquire() waits until the pause expires, eliminating thundering-herd
   retries.
4. Per-waiter jitter: each caller adds a small random offset on top of
   the remaining pause time, so they spread out when waking up.

acquire() execution order:
    wait for semaphore slot → recheck cooldown and QPM together → dispatch
"""

from __future__ import annotations

import asyncio
import collections
import logging
import random
import time

logger = logging.getLogger(__name__)


class LLMRateLimiter:
    """Global LLM request rate limiter.

    Coroutine-safe: all mutable state is protected by asyncio primitives and
    is intended for use within a single event loop.

    Args:
        max_concurrent: Maximum concurrent in-flight LLM calls (semaphore).
        max_qpm: Maximum queries per minute (sliding window). 0 = disabled.
        default_pause_seconds: Global pause duration on a 429 response.
        jitter_range: Random jitter (seconds) added on top of the pause.
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        max_qpm: int = 0,
        default_pause_seconds: float = 5.0,
        jitter_range: float = 1.0,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._pause_until: float = 0.0
        self._lock = asyncio.Lock()
        self._default_pause = default_pause_seconds
        self._jitter_range = jitter_range

        # QPM sliding window — stores monotonic timestamps of dispatched calls.
        self._max_qpm = max_qpm
        self._request_times: collections.deque[float] = collections.deque()
        self._qpm_lock = asyncio.Lock()

        # Own counter instead of reading semaphore._value (private API).
        self._in_flight: int = 0

        self._total_acquired: int = 0
        self._total_paused: int = 0
        self._total_qpm_waited: int = 0
        self._total_rate_limited: int = 0

    async def acquire(self) -> None:
        """Reserve concurrency, then admit against current cooldown and RPM.

        Record quota only at dispatch: a request waiting for concurrency must
        observe any 429 received while it was queued. Recheck both constraints
        after every sleep. Callers retain their existing bounded wait_for.
        """
        await self._semaphore.acquire()
        try:
            await self._wait_for_admission()
        except BaseException:
            # Cancellation/timeout during throttling must return the slot.
            self._semaphore.release()
            raise
        self._in_flight += 1
        self._total_acquired += 1

    async def _wait_for_admission(self) -> None:
        while True:
            async with self._qpm_lock:
                now = time.monotonic()
                remaining = self._pause_until - now
                if remaining > 0:
                    wait_time = remaining + random.uniform(0, self._jitter_range)
                    self._total_paused += 1
                elif self._max_qpm > 0:
                    cutoff = now - 60.0
                    while self._request_times and self._request_times[0] <= cutoff:
                        self._request_times.popleft()
                    if len(self._request_times) < self._max_qpm:
                        self._request_times.append(now)
                        return
                    wait_time = self._request_times[0] + 60.0 - now + 0.05
                    self._total_qpm_waited += 1
                else:
                    return
            # Do not hold the quota lock while sleeping: another caller may
            # report a longer cooldown. The loop checks that before dispatch.
            logger.debug("LLM rate limiter: waiting %.1fs for admission", wait_time)
            await asyncio.sleep(wait_time)

    def release(self) -> None:
        """Release the semaphore slot.
        Must be paired with a prior acquire()."""
        self._in_flight -= 1
        self._semaphore.release()

    async def report_rate_limit(
        self,
        retry_after: float | None = None,
    ) -> None:
        """Record a 429 rate-limit response and set the global pause timestamp.

        Args:
            retry_after: Seconds from the API's Retry-After header.
                         Falls back to the configured default when None.
        """
        pause = retry_after if retry_after is not None else self._default_pause
        async with self._lock:
            new_until = time.monotonic() + pause
            if new_until > self._pause_until:
                self._pause_until = new_until
                self._total_rate_limited += 1
                logger.warning(
                    "LLM rate limiter: global pause set for %.1fs "
                    "(total_rate_limited=%d)",
                    pause,
                    self._total_rate_limited,
                )

    def stats(self) -> dict:
        """Return a snapshot of runtime statistics for logging or
        monitoring."""
        now = time.monotonic()
        cutoff = now - 60.0
        requests_last_60s = sum(1 for t in self._request_times if t >= cutoff)
        return {
            "max_concurrent": self._max_concurrent,
            "current_in_flight": self._in_flight,
            "current_available": max(
                0,
                self._max_concurrent - self._in_flight,
            ),
            "max_qpm": self._max_qpm,
            "requests_last_60s": requests_last_60s,
            "is_paused": now < self._pause_until,
            "pause_remaining_s": max(0.0, self._pause_until - now),
            "total_acquired": self._total_acquired,
            "total_paused": self._total_paused,
            "total_qpm_waited": self._total_qpm_waited,
            "total_rate_limited": self._total_rate_limited,
        }


# One limiter per upstream provider. A 429 or saturated queue from one vendor
# must never freeze unrelated providers or every conversation in the app.
_provider_limiters: dict[str, LLMRateLimiter] = {}
_init_lock: asyncio.Lock | None = None


def _get_init_lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


async def get_rate_limiter(
    max_concurrent: int | None = None,
    max_qpm: int | None = None,
    default_pause_seconds: float | None = None,
    jitter_range: float | None = None,
    scope_key: str | None = None,
) -> LLMRateLimiter:
    """Return one lazily initialized limiter for an upstream provider.

    Calls sharing ``scope_key`` share the provider's quota and cooldown. Calls
    with different keys remain isolated. The default key preserves a safe
    fallback for older callers that do not expose provider identity.

    Args:
        max_concurrent: Cap on concurrent in-flight LLM calls.
        max_qpm: Maximum queries per minute (sliding window). 0 = disabled.
        default_pause_seconds: Pause duration (s) applied on a 429 response.
        jitter_range: Random jitter (s) added on top of the pause.
    """
    normalized_scope = str(scope_key or "default").strip() or "default"
    existing = _provider_limiters.get(normalized_scope)
    if existing is not None:
        return existing
    async with _get_init_lock():
        existing = _provider_limiters.get(normalized_scope)
        if existing is not None:
            return existing
        from ..constant import (
            LLM_MAX_CONCURRENT,
            LLM_MAX_QPM,
            LLM_RATE_LIMIT_JITTER,
            LLM_RATE_LIMIT_PAUSE,
        )

        resolved_max = (
            max_concurrent
            if max_concurrent is not None
            else LLM_MAX_CONCURRENT
        )
        resolved_qpm = max_qpm if max_qpm is not None else LLM_MAX_QPM
        resolved_pause = (
            default_pause_seconds
            if default_pause_seconds is not None
            else LLM_RATE_LIMIT_PAUSE
        )
        resolved_jitter = (
            jitter_range if jitter_range is not None else LLM_RATE_LIMIT_JITTER
        )

        limiter = LLMRateLimiter(
            max_concurrent=resolved_max,
            max_qpm=resolved_qpm,
            default_pause_seconds=resolved_pause,
            jitter_range=resolved_jitter,
        )
        _provider_limiters[normalized_scope] = limiter
        logger.info(
            "LLM rate limiter initialized: scope=%s, max_concurrent=%d, "
            "max_qpm=%d, default_pause=%.1fs, jitter=%.1fs",
            normalized_scope,
            resolved_max,
            resolved_qpm,
            resolved_pause,
            resolved_jitter,
        )
    return limiter


def reset_rate_limiter(scope_key: str | None = None) -> None:
    """Reset one provider limiter, or all limiters when no key is given."""
    if scope_key is None:
        _provider_limiters.clear()
        return
    normalized_scope = str(scope_key).strip() or "default"
    _provider_limiters.pop(normalized_scope, None)
