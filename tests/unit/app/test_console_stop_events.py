# -*- coding: utf-8 -*-
"""Console stop route experience event tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lantai.app.experience.events import (
    build_runtime_events_response,
    build_turn_event_preview,
)
from lantai.app.runner.manager import ChatManager
from lantai.app.runner.models import ChatSpec
from lantai.app.runner.repo.json_repo import JsonChatRepository
from lantai.app.runner.task_tracker import SessionRuntimeRegistry
from lantai.presentation.jobs import PresentationJobStore
from lantai.presentation.models import JobStatus, PresentationJobRequest


class FakeTaskTracker:
    def __init__(self, results: list[bool]):
        self._results = list(results)
        self.calls: list[str] = []

    async def get_snapshot(self, run_key: str):
        if run_key.startswith("console:"):
            return None
        return {
            "status": "running",
            "generation": 4,
            "attempt_id": "turn-current",
        }

    async def request_stop_if_generation(
        self,
        run_key: str,
        _generation: int,
    ) -> bool:
        return await self.request_stop(run_key)

    async def request_stop_if_attempt(
        self,
        run_key: str,
        *,
        attempt_id: str,
        generation: int | None = None,
    ) -> bool:
        assert attempt_id == "turn-current"
        assert generation == 4
        return await self.request_stop(run_key)

    async def request_stop(self, run_key: str) -> bool:
        self.calls.append(run_key)
        if not self._results:
            return False
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_console_stop_writes_stop_requested_event(monkeypatch, tmp_path):
    from lantai.app.routers import console as console_router

    topic_workspace = tmp_path / "topic-workspaces" / "chat-1"
    topic_workspace.mkdir(parents=True, exist_ok=True)
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            name="Chat 1",
            session_id="console:test",
            user_id="test",
            channel="console",
            meta={"topic_workspace_dir": str(topic_workspace)},
        ),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=FakeTaskTracker([True]),
        runner=SimpleNamespace(_chat_manager=manager),
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )

    result = await console_router.post_console_chat_stop(
        SimpleNamespace(),
        chat_id=chat.id,
    )

    assert result == {"stopped": True}
    preview = build_turn_event_preview(topic_workspace, chat.id)
    assert preview is not None
    assert preview.summary_line == "本轮已请求停止"
    assert preview.events[-1].detail == "已请求停止当前运行"


@pytest.mark.asyncio
async def test_console_stop_cancels_only_the_active_runs_presentation_job(
    monkeypatch,
    tmp_path,
) -> None:
    from lantai.app.routers import console as console_router

    topic_workspace = tmp_path / "topic-workspaces" / "chat-ppt-stop"
    topic_workspace.mkdir(parents=True)
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-ppt-stop",
            name="PPT Stop",
            session_id="console:ppt-stop",
            user_id="test",
            channel="console",
            meta={"topic_workspace_dir": str(topic_workspace)},
        ),
    )
    store = PresentationJobStore(topic_workspace / "presentation-jobs")
    active = store.create(
        PresentationJobRequest(
            conversation_id="conversation-ppt-stop",
            user_request="生成 PPT",
            slide_count=1,
        ),
        runtime_run_id="turn-current",
    )
    other = store.create(
        PresentationJobRequest(
            conversation_id="conversation-ppt-stop",
            user_request="另一轮 PPT",
            slide_count=1,
        ),
        runtime_run_id="turn-other",
    )
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=FakeTaskTracker([True]),
        runner=SimpleNamespace(_chat_manager=manager),
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )

    result = await console_router.post_console_chat_stop(
        SimpleNamespace(),
        chat_id=chat.id,
        turn_id="turn-current",
    )

    assert result == {"stopped": True}
    assert store.get(active.job_id).status == JobStatus.CANCELLED
    assert store.get(other.job_id).status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_console_stop_resolved_session_id_writes_event(
    monkeypatch,
    tmp_path,
):
    from lantai.app.routers import console as console_router

    topic_workspace = tmp_path / "topic-workspaces" / "chat-2"
    topic_workspace.mkdir(parents=True, exist_ok=True)
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-2",
            name="Chat 2",
            session_id="console:session-2",
            user_id="test",
            channel="console",
            meta={"topic_workspace_dir": str(topic_workspace)},
        ),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=FakeTaskTracker([False, True]),
        runner=SimpleNamespace(_chat_manager=manager),
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )

    result = await console_router.post_console_chat_stop(
        SimpleNamespace(),
        chat_id=chat.session_id,
    )

    assert result == {"stopped": True}
    preview = build_turn_event_preview(topic_workspace, chat.id)
    assert preview is not None
    assert preview.events[-1].detail == "停止请求已转到当前会话"


@pytest.mark.asyncio
async def test_console_stop_without_running_task_does_not_write_event(
    monkeypatch,
    tmp_path,
):
    from lantai.app.routers import console as console_router

    topic_workspace = tmp_path / "topic-workspaces" / "chat-3"
    topic_workspace.mkdir(parents=True, exist_ok=True)
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-3",
            name="Chat 3",
            session_id="console:session-3",
            user_id="test",
            channel="console",
            meta={"topic_workspace_dir": str(topic_workspace)},
        ),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=FakeTaskTracker([False]),
        runner=SimpleNamespace(_chat_manager=manager),
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )

    result = await console_router.post_console_chat_stop(
        SimpleNamespace(),
        chat_id=chat.id,
    )

    assert result == {"stopped": False}
    assert build_turn_event_preview(topic_workspace, chat.id) is None


@pytest.mark.asyncio
async def test_console_stop_settles_orphaned_pending_turn(
    monkeypatch,
    tmp_path,
):
    from lantai.app.routers import console as console_router

    topic_workspace = tmp_path / "topic-workspaces" / "chat-orphaned-stop"
    topic_workspace.mkdir(parents=True, exist_ok=True)
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-orphaned-stop",
            name="Orphaned stop",
            session_id="console:orphaned-stop",
            user_id="test",
            channel="console",
            meta={
                "topic_workspace_dir": str(topic_workspace),
                "pending_turn": {
                    "text": "继续修第 11 页",
                    "run_id": "run-orphaned-stop",
                    "status": "running",
                },
            },
        ),
    )

    class InactiveTracker:
        async def get_snapshot(self, _run_key):
            return None

        async def get_status(self, _run_key):
            return "idle"

        async def request_stop(self, _run_key):
            return False

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        task_tracker=InactiveTracker(),
        runner=SimpleNamespace(_chat_manager=manager),
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )

    result = await console_router.post_console_chat_stop(
        SimpleNamespace(),
        chat_id=chat.id,
    )

    assert result == {"stopped": True}
    saved = await manager.get_chat(chat.id)
    assert saved is not None
    assert "pending_turn" not in saved.meta
    assert saved.meta["last_turn_status"] == "interrupted"
    preview = build_turn_event_preview(topic_workspace, chat.id)
    assert preview is not None
    assert preview.events[-1].kind == "chat.turn.settled"
    assert preview.events[-1].status == "interrupted"


@pytest.mark.asyncio
async def test_console_stop_rejects_stale_attempt_before_cancelling(
    monkeypatch,
):
    from fastapi import HTTPException

    from lantai.app.routers import console as console_router

    tracker = FakeTaskTracker([True])
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=tracker,
        runner=SimpleNamespace(_chat_manager=None),
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )

    with pytest.raises(HTTPException) as exc_info:
        await console_router.post_console_chat_stop(
            SimpleNamespace(),
            chat_id="chat-stale-stop",
            turn_id="turn-old",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "stale_attempt"
    assert tracker.calls == []


@pytest.mark.asyncio
async def test_console_stop_persists_interrupted_terminal_before_a_new_turn(
    monkeypatch,
    tmp_path,
):
    """Stopping a live console stream must leave a durable terminal event."""

    from lantai.app.routers import console as console_router

    topic_workspace = tmp_path / "topic-workspaces" / "chat-stop-lifecycle"
    topic_workspace.mkdir(parents=True, exist_ok=True)
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    tracker = SessionRuntimeRegistry()
    stream_started = asyncio.Event()
    stream_never_finishes = asyncio.Event()
    seen_chat_ids: list[str] = []

    class FakeConsoleChannel:
        media_dir = tmp_path / "media"

        def resolve_session_id(self, *, sender_id, channel_meta):
            del sender_id
            return str(channel_meta["session_id"])

        async def stream_one(self, _payload):
            stream_started.set()
            yield 'data: {"object":"response","status":"in_progress","output":[]}\n\n'
            await stream_never_finishes.wait()

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        task_tracker=tracker,
        chat_manager=manager,
        runner=SimpleNamespace(_chat_manager=manager),
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    async def fake_get_console_channel(_workspace):
        return FakeConsoleChannel()

    async def fake_ensure_topic_workspace(
        _workspace, _manager, chat, **_kwargs
    ):
        seen_chat_ids.append(chat.id)
        return SimpleNamespace(
            chat=chat,
            topic_workspace_dir=str(topic_workspace),
            effective_workspace_dir=str(topic_workspace),
            manual_workspace_dir=None,
        )

    class FakeRequest:
        async def json(self):
            return {
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "stop me"}],
                    },
                ],
                "session_id": "console:stop-lifecycle",
                "user_id": "default",
                "channel": "console",
                "stream": True,
            }

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )
    monkeypatch.setattr(
        console_router, "_get_console_channel", fake_get_console_channel
    )
    monkeypatch.setattr(
        console_router,
        "ensure_chat_topic_workspace",
        fake_ensure_topic_workspace,
    )

    response = await console_router.post_console_chat(
        await FakeRequest().json(),
        FakeRequest(),
    )
    stream = response.body_iterator
    await asyncio.wait_for(stream.__anext__(), timeout=1)
    await asyncio.wait_for(stream_started.wait(), timeout=1)
    assert seen_chat_ids
    chat_id = seen_chat_ids[0]

    result = await console_router.post_console_chat_stop(
        SimpleNamespace(),
        chat_id=chat_id,
    )
    assert result == {"stopped": True}

    events = build_runtime_events_response(topic_workspace, chat_id).events
    assert any(
        event.type == "turn.interrupted" or event.status == "interrupted"
        for event in events
    ), "console stop returned before persisting its interrupted terminal event"

    for _ in range(2):
        try:
            await asyncio.wait_for(stream.__anext__(), timeout=1)
        except StopAsyncIteration:
            break
    else:
        pytest.fail("console stream did not close after stop")
