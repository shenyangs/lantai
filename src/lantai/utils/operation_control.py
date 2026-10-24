"""An optional operation boundary shared by async work and local processes.

The owner supplies identity, lifecycle and deadline checks. Utilities do not
interpret product state, and ordinary callers retain their existing behavior.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from inspect import Parameter, signature
from threading import Event, Lock
from typing import Any, Callable, Coroutine, Iterator


class _Resources:
    def __init__(self):
        self.lock = Lock()
        self.count = 0
        self.failed = False


@dataclass(frozen=True)
class OperationControl:
    owner_key: tuple[str, str]
    identity: tuple[Any, ...]
    ensure_active: Callable[[], None]
    remaining_seconds: Callable[[], float]
    publication_guard: Callable | None = None
    writer_lock_fd: int | None = None
    # An unsettled resource prevents the durable owner from being retired.
    unsettled: Event = field(default_factory=Event, compare=False)
    resources: _Resources = field(default_factory=_Resources, compare=False)

    def resource_started(self) -> None:
        with self.resources.lock:
            self.resources.count += 1
            self.unsettled.set()

    def resource_settled(self, success: bool) -> None:
        with self.resources.lock:
            self.resources.count -= 1
            self.resources.failed |= not success
            if self.resources.count == 0 and not self.resources.failed:
                self.unsettled.clear()


_CONTROL: ContextVar[OperationControl | None] = ContextVar(
    "lantai_operation_control", default=None,
)


def current_operation() -> OperationControl | None:
    return _CONTROL.get()


@contextmanager
def bind_operation(control: OperationControl) -> Iterator[None]:
    token = _CONTROL.set(control)
    try:
        yield
    finally:
        _CONTROL.reset(token)


def check_operation() -> None:
    control = current_operation()
    if control is not None:
        control.ensure_active()


@contextmanager
def publication_boundary():
    control = current_operation()
    if control is not None and control.publication_guard is not None:
        with control.publication_guard():
            yield
    else:
        check_operation()
        yield


def remaining_timeout(local_timeout: float | None) -> float | None:
    control = current_operation()
    if control is None:
        return local_timeout
    control.ensure_active()
    remaining = control.remaining_seconds()
    if remaining <= 0:
        raise TimeoutError("operation exceeded its absolute deadline")
    return remaining if local_timeout is None else min(float(local_timeout), remaining)


async def _settle(task: asyncio.Task) -> None:
    """Do not forward repeated cancellation into an in-flight cleanup block."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except BaseException:
            if task.done():
                break
    try:
        task.result()
    except BaseException:
        pass


async def cancellable_await(
    work: Coroutine,
    ensure_active: Callable[[], None] | None = None,
) -> Any:
    def check() -> None:
        check_operation()
        if ensure_active is not None:
            ensure_active()

    try:
        check()
    except BaseException:
        work.close()
        raise
    task = asyncio.create_task(work)
    try:
        # Admission was checked above. Give the admitted coroutine its first
        # turn so a subsequent Stop cancels and settles its own cleanup.
        await asyncio.sleep(0)
        while not task.done():
            check()
            await asyncio.wait({task}, timeout=0.05)
        check()
        return task.result()
    except BaseException:
        if not task.done():
            task.cancel()
        await _settle(task)
        raise


def run_controlled(work: Coroutine) -> Any:
    return asyncio.run(cancellable_await(work))


async def run_operation_utility(function: Callable, **kwargs: Any) -> Any:
    if current_operation() is not None:
        parameters = signature(function).parameters
        accepts_extra = any(value.kind == Parameter.VAR_KEYWORD for value in parameters.values())
        for key in (
            "timeout_seconds", "first_output_timeout_seconds", "idle_timeout_seconds",
        ):
            if key in parameters or accepts_extra:
                kwargs[key] = remaining_timeout(kwargs.get(key))
    return await cancellable_await(function(**kwargs))


@contextmanager
def controlled_capacity(semaphore, ensure_active=None, poll_seconds=0.05):
    def check() -> None:
        check_operation()
        if ensure_active is not None:
            ensure_active()

    if current_operation() is None and ensure_active is None:
        semaphore.acquire()
    else:
        while True:
            check()
            if semaphore.acquire(timeout=max(0.001, float(poll_seconds))):
                break
    try:
        check()
        yield
    finally:
        semaphore.release()
