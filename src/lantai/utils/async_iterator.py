"""Explicit cancellation delegation across asynchronous generator boundaries."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager


class DelegatedAsyncIterator:
    """Own a child iterator and forward cancellation without losing its events.

    An async-for statement does not throw into a child suspended at yield.
    Bridges catch cancellation at their outward yield and request it here;
    the next pull throws once into the child, then its interruption events
    pass through the bridge's ordinary conversion and persistence logic.
    All source operations remain in the caller's task and ContextVar context.
    """

    def __init__(self, source):
        self._source = aiter(source)
        self._cancel = None
        self._cancel_sent = False
        self._exhausted = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            if self._cancel is not None and not self._cancel_sent:
                self._cancel_sent = True
                throw = getattr(self._source, "athrow", None)
                if not callable(throw):
                    raise self._cancel
                return await throw(self._cancel)
            return await anext(self._source)
        except StopAsyncIteration:
            self._exhausted = True
            if self._cancel is not None:
                raise self._cancel
            raise
        except asyncio.CancelledError:
            self._exhausted = True
            raise

    def request_cancel(self, exc: asyncio.CancelledError) -> None:
        if self._cancel is None:
            self._cancel = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if not self._exhausted:
                # Early failure/break must not abandon the suspended child or
                # advance ordinary work. Its cleanup runs before owner exit.
                if not self._cancel_sent:
                    self.request_cancel(asyncio.CancelledError())
                try:
                    async for _ in self:
                        pass
                except (StopAsyncIteration, asyncio.CancelledError):
                    pass
        finally:
            close = getattr(self._source, "aclose", None)
            if callable(close):
                await close()
        return False


async def close_async_iterator(source):
    """Await a locally owned transport's close without pulling more events."""
    close = getattr(source, "aclose", None)
    if not callable(close):
        close = getattr(source, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


@asynccontextmanager
async def closing_async_iterator(source):
    """Own acquired streams, including before their first event is consumed."""
    try:
        yield source
    finally:
        await close_async_iterator(source)
