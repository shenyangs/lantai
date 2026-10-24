# -*- coding: utf-8 -*-
"""Behavior locks for bounded per-session channel consumers."""

from __future__ import annotations

import asyncio

import pytest

from lantai.app.channels.unified_queue_manager import (
    QueueRegistryFullError,
    UnifiedQueueManager,
)


@pytest.mark.asyncio
async def test_queue_registry_rejects_unbounded_session_growth() -> None:
    release = asyncio.Event()

    async def consume(_queue, _channel, _session, _priority) -> None:
        await release.wait()

    manager = UnifiedQueueManager(
        consumer_fn=consume,
        queue_maxsize=1,
        max_queues=1,
    )
    await manager.enqueue("console", "session-1", 20, "first")

    with pytest.raises(QueueRegistryFullError):
        await manager.enqueue("console", "session-2", 20, "second")

    metrics = await manager.get_metrics()
    assert metrics["total_queues"] == 1
    await manager.stop_all()
