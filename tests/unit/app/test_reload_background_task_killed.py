# -*- coding: utf-8 -*-
"""Tests for GitHub issue #3275:

Background tasks dispatched via ``--background`` (i.e. submitted through the
Lantai process task endpoint) were unexpectedly cancelled when an agent
workspace underwent a reload.

Root cause
----------
``MultiAgentManager._graceful_stop_old_instance()`` only consulted
``Workspace.task_tracker`` (Lantai's internal ``TaskTracker``).  Background
tasks submitted through the old process task facade were tracked in a separate
``active_tasks`` map (managed by the task engine mixin) and were *invisible* to
the graceful-shutdown check.

Fix (Option A)
--------------
``DynamicMultiAgentRunner.stream_query()`` now registers each request with
the workspace's ``TaskTracker`` via ``register_external_task()`` /
``unregister_external_task()``.  This makes process-dispatched tasks visible
to ``has_active_tasks()`` during graceful shutdown.

This file contains:
- Reproduction tests proving the original bug exists without the fix
- Verification tests proving the fix resolves the issue
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Import TaskTracker directly (bypass heavy __init__.py chains that pull in
# heavyweight runtime packages, numpy, etc.).
# ---------------------------------------------------------------------------


def _import_module_directly(module_name: str, file_path: str) -> ModuleType:
    """Import a single module file without triggering package __init__.py."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert (
        spec is not None and spec.loader is not None
    ), f"Failed to create module spec for {file_path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


_SRC = Path(__file__).resolve().parents[3] / "src"

# Use a test-unique module name so the isolated import does not shadow
# the real ``lantai.app.runner.task_tracker`` in ``sys.modules`` for
# other tests that run in the same pytest session.
_task_tracker_mod = _import_module_directly(
    "_test_issue_3275_task_tracker",
    str(_SRC / "lantai" / "app" / "runner" / "task_tracker.py"),
)
TaskTracker = _task_tracker_mod.TaskTracker


# ---------------------------------------------------------------------------
# Minimal stubs that satisfy MultiAgentManager / Workspace contracts without
# needing a full configuration file or running services.
# ---------------------------------------------------------------------------


class StubServiceManager:
    """Minimal stub for ServiceManager."""

    def __init__(self):
        self.services: dict[str, Any] = {}

    def get_reusable_services(self) -> dict:
        return {}


class StubWorkspace:
    """Minimal stand-in for ``Workspace``.

    Attributes:
        task_tracker: A real ``TaskTracker`` instance.
        stopped: Set to ``True`` when ``stop()`` is called.
    """

    def __init__(self, agent_id: str, workspace_dir: str):
        self.agent_id = agent_id
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._task_tracker = TaskTracker()
        self._service_manager = StubServiceManager()
        self._started = True
        self._manager = None
        self.stopped = False
        self.runner = MagicMock()

    @property
    def task_tracker(self):
        return self._task_tracker

    def set_manager(self, manager: Any) -> None:
        self._manager = manager

    async def start(self) -> None:
        self._started = True

    async def stop(self, final: bool = True) -> None:
        # ``final`` mirrors Workspace.stop() signature but is unused by
        # the stub.
        del final
        self.stopped = True
        self._started = False


# ---------------------------------------------------------------------------
# Standalone reimplementation of the graceful-stop logic from
# MultiAgentManager._graceful_stop_old_instance() — extracted here to avoid
# importing the full module with all its heavy dependencies.
# This mirrors lines 91-186 of multi_agent_manager.py exactly.
# ---------------------------------------------------------------------------


async def graceful_stop_old_instance(
    old_instance: StubWorkspace,
    agent_id: str,
    cleanup_tasks: set,
) -> bool:
    """Return True if stopped immediately, False if delayed cleanup scheduled.

    Reproduces the exact logic of
    ``MultiAgentManager._graceful_stop_old_instance()``.
    """
    # ``agent_id`` kept to mirror the real signature used for logging.
    del agent_id
    has_active = await old_instance.task_tracker.has_active_tasks()

    if has_active:

        async def delayed_cleanup():
            try:
                await old_instance.task_tracker.wait_all_done(timeout=60.0)
                await old_instance.stop(final=False)
            except Exception:
                pass

        cleanup_task = asyncio.create_task(delayed_cleanup())
        cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(cleanup_tasks.discard)
        return False  # delayed
    else:
        await old_instance.stop(final=False)
        return True  # immediate


# ---------------------------------------------------------------------------
# Simulated old process task state (mirrors TaskEngineMixin)
# ---------------------------------------------------------------------------


def _make_process_active_tasks() -> dict[str, dict]:
    """Return an ``active_tasks`` dict with one in-flight background task.

    This mirrors what the process task facade / ``TaskEngineMixin`` would hold
    when a background task was submitted via ``POST /api/agent/process/task``.
    """
    return {
        "task-bg-001": {
            "task_id": "task-bg-001",
            "status": "running",
            "queue": "stream_query",
            "submitted_at": 1000.0,
            "started_at": 1001.0,
        },
    }


# ===========================================================================
# Part 1: Bug reproduction tests (prove the original problem)
# ===========================================================================


@pytest.mark.asyncio
async def test_unregistered_task_causes_immediate_stop():
    """Without external task registration, the old workspace is stopped
    immediately even though a background task is still running.

    This demonstrates the original bug path: when tasks are NOT registered
    with the TaskTracker, ``_graceful_stop_old_instance`` considers the
    workspace idle.
    """
    cleanup_tasks: set[asyncio.Task] = set()

    with tempfile.TemporaryDirectory() as tmpdir:
        old_ws = StubWorkspace("default", str(Path(tmpdir) / "old"))

        # Lantai TaskTracker is empty (no registered tasks)
        assert await old_ws.task_tracker.has_active_tasks() is False

        # The process task facade has a running background task, but it is not
        # registered with the workspace TaskTracker.
        process_tasks = _make_process_active_tasks()
        assert any(
            t["status"] in ("submitted", "running")
            for t in process_tasks.values()
        )

        # Graceful stop runs — stops immediately (the bug path)
        stopped_immediately = await graceful_stop_old_instance(
            old_ws,
            "default",
            cleanup_tasks,
        )

        assert stopped_immediately is True, (
            "Without external task registration, the workspace is stopped "
            "immediately because has_active_tasks() returns False."
        )
        assert old_ws.stopped is True


@pytest.mark.asyncio
async def test_reload_kills_unregistered_background_task():
    """End-to-end reproduction: agent reload kills a background task that is
    NOT registered with the workspace's TaskTracker.
    """
    cleanup_tasks: set[asyncio.Task] = set()

    with tempfile.TemporaryDirectory() as tmpdir:
        old_ws = StubWorkspace("default", str(Path(tmpdir) / "old"))

        async def slow_background_task():
            await asyncio.sleep(60)
            return "completed"

        bg_task = asyncio.create_task(slow_background_task())
        await asyncio.sleep(0)
        assert not bg_task.done()

        # TaskTracker sees nothing — the task was not registered.
        assert await old_ws.task_tracker.has_active_tasks() is False

        # Graceful stop: immediate (the bug).
        stopped_immediately = await graceful_stop_old_instance(
            old_ws,
            "default",
            cleanup_tasks,
        )
        assert stopped_immediately is True
        assert old_ws.stopped is True

        # Clean up the orphaned task.
        bg_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bg_task
        assert bg_task.cancelled()


# ===========================================================================
# Part 2: Fix verification tests
# ===========================================================================


@pytest.mark.asyncio
async def test_register_external_task_makes_tracker_active():
    """``register_external_task`` makes the task visible to
    ``has_active_tasks()`` and ``list_active_tasks()``.
    """
    tracker = TaskTracker()

    assert await tracker.has_active_tasks() is False
    assert await tracker.list_active_tasks() == []

    await tracker.register_external_task("ext-001")

    assert await tracker.has_active_tasks() is True
    assert await tracker.list_active_tasks() == ["ext-001"]


@pytest.mark.asyncio
async def test_unregister_external_task_clears_tracker():
    """``unregister_external_task`` removes the task and makes the tracker
    report idle again.
    """
    tracker = TaskTracker()
    await tracker.register_external_task("ext-001")
    assert await tracker.has_active_tasks() is True

    await tracker.unregister_external_task("ext-001")

    assert await tracker.has_active_tasks() is False
    assert await tracker.list_active_tasks() == []


@pytest.mark.asyncio
async def test_unregister_external_task_is_idempotent():
    """Calling ``unregister_external_task`` twice does not raise."""
    tracker = TaskTracker()
    await tracker.register_external_task("ext-001")
    await tracker.unregister_external_task("ext-001")
    # Second call should be a no-op
    await tracker.unregister_external_task("ext-001")
    assert await tracker.has_active_tasks() is False


@pytest.mark.asyncio
async def test_unregister_external_task_notifies_subscribers():
    """``unregister_external_task`` sends the sentinel to any attached
    subscriber queues so consumers do not hang.
    """
    tracker = TaskTracker()
    await tracker.register_external_task("ext-001")

    # Attach a subscriber queue directly (mirrors what attach() does).
    # Use the internal API to seed a queue for the external task.
    async with tracker.lock:
        state = tracker._runs["ext-001"]  # pylint: disable=protected-access
        subscriber: asyncio.Queue = asyncio.Queue()
        state.queues.append(subscriber)

    await tracker.unregister_external_task("ext-001")

    # The subscriber should have received the sentinel (None) and not
    # hang if a consumer tries to drain it.
    item = await asyncio.wait_for(subscriber.get(), timeout=1.0)
    assert (
        item is None
    ), "Subscriber should receive the sentinel after unregister"


@pytest.mark.asyncio
async def test_register_external_task_duplicate_is_safe():
    """Registering the same run_key twice is idempotent."""
    tracker = TaskTracker()
    await tracker.register_external_task("ext-001")
    await tracker.register_external_task("ext-001")

    assert await tracker.list_active_tasks() == ["ext-001"]

    await tracker.unregister_external_task("ext-001")
    assert await tracker.has_active_tasks() is False


@pytest.mark.asyncio
async def test_multiple_external_tasks():
    """Multiple external tasks can coexist."""
    tracker = TaskTracker()
    await tracker.register_external_task("ext-001")
    await tracker.register_external_task("ext-002")

    active = await tracker.list_active_tasks()
    assert sorted(active) == ["ext-001", "ext-002"]

    await tracker.unregister_external_task("ext-001")
    assert await tracker.list_active_tasks() == ["ext-002"]

    await tracker.unregister_external_task("ext-002")
    assert await tracker.has_active_tasks() is False


@pytest.mark.asyncio
async def test_wait_all_done_waits_for_external_tasks():
    """``wait_all_done`` blocks until external tasks are unregistered."""
    tracker = TaskTracker()
    await tracker.register_external_task("ext-001")

    async def unregister_after_delay():
        await asyncio.sleep(0.2)
        await tracker.unregister_external_task("ext-001")

    asyncio.create_task(unregister_after_delay())

    completed = await tracker.wait_all_done(timeout=5.0)
    assert completed is True
    assert await tracker.has_active_tasks() is False


@pytest.mark.asyncio
async def test_external_tasks_coexist_with_internal_tasks():
    """External tasks and internal streaming tasks are both visible."""
    tracker = TaskTracker()

    # Register an external task
    await tracker.register_external_task("ext-001")

    # Start an internal streaming task
    async def stream(payload):
        del payload
        await asyncio.sleep(0.2)
        yield "data: done\n\n"

    _queue, is_new = await tracker.attach_or_start("chat-1", {}, stream)
    assert is_new is True

    active = await tracker.list_active_tasks()
    assert sorted(active) == ["chat-1", "ext-001"]

    # Unregister external — internal still active
    await tracker.unregister_external_task("ext-001")
    assert await tracker.has_active_tasks() is True
    assert await tracker.list_active_tasks() == ["chat-1"]

    # Wait for internal to finish
    completed = await tracker.wait_all_done(timeout=5.0)
    assert completed is True
    assert await tracker.has_active_tasks() is False


@pytest.mark.asyncio
async def test_cancelled_internal_run_cannot_pop_new_run():
    """A stale producer must not remove or write into a newer run."""
    tracker = TaskTracker()
    release_old = asyncio.Event()

    async def old_stream(_payload):
        await release_old.wait()
        yield "data: stale\n\n"

    old_queue, is_new = await tracker.attach_or_start(
        "chat-1",
        {},
        old_stream,
    )
    assert is_new is True

    assert await tracker.request_stop("chat-1") is True
    assert await old_queue.get() is None

    async def new_stream(_payload):
        yield "data: fresh\n\n"

    new_queue, is_new = await tracker.attach_or_start(
        "chat-1",
        {},
        new_stream,
    )
    assert is_new is True

    release_old.set()
    assert await asyncio.wait_for(new_queue.get(), timeout=1.0) == (
        "data: fresh\n\n"
    )
    assert await asyncio.wait_for(new_queue.get(), timeout=1.0) is None


@pytest.mark.asyncio
async def test_task_tracker_snapshot_replays_active_buffer():
    tracker = TaskTracker()
    release = asyncio.Event()

    async def stream(_payload):
        yield "data: first\n\n"
        await release.wait()
        yield "data: second\n\n"

    _queue, is_new = await tracker.attach_or_start("chat-1", {}, stream)
    assert is_new is True

    await asyncio.sleep(0)
    snapshot = await tracker.get_snapshot("chat-1")
    assert snapshot is not None
    assert snapshot["status"] == "running"
    assert snapshot["event_count"] == 1
    assert snapshot["events"] == ["data: first\n\n"]

    release.set()
    assert await tracker.wait_all_done(timeout=2.0) is True


@pytest.mark.asyncio
async def test_stream_from_queue_emits_keepalive(monkeypatch):
    """Idle streams should keep the SSE connection alive."""
    monkeypatch.setattr(
        _task_tracker_mod,
        "_STREAM_KEEPALIVE_SECONDS",
        0.01,
    )
    tracker = TaskTracker()
    queue: asyncio.Queue = asyncio.Queue()

    stream = tracker.stream_from_queue(queue, "chat-keepalive")

    keepalive = await asyncio.wait_for(stream.__anext__(), timeout=0.2)
    assert keepalive.startswith(": heartbeat\ndata: ")
    assert '"type": "runtime.progress"' in keepalive
    assert '"structured_keepalive": true' in keepalive

    await queue.put(None)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=0.2)


@pytest.mark.asyncio
async def test_task_tracker_stream_error_is_visible_failed_response():
    """Producer crashes should still produce a chat-renderable error frame."""

    tracker = TaskTracker()

    async def stream(_payload):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    queue, is_new = await tracker.attach_or_start("chat-error", {}, stream)
    assert is_new is True

    sse = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert isinstance(sse, str)
    payload = json.loads(sse.removeprefix("data: ").strip())
    assert payload["object"] == "response"
    assert payload["status"] == "failed"
    assert payload["type"] == "response.failed"
    assert payload["error"]["message"] == "internal server error"

    assert await asyncio.wait_for(queue.get(), timeout=1.0) is None


@pytest.mark.asyncio
async def test_registered_external_task_delays_graceful_stop():
    """When a background task IS registered with the TaskTracker via
    ``register_external_task``, ``_graceful_stop_old_instance`` schedules
    delayed cleanup instead of stopping immediately.

    This is the core fix for issue #3275.
    """
    cleanup_tasks: set[asyncio.Task] = set()

    with tempfile.TemporaryDirectory() as tmpdir:
        old_ws = StubWorkspace("default", str(Path(tmpdir) / "old"))

        # Simulate what the fixed DynamicMultiAgentRunner.stream_query()
        # now does: register the background task with the TaskTracker.
        await old_ws.task_tracker.register_external_task("ext-bg-001")
        assert await old_ws.task_tracker.has_active_tasks() is True

        # Graceful stop should now detect the active task.
        stopped_immediately = await graceful_stop_old_instance(
            old_ws,
            "default",
            cleanup_tasks,
        )

        # NOT stopped immediately — delayed cleanup was scheduled.
        assert stopped_immediately is False, (
            "With external task registered, graceful stop should delay "
            "rather than stop immediately."
        )
        assert (
            old_ws.stopped is False
        ), "Workspace should still be running while external task is active"
        assert (
            len(cleanup_tasks) == 1
        ), "Expected delayed cleanup task to be scheduled"

        # Capture the cleanup task so we can await it deterministically
        # instead of relying on a fixed sleep.
        (cleanup_task,) = cleanup_tasks

        # Simulate the background task completing (unregister it).
        await old_ws.task_tracker.unregister_external_task("ext-bg-001")

        # Deterministically wait for delayed cleanup to finish.
        await cleanup_task

        # Now the workspace should have been stopped by the cleanup task.
        assert (
            old_ws.stopped is True
        ), "Workspace should be stopped after external task completes"


@pytest.mark.asyncio
async def test_internal_tracked_task_delays_shutdown():
    """Verify that tasks tracked by Lantai's ``TaskTracker`` (internal
    streaming) DO delay the shutdown — proving the mechanism was always
    correct for internal tasks.
    """
    cleanup_tasks: set[asyncio.Task] = set()

    with tempfile.TemporaryDirectory() as tmpdir:
        old_ws = StubWorkspace("default", str(Path(tmpdir) / "old"))

        completion_event = asyncio.Event()

        async def slow_stream(payload):
            del payload
            await asyncio.sleep(0.3)
            completion_event.set()
            yield "data: done\n\n"

        _queue, is_new = await old_ws.task_tracker.attach_or_start(
            "chat-123",
            {},
            slow_stream,
        )
        assert is_new is True
        assert await old_ws.task_tracker.has_active_tasks() is True

        stopped_immediately = await graceful_stop_old_instance(
            old_ws,
            "default",
            cleanup_tasks,
        )

        assert stopped_immediately is False
        assert len(cleanup_tasks) == 1

        # Capture and await the cleanup task directly — no fixed sleep.
        (cleanup_task,) = cleanup_tasks
        await cleanup_task

        assert completion_event.is_set()
        assert old_ws.stopped is True
