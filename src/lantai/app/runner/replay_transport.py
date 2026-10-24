# -*- coding: utf-8 -*-
"""Bounded-memory, lossless transport replay; never a durable run authority.

Frames share one private, automatically deleted temporary spool. Subscribers
own byte cursors, not payload queues. Disk work has bounded admission, happens
outside the event loop, and settles before cancellation relinquishes ownership.
"""
from __future__ import annotations

import asyncio
import struct
import tempfile
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from lantai.utils.async_settlement import await_settled

_MEMORY_BYTES = 256 * 1024
_WORKERS = 2
_POOL = ThreadPoolExecutor(
    max_workers=_WORKERS, thread_name_prefix="replay-io"
)
_ADMISSION = threading.BoundedSemaphore(_WORKERS)
_HEADER = struct.Struct("!Q")


class ReplayTransport:
    def __init__(self) -> None:
        self.count = 0
        self._end = 0
        self._file = None
        self._io_lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._readers: weakref.WeakSet[ReplayCursor] = weakref.WeakSet()
        self._finished = False
        self._closed = False
        self._failure: str | None = None

    async def _io(self, operation):
        async with self._io_lock:
            while not _ADMISSION.acquire(blocking=False):
                await asyncio.sleep(0.005)
            try:
                return await asyncio.get_running_loop().run_in_executor(
                    _POOL,
                    operation,
                )
            finally:
                _ADMISSION.release()

    def _write(self, frame: str) -> int:
        if self._file is None:
            self._file = tempfile.SpooledTemporaryFile(
                max_size=_MEMORY_BYTES,
                mode="w+b",
            )
        data = frame.encode("utf-8")
        self._file.seek(self._end)
        self._file.write(_HEADER.pack(len(data)))
        self._file.write(data)
        return self._file.tell()

    async def append(
        self,
        frame: str,
        *,
        accept: Callable[[], bool],
        published: Callable[[], None],
    ) -> None:
        async def own_write() -> None:
            if self._finished or not accept():
                return
            end = await self._io(lambda: self._write(frame))
            # Publication and its identity/stop fence execute atomically on
            # the event loop. An unpublished write is overwritten next time.
            if not accept():
                return
            self._end = end
            self.count += 1
            published()
            self._changed.set()

        await await_settled(asyncio.create_task(own_write()))

    def subscribe(self, *, through_count: int | None = None) -> ReplayCursor:
        if self._closed:
            raise RuntimeError("Replay transport is closed")
        reader = ReplayCursor(self, through_count=through_count)
        self._readers.add(reader)
        return reader

    def fail(self, frame: str) -> None:
        # A single terminal can still be delivered after spool write failure.
        self._failure = frame
        self._changed.set()

    def seal(self) -> None:
        self._finished = True
        self._changed.set()

    async def close_if_unused(self) -> None:
        if not self._finished or self._readers:
            return

        async def close() -> None:
            def close_file() -> None:
                if self._file is not None:
                    self._file.close()

            await self._io(close_file)
            self._closed = True

        await await_settled(asyncio.create_task(close()))

    def _read(self, position: int) -> tuple[str, int]:
        self._file.seek(position)
        header = self._file.read(_HEADER.size)
        if len(header) != _HEADER.size:
            raise OSError("Incomplete replay frame header")
        size = _HEADER.unpack(header)[0]
        # Never trust a corrupt length to allocate beyond committed data.
        if size > self._end - position - _HEADER.size:
            raise OSError("Invalid replay frame size")
        data = self._file.read(size)
        if len(data) != size:
            raise OSError("Incomplete replay frame")
        return data.decode("utf-8"), self._file.tell()


class ReplayCursor:
    """Async queue-shaped reader with constant metadata per subscriber."""

    def __init__(
        self,
        transport: ReplayTransport,
        *,
        through_count: int | None = None,
    ) -> None:
        self._transport = transport
        self._position = 0
        self._count = 0
        self._through_count = through_count
        self._failure_seen = False
        self._closed = False
        self._get_lock = asyncio.Lock()

    async def get(self) -> str | None:
        async with self._get_lock:
            journal = self._transport
            while not self._closed:
                if (
                    self._through_count is not None
                    and self._count >= self._through_count
                ):
                    return None
                if self._count < journal.count:
                    frame, position = await await_settled(
                        asyncio.create_task(
                            journal._io(
                                lambda: journal._read(self._position),
                            )
                        ),
                    )
                    # If a timed-out/disconnected reader is cancelled during
                    # IO, settlement propagates cancellation without moving
                    # its cursor, so the frame remains available on retry.
                    if self._closed:
                        return None
                    self._position = position
                    self._count += 1
                    return frame
                if journal._failure is not None and not self._failure_seen:
                    self._failure_seen = True
                    return journal._failure
                if journal._finished:
                    return None
                journal._changed.clear()
                await journal._changed.wait()
            return None

    async def close(self) -> None:
        self._closed = True
        journal = self._transport
        journal._changed.set()
        journal._readers.discard(self)
        await journal.close_if_unused()
