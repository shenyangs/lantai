# -*- coding: utf-8 -*-
"""Console debug turn event route tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lantai.app.experience.events import append_chat_event
from lantai.app.runner.manager import ChatManager
from lantai.app.runner.models import ChatSpec
from lantai.app.runner.repo.json_repo import JsonChatRepository


@pytest.mark.asyncio
async def test_debug_turn_events_aggregates_recent_events(monkeypatch, tmp_path):
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
    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-1",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "hello"},
    )

    workspace = SimpleNamespace(
        agent_id="default",
        chat_manager=manager,
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )

    result = await console_router.get_debug_turn_events(
        SimpleNamespace(),
        limit=20,
    )

    assert result["exists"] is True
    assert result["event_count"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["label"] == "开始运行"
    assert "events.jsonl" in result["paths"][0]
