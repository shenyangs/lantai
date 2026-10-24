"""Keep ownership of work that cannot safely end with its awaiting caller."""

from __future__ import annotations

import asyncio
from typing import Callable, ParamSpec, TypeVar

T = TypeVar("T")
P = ParamSpec("P")


async def await_settled(task: asyncio.Task[T]) -> T:
    """Shield owned work and propagate caller cancellation after settlement.

    Repeated cancellation must not detach a constructor, synchronous worker,
    or cleanup. The owner must register its result inside ``task`` before
    this wait returns or raises. This does not turn failed work into success.
    """
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            continue
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        else:
            if cancelled:
                raise asyncio.CancelledError
            return result


async def run_sync_settled(
    operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run blocking work without detaching it when its caller is stopped."""

    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    return await await_settled(task)
