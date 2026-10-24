"""Bound application audio retention without silently dropping PCM frames."""

from __future__ import annotations

import asyncio


class AudioInbox(asyncio.Queue):
    """A finite PCM queue with a reserved, idempotent end-of-stream slot.

    Producers use put_nowait and terminate the call on QueueFull. Waiting for
    space would move the backlog into the SDK's separate ingress queue.
    This bounds application retention, not the SDK or native RTP buffers.
    """

    def __init__(self, max_frames: int = 64, max_bytes: int = 2 << 20):
        super().__init__(maxsize=max_frames + 1)
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._retained_bytes = 0
        self.closed = False
        self.failure: str | None = None

    def full(self) -> bool:
        return self.closed or self.qsize() >= self._max_frames

    def put_nowait(self, item: bytes) -> None:
        if self.full() or self._retained_bytes + len(item) > self._max_bytes:
            raise asyncio.QueueFull
        super().put_nowait(item)
        self._retained_bytes += len(item)

    def get_nowait(self):
        item = super().get_nowait()
        if item is not None:
            self._retained_bytes -= len(item)
        return item

    def close(self, failure: str | None = None) -> None:
        if failure is not None and self.failure is None:
            self.failure = failure
        if self.closed:
            return
        self.closed = True
        # Reserve the terminal slot without evicting any admitted frame.
        self._put(None)
        self._unfinished_tasks += 1
        self._finished.clear()
        self._wakeup_next(self._getters)
        # Wake any waiting producer so it can observe closure.
        self._wakeup_next(self._putters)
