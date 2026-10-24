# -*- coding: utf-8 -*-
"""Shared admission control for expensive local runtime work.

The desktop app has several independently bounded executors (agent turns,
coding, browser automation and capability pipelines).  Independent limits do
not protect the process when they all become busy at once, and they do not
prevent two turns for the same conversation from interleaving.  This module
provides one process-wide governor with bounded backpressure and per-owner
serialization.
"""
from __future__ import annotations

import asyncio
import contextvars
import weakref
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator, Mapping

from ..constant import EnvVarLoader


_HELD_SLOT: contextvars.ContextVar[
    tuple[asyncio.Task, str, str] | None
] = contextvars.ContextVar("lantai_runtime_held_slot", default=None)


class ConcurrencyAdmissionError(RuntimeError):
    """Raised when runtime work cannot safely enter the bounded queue."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeConcurrencyGovernor:
    """Coordinate global, category and same-owner concurrency limits."""

    def __init__(
        self,
        *,
        max_active: int,
        max_waiters: int,
        acquire_timeout_seconds: float,
        category_limits: Mapping[str, int] | None = None,
    ) -> None:
        self.max_active = max(1, int(max_active))
        self.max_waiters = max(1, int(max_waiters))
        self.acquire_timeout_seconds = max(0.05, float(acquire_timeout_seconds))
        self._global = asyncio.Semaphore(self.max_active)
        self._category_limits = {
            str(key): max(1, int(value))
            for key, value in (category_limits or {}).items()
        }
        self._categories = {
            key: asyncio.Semaphore(value)
            for key, value in self._category_limits.items()
        }
        self._owner_locks: dict[str, asyncio.Lock] = {}
        self._owner_refs: dict[str, int] = defaultdict(int)
        self._state_lock = asyncio.Lock()
        self._active = 0
        self._waiters = 0
        self._active_by_category: dict[str, int] = defaultdict(int)

    def snapshot(self) -> dict[str, object]:
        """Return a lightweight event-loop-local diagnostic snapshot."""

        return {
            "active": self._active,
            "waiters": self._waiters,
            "max_active": self.max_active,
            "max_waiters": self.max_waiters,
            "active_by_category": {
                key: value
                for key, value in sorted(self._active_by_category.items())
                if value
            },
            "category_limits": dict(sorted(self._category_limits.items())),
        }

    async def _register_waiter(self, owner_key: str) -> asyncio.Lock | None:
        async with self._state_lock:
            if self._waiters >= self.max_waiters:
                raise ConcurrencyAdmissionError(
                    "runtime_concurrency_queue_full",
                    "Runtime concurrency queue is full; retry after active work settles.",
                )
            self._waiters += 1
            if not owner_key:
                return None
            lock = self._owner_locks.setdefault(owner_key, asyncio.Lock())
            self._owner_refs[owner_key] += 1
            return lock

    async def _finish_waiting(self) -> None:
        async with self._state_lock:
            self._waiters = max(0, self._waiters - 1)

    async def _release_owner_ref(self, owner_key: str) -> None:
        if not owner_key:
            return
        async with self._state_lock:
            remaining = self._owner_refs.get(owner_key, 0) - 1
            if remaining <= 0:
                self._owner_refs.pop(owner_key, None)
                lock = self._owner_locks.get(owner_key)
                if lock is not None and not lock.locked():
                    self._owner_locks.pop(owner_key, None)
            else:
                self._owner_refs[owner_key] = remaining

    @asynccontextmanager
    async def slot(
        self,
        *,
        category: str,
        owner_key: str = "",
    ) -> AsyncIterator[None]:
        """Acquire a bounded slot and release every permit on cancellation."""

        normalized_category = str(category or "background").strip() or "background"
        normalized_owner = str(owner_key or "").strip()
        current_task = asyncio.current_task()
        held_slot = _HELD_SLOT.get()
        if (
            held_slot is not None
            and held_slot[0] is current_task
            and held_slot[1] == normalized_category
            and held_slot[2] == normalized_owner
        ):
            # Top-level adapters and the central runner intentionally share
            # the same governor.  Re-entering the exact same logical slot in
            # one task must not consume a second permit or deadlock, while a
            # child task (which inherits ContextVars) still has to queue.
            yield
            return

        borrows_global_slot = held_slot is not None and held_slot[0] is current_task
        borrows_category_slot = (
            borrows_global_slot and held_slot[1] == normalized_category
        )
        owner_lock_key = (
            f"{normalized_category}\0{normalized_owner}"
            if normalized_owner
            else ""
        )
        owner_lock = await self._register_waiter(owner_lock_key)
        category_sem = self._categories.get(normalized_category)
        owner_acquired = False
        category_acquired = False
        global_acquired = False
        global_active_registered = False
        category_active_registered = False
        held_slot_token: contextvars.Token | None = None

        async def acquire() -> None:
            nonlocal owner_acquired, category_acquired, global_acquired
            if owner_lock is not None:
                await owner_lock.acquire()
                owner_acquired = True
            if category_sem is not None and not borrows_category_slot:
                await category_sem.acquire()
                category_acquired = True
            if not borrows_global_slot:
                await self._global.acquire()
                global_acquired = True

        try:
            try:
                # Preserve the original single-tick streaming behavior when
                # every permit is immediately available.  ``wait_for`` wraps
                # the coroutine in another task on Python 3.10, which adds a
                # scheduler turn and can make a just-started stream appear
                # empty to reconnect/snapshot callers.
                permits_available = (
                    (owner_lock is None or not owner_lock.locked())
                    and (
                        category_sem is None
                        or borrows_category_slot
                        or not category_sem.locked()
                    )
                    and (borrows_global_slot or not self._global.locked())
                )
                if permits_available:
                    await acquire()
                else:
                    await asyncio.wait_for(
                        acquire(),
                        timeout=self.acquire_timeout_seconds,
                    )
            except asyncio.TimeoutError as exc:
                raise ConcurrencyAdmissionError(
                    "runtime_concurrency_acquire_timeout",
                    "Runtime work waited too long for a concurrency slot.",
                ) from exc
            finally:
                await self._finish_waiting()

            async with self._state_lock:
                if global_acquired:
                    self._active += 1
                    global_active_registered = True
                if not borrows_category_slot:
                    self._active_by_category[normalized_category] += 1
                    category_active_registered = True
            if current_task is not None:
                held_slot_token = _HELD_SLOT.set(
                    (current_task, normalized_category, normalized_owner)
                )
            yield
        finally:
            if held_slot_token is not None:
                try:
                    _HELD_SLOT.reset(held_slot_token)
                except ValueError:
                    # A pending async generator can be finalized by the loop
                    # in a copied context during abrupt shutdown.  Permit
                    # release must still finish instead of leaking capacity.
                    if _HELD_SLOT.get() is not None:
                        _HELD_SLOT.set(None)
            if category_active_registered or global_active_registered:
                async with self._state_lock:
                    if global_active_registered:
                        self._active = max(0, self._active - 1)
                    if category_active_registered:
                        self._active_by_category[normalized_category] = max(
                            0,
                            self._active_by_category[normalized_category] - 1,
                        )
            if global_acquired:
                self._global.release()
            if category_acquired and category_sem is not None:
                category_sem.release()
            if owner_acquired and owner_lock is not None:
                owner_lock.release()
            await self._release_owner_ref(owner_lock_key)


_LOOP_GOVERNORS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    RuntimeConcurrencyGovernor,
] = weakref.WeakKeyDictionary()


def get_runtime_concurrency_governor() -> RuntimeConcurrencyGovernor:
    """Return the governor bound to the current event loop."""

    loop = asyncio.get_running_loop()
    governor = _LOOP_GOVERNORS.get(loop)
    if governor is not None:
        return governor
    governor = RuntimeConcurrencyGovernor(
        max_active=EnvVarLoader.get_int(
            "LANTAI_RUNTIME_MAX_CONCURRENT",
            5,
            min_value=1,
            max_value=8,
        ),
        max_waiters=EnvVarLoader.get_int(
            "LANTAI_RUNTIME_MAX_WAITERS",
            12,
            min_value=1,
            max_value=64,
        ),
        acquire_timeout_seconds=EnvVarLoader.get_float(
            "LANTAI_RUNTIME_ACQUIRE_TIMEOUT_SECONDS",
            45.0,
            min_value=1.0,
            max_value=300.0,
        ),
        category_limits={
            "agent": EnvVarLoader.get_int(
                "LANTAI_AGENT_MAX_CONCURRENT", 5, min_value=1, max_value=8
            ),
            "coding": EnvVarLoader.get_int(
                "LANTAI_CODING_MAX_CONCURRENT", 1, min_value=1, max_value=2
            ),
            "browser": EnvVarLoader.get_int(
                "LANTAI_BROWSER_MAX_CONCURRENT", 1, min_value=1, max_value=2
            ),
            "pipeline": EnvVarLoader.get_int(
                "LANTAI_PIPELINE_MAX_CONCURRENT", 2, min_value=1, max_value=4
            ),
            "background": EnvVarLoader.get_int(
                "LANTAI_BACKGROUND_MAX_CONCURRENT", 2, min_value=1, max_value=4
            ),
        },
    )
    _LOOP_GOVERNORS[loop] = governor
    return governor
