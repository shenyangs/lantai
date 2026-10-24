# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from lantai.app.beta_access.admission import BetaAccessAdmissionGuard
from lantai.app.beta_access.wiring import register_beta_access_runtime
from lantai.app.channels.console.channel import ConsoleChannel
from lantai.app.multi_agent_manager import MultiAgentManager
from lantai.app.runner.manager import ChatManager
from lantai.app.runner.repo.json_repo import JsonChatRepository
from lantai.app.runner.task_tracker import TaskTracker
from lantai.app.runtime_tasks.schemas import CreateRuntimeTaskRequest
from lantai.app.runtime_tasks.service import (
    create_runtime_task,
    start_runtime_task,
)
from lantai.security.beta_access import AccessSnapshot, BetaAccessStatus


def _blocked_guard() -> BetaAccessAdmissionGuard:
    return _guard_for(BetaAccessStatus.NOT_ACTIVATED)


def _guard_for(status: BetaAccessStatus) -> BetaAccessAdmissionGuard:
    return BetaAccessAdmissionGuard(
        lambda: AccessSnapshot(
            True,
            status,
            "account-1",
            1,
            7,
        ),
    )


def test_private_beta_router_is_registered_before_fallback_routes() -> None:
    beta_router = APIRouter(prefix="/beta-access")

    @beta_router.get("/status")
    async def status() -> dict[str, bool]:
        return {"enabled": True}

    app = FastAPI()
    runtime = SimpleNamespace(
        enabled=True,
        router=beta_router,
        admission_guard=_blocked_guard(),
    )

    register_beta_access_runtime(app, runtime)

    assert app.state.beta_access_runtime is runtime
    response = TestClient(app).get("/api/beta-access/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": True}


def test_non_beta_router_registration_preserves_existing_routes() -> None:
    app = FastAPI()
    runtime = SimpleNamespace(
        enabled=False,
        router=None,
        admission_guard=BetaAccessAdmissionGuard(AccessSnapshot.unrestricted),
    )

    register_beta_access_runtime(app, runtime)

    assert app.state.beta_access_runtime is runtime
    assert TestClient(app).get("/api/beta-access/status").status_code == 404


@pytest.mark.asyncio
async def test_task_creation_is_denied_before_workspace_mutation(
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        beta_access_admission_guard=_blocked_guard(),
    )
    body = CreateRuntimeTaskRequest(
        client_task_id="client-1",
        objective="Start a new task",
    )

    with pytest.raises(HTTPException) as raised:
        await create_runtime_task(workspace, body)

    assert raised.value.status_code == 403
    assert raised.value.detail == {
        "code": "beta_access_denied",
        "reason": "beta_access_not_activated:work_start",
        "snapshot_revision": 7,
    }
    assert not (tmp_path / "topic-workspaces").exists()


@pytest.mark.asyncio
async def test_task_start_is_denied_before_chat_or_runtime_lookup(
    tmp_path,
) -> None:
    class _ChatManager:
        async def list_chats(self):
            pytest.fail("denied task start must not inspect or launch a task")

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        beta_access_admission_guard=_blocked_guard(),
        chat_manager=_ChatManager(),
    )

    with pytest.raises(HTTPException) as raised:
        await start_runtime_task(
            workspace,
            SimpleNamespace(),
            "run-beta-denied",
        )

    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "beta_access_denied"
    assert raised.value.detail["snapshot_revision"] == 7


@pytest.mark.parametrize(
    ("status", "command"),
    [
        (BetaAccessStatus.EXPIRED, "/stop"),
        (BetaAccessStatus.REVOKED, "/stop"),
        (BetaAccessStatus.EXPIRED, "/proactive off"),
        (BetaAccessStatus.REVOKED, "/proactive off"),
    ],
)
def test_console_http_rescue_commands_reach_the_command_pipeline_when_blocked(
    monkeypatch,
    tmp_path,
    status: BetaAccessStatus,
    command: str,
) -> None:
    from lantai.app.routers import console as console_router

    processed: list[str] = []

    async def process(request):
        text = request.input[-1].content[-1].text
        processed.append(text)
        for _never in ():
            yield ""  # pragma: no cover

    channel = ConsoleChannel(
        process=process,
        enabled=True,
        bot_prefix="",
        workspace_dir=tmp_path,
    )

    class _ChannelManager:
        async def get_channel(self, name: str):
            return channel if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=ChatManager(
            repo=JsonChatRepository(tmp_path / "chats.json"),
        ),
        task_tracker=TaskTracker(),
        channel_manager=_ChannelManager(),
        beta_access_admission_guard=_guard_for(status),
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )
    app = FastAPI()
    app.include_router(console_router.router)

    response = TestClient(app).post(
        "/console/chat",
        json={
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": command}],
                },
            ],
            "session_id": f"rescue-{status.value}-{command.split()[0][1:]}",
            "user_id": "default",
            "channel": "console",
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert processed == [command]


@pytest.mark.parametrize(
    "text",
    [
        "start new work",
        "/proactive on",
        "/proactive 5",
        "/proactive off extra",
        "/stop-now",
    ],
)
def test_console_http_normal_turn_remains_blocked_before_command_pipeline(
    monkeypatch,
    tmp_path,
    text: str,
) -> None:
    from lantai.app.routers import console as console_router

    async def fail_process(_request):
        pytest.fail(
            "a blocked normal turn must not reach the command pipeline",
        )
        for _never in ():
            yield ""  # pragma: no cover

    channel = ConsoleChannel(
        process=fail_process,
        enabled=True,
        bot_prefix="",
        workspace_dir=tmp_path,
    )

    class _ChannelManager:
        async def get_channel(self, name: str):
            return channel if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=ChatManager(
            repo=JsonChatRepository(tmp_path / "chats.json"),
        ),
        task_tracker=TaskTracker(),
        channel_manager=_ChannelManager(),
        beta_access_admission_guard=_guard_for(BetaAccessStatus.EXPIRED),
    )

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )
    app = FastAPI()
    app.include_router(console_router.router)

    response = TestClient(app).post(
        "/console/chat",
        json={
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            ],
            "session_id": "blocked-normal-turn",
            "user_id": "default",
            "channel": "console",
            "stream": True,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "beta_access_denied",
        "reason": "beta_access_expired:work_start",
        "snapshot_revision": 7,
    }


@pytest.mark.asyncio
async def test_manager_reload_preserves_beta_admission_guard(
    monkeypatch,
    tmp_path,
) -> None:
    guard = _guard_for(BetaAccessStatus.REVOKED)
    captured: dict[str, object] = {}
    stopped = []

    class _Services:
        services = {}

        def get_reusable_services(self):
            return {}

    class _Tracker:
        async def has_active_tasks(self):
            return False

        async def has_unsettled_tasks(self):
            return False

    class _Workspace:
        def __init__(self, *_args, **kwargs):
            captured.update(kwargs)
            self.agent_id = kwargs.get("agent_id", "default")
            self._service_manager = _Services()
            self.task_tracker = _Tracker()

        async def start(self):
            return None

        async def stop(self, final=False):
            del final
            stopped.append(self)
            return None

        def set_manager(self, _manager):
            return None

        async def set_reusable_components(self, _reusable):
            return None

    old_workspace = _Workspace()
    captured.clear()
    monkeypatch.setattr(
        "lantai.app.multi_agent_manager.load_config",
        lambda: SimpleNamespace(
            agents=SimpleNamespace(
                profiles={
                    "default": SimpleNamespace(workspace_dir=str(tmp_path)),
                },
            ),
        ),
    )
    monkeypatch.setattr(
        "lantai.app.multi_agent_manager.should_defer_desktop_workspace_io",
        lambda _path: False,
    )
    monkeypatch.setattr(
        "lantai.app.multi_agent_manager.Workspace",
        _Workspace,
    )
    manager = MultiAgentManager(beta_access_admission_guard=guard)
    manager.agents["default"] = old_workspace

    assert await manager.reload_agent("default") is True
    assert captured["beta_access_admission_guard"] is guard
    assert stopped == [old_workspace]
