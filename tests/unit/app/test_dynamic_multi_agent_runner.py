# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lantai.app._app import DynamicMultiAgentRunner
from lantai.app.agent_context import set_current_agent_id
from lantai.app.channels.runtime_schema import (
    AgentRequest,
    ContentType,
    Message,
    Role,
    TextContent,
)


class _FakeTaskTracker:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self.owners: list[asyncio.Task | None] = []

    async def register_external_task(self, run_key: str, *, owner_task=None) -> None:
        self.registered.append(run_key)
        self.owners.append(owner_task)

    async def unregister_external_task(self, run_key: str) -> None:
        self.unregistered.append(run_key)


@pytest.mark.asyncio
async def test_dynamic_runner_query_handler_adapts_request_to_runner_contract():
    """The app wrapper must pass msgs and request separately to AgentRunner."""

    seen: dict[str, object] = {}

    class FakeRunner:
        async def query_handler(self, msgs, *args, request=None, **kwargs):
            seen["msgs"] = msgs
            seen["request"] = request
            seen["args"] = args
            seen["kwargs"] = kwargs
            yield "ok", True

    class FakeManager:
        async def get_agent(self, agent_id: str):
            seen["agent_id"] = agent_id
            return workspace

    message = Message(
        role=Role.USER,
        content=[
            TextContent(type=ContentType.TEXT, text="hello"),
        ],
    )
    request = AgentRequest(
        input=[message],
        session_id="dynamic:query",
        user_id="user",
        channel="console",
    )
    tracker = _FakeTaskTracker()
    workspace = SimpleNamespace(
        runner=FakeRunner(),
        task_tracker=tracker,
        agent_id="default",
    )
    dynamic = DynamicMultiAgentRunner()
    dynamic.set_multi_agent_manager(FakeManager())
    set_current_agent_id("default")

    result = [item async for item in dynamic.query_handler(request)]

    assert result == [("ok", True)]
    assert seen["agent_id"] == "default"
    assert seen["msgs"] == [message]
    assert seen["request"] is request
    assert tracker.registered
    assert tracker.unregistered == tracker.registered
    assert tracker.owners == [asyncio.current_task()]
