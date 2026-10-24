# -*- coding: utf-8 -*-
"""Cron activity keeps its run identity, independently of foreground chats."""
import asyncio
from types import SimpleNamespace

import pytest

from lantai.app.runner.api import list_chat_activity
from lantai.app.runner.task_tracker import SessionRuntimeRegistry


@pytest.mark.asyncio
async def test_cron_activity_survives_missing_chat_and_cleanup_without_aliasing():
    registry = SessionRuntimeRegistry()
    queried = []

    class Chats:
        async def get_chat(self, key):
            queried.append(key)
            if key == "chat-id":
                return SimpleNamespace(id=key, name="Foreground")
            return None

    cleaning, release = asyncio.Event(), asyncio.Event()

    async def cleanup():
        cleaning.set()
        await release.wait()

    async def activity():
        rows = await list_chat_activity(
            Chats(), SimpleNamespace(task_tracker=registry)
        )
        return [row.model_dump(exclude_none=True) for row in rows]

    settling = None
    try:
        await registry.register_external_task("chat-id")
        await registry.register_external_task("deleted-chat")
        await registry.register_external_task(
            "opaque-invocation",
            root_session_id="session-not-chat-id",
            activity_kind="cron",
            activity_name="Scheduled report",
            cleanup=cleanup,
        )
        expected = {
            "id": "opaque-invocation",
            "name": "Scheduled report",
            "status": "running",
            "kind": "cron",
        }
        assert await activity() == [
            {"id": "chat-id", "name": "Foreground", "status": "running"},
            expected,
        ]
        assert "opaque-invocation" not in queried
        assert "session-not-chat-id" not in queried
        await registry.unregister_external_task("chat-id")
        assert await activity() == [expected]
        settling = asyncio.create_task(
            registry.unregister_external_task("opaque-invocation")
        )
        await asyncio.wait_for(cleaning.wait(), 1)
        assert await activity() == [expected]
        release.set()
        await settling
        assert await activity() == []
    finally:
        release.set()
        if settling:
            await settling
        for key in await registry.list_active_tasks():
            await registry.unregister_external_task(key)


@pytest.mark.asyncio
async def test_activity_http_preserves_chat_shape_and_projects_opaque_cron():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from lantai.app.runner.api import get_chat_manager, get_workspace, router

    registry = SessionRuntimeRegistry()

    class Chats:
        async def get_chat(self, key):
            assert key != "scheduled"
            return SimpleNamespace(id=key, name="Chat")

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_chat_manager] = lambda: Chats()
    app.dependency_overrides[get_workspace] = lambda: SimpleNamespace(
        task_tracker=registry
    )
    await registry.register_external_task("foreground")
    await registry.register_external_task("scheduled", activity_kind="cron")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/chats/activity")
        assert response.status_code == 200
        assert response.json() == [
            {"id": "foreground", "name": "Chat", "status": "running"},
            {
                "id": "scheduled",
                "name": "定时任务",
                "status": "running",
                "kind": "cron",
            },
        ]
    finally:
        for key in await registry.list_active_tasks():
            await registry.unregister_external_task(key)
