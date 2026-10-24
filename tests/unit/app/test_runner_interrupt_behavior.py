# -*- coding: utf-8 -*-
"""Behavior locks for chat turn interruption surfaces."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app.experience import build_runtime_events_response
from lantai.app.runner.api import interrupt_chat_turn
from lantai.app.runner.task_tracker import SessionRuntimeRegistry, TaskTracker
from lantai.app.concurrency import RuntimeConcurrencyGovernor
from lantai.presentation.jobs import PresentationJobStore
from lantai.presentation.models import JobStatus, PresentationJobRequest


class _FakeChatManager:
    def __init__(self, chat) -> None:
        self.chat = chat
        self.requested: list[str] = []

    async def get_chat(self, chat_id: str):
        self.requested.append(chat_id)
        return self.chat


class _FakeTaskTracker:
    def __init__(
        self,
        stopped: bool,
        *,
        snapshot: dict | None = None,
    ) -> None:
        self.stopped = stopped
        self.snapshot = snapshot
        self.snapshot_requests: list[str] = []
        self.requested: list[tuple[str, int | None]] = []

    async def get_snapshot(self, run_key: str):
        self.snapshot_requests.append(run_key)
        return self.snapshot

    async def request_stop_if_generation(self, run_key: str, generation: int) -> bool:
        self.requested.append((run_key, generation))
        return self.stopped

    async def request_stop(self, run_key: str) -> bool:
        self.requested.append((run_key, None))
        return self.stopped


@pytest.mark.asyncio
async def test_interrupt_chat_turn_requests_stop_and_records_runtime_event(
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    chat = SimpleNamespace(
        id="chat-stop",
        session_id="session-stop",
        meta={"topic_workspace_dir": str(workspace_dir)},
    )
    tracker = _FakeTaskTracker(
        stopped=True,
        snapshot={"status": "running", "generation": 3},
    )
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=tracker,
        workspace_dir=tmp_path,
    )
    manager = _FakeChatManager(chat)

    response = await interrupt_chat_turn(
        "chat-stop",
        "turn-from-ui",
        mgr=manager,
        workspace=workspace,
    )

    assert response == {
        "stopped": True,
        "chat_id": "chat-stop",
        "turn_id": "turn-from-ui",
    }
    assert manager.requested == ["chat-stop"]
    assert tracker.snapshot_requests == ["chat-stop"]
    assert tracker.requested == [("chat-stop", 3)]

    events = build_runtime_events_response(workspace_dir, "chat-stop").events
    assert len(events) == 1
    assert events[0].type == "runtime.warning"
    assert events[0].payload["requested_chat_id"] == "turn-from-ui"
    assert events[0].payload["resolved_chat_id"] == "chat-stop"


@pytest.mark.asyncio
async def test_interrupt_chat_turn_cancels_its_active_presentation_job(
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    workspace_dir.mkdir()
    chat = SimpleNamespace(
        id="chat-ppt-stop",
        session_id="session-ppt-stop",
        meta={"topic_workspace_dir": str(workspace_dir)},
    )
    store = PresentationJobStore(workspace_dir / "presentation-jobs")
    record = store.create(
        PresentationJobRequest(
            conversation_id="conversation-ppt-stop",
            user_request="生成 PPT",
            slide_count=1,
        ),
        runtime_run_id="run-ppt-stop",
    )
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=_FakeTaskTracker(
            stopped=True,
            snapshot={
                "status": "running",
                "generation": 3,
                "attempt_id": "run-ppt-stop",
            },
        ),
        workspace_dir=tmp_path,
    )

    result = await interrupt_chat_turn(
        chat.id,
        "run-ppt-stop",
        mgr=_FakeChatManager(chat),
        workspace=workspace,
    )

    assert result["stopped"] is True
    assert store.get(record.job_id).status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_interrupt_chat_turn_without_running_task_does_not_need_workspace():
    chat = SimpleNamespace(id="chat-idle", session_id="session-idle", meta={})
    tracker = _FakeTaskTracker(stopped=False, snapshot=None)
    workspace = SimpleNamespace(agent_id="default", task_tracker=tracker)

    response = await interrupt_chat_turn(
        "chat-idle",
        "turn-idle",
        mgr=_FakeChatManager(chat),
        workspace=workspace,
    )

    assert response == {
        "stopped": False,
        "chat_id": "chat-idle",
        "turn_id": "turn-idle",
    }
    assert tracker.snapshot_requests == ["chat-idle"]
    assert tracker.requested == []


@pytest.mark.asyncio
async def test_interrupt_chat_turn_rejects_stale_turn_without_stopping_current_run(
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    chat = SimpleNamespace(
        id="chat-stale",
        session_id="session-stale",
        meta={
            "topic_workspace_dir": str(workspace_dir),
            "pending_turn": {
                "run_id": "current-run",
                "turn_id": "current-turn",
            },
        },
    )
    tracker = _FakeTaskTracker(
        stopped=True,
        snapshot={"status": "running", "generation": 5},
    )
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=tracker,
        workspace_dir=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await interrupt_chat_turn(
            "chat-stale",
            "old-turn",
            mgr=_FakeChatManager(chat),
            workspace=workspace,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "stale_attempt"
    assert tracker.snapshot_requests == ["chat-stale"]
    assert tracker.requested == []
    assert not workspace_dir.exists()


@pytest.mark.asyncio
async def test_task_tracker_generation_fence_rejects_stale_stop() -> None:
    tracker = TaskTracker()
    await tracker.register_external_task("chat-race")
    old_snapshot = await tracker.get_snapshot("chat-race")
    assert old_snapshot is not None
    await tracker.unregister_external_task("chat-race")
    await tracker.register_external_task("chat-race")

    stopped = await tracker.request_stop_if_generation(
        "chat-race",
        old_snapshot["generation"],
    )

    assert stopped is False
    current_snapshot = await tracker.get_snapshot("chat-race")
    assert current_snapshot is not None
    assert current_snapshot["generation"] != old_snapshot["generation"]
    await tracker.unregister_external_task("chat-race")


@pytest.mark.asyncio
async def test_session_runtime_registry_keeps_distinct_chats_parallel() -> None:
    registry = SessionRuntimeRegistry()
    await registry.register_external_task("chat-a", attempt_id="turn-a")
    await registry.register_external_task("chat-b", attempt_id="turn-b")

    assert set(await registry.list_active_tasks()) == {"chat-a", "chat-b"}
    assert await registry.request_stop_if_attempt(
        "chat-a",
        attempt_id="turn-a",
    )
    assert await registry.get_status("chat-a") == "stopping"
    assert await registry.get_status("chat-b") == "running"

    await registry.unregister_external_task("chat-a")
    assert await registry.get_status("chat-a") == "idle"
    await registry.unregister_external_task("chat-b")


@pytest.mark.asyncio
async def test_task_tracker_does_not_serialize_distinct_child_runs_by_root_session(
    monkeypatch,
) -> None:
    """Approval routing identity must not become a runtime capacity lock."""

    governor = RuntimeConcurrencyGovernor(
        max_active=2,
        max_waiters=2,
        acquire_timeout_seconds=0.2,
        category_limits={"agent": 2},
    )
    monkeypatch.setattr(
        "lantai.app.runner.task_tracker.get_runtime_concurrency_governor",
        lambda: governor,
    )
    tracker = TaskTracker()
    entered = {"child-a": asyncio.Event(), "child-b": asyncio.Event()}
    release = asyncio.Event()

    async def stream(payload):
        entered[payload].set()
        await release.wait()
        yield f"data: {payload}"

    queues = []
    for run_key in entered:
        queue, is_new = await tracker.attach_or_start(
            run_key,
            run_key,
            stream,
            root_session_id="shared-host-session",
        )
        assert is_new
        queues.append((queue, run_key))

    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in entered.values())),
        timeout=0.1,
    )
    assert governor.snapshot()["active"] == 2

    release.set()
    for queue, run_key in queues:
        async for _ in tracker.stream_from_queue(queue, run_key):
            pass
    assert governor.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_task_tracker_terminal_response_is_immediately_not_stoppable() -> None:
    """Cleanup latency must not keep a completed attempt visibly active."""

    tracker = TaskTracker()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    async def stream(_payload):
        yield (
            'data: {"object":"response","type":"response.completed",'
            '"status":"completed","output":[]}\n\n'
        )

    queue, is_new = await tracker.attach_or_start(
        "chat-terminal",
        {},
        stream,
        attempt_id="attempt-terminal",
        cleanup=cleanup,
    )
    assert is_new is True

    terminal = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert "response.completed" in terminal
    await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
    assert await tracker.get_status("chat-terminal") == "idle"
    assert await tracker.get_snapshot("chat-terminal") is None
    assert await tracker.has_active_tasks() is False
    assert await tracker.list_active_tasks() == []
    assert await tracker.attach("chat-terminal") is None
    assert await tracker.request_stop("chat-terminal") is False

    release_cleanup.set()
    await asyncio.wait_for(tracker.wait_all_done(timeout=1.0), timeout=1.1)


@pytest.mark.asyncio
async def test_session_runtime_registry_attempt_fence_rejects_stale_stop() -> None:
    registry = SessionRuntimeRegistry()
    await registry.register_external_task(
        "chat-attempt",
        attempt_id="attempt-1",
    )
    first = await registry.get_snapshot("chat-attempt")
    assert first is not None
    await registry.unregister_external_task("chat-attempt")
    await registry.register_external_task(
        "chat-attempt",
        attempt_id="attempt-2",
    )

    stopped = await registry.request_stop_if_attempt(
        "chat-attempt",
        attempt_id="attempt-1",
        generation=first["generation"],
    )

    assert stopped is False
    current = await registry.get_snapshot("chat-attempt")
    assert current is not None
    assert current["attempt_id"] == "attempt-2"
    await registry.unregister_external_task("chat-attempt")


@pytest.mark.asyncio
async def test_session_runtime_registry_runs_cleanup_once_for_owner_attempt() -> None:
    registry = SessionRuntimeRegistry()
    cleanup_calls: list[str] = []

    async def cleanup() -> None:
        cleanup_calls.append("cleanup")

    await registry.register_external_task(
        "chat-cleanup",
        attempt_id="attempt-cleanup",
        root_session_id="root-session",
        cleanup=cleanup,
    )
    snapshot = await registry.get_snapshot("chat-cleanup")
    assert snapshot is not None
    assert snapshot["root_session_id"] == "root-session"

    assert await registry.request_stop_if_attempt(
        "chat-cleanup",
        attempt_id="attempt-cleanup",
        generation=snapshot["generation"],
    )
    await registry.unregister_external_task("chat-cleanup")

    assert cleanup_calls == ["cleanup"]


@pytest.mark.asyncio
async def test_internal_stop_waits_for_stream_cancellation_lifecycle() -> None:
    """A successful stop must not outrun the stream's durable settlement."""

    registry = SessionRuntimeRegistry()
    stream_started = asyncio.Event()
    stream_settled = asyncio.Event()

    async def stream(_payload):
        try:
            stream_started.set()
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            stream_settled.set()

    queue, is_new = await registry.attach_or_start(
        "chat-stop-settlement",
        {},
        stream,
    )
    assert is_new is True
    await asyncio.wait_for(stream_started.wait(), timeout=1.0)

    assert await registry.request_stop("chat-stop-settlement") is True
    assert stream_settled.is_set()
    assert await asyncio.wait_for(queue.get(), timeout=1.0) is None
    assert await registry.get_snapshot("chat-stop-settlement") is None
