# -*- coding: utf-8 -*-
"""Tests for agent discovery and inter-agent chat helpers."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from lantai.agents.tools import agent_management
from lantai.config.context import WorkspaceContext, current_workspace_context
from lantai.runtime.effect_context import (
    RuntimeEffectContext,
    bind_runtime_effect_context,
)
from lantai.runtime.adapters.legacy_toolkit_components import (
    LegacyToolkit as Toolkit,
)


class _FakeResponse:
    def __init__(self, json_data=None, lines=None, status_code=200):
        self._json_data = json_data or {}
        self._lines = lines or []
        self.status_code = status_code
        self.request = httpx.Request("GET", "http://test/api")

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "request failed",
                request=self.request,
                response=httpx.Response(
                    self.status_code,
                    request=self.request,
                ),
            )

    def iter_lines(self):
        yield from self._lines


class _FakeStreamContext:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeClient:
    def __init__(
        self,
        get_response=None,
        post_response=None,
        stream_response=None,
    ):
        self.get_response = get_response or _FakeResponse()
        self.post_response = post_response or _FakeResponse()
        self.stream_response = stream_response or _FakeResponse(lines=[])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, *_args, **_kwargs):
        return self.get_response

    def post(self, *_args, **_kwargs):
        return self.post_response

    def stream(self, *_args, **_kwargs):
        return _FakeStreamContext(self.stream_response)


@pytest.fixture(autouse=True)
def _default_runtime_effect_identity(tmp_path: Path):
    with bind_runtime_effect_context(
        RuntimeEffectContext(
            run_id="run-test-root",
            attempt_id="attempt-test-parent",
            effect_id="effect-test-parent",
            state_workspace_dir=str(tmp_path),
        )
    ):
        yield


@pytest.mark.asyncio
async def test_collaboration_rejects_unknown_mode_before_api_access():
    collaboration = agent_management.collaborate_with_agents(
        [],
        mode="unbounded",
    )

    with pytest.raises(ValueError, match="mode"):
        await anext(collaboration)


@pytest.mark.asyncio
async def test_execution_team_rejects_missing_parent_effect_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from lantai.runtime import effect_context

    home = tmp_path / "worker"
    home.mkdir()
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": "worker",
                    "name": "worker",
                    "enabled": True,
                    "workspace_dir": str(home),
                }
            ]
        },
    )
    monkeypatch.setattr(
        effect_context,
        "current_runtime_effect_context",
        lambda: None,
    )

    collaboration = agent_management.collaborate_with_agents(
        [
            {
                "to_agent": "worker",
                "text": "执行任务",
                "label": "执行",
                "allowed_tools": ["write_file"],
            }
        ],
        mode="execution",
    )

    with pytest.raises(ValueError, match="Runtime Effect identity"):
        await anext(collaboration)


def test_single_member_is_execution_only():
    agents = {
        "agent_a": {
            "id": "agent_a",
            "name": "甲",
            "enabled": True,
        },
    }
    task = {
        "to_agent": "agent_a",
        "text": "完成交付",
        "label": "执行",
        "allowed_tools": ["write_file"],
    }

    normalized = agent_management._validated_collaboration_tasks(
        [task],
        agents_by_id=agents,
        execution=True,
    )
    assert normalized[0]["to_agent"] == "agent_a"

    with pytest.raises(ValueError, match="2-4"):
        agent_management._validated_collaboration_tasks(
            [task],
            agents_by_id=agents,
            execution=False,
        )


def test_build_agent_chat_request_adds_identity_prefix():
    (
        session_id,
        payload,
        prefix_added,
    ) = agent_management.build_agent_chat_request(
        "bot_b",
        "Need a summary",
        from_agent="bot_a",
    )

    assert session_id.startswith("bot_a:to:bot_b:")
    assert prefix_added is True
    assert payload["session_id"] == session_id
    assert payload["user_id"] == "bot_a"
    assert payload["metadata"]["presentation_context"] == "subagent_internal"
    assert payload["input"][0]["content"][0]["text"].startswith(
        "[Agent bot_a requesting] ",
    )


def test_build_agent_chat_request_discovers_calling_agent(monkeypatch):
    monkeypatch.setattr(
        agent_management,
        "resolve_calling_agent_id",
        lambda _from_agent=None: "auto_bot",
    )

    (
        session_id,
        payload,
        prefix_added,
    ) = agent_management.build_agent_chat_request(
        "bot_b",
        "Need a summary",
        from_agent=None,
    )

    assert session_id.startswith("auto_bot:to:bot_b:")
    assert payload["user_id"] == "auto_bot"
    assert payload["input"][0]["content"][0]["text"].startswith(
        "[Agent auto_bot requesting] ",
    )
    assert prefix_added is True


def test_extract_agent_text_content_preserves_main_answer_before_ququ() -> (
    None
):
    response = {
        "output": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "这是完整主稿。"},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "~>[echo] 这是曲曲短句。"},
                ],
            },
        ],
    }

    text = agent_management.extract_agent_text_content(response)

    assert "这是完整主稿。" in text
    assert "~>[echo] 这是曲曲短句。" in text
    assert text.index("完整主稿") < text.index("曲曲短句")


def test_build_agent_chat_request_reuses_session_id_when_provided():
    (
        session_id,
        payload,
        prefix_added,
    ) = agent_management.build_agent_chat_request(
        "bot_b",
        "Need a summary",
        session_id="existing-session",
        from_agent="bot_a",
    )

    assert session_id == "existing-session"
    assert payload["session_id"] == "existing-session"
    assert prefix_added is True


def test_list_agents_data_uses_shared_client(monkeypatch):
    fake_client = _FakeClient(
        get_response=_FakeResponse(
            json_data={
                "agents": [
                    {"id": "default", "name": "Default", "enabled": True},
                ],
            },
        ),
    )
    monkeypatch.setattr(
        agent_management,
        "create_agent_api_client",
        lambda _base_url: fake_client,
    )

    result = agent_management.list_agents_data("http://127.0.0.1:8088")

    assert result["agents"][0]["id"] == "default"


def test_extract_agent_ids_normalizes_values():
    result = agent_management.extract_agent_ids(
        {
            "agents": [
                {"id": "bot_a"},
                {"id": "bot_b"},
                {"id": None},
                "invalid",
            ],
        },
    )

    assert result == {"bot_a", "bot_b"}


def test_background_running_status_reports_elapsed_and_deadline() -> None:
    text = agent_management.format_background_status_text(
        "task-1",
        {
            "status": "running",
            "started_at": 100.0,
            "timeout_seconds": 900,
        },
        now=147.0,
    )

    assert "[ELAPSED_SECONDS: 47]" in text
    assert "[TIMEOUT_SECONDS: 900]" in text
    assert "Do not treat a running task as timed out" in text


def test_resolve_agent_api_base_url_uses_last_api(monkeypatch):
    monkeypatch.setattr(
        agent_management,
        "read_last_api",
        lambda: ("192.168.1.8", 18088),
    )

    result = agent_management.resolve_agent_api_base_url()

    assert result == "http://192.168.1.8:18088"


def test_resolve_agent_api_base_url_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(agent_management, "read_last_api", lambda: None)

    result = agent_management.resolve_agent_api_base_url()

    assert result == agent_management.DEFAULT_AGENT_API_BASE_URL


def test_create_agent_api_client_authenticates_packaged_desktop_loopback(
    monkeypatch,
):
    monkeypatch.setenv("LANTAI_DESKTOP_TOKEN", "desktop-secret")
    monkeypatch.setattr(
        agent_management,
        "read_last_api",
        lambda: ("127.0.0.1", 8088),
    )

    with agent_management.create_agent_api_client(
        "http://127.0.0.1:8088",
    ) as client:
        assert client.headers["X-Lantai-Desktop-Token"] == "desktop-secret"


def test_create_agent_api_client_does_not_leak_desktop_token_remotely(
    monkeypatch,
):
    monkeypatch.setenv("LANTAI_DESKTOP_TOKEN", "desktop-secret")
    monkeypatch.setattr(
        agent_management,
        "read_last_api",
        lambda: ("127.0.0.1", 8088),
    )

    with agent_management.create_agent_api_client(
        "https://agents.example.com",
    ) as client:
        assert "X-Lantai-Desktop-Token" not in client.headers


@pytest.mark.asyncio
async def test_create_async_agent_api_client_authenticates_desktop_loopback(
    monkeypatch,
):
    monkeypatch.setenv("LANTAI_DESKTOP_TOKEN", "desktop-secret")
    monkeypatch.setattr(
        agent_management,
        "read_last_api",
        lambda: ("127.0.0.1", 8088),
    )

    async with agent_management.create_async_agent_api_client(
        "http://127.0.0.1:8088",
    ) as client:
        assert client.headers["X-Lantai-Desktop-Token"] == "desktop-secret"


@pytest.mark.asyncio
async def test_create_async_agent_api_client_does_not_leak_token_remotely(
    monkeypatch,
):
    monkeypatch.setenv("LANTAI_DESKTOP_TOKEN", "desktop-secret")
    monkeypatch.setattr(
        agent_management,
        "read_last_api",
        lambda: ("127.0.0.1", 8088),
    )

    async with agent_management.create_async_agent_api_client(
        "https://agents.example.com",
    ) as client:
        assert "X-Lantai-Desktop-Token" not in client.headers


def test_cancel_agent_chat_task_uses_internal_cancel_endpoint(monkeypatch):
    calls = []

    class _CancelClient(_FakeClient):
        def post(self, path, **kwargs):
            calls.append((path, kwargs))
            return _FakeResponse(
                json_data={"task_id": "task-1", "status": "cancelled"},
            )

    monkeypatch.setattr(
        agent_management,
        "create_agent_api_client",
        lambda _base_url: _CancelClient(),
    )

    result = agent_management.cancel_agent_chat_task(
        "http://127.0.0.1:8088",
        "task-1",
        to_agent="bot_b",
    )

    assert result["status"] == "cancelled"
    assert calls[0][0] == "/agent/process/task/task-1/cancel"


def test_collect_final_agent_chat_response_keeps_last_sse_payload(monkeypatch):
    fake_lines = [
        'data: {"output": [{"content": [{"type": "text", "text": "first"}]}]}',
        (
            'data: {"output": [{"content": '
            '[{"type": "text", "text": "second"}]}]}'
        ),
    ]
    fake_client = _FakeClient(stream_response=_FakeResponse(lines=fake_lines))
    monkeypatch.setattr(
        agent_management,
        "create_agent_api_client",
        lambda _base_url: fake_client,
    )

    result = agent_management.collect_final_agent_chat_response(
        "http://127.0.0.1:8088",
        {"session_id": "sid", "input": []},
        "bot_b",
        30,
    )

    assert result is not None
    assert agent_management.extract_agent_text_content(result) == "second"


def test_collect_final_agent_chat_response_raises_for_sse_error(monkeypatch):
    fake_client = _FakeClient(
        stream_response=_FakeResponse(
            lines=['data: {"type": "error", "error": "agent failed"}'],
        ),
    )
    monkeypatch.setattr(
        agent_management,
        "create_agent_api_client",
        lambda _base_url: fake_client,
    )

    with pytest.raises(RuntimeError, match="agent failed"):
        agent_management.collect_final_agent_chat_response(
            "http://127.0.0.1:8088",
            {"session_id": "sid", "input": []},
            "bot_b",
            30,
        )


def test_collect_final_agent_chat_response_keeps_native_completed_message(
    monkeypatch,
):
    fake_client = _FakeClient(
        stream_response=_FakeResponse(
            lines=[
                (
                    'data: {"object": "message", "type": "message", '
                    '"role": "assistant", "status": "completed", '
                    '"content": [{"type": "text", "text": "native reply"}]}'
                ),
                (
                    'data: {"object": "response", "status": "completed", '
                    '"output": []}'
                ),
                (
                    'data: {"object": "message", "type": "message", '
                    '"role": "assistant", "status": "completed", '
                    '"content": [{"type": "text", "text": "~>[echo] ququ"}]}'
                ),
            ],
        ),
    )
    monkeypatch.setattr(
        agent_management,
        "create_agent_api_client",
        lambda _base_url: fake_client,
    )

    result = agent_management.collect_final_agent_chat_response(
        "http://127.0.0.1:8088",
        {"session_id": "sid", "input": []},
        "bot_b",
        30,
    )

    assert result is not None
    assert agent_management.extract_agent_text_content(result) == (
        "native reply\n\n~>[echo] ququ"
    )


def test_agent_management_tools_can_be_registered_in_toolkit():
    toolkit = Toolkit()

    toolkit.register_tool_function(agent_management.list_agents)
    toolkit.register_tool_function(agent_management.chat_with_agent)

    schemas = toolkit.get_json_schemas()
    schema_names = {schema["function"]["name"] for schema in schemas}

    assert "list_agents" in schema_names
    assert "chat_with_agent" in schema_names


async def test_list_agents_uses_to_thread(monkeypatch):
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url: {"agents": [{"id": "bot_a"}]},
    )

    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(agent_management.asyncio, "to_thread", fake_to_thread)

    response = await agent_management.list_agents()

    assert calls
    assert calls[0][0] is agent_management.list_agents_data
    assert '"id": "bot_a"' in response.content[0].get("text", "")


async def test_submit_to_agent_carries_an_explicit_background_deadline(
    monkeypatch,
):
    captured = {}

    def fake_submit(_base_url, payload, to_agent, _timeout):
        captured["payload"] = payload
        captured["to_agent"] = to_agent
        return {"task_id": "task-1"}

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(agent_management.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(agent_management, "agent_exists", lambda *_args: True)
    monkeypatch.setattr(
        agent_management,
        "resolve_calling_agent_id",
        lambda _from_agent=None: "bot_a",
    )

    response = await agent_management.submit_to_agent(
        to_agent="bot_b",
        text="复核长文档",
        timeout_seconds=1200,
    )

    assert captured["to_agent"] == "bot_b"
    assert (
        captured["payload"]["metadata"]["agent_task_timeout_seconds"] == 1200
    )
    assert (
        captured["payload"]["metadata"]["native_turn_timeout_seconds"] == 1200
    )
    assert "[TASK_ID: task-1]" in response.content[0].get("text", "")


@pytest.mark.asyncio
async def test_collaborate_with_agents_starts_all_members_before_any_finishes(
    monkeypatch,
):
    agent_ids = [f"agent_{index}" for index in range(4)]
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": agent_id,
                    "name": f"成员 {index + 1}",
                    "description": "独立咨询",
                    "enabled": True,
                }
                for index, agent_id in enumerate(agent_ids)
            ],
        },
    )

    barrier = threading.Barrier(4)
    lock = threading.Lock()
    active = 0
    peak_active = 0
    started: list[str] = []
    submitted_payloads: list[dict] = []

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        nonlocal active, peak_active
        with lock:
            started.append(to_agent)
            submitted_payloads.append(_payload)
            active += 1
            peak_active = max(peak_active, active)
        barrier.wait(timeout=3)
        with lock:
            active -= 1
        return {"task_id": f"task-{to_agent}"}

    def fake_status(_base_url, task_id, **_kwargs):
        agent_id = task_id.removeprefix("task-")
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {"type": "text", "text": f"{agent_id} 的结果"},
                        ],
                    },
                ],
            },
        }

    monkeypatch.setattr(
        agent_management,
        "submit_agent_chat_task",
        fake_submit,
    )
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        fake_status,
    )

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {
                    "to_agent": agent_id,
                    "text": f"{agent_id} 的私有完整任务",
                    "label": f"公开任务 {index + 1}",
                }
                for index, agent_id in enumerate(agent_ids)
            ],
        )
    ]

    final = updates[-1]
    assert sorted(started) == sorted(agent_ids)
    assert peak_active == 4
    assert all(
        payload["metadata"]["native_response_max_tokens"] == 1024
        for payload in submitted_payloads
    )
    assert all(
        payload["metadata"]["native_accept_truncated_best_effort"] is True
        for payload in submitted_payloads
    )
    assert all(
        "总正文控制在 800 个中文字符以内"
        in payload["input"][0]["content"][0]["text"]
        for payload in submitted_payloads
    )
    assert all(
        payload["metadata"]["native_turn_timeout_seconds"] == 300
        for payload in submitted_payloads
    )
    assert final.is_last is True
    assert final.metadata["collaboration"]["status"] == "completed"
    assert final.metadata["collaboration"]["phase"] == "closed"
    assert final.metadata["collaboration"]["host"]["display_name"] == "沈行知"
    assert final.metadata["collaboration"]["closure"]["outcome"] == (
        "ready_for_synthesis"
    )
    assert (
        final.metadata["collaboration"]["closure"]["synthesis_ready"] is True
    )
    assert (
        final.metadata["collaboration"]["closure"]["deliverable_ready"]
        is False
    )
    assert final.metadata["collaboration"]["unattended"] is True
    assert final.metadata["collaboration_contract"]["goal"] == (
        "并行收集独立专业判断"
    )
    assert final.metadata["collaboration_contract"]["effective_permissions"] == (
        "read_only"
    )
    assert [
        member["status"]
        for member in final.metadata["collaboration"]["members"]
    ] == ["completed"] * 4
    assert all(
        "submission_ms" in member and "remote_wait_ms" in member
        for member in final.metadata["collaboration"]["members"]
    )
    assert all(
        member.get("session_id") and member.get("task_id")
        for member in final.metadata["collaboration"]["members"]
    )
    assert [
        member.get("result")
        for member in final.metadata["collaboration"]["members"]
    ] == [f"{agent_id} 的结果" for agent_id in agent_ids]
    assert '"synthesis_required"' in final.content[0]["text"]
    assert '"host_delivery_instruction"' in final.content[0]["text"]
    assert "私有完整任务" not in json.dumps(
        final.metadata,
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_roundtable_runs_one_cross_review_and_reuses_child_sessions(
    monkeypatch,
) -> None:
    agent_ids = ["agent_a", "agent_b"]
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": agent_id,
                    "name": agent_id,
                    "description": "圆桌成员",
                    "enabled": True,
                }
                for agent_id in agent_ids
            ],
        },
    )
    submitted: list[tuple[str, dict]] = []
    payload_by_task: dict[str, dict] = {}
    agent_by_task: dict[str, str] = {}

    def fake_submit(_base_url, payload, to_agent, _timeout):
        task_id = f"task-{len(submitted) + 1}"
        submitted.append((to_agent, payload))
        payload_by_task[task_id] = payload
        agent_by_task[task_id] = to_agent
        return {"task_id": task_id}

    def fake_status(_base_url, task_id, **_kwargs):
        payload = payload_by_task[task_id]
        round_number = payload["metadata"]["collaboration_round"]
        if round_number == 2 and agent_by_task[task_id] == "agent_b":
            return {
                "status": "failed",
                "error": {"code": "policy_denied", "message": "policy denied"},
            }
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": f"第 {round_number} 轮的完整结论与依据。",
                            },
                        ],
                    },
                ],
            },
        }

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        fake_status,
    )

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {"to_agent": "agent_a", "text": "评估路线 A", "label": "A"},
                {"to_agent": "agent_b", "text": "评估路线 B", "label": "B"},
            ],
            goal="在两条路线中形成可执行判断",
            why_parallel="先独立评估，再用一次交叉复核暴露分歧",
            acceptance_criteria=["指出会改变最终判断的分歧", "标明证据强弱"],
            protocol="roundtable",
        )
    ]

    assert len(submitted) == 4
    assert [
        payload["metadata"]["collaboration_round"]
        for _agent_id, payload in submitted
    ] == [1, 1, 2, 2]
    first_sessions = {
        agent_id: payload["session_id"]
        for agent_id, payload in submitted[:2]
    }
    assert all(
        payload["session_id"] == first_sessions[agent_id]
        for agent_id, payload in submitted[2:]
    )
    assert all(
        "且仅完成一次交叉审阅" in payload["input"][0]["content"][0]["text"]
        for _agent_id, payload in submitted[2:]
    )
    assert all(
        "在两条路线中形成可执行判断"
        in payload["input"][0]["content"][0]["text"]
        and "指出会改变最终判断的分歧"
        in payload["input"][0]["content"][0]["text"]
        and "会改变最终判断"
        in payload["input"][0]["content"][0]["text"]
        for _agent_id, payload in submitted[2:]
    )
    round_two_running = [
        update.metadata["collaboration"]
        for update in updates
        if not update.is_last
        and update.metadata.get("collaboration", {}).get("current_round") == 2
        and any(
            member.get("review_status") == "running"
            for member in update.metadata.get("collaboration", {}).get("members", [])
        )
    ]
    assert round_two_running
    assert all(
        member.get("round_completed") == 1
        for member in round_two_running[0]["members"]
    )
    final = updates[-1]
    assert final.metadata["collaboration"]["current_round"] == 2
    assert final.metadata["collaboration"]["protocol"] == "roundtable"
    assert final.metadata["collaboration"]["status"] == "completed"
    final_members = final.metadata["collaboration"]["members"]
    assert final_members[0]["review_status"] == "completed"
    assert final_members[0]["round_completed"] == 2
    assert final_members[1]["review_status"] == "unavailable"
    assert final_members[1]["round_completed"] == 1
    assert final_members[1]["result"] == "第 1 轮的完整结论与依据。"
    assert any(
        "交叉复核未完成" in conflict["safe_summary"]
        for conflict in final.metadata["collaboration"]["conflicts"]
    )
    delivery = json.loads(final.content[0]["text"])
    assert delivery["protocol"] == "roundtable"
    assert delivery["current_round"] == 2
    assert delivery["results"][0]["round_completed"] == 2
    assert delivery["results"][0]["review_status"] == "completed"
    assert delivery["results"][1]["round_completed"] == 1
    assert delivery["results"][1]["review_status"] == "unavailable"


def test_collaboration_independence_reports_same_model_reviewers() -> None:
    result = agent_management._collaboration_independence(  # noqa: SLF001
        [
            {"to_agent": "agent_a"},
            {"to_agent": "agent_b"},
        ],
        agents_by_id={
            "agent_a": {
                "active_model": {"provider_id": "openai", "model": "gpt-x"},
            },
            "agent_b": {
                "active_model": {"provider_id": "openai", "model": "gpt-x"},
            },
        },
    )

    assert result["assessment"] == "same_model_reviewers"
    assert result["model_independent"] is False
    assert result["distinct_agent_count"] == 2
    assert result["distinct_model_count"] == 1
    assert result["limitation"]


def test_collaboration_independence_reports_distinct_models() -> None:
    result = agent_management._collaboration_independence(  # noqa: SLF001
        [
            {"to_agent": "agent_a"},
            {"to_agent": "agent_b"},
        ],
        agents_by_id={
            "agent_a": {
                "active_model": {"provider_id": "openai", "model": "gpt-x"},
            },
            "agent_b": {
                "active_model": {
                    "provider_id": "anthropic",
                    "model": "claude-y",
                },
            },
        },
    )

    assert result["assessment"] == "model_independent"
    assert result["model_independent"] is True
    assert result["distinct_model_count"] == 2
    assert result["limitation"] is None


def test_collaboration_accepts_unique_agent_display_names() -> None:
    tasks = agent_management._validated_collaboration_tasks(  # noqa: SLF001
        [
            {"to_agent": "椰汁", "text": "私有 A", "label": "生物学"},
            {"to_agent": "美式", "text": "私有 B", "label": "逻辑学"},
        ],
        agents_by_id={
            "Lantai_Coconut_Agent": {
                "id": "Lantai_Coconut_Agent",
                "name": "椰汁",
                "enabled": True,
            },
            "Lantai_Americano_Agent": {
                "id": "Lantai_Americano_Agent",
                "name": "美式",
                "enabled": True,
            },
        },
    )

    assert [task["to_agent"] for task in tasks] == [
        "Lantai_Coconut_Agent",
        "Lantai_Americano_Agent",
    ]


@pytest.mark.parametrize(
    "value",
    ["", "   ", "<params>", "<params></params>", "<tool_call>", "{}", "[]"],
)
def test_collaboration_rejects_protocol_placeholders_as_member_results(
    value: str,
) -> None:
    assert (
        agent_management._usable_collaboration_result(value) is False
    )  # noqa: SLF001


def test_collaboration_accepts_substantive_member_result() -> None:
    assert (
        agent_management._usable_collaboration_result(  # noqa: SLF001
            "最美的人不是固定的一张脸，而是你真正愿意看见的人。"
        )
        is True
    )


def test_collaboration_infers_upstream_dependencies_for_a_consolidation_task() -> (
    None
):
    tasks = agent_management._validated_collaboration_tasks(  # noqa: SLF001
        [
            {"to_agent": "research", "text": "交付材料 A 正文", "label": "健康与安全"},
            {"to_agent": "operations", "text": "交付材料 B 正文", "label": "实操与情绪"},
            {
                "to_agent": "editor",
                "text": "整合材料 A 和材料 B，输出完整 Markdown 文档",
                "label": "内容整合与排版",
            },
        ],
        agents_by_id={
            agent_id: {"id": agent_id, "name": agent_id, "enabled": True}
            for agent_id in ("research", "operations", "editor")
        },
    )

    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == []
    assert tasks[2]["depends_on"] == ["research", "operations"]


@pytest.mark.asyncio
async def test_collaboration_stages_dependencies_and_injects_accepted_results(
    monkeypatch,
) -> None:
    agent_ids = ["research", "operations", "editor"]
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {"id": agent_id, "name": agent_id, "enabled": True}
                for agent_id in agent_ids
            ]
        },
    )
    submitted: list[str] = []
    prompts: dict[str, str] = {}

    def fake_submit(_base_url, payload, to_agent, _timeout):
        submitted.append(to_agent)
        prompts[to_agent] = payload["input"][0]["content"][0]["text"]
        if to_agent == "editor":
            assert set(submitted[:2]) == {"research", "operations"}
        return {"task_id": f"task-{to_agent}"}

    def fake_status(_base_url, task_id, **_kwargs):
        agent_id = task_id.removeprefix("task-")
        results = {
            "research": "这是已验收的材料 A 正文。",
            "operations": "这是已验收的材料 B 正文。",
            "editor": "# 统一成稿\n\n已依据两份上游交付完成整合。",
        }
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {"content": [{"type": "text", "text": results[agent_id]}]},
                ],
            },
        }

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(agent_management, "get_agent_chat_task_status", fake_status)

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {"to_agent": "research", "text": "交付材料 A", "label": "健康与安全"},
                {"to_agent": "operations", "text": "交付材料 B", "label": "实操与情绪"},
                {
                    "to_agent": "editor",
                    "text": "整合材料 A 和材料 B，输出完整 Markdown 文档",
                    "label": "内容整合与排版",
                },
            ],
        )
    ]

    assert submitted[-1] == "editor"
    assert "这是已验收的材料 A 正文" in prompts["editor"]
    assert "这是已验收的材料 B 正文" in prompts["editor"]
    editor = updates[-1].metadata["collaboration"]["members"][2]
    assert editor["status"] == "completed"
    assert editor["depends_on"] == ["member-1", "member-2"]


@pytest.mark.asyncio
async def test_execution_team_uses_scoped_tools_artifacts_and_durable_replay(
    monkeypatch,
    tmp_path: Path,
) -> None:
    agent_homes = {
        "research": tmp_path / "research",
        "editor": tmp_path / "editor",
    }
    for home in agent_homes.values():
        home.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    context_token = current_workspace_context.set(
        WorkspaceContext(
            agent_home=tmp_path,
            state_root=state_root,
            project_root=project_root,
        )
    )
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": agent_id,
                    "name": agent_id,
                    "enabled": True,
                    "workspace_dir": str(home),
                }
                for agent_id, home in agent_homes.items()
            ]
        },
    )
    submitted_payloads: dict[str, dict] = {}
    submit_count = 0

    def fake_submit(_base_url, payload, to_agent, _timeout):
        nonlocal submit_count
        submit_count += 1
        submitted_payloads[to_agent] = payload
        workspace = agent_homes[to_agent] / payload["channel_meta"][
            "workspace_dir"
        ]
        output = workspace / "outputs" / (
            "research.md" if to_agent == "research" else "final.md"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        if to_agent == "editor":
            dependency = workspace / "inputs" / "member-1" / "outputs" / (
                "research.md"
            )
            assert dependency.read_text(encoding="utf-8") == "可核验研究材料"
            assert dependency.stat().st_mode & 0o222 == 0
        output.write_text(
            "可核验研究材料" if to_agent == "research" else "# 最终办公成稿\n",
            encoding="utf-8",
        )
        return {"task_id": f"task-{to_agent}"}

    def fake_status(_base_url, task_id, **_kwargs):
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": f"{task_id} 已完成并提交核验文件",
                            }
                        ]
                    }
                ],
            },
        }

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        fake_status,
    )
    tasks = [
        {
            "to_agent": "research",
            "text": "查阅资料并形成可核验研究材料",
            "label": "研究",
            "allowed_tools": ["web_search", "write_file"],
            "resource_claims": ["service:4173"],
            "expected_artifacts": [
                {
                    "path": "outputs/research.md",
                    "media_type": "text/markdown",
                    "minimum_bytes": 6,
                }
            ],
        },
        {
            "to_agent": "editor",
            "text": "读取上游文件并生成最终办公成稿",
            "label": "成稿",
            "depends_on": ["research"],
            "allowed_tools": ["read_file", "write_file"],
            "expected_artifacts": [
                {
                    "path": "outputs/final.md",
                    "media_type": "text/markdown",
                    "minimum_bytes": 6,
                }
            ],
        },
    ]
    effect_context = RuntimeEffectContext(
        run_id="run-root",
        attempt_id="attempt-parent",
        effect_id="effect-parent",
        state_workspace_dir=str(state_root),
    )
    try:
        with bind_runtime_effect_context(effect_context):
            first = [
                update
                async for update in agent_management.collaborate_with_agents(
                    tasks,
                    goal="研究并生成办公文档",
                    mode="execution",
                    team_id="golden-office-flow",
                )
            ]
            second = [
                update
                async for update in agent_management.collaborate_with_agents(
                    tasks,
                    goal="研究并生成办公文档",
                    mode="execution",
                    team_id="golden-office-flow",
                )
            ]
    finally:
        current_workspace_context.reset(context_token)

    assert submit_count == 2
    assert len(second) == 1
    assert second[0].is_last is True
    assert all(
        payload["metadata"]["execution_team_mode"] == "scoped_execute"
        and payload["metadata"]["delegation_nested_allowed"] is False
        and payload["metadata"]["delegation_root_run_id"] == "run-root"
        and payload["metadata"]["delegation_parent_run_id"] == "run-root"
        and payload["metadata"]["delegation_parent_attempt_id"]
        == "attempt-parent"
        and payload["metadata"]["delegation_parent_effect_id"]
        == "effect-parent"
        and payload["metadata"]["delegation_attempt"] == 1
        and "collaboration_read_only" not in payload["metadata"]
        and payload["metadata"]["native_response_max_tokens"] == 8192
        for payload in submitted_payloads.values()
    )
    assert submitted_payloads["research"]["metadata"][
        "delegation_allowed_tools"
    ] == ["web_search", "write_file"]
    assert submitted_payloads["research"]["metadata"][
        "delegation_task_id"
    ] == "golden-office-flow:member-1"
    assert "总正文控制在 800 个中文字符以内" not in submitted_payloads[
        "research"
    ]["input"][0]["content"][0]["text"]
    delivery = json.loads(first[-1].content[0]["text"])
    assert delivery["mode"] == "execution"
    assert delivery["execution_scope"] == {
        "root_run_id": "run-root",
        "parent_run_id": "run-root",
        "parent_attempt_id": "attempt-parent",
        "parent_effect_id": "effect-parent",
    }
    assert delivery["budget_usage"]["member_attempts_started"] == 2
    assert delivery["budget_usage"]["verified_artifact_bytes"] > 0
    assert [row["status"] for row in delivery["results"]] == [
        "completed",
        "completed",
    ]
    assert all(
        len(row["artifacts"][0]["sha256"]) == 64
        for row in delivery["results"]
    )
    snapshot_files = list(
        (state_root / ".lantai" / "runtime" / "v1" / "execution-teams").glob(
            "*/snapshot.json"
        )
    )
    assert len(snapshot_files) == 1


@pytest.mark.asyncio
async def test_execution_team_development_flow_hands_off_plan_and_code(
    monkeypatch,
    tmp_path: Path,
) -> None:
    agent_homes = {
        "inspector": tmp_path / "inspector",
        "implementer": tmp_path / "implementer",
    }
    for home in agent_homes.values():
        home.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    context_token = current_workspace_context.set(
        WorkspaceContext(
            agent_home=tmp_path,
            state_root=state_root,
            project_root=project_root,
        )
    )
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": agent_id,
                    "name": agent_id,
                    "enabled": True,
                    "workspace_dir": str(home),
                }
                for agent_id, home in agent_homes.items()
            ]
        },
    )
    submitted_payloads: dict[str, dict] = {}

    def fake_submit(_base_url, payload, to_agent, _timeout):
        submitted_payloads[to_agent] = payload
        workspace = agent_homes[to_agent] / payload["channel_meta"][
            "workspace_dir"
        ]
        outputs = workspace / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        if to_agent == "inspector":
            (outputs / "plan.md").write_text(
                "# 修复计划\n\n增加输入校验并运行回归测试。\n",
                encoding="utf-8",
            )
        else:
            dependency = (
                workspace
                / "inputs"
                / "member-1"
                / "outputs"
                / "plan.md"
            )
            assert "输入校验" in dependency.read_text(encoding="utf-8")
            assert dependency.stat().st_mode & 0o222 == 0
            (outputs / "fix.py").write_text(
                "def normalize(value: str) -> str:\n    return value.strip()\n",
                encoding="utf-8",
            )
            (outputs / "test-report.txt").write_text(
                "2 passed\n",
                encoding="utf-8",
            )
        return {"task_id": f"task-{to_agent}"}

    def fake_status(_base_url, task_id, **_kwargs):
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": f"{task_id} 已完成开发交付",
                            }
                        ]
                    }
                ],
            },
        }

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        fake_status,
    )
    tasks = [
        {
            "to_agent": "inspector",
            "text": "检查缺陷并输出修复计划",
            "label": "诊断",
            "allowed_tools": ["search_text", "read_file", "write_file"],
            "expected_artifacts": [
                {
                    "path": "outputs/plan.md",
                    "media_type": "text/markdown",
                    "minimum_bytes": 12,
                }
            ],
        },
        {
            "to_agent": "implementer",
            "text": "依据上游计划实现修复并运行测试",
            "label": "实现与验证",
            "depends_on": ["inspector"],
            "allowed_tools": ["read_file", "write_file", "run_command"],
            "expected_artifacts": [
                {
                    "path": "outputs/fix.py",
                    "media_type": "text/x-python",
                    "minimum_bytes": 20,
                },
                {
                    "path": "outputs/test-report.txt",
                    "media_type": "text/plain",
                    "minimum_bytes": 5,
                },
            ],
        },
    ]
    try:
        updates = [
            update
            async for update in agent_management.collaborate_with_agents(
                tasks,
                goal="定位缺陷、实现修复并交付测试证据",
                mode="execution",
                team_id="golden-development-flow",
            )
        ]
    finally:
        current_workspace_context.reset(context_token)

    delivery = json.loads(updates[-1].content[0]["text"])
    assert [row["status"] for row in delivery["results"]] == [
        "completed",
        "completed",
    ]
    assert len(delivery["results"][1]["artifacts"]) == 2
    assert submitted_payloads["implementer"]["metadata"][
        "delegation_allowed_tools"
    ] == ["read_file", "write_file", "run_command"]


@pytest.mark.asyncio
async def test_execution_team_serializes_and_renews_exclusive_resources(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from lantai.runtime.execution_team import ExecutionResourceLeaseStore

    agent_homes = {
        agent_id: tmp_path / agent_id for agent_id in ("alpha", "beta")
    }
    for home in agent_homes.values():
        home.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    global_working_dir = tmp_path / "global-lantai-home"
    monkeypatch.setattr(agent_management, "WORKING_DIR", global_working_dir)
    project_root = tmp_path / "project"
    project_root.mkdir()
    context_token = current_workspace_context.set(
        WorkspaceContext(
            agent_home=tmp_path,
            state_root=state_root,
            project_root=project_root,
        )
    )
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": agent_id,
                    "name": agent_id,
                    "enabled": True,
                    "workspace_dir": str(home),
                }
                for agent_id, home in agent_homes.items()
            ]
        },
    )
    original_wait_acquire = ExecutionResourceLeaseStore.wait_acquire
    original_renew = ExecutionResourceLeaseStore.renew
    renewals: list[list[str]] = []

    def expiring_wait_acquire(self, resources, **kwargs):
        records = original_wait_acquire(self, resources, **kwargs)
        return [
            {**record, "expires_at_epoch_seconds": 0.0}
            for record in records
        ]

    def recording_renew(self, records, **kwargs):
        renewals.append([str(item["resource"]) for item in records])
        return original_renew(self, records, **kwargs)

    monkeypatch.setattr(
        ExecutionResourceLeaseStore,
        "wait_acquire",
        expiring_wait_acquire,
    )
    monkeypatch.setattr(
        ExecutionResourceLeaseStore,
        "renew",
        recording_renew,
    )
    active_status_calls = 0
    max_active_status_calls = 0
    activity_lock = threading.Lock()

    def fake_submit(_base_url, payload, to_agent, _timeout):
        workspace = agent_homes[to_agent] / payload["channel_meta"][
            "workspace_dir"
        ]
        artifact = workspace / "outputs" / f"{to_agent}.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"{to_agent} complete", encoding="utf-8")
        return {"task_id": f"task-{to_agent}"}

    def fake_status(_base_url, task_id, **_kwargs):
        nonlocal active_status_calls, max_active_status_calls
        with activity_lock:
            active_status_calls += 1
            max_active_status_calls = max(
                max_active_status_calls,
                active_status_calls,
            )
        threading.Event().wait(0.05)
        with activity_lock:
            active_status_calls -= 1
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": f"{task_id} completed",
                            }
                        ]
                    }
                ],
            },
        }

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        fake_status,
    )
    tasks = [
        {
            "to_agent": agent_id,
            "text": f"使用共享浏览器完成 {agent_id} 任务",
            "label": agent_id,
            "allowed_tools": ["browser_session", "write_file"],
            "resource_claims": ["browser-profile:default"],
            "expected_artifacts": [
                {"path": f"outputs/{agent_id}.txt", "minimum_bytes": 5}
            ],
        }
        for agent_id in agent_homes
    ]
    try:
        updates = [
            update
            async for update in agent_management.collaborate_with_agents(
                tasks,
                goal="串行使用同一个稀缺资源",
                mode="execution",
                team_id="exclusive-resource-flow",
            )
        ]
    finally:
        current_workspace_context.reset(context_token)

    assert max_active_status_calls == 1
    assert renewals == [
        ["browser", "browser-profile:default"],
        ["browser", "browser-profile:default"],
    ]
    assert any(
        any(member["status"] == "waiting_resource" for member in room)
        for room in (
            update.metadata["collaboration"]["members"] for update in updates
        )
    )
    delivery = json.loads(updates[-1].content[0]["text"])
    assert [row["status"] for row in delivery["results"]] == [
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_execution_team_retries_then_rejects_missing_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    agents = ["writer", "reviewer"]
    homes = {agent: tmp_path / agent for agent in agents}
    for home in homes.values():
        home.mkdir()
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": agent,
                    "name": agent,
                    "enabled": True,
                    "workspace_dir": str(homes[agent]),
                }
                for agent in agents
            ]
        },
    )
    attempts = {agent: 0 for agent in agents}

    def fake_submit(_base_url, payload, to_agent, _timeout):
        attempts[to_agent] += 1
        if to_agent == "reviewer":
            workspace = homes[to_agent] / payload["channel_meta"]["workspace_dir"]
            artifact = workspace / "outputs" / "review.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("复核通过", encoding="utf-8")
        return {"task_id": f"task-{to_agent}-{attempts[to_agent]}"}

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        lambda _base_url, task_id, **_kwargs: {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {"content": [{"type": "text", "text": f"{task_id} 完成"}]}
                ],
            },
        },
    )
    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {
                    "to_agent": "writer",
                    "text": "生成正文",
                    "label": "正文",
                    "allowed_tools": ["write_file"],
                    "expected_artifacts": [{"path": "outputs/missing.md"}],
                },
                {
                    "to_agent": "reviewer",
                    "text": "独立复核",
                    "label": "复核",
                    "allowed_tools": ["write_file"],
                    "expected_artifacts": [{"path": "outputs/review.md"}],
                },
            ],
            mode="execution",
        )
    ]

    assert attempts == {"writer": 2, "reviewer": 1}
    writer = updates[-1].metadata["collaboration"]["members"][0]
    assert writer["status"] == "failed"
    assert writer["error_code"] == "artifact_verification_failed"
    assert updates[-1].metadata["collaboration"]["status"] == "partial"


@pytest.mark.asyncio
async def test_execution_team_root_attempt_budget_blocks_semantic_retry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "writer"
    home.mkdir()
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": "writer",
                    "name": "writer",
                    "enabled": True,
                    "workspace_dir": str(home),
                }
            ]
        },
    )
    submissions = 0

    def fake_submit(_base_url, _payload, _to_agent, _timeout):
        nonlocal submissions
        submissions += 1
        return {"task_id": f"task-writer-{submissions}"}

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        lambda _base_url, _task_id, **_kwargs: {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {"type": "text", "text": "已完成但未写入产物"}
                        ]
                    }
                ],
            },
        },
    )

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {
                    "to_agent": "writer",
                    "text": "生成正文",
                    "label": "正文",
                    "allowed_tools": ["write_file"],
                    "expected_artifacts": [{"path": "outputs/missing.md"}],
                }
            ],
            mode="execution",
            max_member_attempts=1,
        )
    ]

    assert submissions == 1
    member = updates[-1].metadata["collaboration"]["members"][0]
    assert member["status"] == "failed"
    assert member["error_code"] == "team_budget_exhausted"
    assert updates[-1].metadata["collaboration"]["budget_usage"][
        "member_attempts_started"
    ] == 1


@pytest.mark.asyncio
async def test_execution_team_wall_budget_stops_polling_before_host_reserve(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "worker"
    home.mkdir()
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": "worker",
                    "name": "worker",
                    "enabled": True,
                    "workspace_dir": str(home),
                }
            ]
        },
    )
    now = [1000.0]
    monkeypatch.setattr(agent_management.time, "time", lambda: now[0])

    def fake_submit(_base_url, _payload, _to_agent, _timeout):
        now[0] = 1031.0
        return {"task_id": "task-worker"}

    status_calls: list[str] = []
    cancelled: list[str] = []

    def fake_status(_base_url, task_id, **_kwargs):
        status_calls.append(task_id)
        return {"status": "running"}

    async def fake_cancel(task_id, **_kwargs):
        if task_id:
            cancelled.append(task_id)
        return True

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        fake_status,
    )
    monkeypatch.setattr(
        agent_management,
        "_cancel_collaboration_remote_task",
        fake_cancel,
    )

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {
                    "to_agent": "worker",
                    "text": "执行任务",
                    "label": "执行",
                    "allowed_tools": ["write_file"],
                }
            ],
            mode="execution",
            max_team_wall_seconds=60,
        )
    ]

    member = updates[-1].metadata["collaboration"]["members"][0]
    assert status_calls == []
    assert cancelled == ["task-worker"]
    assert member["status"] == "failed"
    assert member["error_code"] == "team_budget_exhausted"


@pytest.mark.asyncio
async def test_execution_team_wall_budget_interrupts_slow_submission(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "worker"
    home.mkdir()
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": "worker",
                    "name": "worker",
                    "enabled": True,
                    "workspace_dir": str(home),
                }
            ]
        },
    )
    now = [1000.0]
    monkeypatch.setattr(agent_management.time, "time", lambda: now[0])
    submission_started = threading.Event()
    submission_finished = threading.Event()
    release_submission = threading.Event()
    cancelled: list[str] = []

    def slow_submit(_base_url, _payload, _to_agent, _timeout):
        submission_started.set()
        assert release_submission.wait(2.0)
        submission_finished.set()
        return {"task_id": "remote-late"}

    async def fake_cancel(task_id, **_kwargs):
        if task_id:
            cancelled.append(task_id)
        return True

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", slow_submit)
    monkeypatch.setattr(
        agent_management,
        "_cancel_collaboration_remote_task",
        fake_cancel,
    )

    stream = agent_management.collaborate_with_agents(
        [
            {
                "to_agent": "worker",
                "text": "执行任务",
                "label": "执行",
                "allowed_tools": ["write_file"],
            }
        ],
        mode="execution",
        max_team_wall_seconds=60,
    )
    await anext(stream)
    now[0] = 1029.0
    started = time.monotonic()
    updates = [update async for update in stream]
    elapsed = time.monotonic() - started

    member = updates[-1].metadata["collaboration"]["members"][0]
    assert elapsed < 1.4
    assert submission_started.is_set()
    assert member["status"] == "failed"
    assert member["error_code"] == "team_budget_exhausted"
    release_submission.set()
    for _ in range(100):
        if submission_finished.is_set() and cancelled:
            break
        await asyncio.sleep(0.01)
    assert submission_finished.is_set()
    for _ in range(5):
        if cancelled:
            break
        await asyncio.sleep(0)
    assert cancelled == ["remote-late"]


def test_execution_team_rejects_tampered_dependency_artifact(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_file = source_root / "outputs" / "research.md"
    source_file.parent.mkdir(parents=True)
    target_root.mkdir()
    accepted_content = b"accepted research"
    source_file.write_bytes(accepted_content)
    recorded_artifact = {
        "path": "outputs/research.md",
        "media_type": "text/markdown",
        "size_bytes": len(accepted_content),
        "sha256": hashlib.sha256(accepted_content).hexdigest(),
    }
    source_file.write_bytes(b"changed after acceptance")

    with pytest.raises(ValueError, match="digest changed"):
        agent_management._copy_dependency_artifacts(  # noqa: SLF001
            {"depends_on": ["research"]},
            member={"_workspace_root": str(target_root)},
            members_by_agent={
                "research": {
                    "member_id": "member-1",
                    "agent_id": "research",
                }
            },
            private_results={
                "member-1": {
                    "workspace_root": str(source_root),
                    "artifacts": [recorded_artifact],
                }
            },
        )

    assert not (target_root / "inputs" / "member-1").exists()


@pytest.mark.asyncio
async def test_execution_team_reattaches_running_tasks_after_process_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from lantai.runtime.execution_team import (
        ExecutionTeamStore,
        build_execution_team_contract,
    )
    from lantai.runtime.hosted_collaboration import (
        HostedCollaborationCoordinator,
    )

    state_root = tmp_path / "state"
    project_root = tmp_path / "project"
    state_root.mkdir()
    project_root.mkdir()
    agents = {
        agent_id: {
            "id": agent_id,
            "name": agent_id,
            "enabled": True,
            "workspace_dir": str(tmp_path / agent_id),
        }
        for agent_id in ("builder", "checker")
    }
    for agent in agents.values():
        Path(agent["workspace_dir"]).mkdir()
    raw_tasks = [
        {
            "to_agent": "builder",
            "text": "生成实现文件",
            "label": "实现",
            "allowed_tools": ["write_file"],
            "expected_artifacts": [{"path": "outputs/build.txt"}],
        },
        {
            "to_agent": "checker",
            "text": "生成验收文件",
            "label": "验收",
            "allowed_tools": ["write_file"],
            "expected_artifacts": [{"path": "outputs/check.txt"}],
        },
    ]
    normalized = agent_management._validated_collaboration_tasks(  # noqa: SLF001
        raw_tasks,
        agents_by_id=agents,
        execution=True,
    )
    contract = build_execution_team_contract(
        contract_id="restart-running-team",
        root_run_id="run-test-root",
        parent_run_id="run-test-root",
        parent_attempt_id="attempt-test-parent",
        parent_effect_id="effect-test-parent",
        goal="恢复运行中的执行团队",
        why_parallel="两个任务可以独立执行",
        tasks=normalized,
    )
    public_contract = contract.public_dump()
    public_contract["review_independence"] = (
        agent_management._collaboration_independence(  # noqa: SLF001
            normalized,
            agents_by_id=agents,
        )
    )
    members = []
    for index, (task, member_contract) in enumerate(
        zip(normalized, contract.members),
    ):
        members.append(
            {
                "member_id": member_contract.member_id,
                "agent_id": task["to_agent"],
                "display_name": task["to_agent"],
                "role": "",
                "label": task["label"],
                "status": "running",
                "attempt": 1,
                "duration_ms": 10,
                "submission_ms": 2,
                "remote_wait_ms": 8,
                "task_id": f"existing-{task['to_agent']}",
                "session_id": f"session-{task['to_agent']}",
                "depends_on": [],
                "execution_mode": "scoped_execute",
                "permission_profile": "delegated_workspace",
                "workspace_scope": member_contract.workspace_scope,
                "allowed_tools": list(member_contract.allowed_tools),
                "expected_artifacts": [
                    item.model_dump(mode="json")
                    for item in member_contract.expected_artifacts
                ],
            }
        )
    coordinator = HostedCollaborationCoordinator(contract)
    coordinator.transition("staffing")
    coordinator.transition("working")
    store = ExecutionTeamStore(state_root)
    store.save(
        contract.contract_id,
        {
            "contract": public_contract,
            "collaboration": coordinator.clone_public_snapshot(
                batch_status="running",
                members=members,
            ),
            "private_results": {},
            "final_delivery": None,
        },
        expected_revision=0,
    )
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {"agents": list(agents.values())},
    )
    submitted: list[str] = []
    polled: list[str] = []

    def fake_status(_base_url, task_id, **_kwargs):
        polled.append(task_id)
        agent_id = task_id.removeprefix("existing-")
        member_contract = next(
            item for item in contract.members if item.agent_id == agent_id
        )
        workspace = (
            Path(agents[agent_id]["workspace_dir"])
            / member_contract.workspace_scope
        )
        artifact = workspace / member_contract.expected_artifacts[0].path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"{agent_id} recovered", encoding="utf-8")
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {"type": "text", "text": f"{agent_id} 已恢复完成"}
                        ]
                    }
                ],
            },
        }

    monkeypatch.setattr(
        agent_management,
        "submit_agent_chat_task",
        lambda *_args, **_kwargs: submitted.append("unexpected"),
    )
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        fake_status,
    )
    context_token = current_workspace_context.set(
        WorkspaceContext(
            agent_home=tmp_path,
            state_root=state_root,
            project_root=project_root,
        )
    )
    try:
        updates = [
            update
            async for update in agent_management.collaborate_with_agents(
                raw_tasks,
                goal="恢复运行中的执行团队",
                why_parallel="两个任务可以独立执行",
                mode="execution",
                team_id="restart-running-team",
            )
        ]
    finally:
        current_workspace_context.reset(context_token)

    assert submitted == []
    assert sorted(polled) == ["existing-builder", "existing-checker"]
    assert updates[-1].metadata["collaboration"]["status"] == "completed"


@pytest.mark.asyncio
async def test_execution_team_restarts_only_host_pass_from_late_phase(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from lantai.runtime.execution_team import (
        ExecutionTeamStore,
        build_execution_team_contract,
        verify_execution_artifacts,
    )
    from lantai.runtime.hosted_collaboration import (
        HostedCollaborationCoordinator,
    )

    state_root = tmp_path / "state"
    project_root = tmp_path / "project"
    state_root.mkdir()
    project_root.mkdir()
    agents = {
        agent_id: {
            "id": agent_id,
            "name": agent_id,
            "enabled": True,
            "workspace_dir": str(tmp_path / agent_id),
        }
        for agent_id in ("author", "reviewer")
    }
    for agent in agents.values():
        Path(agent["workspace_dir"]).mkdir()
    raw_tasks = [
        {
            "to_agent": agent_id,
            "text": f"生成 {agent_id} 结果",
            "label": agent_id,
            "allowed_tools": ["write_file"],
            "expected_artifacts": [{"path": f"outputs/{agent_id}.txt"}],
        }
        for agent_id in agents
    ]
    normalized = agent_management._validated_collaboration_tasks(  # noqa: SLF001
        raw_tasks,
        agents_by_id=agents,
        execution=True,
    )
    contract = build_execution_team_contract(
        contract_id="restart-host-pass",
        root_run_id="run-test-root",
        parent_run_id="run-test-root",
        parent_attempt_id="attempt-test-parent",
        parent_effect_id="effect-test-parent",
        goal="恢复主持验收",
        why_parallel="独立产出后统一验收",
        tasks=normalized,
    )
    public_contract = contract.public_dump()
    public_contract["review_independence"] = (
        agent_management._collaboration_independence(  # noqa: SLF001
            normalized,
            agents_by_id=agents,
        )
    )
    members = []
    private_results = {}
    for task, member_contract in zip(normalized, contract.members):
        workspace = (
            Path(agents[task["to_agent"]]["workspace_dir"])
            / member_contract.workspace_scope
        )
        artifact = workspace / member_contract.expected_artifacts[0].path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"{task['to_agent']} complete", encoding="utf-8")
        verified = verify_execution_artifacts(
            workspace,
            list(member_contract.expected_artifacts),
        )
        members.append(
            {
                "member_id": member_contract.member_id,
                "agent_id": task["to_agent"],
                "display_name": task["to_agent"],
                "role": "",
                "label": task["label"],
                "status": "completed",
                "attempt": 1,
                "duration_ms": 10,
                "submission_ms": 2,
                "remote_wait_ms": 8,
                "session_id": f"session-{task['to_agent']}",
                "depends_on": [],
                "result": f"{task['to_agent']} 已完成",
                "execution_mode": "scoped_execute",
                "permission_profile": "delegated_workspace",
                "workspace_scope": member_contract.workspace_scope,
                "allowed_tools": list(member_contract.allowed_tools),
                "expected_artifacts": [
                    item.model_dump(mode="json")
                    for item in member_contract.expected_artifacts
                ],
                "artifacts": verified,
            }
        )
        private_results[member_contract.member_id] = {
            "result": f"{task['to_agent']} 已完成",
            "session_id": f"session-{task['to_agent']}",
            "workspace_root": str(workspace),
            "artifacts": verified,
        }
    coordinator = HostedCollaborationCoordinator(contract)
    coordinator.transition("staffing")
    coordinator.transition("working")
    coordinator.transition("reconciling")
    coordinator.transition("verifying")
    ExecutionTeamStore(state_root).save(
        contract.contract_id,
        {
            "contract": public_contract,
            "collaboration": coordinator.clone_public_snapshot(
                batch_status="running",
                members=members,
            ),
            "private_results": private_results,
            "final_delivery": None,
        },
        expected_revision=0,
    )
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {"agents": list(agents.values())},
    )
    monkeypatch.setattr(
        agent_management,
        "submit_agent_chat_task",
        lambda *_args, **_kwargs: pytest.fail("terminal members must not resubmit"),
    )
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        lambda *_args, **_kwargs: pytest.fail("terminal members must not repoll"),
    )
    context_token = current_workspace_context.set(
        WorkspaceContext(
            agent_home=tmp_path,
            state_root=state_root,
            project_root=project_root,
        )
    )
    try:
        updates = [
            update
            async for update in agent_management.collaborate_with_agents(
                raw_tasks,
                goal="恢复主持验收",
                why_parallel="独立产出后统一验收",
                mode="execution",
                team_id="restart-host-pass",
            )
        ]
    finally:
        current_workspace_context.reset(context_token)

    delivery = json.loads(updates[-1].content[0]["text"])
    assert delivery["batch_status"] == "completed"
    assert updates[-1].metadata["collaboration"]["phase"] == "closed"


@pytest.mark.asyncio
async def test_collaboration_rejects_a_status_report_that_omits_the_deliverable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {"id": "writer", "name": "writer", "enabled": True},
                {"id": "reviewer", "name": "reviewer", "enabled": True},
            ]
        },
    )
    attempts = {"writer": 0, "reviewer": 0}

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        attempts[to_agent] += 1
        return {"task_id": f"task-{to_agent}-{attempts[to_agent]}"}

    def fake_status(_base_url, task_id, **_kwargs):
        text = (
            "我已完整撰写六个章节，但未能核验部分来源。"
            "如需我输出初稿 Markdown，请告诉我，我可以现在就给。"
            if "writer" in task_id
            else "已完成独立复核并给出结论。"
        )
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [{"content": [{"type": "text", "text": text}]}],
            },
        }

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(agent_management, "get_agent_chat_task_status", fake_status)

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {
                    "to_agent": "writer",
                    "text": "审核并撰写六个章节，输出完整结构化 Markdown",
                    "label": "正文撰写",
                },
                {"to_agent": "reviewer", "text": "独立复核", "label": "复核"},
            ],
        )
    ]

    writer = updates[-1].metadata["collaboration"]["members"][0]
    assert attempts["writer"] == 2
    assert writer["status"] == "failed"
    assert writer["error_code"] == "deliverable_not_submitted"
    assert updates[-1].metadata["collaboration"]["status"] == "partial"


def test_collaboration_enforces_an_explicit_assignment_length() -> None:
    error = agent_management._collaboration_result_acceptance_error(  # noqa: SLF001
        "只有一段摘要",
        {
            "text": "输出 3000-5000 字完整 Markdown 文档",
            "minimum_output_chars": 0,
            "required_output_markers": [],
        },
    )

    assert error == (
        "deliverable_too_short",
        "交付正文不足 3000 字，不能标记为已验收",
    )


def test_collaboration_rejects_deferred_material_delivery() -> None:
    error = agent_management._collaboration_result_acceptance_error(  # noqa: SLF001
        "证据已经核对完成。如需我输出初稿 Markdown，请告诉我。",
        {
            "text": "检索权威证据并形成材料 A，交付给下游编辑。",
            "minimum_output_chars": 0,
            "required_output_markers": [],
        },
    )

    assert error == (
        "deliverable_not_submitted",
        "协作者只返回了状态说明，未随结果提交承诺的正文",
    )


@pytest.mark.asyncio
async def test_collaboration_does_not_launch_a_member_after_dependency_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {"id": "source", "name": "source", "enabled": True},
                {"id": "editor", "name": "editor", "enabled": True},
            ]
        },
    )
    submitted: list[str] = []

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        submitted.append(to_agent)
        return {"task_id": f"task-{to_agent}"}

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        lambda *_args, **_kwargs: {
            "status": "failed",
            "error": {"code": "permission_denied",
                "message": "permission denied",
            },
        },
    )

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {"to_agent": "source", "text": "交付源文", "label": "源文"},
                {
                    "to_agent": "editor",
                    "text": "整合源文并输出完整 Markdown",
                    "label": "内容整合",
                    "depends_on": ["source"],
                },
            ],
        )
    ]

    assert submitted == ["source"]
    editor = updates[-1].metadata["collaboration"]["members"][1]
    assert editor["status"] == "failed"
    assert editor["error_code"] == "dependency_failed"
    assert "上游交付未通过验收" in editor["error_message"]


@pytest.mark.asyncio
async def test_collaboration_retries_then_fails_protocol_placeholder_result(
    monkeypatch,
) -> None:
    agent_ids = ["agent_placeholder", "agent_valid"]
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": agent_id,
                    "name": agent_id,
                    "description": "独立复核",
                    "enabled": True,
                }
                for agent_id in agent_ids
            ]
        },
    )
    attempts: dict[str, int] = {agent_id: 0 for agent_id in agent_ids}

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        attempts[to_agent] += 1
        return {"task_id": f"task-{to_agent}-{attempts[to_agent]}"}

    def fake_status(_base_url, task_id, **_kwargs):
        result_text = (
            "<params>"
            if "agent_placeholder" in task_id
            else "这是可供主智能体汇总的实质结果。"
        )
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {"content": [{"type": "text", "text": result_text}]},
                ],
            },
        }

    monkeypatch.setattr(
        agent_management,
        "submit_agent_chat_task",
        fake_submit,
    )
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        fake_status,
    )

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {
                    "to_agent": agent_id,
                    "text": f"复核 {agent_id}",
                    "label": f"复核 {agent_id}",
                }
                for agent_id in agent_ids
            ],
        )
    ]

    snapshot = updates[-1].metadata["collaboration"]
    placeholder_member = next(
        member
        for member in snapshot["members"]
        if member["agent_id"] == "agent_placeholder"
    )
    assert attempts["agent_placeholder"] == 2
    assert placeholder_member["attempt"] == 2
    assert placeholder_member["status"] == "failed"
    assert placeholder_member["error_code"] == "invalid_member_result"
    assert snapshot["status"] == "partial"
    assert snapshot["closure"]["outcome"] == "partial"
    assert snapshot["closure"]["synthesis_ready"] is True
    assert snapshot["closure"]["deliverable_ready"] is False


def test_collaboration_accepts_unique_stable_agent_core_aliases() -> None:
    tasks = agent_management._validated_collaboration_tasks(  # noqa: SLF001
        [
            {"to_agent": "longjing", "text": "私有 A", "label": "军事史"},
            {"to_agent": "coconut", "text": "私有 B", "label": "人物对比"},
        ],
        agents_by_id={
            "Lantai_Longjing_Agent": {
                "id": "Lantai_Longjing_Agent",
                "name": "龙井",
                "enabled": True,
            },
            "Lantai_Coconut_Agent": {
                "id": "Lantai_Coconut_Agent",
                "name": "椰汁",
                "enabled": True,
            },
        },
    )

    assert [task["to_agent"] for task in tasks] == [
        "Lantai_Longjing_Agent",
        "Lantai_Coconut_Agent",
    ]


def test_collaboration_repairs_duplicate_model_staffing_with_distinct_agent() -> (
    None
):
    tasks = agent_management._validated_collaboration_tasks(  # noqa: SLF001
        [
            {"to_agent": "龙井", "text": "评估拿破仑", "label": "拿破仑"},
            {"to_agent": "龙井", "text": "评估希特勒", "label": "希特勒"},
        ],
        agents_by_id={
            "default": {
                "id": "default",
                "name": "沈行知",
                "enabled": True,
            },
            "Lantai_Longjing_Agent": {
                "id": "Lantai_Longjing_Agent",
                "name": "龙井",
                "enabled": True,
            },
            "Lantai_Coconut_Agent": {
                "id": "Lantai_Coconut_Agent",
                "name": "椰汁",
                "enabled": True,
            },
        },
    )

    assert [task["to_agent"] for task in tasks] == [
        "Lantai_Longjing_Agent",
        "Lantai_Coconut_Agent",
    ]


def test_collaboration_repairs_unavailable_disabled_and_host_staffing() -> (
    None
):
    tasks = agent_management._validated_collaboration_tasks(  # noqa: SLF001
        [
            {"to_agent": "yangzhi", "text": "评估拿破仑", "label": "拿破仑"},
            {"to_agent": "disabled", "text": "评估希特勒", "label": "希特勒"},
            {"to_agent": "default", "text": "独立复核", "label": "复核"},
        ],
        agents_by_id={
            "default": {
                "id": "default",
                "name": "沈行知",
                "enabled": True,
            },
            "disabled": {
                "id": "disabled",
                "name": "已禁用",
                "enabled": False,
            },
            "Lantai_Coconut_Agent": {
                "id": "Lantai_Coconut_Agent",
                "name": "椰汁",
                "enabled": True,
            },
            "Lantai_Longjing_Agent": {
                "id": "Lantai_Longjing_Agent",
                "name": "龙井",
                "enabled": True,
            },
            "Lantai_Americano_Agent": {
                "id": "Lantai_Americano_Agent",
                "name": "美式",
                "enabled": True,
            },
        },
    )

    assert [task["to_agent"] for task in tasks] == [
        "Lantai_Coconut_Agent",
        "Lantai_Longjing_Agent",
        "Lantai_Americano_Agent",
    ]


@pytest.mark.asyncio
async def test_collaboration_emits_heartbeat_while_members_are_still_running(
    monkeypatch,
):
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {"id": "agent_a", "name": "甲", "enabled": True},
                {"id": "agent_b", "name": "乙", "enabled": True},
            ],
        },
    )
    monkeypatch.setattr(
        agent_management,
        "COLLABORATION_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        agent_management,
        "submit_agent_chat_task",
        lambda _base_url, _payload, to_agent, _timeout: {
            "task_id": f"task-{to_agent}",
        },
    )
    checks = {"task-agent_a": 0, "task-agent_b": 0}

    def fake_status(_base_url, task_id, **_kwargs):
        checks[task_id] += 1
        if checks[task_id] < 4:
            return {"status": "running"}
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [{"content": [{"type": "text", "text": "完成"}]}],
            },
        }

    monkeypatch.setattr(agent_management, "get_agent_chat_task_status", fake_status)
    monkeypatch.setattr(agent_management, "COLLABORATION_POLL_INTERVAL_SECONDS", 0.02)

    snapshots = []
    async for update in agent_management.collaborate_with_agents(
        [
            {"to_agent": "agent_a", "text": "私有 A", "label": "研究"},
            {"to_agent": "agent_b", "text": "私有 B", "label": "复核"},
        ],
    ):
        if update.metadata.get("collaboration"):
            snapshots.append(update.metadata["collaboration"])

    running = [item for item in snapshots if item["status"] == "running"]
    assert len(running) >= 3
    assert any(
        member.get("duration_ms", 0) > 0
        for snapshot in running
        for member in snapshot["members"]
        if member["status"] == "running"
    )


@pytest.mark.asyncio
async def test_collaborate_with_agents_retries_one_recoverable_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {
                    "id": "agent_a",
                    "name": "甲",
                    "description": "研究",
                    "enabled": True,
                },
                {
                    "id": "agent_b",
                    "name": "乙",
                    "description": "复核",
                    "enabled": True,
                },
            ],
        },
    )
    attempts = {"agent_a": 0, "agent_b": 0}

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        attempts[to_agent] += 1
        if to_agent == "agent_a" and attempts[to_agent] == 1:
            raise httpx.ConnectError(
                "temporary disconnect",
                request=httpx.Request("POST", "http://test/api/task"),
            )
        return {"task_id": f"task-{to_agent}"}

    monkeypatch.setattr(
        agent_management,
        "submit_agent_chat_task",
        fake_submit,
    )
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        lambda _base_url, task_id, **_kwargs: {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {"type": "text", "text": f"{task_id} 完成"},
                        ],
                    },
                ],
            },
        },
    )

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {"to_agent": "agent_a", "text": "私有 A", "label": "研究"},
                {"to_agent": "agent_b", "text": "私有 B", "label": "复核"},
            ],
        )
    ]

    snapshots = [
        update.metadata["collaboration"]
        for update in updates
        if update.metadata and update.metadata.get("collaboration")
    ]
    final_members = snapshots[-1]["members"]
    assert attempts == {"agent_a": 2, "agent_b": 1}
    assert any(
        snapshot["members"][0]["status"] == "retrying"
        for snapshot in snapshots
    )
    assert final_members[0]["attempt"] == 2
    assert [member["status"] for member in final_members] == [
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_collaboration_business_failure_delivers_partial_results_without_retry(
    monkeypatch,
):
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {"id": "agent_a", "name": "甲", "enabled": True},
                {"id": "agent_b", "name": "乙", "enabled": True},
            ],
        },
    )
    submissions = {"agent_a": 0, "agent_b": 0}

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        submissions[to_agent] += 1
        return {"task_id": f"task-{to_agent}"}

    def fake_status(_base_url, task_id, **_kwargs):
        if task_id == "task-agent_a":
            return {
                "status": "failed",
                "error": {"message": "permission denied"},
            }
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {"content": [{"type": "text", "text": "有效结果"}]},
                ],
            },
        }

    monkeypatch.setattr(
        agent_management,
        "submit_agent_chat_task",
        fake_submit,
    )
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        fake_status,
    )

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {"to_agent": "agent_a", "text": "私有 A", "label": "风险"},
                {"to_agent": "agent_b", "text": "私有 B", "label": "建议"},
            ],
        )
    ]
    final = updates[-1]

    assert submissions == {"agent_a": 1, "agent_b": 1}
    assert final.metadata["collaboration"]["status"] == "partial"
    assert [
        member["status"]
        for member in final.metadata["collaboration"]["members"]
    ] == ["failed", "completed"]
    assert "有效结果" in final.content[0]["text"]


@pytest.mark.asyncio
async def test_collaboration_retries_terminal_empty_model_output_once(
    monkeypatch,
):
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {"id": "agent_a", "name": "甲", "enabled": True},
                {"id": "agent_b", "name": "乙", "enabled": True},
            ],
        },
    )
    submissions = {"agent_a": 0, "agent_b": 0}

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        submissions[to_agent] += 1
        return {"task_id": f"task-{to_agent}-{submissions[to_agent]}"}

    def fake_status(_base_url, task_id, **_kwargs):
        if task_id == "task-agent_a-1":
            return {
                "status": "failed",
                "error": {
                    "message": "模型输出在正文完成前被截断",
                },
            }
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {"content": [{"type": "text", "text": "有效结果"}]},
                ],
            },
        }

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(agent_management, "get_agent_chat_task_status", fake_status)

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {"to_agent": "agent_a", "text": "私有 A", "label": "研究"},
                {"to_agent": "agent_b", "text": "私有 B", "label": "复核"},
            ],
        )
    ]

    snapshots = [
        update.metadata["collaboration"]
        for update in updates
        if update.metadata and update.metadata.get("collaboration")
    ]
    assert submissions == {"agent_a": 2, "agent_b": 1}
    assert any(
        snapshot["members"][0]["status"] == "retrying"
        for snapshot in snapshots
    )
    assert snapshots[-1]["status"] == "completed"
    assert [member["attempt"] for member in snapshots[-1]["members"]] == [2, 1]


@pytest.mark.asyncio
async def test_collaboration_retries_local_socket_disappearance_once(
    monkeypatch,
):
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {"id": "agent_a", "name": "甲", "enabled": True},
                {"id": "agent_b", "name": "乙", "enabled": True},
            ],
        },
    )
    submissions = {"agent_a": 0, "agent_b": 0}
    cancellations = []

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        submissions[to_agent] += 1
        return {"task_id": f"task-{to_agent}-{submissions[to_agent]}"}

    def fake_status(_base_url, task_id, **_kwargs):
        if task_id == "task-agent_a-1":
            raise FileNotFoundError(errno.ENOENT, "local API socket disappeared")
        return {
            "status": "finished",
            "result": {
                "status": "completed",
                "output": [
                    {"content": [{"type": "text", "text": "有效结果"}]},
                ],
            },
        }

    def fake_cancel(_base_url, task_id, **_kwargs):
        cancellations.append(task_id)
        return {"status": "cancelled"}

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(agent_management, "get_agent_chat_task_status", fake_status)
    monkeypatch.setattr(agent_management, "cancel_agent_chat_task", fake_cancel)

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {"to_agent": "agent_a", "text": "私有 A", "label": "研究"},
                {"to_agent": "agent_b", "text": "私有 B", "label": "复核"},
            ],
        )
    ]

    snapshots = [
        update.metadata["collaboration"]
        for update in updates
        if update.metadata and update.metadata.get("collaboration")
    ]
    assert submissions == {"agent_a": 2, "agent_b": 1}
    assert cancellations == ["task-agent_a-1"]
    assert any(
        snapshot["members"][0]["status"] == "retrying"
        for snapshot in snapshots
    )
    assert snapshots[-1]["status"] == "completed"
    assert [member["attempt"] for member in snapshots[-1]["members"]] == [2, 1]


@pytest.mark.asyncio
async def test_collaboration_close_cancels_all_remote_members(monkeypatch):
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {"id": "agent_a", "name": "甲", "enabled": True},
                {"id": "agent_b", "name": "乙", "enabled": True},
            ],
        },
    )
    submitted: list[str] = []
    cancelled: list[str] = []

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        submitted.append(to_agent)
        return {"task_id": f"task-{to_agent}"}

    monkeypatch.setattr(
        agent_management,
        "submit_agent_chat_task",
        fake_submit,
    )
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        lambda *_args, **_kwargs: {"status": "running"},
    )
    monkeypatch.setattr(
        agent_management,
        "cancel_agent_chat_task",
        lambda _base_url, task_id, **_kwargs: (
            cancelled.append(task_id) or {"status": "cancelled"}
        ),
    )

    collaboration = agent_management.collaborate_with_agents(
        [
            {"to_agent": "agent_a", "text": "私有 A", "label": "研究"},
            {"to_agent": "agent_b", "text": "私有 B", "label": "复核"},
        ],
    )
    await anext(collaboration)
    while len(submitted) < 2:
        await asyncio.wait_for(anext(collaboration), timeout=2)

    await collaboration.aclose()

    assert sorted(cancelled) == ["task-agent_a", "task-agent_b"]


@pytest.mark.asyncio
async def test_collaboration_hard_timeout_does_not_replay_the_same_member_run(
    monkeypatch,
):
    monkeypatch.setattr(
        agent_management,
        "list_agents_data",
        lambda _base_url=None: {
            "agents": [
                {"id": "agent_a", "name": "甲", "enabled": True},
                {"id": "agent_b", "name": "乙", "enabled": True},
            ],
        },
    )
    monkeypatch.setattr(
        agent_management,
        "COLLABORATION_MEMBER_TIMEOUT_SECONDS",
        0,
    )
    submissions = {"agent_a": 0, "agent_b": 0}
    cancelled: list[str] = []

    def fake_submit(_base_url, _payload, to_agent, _timeout):
        submissions[to_agent] += 1
        return {"task_id": f"task-{to_agent}-{submissions[to_agent]}"}

    monkeypatch.setattr(agent_management, "submit_agent_chat_task", fake_submit)
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        lambda *_args, **_kwargs: {"status": "running"},
    )
    monkeypatch.setattr(
        agent_management,
        "cancel_agent_chat_task",
        lambda _base_url, task_id, **_kwargs: (
            cancelled.append(task_id) or {"status": "cancelled"}
        ),
    )

    updates = [
        update
        async for update in agent_management.collaborate_with_agents(
            [
                {"to_agent": "agent_a", "text": "私有 A", "label": "研究"},
                {"to_agent": "agent_b", "text": "私有 B", "label": "复核"},
            ],
        )
    ]
    final = updates[-1]

    assert submissions == {"agent_a": 1, "agent_b": 1}
    assert sorted(cancelled) == [
        "task-agent_a-1",
        "task-agent_b-1",
    ]
    assert final.metadata["collaboration"]["status"] == "failed"
    assert [
        member["attempt"]
        for member in final.metadata["collaboration"]["members"]
    ] == [1, 1]


async def test_check_agent_task_formats_finished_background_result(
    monkeypatch,
):
    monkeypatch.setattr(
        agent_management,
        "get_agent_chat_task_status",
        lambda *_args, **_kwargs: {
            "status": "finished",
            "result": {
                "status": "completed",
                "session_id": "sid-1",
                "output": [
                    {
                        "content": [
                            {"type": "text", "text": "Background reply"},
                        ],
                    },
                ],
            },
        },
    )

    response = await agent_management.check_agent_task("task-1")

    text = response.content[0].get("text", "")
    assert "[TASK_ID: task-1]" in text
    assert "Background reply" in text


async def test_chat_with_agent_uses_owned_async_stream_for_final_mode(
    monkeypatch,
):
    async def collect(*args, **kwargs):
        return {
            "output": [
                {
                    "content": [
                        {"type": "text", "text": "reply from peer"},
                    ],
                },
            ],
        }

    monkeypatch.setattr(agent_management, "collect_final_agent_chat_response_async", collect)
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(agent_management.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        agent_management,
        "resolve_calling_agent_id",
        lambda _from_agent=None: "auto_bot",
    )
    monkeypatch.setattr(
        agent_management,
        "agent_exists",
        lambda _to_agent, _base_url=None: True,
    )

    response = await agent_management.chat_with_agent(
        to_agent="bot_b",
        text="Need help",
    )

    assert calls
    assert all(item[0] is not agent_management.collect_final_agent_chat_response for item in calls)
    assert "reply from peer" in response.content[0].get("text", "")


async def test_chat_with_agent_normalizes_agent_ids(monkeypatch):
    captured = {}

    async def fake_collect_final(_base_url, request_payload, to_agent, _timeout):
        captured["to_agent"] = to_agent
        captured["session_id"] = request_payload["session_id"]
        captured["text"] = request_payload["input"][0]["content"][0]["text"]
        return {
            "output": [
                {
                    "content": [
                        {"type": "text", "text": "reply from peer"},
                    ],
                },
            ],
        }

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(
        agent_management,
        "collect_final_agent_chat_response_async",
        fake_collect_final,
    )
    monkeypatch.setattr(agent_management.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        agent_management,
        "agent_exists",
        lambda _to_agent, _base_url=None: True,
    )
    monkeypatch.setattr(
        agent_management,
        "resolve_calling_agent_id",
        lambda _from_agent=None: "bot_a",
    )

    response = await agent_management.chat_with_agent(
        to_agent='  "bot_b"  ',
        text="Need help",
    )

    assert captured["to_agent"] == "bot_b"
    assert captured["session_id"].startswith("bot_a:to:bot_b:")
    assert captured["text"].startswith("[Agent bot_a requesting] ")
    assert "reply from peer" in response.content[0].get("text", "")


async def test_chat_with_agent_returns_clear_error_when_agent_missing(
    monkeypatch,
):
    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(agent_management.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        agent_management,
        "agent_exists",
        lambda _to_agent, _base_url=None: False,
    )

    response = await agent_management.chat_with_agent(
        to_agent='  "missing_bot"  ',
        text="Need help",
    )

    assert (
        response.content[0].get("text", "") == "Agent [missing_bot] not exists"
    )


async def test_missing_agent_is_failed_tool_result(monkeypatch):
    monkeypatch.setattr(agent_management, "agent_exists", lambda *_args: False)
    response = await agent_management.chat_with_agent("missing", "help")
    assert response.is_error


async def test_cancelled_foreground_agent_chat_closes_stream(monkeypatch):
    import asyncio
    from contextlib import suppress

    received, disconnected = asyncio.Event(), asyncio.Event()
    writers = []
    handlers = []
    async def handler(reader, writer):
        handlers.append(asyncio.current_task())
        writers.append(writer)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:"))
            await reader.readexactly(length)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n: ready\n\n")
            await writer.drain()
            received.set()
            await reader.read()
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(agent_management, "read_last_api", lambda: ("127.0.0.1", port))
    monkeypatch.setattr(agent_management, "agent_exists", lambda *_: True)
    monkeypatch.setattr(agent_management, "resolve_calling_agent_id", lambda *_: "source")
    call = asyncio.create_task(agent_management.chat_with_agent("target", "synthetic probe", timeout=2))
    try:
        await asyncio.wait_for(received.wait(), 2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(disconnected.wait(), 0.3)
    finally:
        call.cancel()
        with suppress(asyncio.CancelledError):
            await call
        for writer in writers:
            writer.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
def test_terminal_agent_failure_cannot_be_hidden_by_prior_text(monkeypatch, status):
    fake_client = _FakeClient(stream_response=_FakeResponse(
            lines=[
                'data: {"object":"message","type":"message","role":"assistant","status":"completed","content":[{"type":"text","text":"partial"}]}',
                "data: "
                + json.dumps(
                    {"object": "response", "status": status, "output": []}),
    ]))
    monkeypatch.setattr(agent_management, "create_agent_api_client", lambda *_: fake_client)
    with pytest.raises(RuntimeError):
        agent_management.collect_final_agent_chat_response(None, {"session_id": "test"}, "target", 1)
