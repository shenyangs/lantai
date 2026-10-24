# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name
"""Regression tests for chat update semantics."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.runner import api as chat_api_module
from lantai.app.runner import manager as chat_manager_module
from lantai.app.runner.api import (
    get_chat_manager,
    get_session,
    get_workspace,
    get_workspace_grant_registry,
    router,
    stream_chat_runtime_events,
)
from lantai.app.runner.manager import ChatManager
from lantai.app.runner.models import ChatSpec, ChatUpdate
from lantai.app.experience import (
    append_chat_event,
    runtime_event_stream_generation,
    wait_for_runtime_event_append,
)
from lantai.app.runner.repo.json_repo import JsonChatRepository
from lantai.app.workspace_grants import (
    WorkspaceGrantRegistry,
    create_workspace_selection_receipt,
)
from lantai.analysis import AnalysisError, AnalysisService
from lantai.agents.tools.presentation import _store_analysis_validation


_TRANSACTION_WRITER_SCRIPT = """
import asyncio
import sys
import time
from pathlib import Path

from lantai.app.runner.models import ChatSpec
from lantai.app.runner.repo.json_repo import JsonChatRepository

repo_path, chat_id, ready_path, entered_path, release_path = sys.argv[1:]

async def write():
    repo = JsonChatRepository(repo_path)
    Path(ready_path).touch()
    async with repo.transaction():
        chats_file = await repo.load()
        Path(entered_path).touch()
        deadline = time.monotonic() + 10
        while not Path(release_path).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("transaction release was not signalled")
            await asyncio.sleep(0.01)
        chats_file.chats.append(ChatSpec(
            id=chat_id,
            name=chat_id,
            session_id=f"console:{chat_id}",
            user_id="default",
            channel="console",
        ))
        await repo.save(chats_file)

asyncio.run(write())
"""


def _wait_for_path(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()


class FakeTaskTracker:
    """Minimal task tracker for chat API tests."""

    def __init__(self, active_chat_ids: list[str] | None = None) -> None:
        self.active_chat_ids = list(active_chat_ids or [])
        self.snapshots: dict[str, dict] = {}

    async def get_status(self, chat_id: str) -> str:
        return "running" if chat_id in self.snapshots else "idle"

    async def get_snapshot(self, chat_id: str) -> dict | None:
        return self.snapshots.get(chat_id)

    async def list_active_tasks(self) -> list[str]:
        return list(self.active_chat_ids)


class FakeWorkspace:
    """Workspace dependency carrying only the fields the route needs."""

    def __init__(self, workspace_dir: Path):
        self.agent_id = "default"
        self.task_tracker = FakeTaskTracker()
        self.workspace_dir = workspace_dir


class FakeSession:
    """Session dependency with empty persisted agent state."""

    def __init__(self) -> None:
        self.state: dict = {}
        self.states_by_session: dict[str, dict] = {}
        self.read_count = 0
        self.read_session_ids: list[str] = []
        self.saved_updates: list[dict] = []

    async def get_session_state_dict(
        self,
        session_id: str,
        _user_id: str,
        _channel: str,
        **_kwargs,
    ) -> dict:
        self.read_count += 1
        self.read_session_ids.append(session_id)
        return self.states_by_session.get(session_id, self.state)

    async def update_session_state(
        self,
        session_id: str,
        key: str,
        value: object,
        user_id: str = "",
        channel: str = "",
        **_kwargs,
    ) -> None:
        state = self.states_by_session.setdefault(session_id, {})
        state[key] = value
        self.saved_updates.append(
            {
                "session_id": session_id,
                "key": key,
                "value": value,
                "user_id": user_id,
                "channel": channel,
            }
        )


@pytest.fixture
def chat_manager(tmp_path: Path) -> ChatManager:
    """Create a chat manager backed by a temporary chats.json file."""
    return ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def api_client(
    chat_manager: ChatManager, fake_session: FakeSession
) -> AsyncClient:
    """Create an isolated chat API client."""
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_chat_manager] = lambda: chat_manager
    app.dependency_overrides[get_workspace] = lambda: FakeWorkspace(
        chat_manager._repo.path.parent,
    )
    app.dependency_overrides[get_session] = lambda: fake_session
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _seed_chat(chat_manager: ChatManager) -> ChatSpec:
    """Create a representative persisted chat."""
    spec = ChatSpec(
        id="chat-1",
        name="Old Chat",
        session_id="console:default",
        user_id="default",
        channel="console",
        meta={"source": "test"},
    )
    return await chat_manager.create_chat(spec)


async def _seed_legacy_chat(
    chat_manager: ChatManager,
    spec: ChatSpec,
) -> ChatSpec:
    """Persist a pre-owner chat row without passing through new-chat logic."""
    await chat_manager._repo.upsert_chat(spec)
    return spec


async def test_delete_chat_is_idempotent(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    chat = await _seed_chat(chat_manager)

    async with api_client:
        first = await api_client.delete(f"/api/chats/{chat.id}")
        repeated = await api_client.delete(f"/api/chats/{chat.id}")

    assert first.status_code == 200
    assert first.json() == {"deleted": True}
    assert repeated.status_code == 200
    assert repeated.json() == {"deleted": False}


async def test_analysis_chart_reference_reads_only_bound_chat_result(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = chat_manager._repo.path.parent
    owner = await chat_manager.create_chat(ChatSpec(
        id="chart-owner", name="Chart", session_id="console:default",
        user_id="default", channel="console",
    ))
    other = await chat_manager.create_chat(ChatSpec(
        id="chart-other", name="Other", session_id="console:default",
        user_id="default", channel="console",
    ))
    source = home / "chart-sales.csv"
    source.write_text("region,amount\nEast,10.20\nWest,5.30\n", encoding="utf-8")
    service = AnalysisService(home, scope=owner.meta["session_state_id"])
    dataset = service.ingest(source)
    result = service.run(dataset["dataset_id"], {
        "group_by": ["region"],
        "metrics": [{"column": "amount", "op": "sum", "as": "revenue"}],
        "sort": [{"column": "region"}],
    })
    path = f"/api/chats/{{}}/analysis/{result['analysis_id']}/chart"
    params = {"x_column": "region", "y_column": "revenue"}
    payload_ready = threading.Event()
    release_payload = threading.Event()
    original_payload = chat_api_module._analysis_chart_payload

    def delayed_payload(*args, **kwargs):
        payload = original_payload(*args, **kwargs)
        payload_ready.set()
        assert release_payload.wait(5), "chart lookup did not resume"
        return payload

    async with api_client:
        chart = await api_client.get(path.format(owner.id), params=params)
        unrelated = await api_client.get(path.format(other.id), params=params)
        invalid = await api_client.get(path.format(owner.id), params={"x_column": "region", "y_column": "fake"})
        source.write_text("region,amount\nEast,99\n", encoding="utf-8")
        older = await api_client.get(path.format(owner.id), params=params)
        monkeypatch.setattr(chat_api_module, "_analysis_chart_payload", delayed_payload)
        in_flight = asyncio.create_task(api_client.get(
            path.format(owner.id), params=params,
        ))
        assert await asyncio.to_thread(payload_ready.wait, 5)
        deleted = await api_client.delete(f"/api/chats/{owner.id}")
        release_payload.set()
        deleted_in_flight = await in_flight
        retired = await api_client.get(path.format(owner.id), params=params)

    assert chart.status_code == 200
    assert chart.json()["points"] == [
        {"label": "East", "value": "10.20"},
        {"label": "West", "value": "5.30"},
    ]
    assert chart.json()["analysis_id"] == result["analysis_id"]
    assert chart.json()["source_sha256"] == dataset["source_sha256"]
    assert chart.json()["semantic_review"] == "pending"
    assert unrelated.status_code == 404
    assert invalid.status_code == 400

    assert older.json()["points"] == chart.json()["points"]
    assert older.json()["source_status"] == "changed"
    assert deleted.json() == {"deleted": True}
    assert deleted_in_flight.status_code == 404
    assert retired.status_code == 404


async def test_analysis_chart_requires_current_source_authorization(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = chat_manager._repo.path.parent
    project_root = tmp_path.parent / f"{tmp_path.name}-external-project"
    project_root.mkdir()
    source = project_root / "sales.csv"
    source.write_text("region,amount\nEast,10\n", encoding="utf-8")
    now = datetime.now(timezone.utc)
    registry = WorkspaceGrantRegistry(
        tmp_path / "managed" / "workspace-grants.json",
        clock=lambda: now,
    )
    grant = registry.claim_selection(
        create_workspace_selection_receipt(
            project_root=project_root,
            agent_id="default",
            desktop_token="desktop-secret",
            issued_at=now,
            nonce="chart-source-selection",
        ),
        desktop_token="desktop-secret",
        agent_id="default",
    )
    api_client._transport.app.dependency_overrides[  # type: ignore[attr-defined]
        get_workspace_grant_registry
    ] = lambda: registry
    owner = await chat_manager.create_chat(ChatSpec(
        id="authorized-chart", name="Authorized chart",
        session_id="console:default", user_id="default", channel="console",
        meta={"workspace_grant_id": grant.id},
    ))
    unbound = await chat_manager.create_chat(ChatSpec(
        id="unbound-chart", name="Unbound chart",
        session_id="console:default", user_id="default", channel="console",
    ))
    path = "/api/chats/{}/analysis/{}/chart"
    params = {"x_column": "region", "y_column": "revenue"}

    async def seed_result(chat: ChatSpec) -> str:
        service = AnalysisService(home, scope=chat.meta["session_state_id"])
        dataset = service.ingest(source)
        result = service.run(dataset["dataset_id"], {
            "group_by": ["region"],
            "metrics": [{"column": "amount", "op": "sum", "as": "revenue"}],
        })
        return result["analysis_id"]

    owner_analysis_id = await seed_result(owner)
    unbound_analysis_id = await seed_result(unbound)
    entered = threading.Event()
    release = threading.Event()
    original_payload = chat_api_module._analysis_chart_payload

    def delayed_payload(*args, **kwargs):
        entered.set()
        assert release.wait(5), "chart lookup did not resume"
        return original_payload(*args, **kwargs)

    async with api_client:
        permitted = await api_client.get(
            path.format(owner.id, owner_analysis_id), params=params,
        )
        outside_scope = await api_client.get(
            path.format(unbound.id, unbound_analysis_id), params=params,
        )
        monkeypatch.setattr(chat_api_module, "_analysis_chart_payload", delayed_payload)
        in_flight = asyncio.create_task(api_client.get(
            path.format(owner.id, owner_analysis_id), params=params,
        ))
        assert await asyncio.to_thread(entered.wait, 5)
        registry.revoke(grant.id, agent_id="default")
        release.set()
        revoked_in_flight = await in_flight
        revoked = await api_client.get(
            path.format(owner.id, owner_analysis_id), params=params,
        )

    assert permitted.status_code == 200
    assert permitted.json()["points"] == [{"label": "East", "value": "10"}]
    assert outside_scope.status_code == 404
    assert revoked_in_flight.status_code == 404
    assert revoked.status_code == 404


async def test_delete_chat_retires_only_its_analysis_in_topic_state_root(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    home = chat_manager._repo.path.parent
    first = await chat_manager.create_chat(ChatSpec(
        id="analysis-chat", name="Analysis", session_id="console:default",
        user_id="default", channel="console",
        meta={"topic_workspace_dir": "topic-workspaces/analysis-chat"},
    ))
    second = await chat_manager.create_chat(ChatSpec(
        id="other-chat", name="Other", session_id="console:default",
        user_id="default", channel="console",
        meta={"topic_workspace_dir": "topic-workspaces/analysis-chat"},
    ))
    state_root = home / "topic-workspaces" / "analysis-chat"
    state_root.mkdir(parents=True)
    source = home / "sales.csv"
    source.write_text("region,amount\nEast,10\n", encoding="utf-8")
    exported = home / "report.docx"
    exported.write_bytes(b"exported copy")
    first_scope = first.meta["session_state_id"]
    second_scope = second.meta["session_state_id"]
    first_service = AnalysisService(state_root, scope=first_scope)
    second_service = AnalysisService(state_root, scope=second_scope)
    first_dataset = first_service.ingest(source)
    second_dataset = second_service.ingest(source)
    _store_analysis_validation(state_root, "first-job", {
        "schema_version": 1, "session_id": first_scope,
    })
    _store_analysis_validation(state_root, "second-job", {
        "schema_version": 1, "session_id": second_scope,
    })

    async with api_client:
        response = await api_client.delete(f"/api/chats/{first.id}")
        repeated = await api_client.delete(f"/api/chats/{first.id}")

    assert response.json() == {"deleted": True}
    assert repeated.json() == {"deleted": False}
    assert not first_service.root.exists()
    with pytest.raises(AnalysisError, match="retired"):
        first_service.ingest(source)
    with pytest.raises(AnalysisError, match="retired"):
        _store_analysis_validation(state_root, "late-job", {
            "schema_version": 1, "session_id": first_scope,
        })
    assert not (state_root / "presentation-analysis-validations" / "late-job.json").exists()
    assert second_service.get_dataset(second_dataset["dataset_id"])["dataset_id"] == second_dataset["dataset_id"]
    assert source.exists() and exported.read_bytes() == b"exported copy"
    assert b"analysis_cleared" in (state_root / "presentation-analysis-validations" / "first-job.json").read_bytes()
    assert b"analysis_cleared" not in (state_root / "presentation-analysis-validations" / "second-job.json").read_bytes()
    assert await chat_manager.pending_analysis_cleanup_jobs() == []


async def test_failed_analysis_retirement_is_durable_and_retried_on_chat_list(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = chat_manager._repo.path.parent
    chat = await chat_manager.create_chat(ChatSpec(
        id="retry-cleanup", name="Delete me", session_id="console:default",
        user_id="default", channel="console",
    ))
    source = home / "retry-source.csv"
    source.write_text("amount\n7\n", encoding="utf-8")
    service = AnalysisService(home, scope=chat.meta["session_state_id"])
    service.ingest(source)
    original_retire = AnalysisService.retire
    failures = 0

    def fail_once(self: AnalysisService):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OSError("simulated interrupted cleanup")
        return original_retire(self)

    monkeypatch.setattr(AnalysisService, "retire", fail_once)
    async with api_client:
        deleted = await api_client.delete(f"/api/chats/{chat.id}")
        assert deleted.status_code == 500
        assert await chat_manager.get_chat(chat.id) is None
        assert service.root.exists()
        pending = await chat_manager.pending_analysis_cleanup_jobs()
        assert len(pending) == 1 and pending[0].chat_id == chat.id
        restarted_manager = ChatManager(repo=JsonChatRepository(chat_manager._repo.path))
        assert await restarted_manager.pending_analysis_cleanup_jobs() == pending
        assert "Delete me" not in chat_manager._repo.path.read_text(encoding="utf-8")
        api_client._transport.app.dependency_overrides[  # type: ignore[attr-defined]
            get_chat_manager
        ] = lambda: restarted_manager
        listed = await api_client.get("/api/chats", params={"include_blank": True})

    assert listed.status_code == 200
    assert not service.root.exists()
    assert await chat_manager.pending_analysis_cleanup_jobs() == []
    with pytest.raises(AnalysisError, match="retired"):
        service.ingest(source)
    assert source.exists()


async def test_batch_delete_retires_each_bound_analysis_scope(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    home = chat_manager._repo.path.parent
    source = home / "source.csv"
    source.write_text("amount\n7\n", encoding="utf-8")
    chats = [await chat_manager.create_chat(ChatSpec(
        id=f"batch-{index}", name=f"Batch {index}",
        session_id="console:default", user_id="default", channel="console",
    )) for index in range(2)]
    services = [AnalysisService(home, scope=chat.meta["session_state_id"]) for chat in chats]
    for service in services:
        service.ingest(source)

    async with api_client:
        response = await api_client.post(
            "/api/chats/batch-delete", json=[chat.id for chat in chats],
        )

    assert response.json() == {"deleted": True}
    for service in services:
        assert not service.root.exists()
        with pytest.raises(AnalysisError, match="retired"):
            service.ingest(source)
    assert source.exists()


async def test_delete_legacy_chat_does_not_guess_shared_transport_scope(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    home = chat_manager._repo.path.parent
    first = await _seed_legacy_chat(chat_manager, ChatSpec(
        id="legacy-one", name="Legacy one", session_id="shared-transport",
        user_id="user-one", channel="console",
    ))
    await _seed_legacy_chat(chat_manager, ChatSpec(
        id="legacy-two", name="Legacy two", session_id="shared-transport",
        user_id="user-two", channel="console",
    ))
    source = home / "legacy.csv"
    source.write_text("amount\n7\n", encoding="utf-8")
    shared = AnalysisService(home, scope="shared-transport")
    dataset = shared.ingest(source)

    async with api_client:
        response = await api_client.delete(f"/api/chats/{first.id}")

    assert response.json() == {"deleted": True}
    assert shared.get_dataset(dataset["dataset_id"])["dataset_id"] == dataset["dataset_id"]


async def test_chat_activity_lists_only_active_runs_without_loading_histories(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    active = await _seed_chat(chat_manager)
    await chat_manager.create_chat(
        ChatSpec(
            id="chat-idle",
            name="Idle Chat",
            session_id="console:idle",
            user_id="default",
            channel="console",
        )
    )
    workspace = FakeWorkspace(chat_manager._repo.path.parent)
    workspace.task_tracker.active_chat_ids = [active.id, "missing-chat"]
    api_client._transport.app.dependency_overrides[  # type: ignore[attr-defined]
        get_workspace
    ] = lambda: workspace

    async with api_client:
        response = await api_client.get("/api/chats/activity")

    assert response.status_code == 200
    assert response.json() == [
        {"id": active.id, "name": active.name, "status": "running"}
    ]


async def test_runtime_event_stream_disables_intermediary_buffering(
    chat_manager: ChatManager,
) -> None:
    chat = await _seed_chat(chat_manager)
    await chat_manager.merge_chat_meta(
        chat.id,
        {"topic_workspace_dir": "runtime-events"},
    )

    response = await stream_chat_runtime_events(
        request=SimpleNamespace(headers={}),
        chat_id=chat.id,
        after_seq=0,
        stream_epoch=None,
        mgr=chat_manager,
        workspace=FakeWorkspace(chat_manager._repo.path.parent),
    )

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"


async def test_runtime_event_stream_emits_epoch_resync_once(
    chat_manager: ChatManager,
) -> None:
    workspace_dir = chat_manager._repo.path.parent
    topic_workspace = workspace_dir / "topic-workspaces" / "chat-resync"
    topic_workspace.mkdir(parents=True)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="chat-resync",
            name="Resync chat",
            session_id="console:resync",
            user_id="default",
            channel="console",
            meta={"topic_workspace_dir": str(topic_workspace)},
        )
    )
    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-resync",
        kind="chat.turn.started",
        status="running",
        summary="started",
    )

    class DisconnectedRequest:
        headers = {"last-event-id": "stale-epoch:99"}

        async def is_disconnected(self) -> bool:
            return True

    response = await stream_chat_runtime_events(
        request=DisconnectedRequest(),
        chat_id=chat.id,
        after_seq=0,
        stream_epoch=None,
        mgr=chat_manager,
        workspace=FakeWorkspace(workspace_dir),
    )

    first_chunk = await anext(response.body_iterator)
    assert '"resync_required": true' in first_chunk
    assert '"resync_reason": "stream_epoch_mismatch"' in first_chunk
    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)


async def test_runtime_event_stream_emits_empty_epoch_resync_once(
    chat_manager: ChatManager,
) -> None:
    workspace_dir = chat_manager._repo.path.parent
    topic_workspace = workspace_dir / "topic-workspaces" / "chat-empty-resync"
    topic_workspace.mkdir(parents=True)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="chat-empty-resync",
            name="Empty resync chat",
            session_id="console:empty-resync",
            user_id="default",
            channel="console",
            meta={"topic_workspace_dir": str(topic_workspace)},
        )
    )

    class DisconnectedRequest:
        headers = {"last-event-id": "stale-epoch:99"}

        async def is_disconnected(self) -> bool:
            return True

    response = await stream_chat_runtime_events(
        request=DisconnectedRequest(),
        chat_id=chat.id,
        after_seq=0,
        stream_epoch=None,
        mgr=chat_manager,
        workspace=FakeWorkspace(workspace_dir),
    )

    first_chunk = await anext(response.body_iterator)
    assert '"resync_required": true' in first_chunk
    assert '"events": []' in first_chunk
    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)


async def test_runtime_event_stream_advances_past_fully_filtered_page(
    chat_manager: ChatManager,
    monkeypatch,
) -> None:
    """A private-only page must not be fetched forever at the same cursor."""

    workspace_dir = chat_manager._repo.path.parent
    topic_workspace = workspace_dir / "topic-workspaces" / "chat-filtered-page"
    topic_workspace.mkdir(parents=True)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="chat-filtered-page",
            name="Filtered page chat",
            session_id="console:filtered-page",
            user_id="default",
            channel="console",
            meta={"topic_workspace_dir": str(topic_workspace)},
        )
    )
    for index in range(251):
        await append_chat_event(
            workspace_dir=topic_workspace,
            agent_id="default",
            chat_id=chat.id,
            session_id=chat.session_id,
            run_id="run-filtered-page",
            kind="chat.turn.started",
            status="running",
            summary=f"event-{index + 1}",
            payload={},
        )

    class FilteringProjector:
        def project(self, event):
            if event.seq <= 250:
                return None
            return event

    monkeypatch.setattr(
        chat_api_module,
        "PublicReasoningProjector",
        FilteringProjector,
    )
    original_builder = chat_api_module.build_runtime_events_response
    fetched_cursors: list[int] = []

    def tracking_builder(*args, **kwargs):
        fetched_cursors.append(int(kwargs.get("after_seq", 0)))
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(
        chat_api_module,
        "build_runtime_events_response",
        tracking_builder,
    )

    class DisconnectedRequest:
        headers: dict[str, str] = {}

        async def is_disconnected(self) -> bool:
            return True

    response = await stream_chat_runtime_events(
        request=DisconnectedRequest(),
        chat_id=chat.id,
        after_seq=0,
        stream_epoch=None,
        mgr=chat_manager,
        workspace=FakeWorkspace(workspace_dir),
    )

    first_chunk = await anext(response.body_iterator)
    assert "event-251" in first_chunk
    assert "event-250" not in first_chunk
    assert fetched_cursors == [0, 250]
    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)


async def test_runtime_event_stream_heartbeat_reconciles_orphaned_pending_turn(
    chat_manager: ChatManager,
    monkeypatch,
) -> None:
    workspace_dir = chat_manager._repo.path.parent
    topic_workspace = workspace_dir / "topic-workspaces" / "chat-orphaned"
    topic_workspace.mkdir(parents=True)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="chat-orphaned",
            name="Orphaned chat",
            session_id="console:orphaned",
            user_id="default",
            channel="console",
            meta={
                "topic_workspace_dir": str(topic_workspace),
                "pending_turn": {
                    "text": "继续修第 11 页",
                    "run_id": "run-orphaned",
                    "status": "running",
                    "created_at": "2020-01-01T00:00:00Z",
                },
            },
        )
    )

    async def immediate_heartbeat(
        _workspace_dir,
        *,
        after_generation,
        timeout_seconds,
    ):
        del timeout_seconds
        return after_generation

    monkeypatch.setattr(
        chat_api_module,
        "wait_for_runtime_event_append",
        immediate_heartbeat,
    )

    class ConnectedRequest:
        headers: dict[str, str] = {}

        async def is_disconnected(self) -> bool:
            return False

    response = await stream_chat_runtime_events(
        request=ConnectedRequest(),
        chat_id=chat.id,
        after_seq=0,
        stream_epoch=None,
        mgr=chat_manager,
        workspace=FakeWorkspace(workspace_dir),
    )

    first_chunk = await anext(response.body_iterator)
    assert "turn.stalled" in first_chunk
    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    assert saved.meta["pending_turn"]["status"] == "stalled"
    await response.body_iterator.aclose()


async def test_runtime_event_stream_waiter_wakes_on_append(
    tmp_path: Path,
) -> None:
    generation = runtime_event_stream_generation(tmp_path)
    waiter = asyncio.create_task(
        wait_for_runtime_event_append(
            tmp_path,
            after_generation=generation,
            timeout_seconds=5,
        )
    )
    await asyncio.sleep(0)

    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="Lantai_Cola_Agent",
        chat_id="chat-signal",
        session_id="console:chat-signal",
        kind="chat.topic_workspace.bound",
        status="info",
        summary="workspace ready",
        payload={},
    )

    next_generation = await asyncio.wait_for(waiter, timeout=0.5)
    assert next_generation > generation


async def test_put_chat_accepts_partial_rename_payload(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """PUT rename should merge into the stored chat.

    This should not require callers to send a full ChatSpec.
    """
    chat = await _seed_chat(chat_manager)

    async with api_client:
        response = await api_client.put(
            f"/api/chats/{chat.id}",
            json={"name": "Renamed Chat"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == chat.id
    assert body["name"] == "Renamed Chat"
    assert body["session_id"] == chat.session_id
    assert body["user_id"] == chat.user_id
    assert body["channel"] == chat.channel

    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    assert saved.name == "Renamed Chat"
    assert saved.meta == {
        "source": "test",
        "session_state_id": f"console:chat:{chat.id}",
        "state_binding_version": 1,
        "state_migration_state": "bound",
    }


async def test_put_chat_can_archive_and_restore_a_chat(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    chat = await _seed_chat(chat_manager)

    async with api_client:
        archived = await api_client.put(
            f"/api/chats/{chat.id}",
            json={"archived": True},
        )
        active_list = await api_client.get("/api/chats")
        full_list = await api_client.get("/api/chats?include_archived=true")
        restored = await api_client.put(
            f"/api/chats/{chat.id}",
            json={"archived": False},
        )

    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert active_list.json() == []
    assert [item["id"] for item in full_list.json()] == [chat.id]
    assert restored.status_code == 200
    assert restored.json()["archived"] is False


async def test_list_hides_unstarted_default_chats_without_deleting_them(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    blank = await chat_manager.create_chat(
        ChatSpec(
            id="chat-blank",
            name="New Chat",
            session_id="console:blank",
            user_id="default",
            channel="console",
        ),
    )
    started = await chat_manager.create_chat(
        ChatSpec(
            id="chat-started",
            name="New Chat",
            session_id="console:started",
            user_id="default",
            channel="console",
            meta={"last_turn_text": "请继续"},
        ),
    )

    async with api_client:
        visible = await api_client.get("/api/chats")
        with_blank = await api_client.get("/api/chats?include_blank=true")

    assert [item["id"] for item in visible.json()] == [started.id]
    assert {item["id"] for item in with_blank.json()} == {
        blank.id,
        started.id,
    }
    assert await chat_manager.get_chat(blank.id) is not None


async def test_get_chat_includes_channel_context(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """GET history must preserve channel routing metadata for direct URL opens."""
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="wechat-chat-1",
            name="微信好友",
            session_id="wechat:friend-1",
            user_id="friend-1",
            channel="wechat",
            meta={"source": "wechat"},
            pinned=True,
        ),
    )

    async with api_client:
        response = await api_client.get(f"/api/chats/{chat.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["chat_id"] == chat.id
    assert body["name"] == "微信好友"
    assert body["session_id"] == "wechat:friend-1"
    assert body["user_id"] == "friend-1"
    assert body["channel"] == "wechat"
    assert body["meta"] == {
        "source": "wechat",
        "session_state_id": f"wechat:chat:{chat.id}",
        "state_binding_version": 1,
        "state_migration_state": "bound",
    }
    assert body["pinned"] is True
    assert body["runtime_state"] is None


async def test_get_chat_projects_completed_ququ_continuations_without_model_work(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """History restores saved child records in one batch with the chat."""

    record = {
        "continuation_id": "ququ-continuation:one",
        "request_id": "request-one",
        "agent_id": "default",
        "workspace_scope_id": "workspace:test",
        "chat_id": "chat-with-ququ",
        "session_id": "console:ququ",
        "user_id": "default",
        "channel": "console",
        "source_message_id": "assistant-one",
        "source_message_digest": "digest-one",
        "source_ququ_id": "ququ:one",
        "ququ_ordinal": 0,
        "placement": "echo",
        "text": "这是当时已经保存的同一段续念。",
        "status": "completed",
        "created_at": "2026-09-20T09:00:00+00:00",
        "updated_at": "2026-09-20T09:00:01+00:00",
    }
    await chat_manager.create_chat(
        ChatSpec(
            id="chat-with-ququ",
            name="带续念的会话",
            session_id="console:ququ",
            user_id="default",
            channel="console",
            meta={
                "_ququ_continuations_v1": [
                    record,
                    {
                        **record,
                        "continuation_id": "ququ-continuation:pending",
                        "source_ququ_id": "ququ:pending",
                        "status": "generating",
                        "text": "",
                        "claim_token": "private-token",
                    },
                ]
            },
        )
    )

    async with api_client:
        response = await api_client.get("/api/chats/chat-with-ququ")

    assert response.status_code == 200
    body = response.json()
    assert body["ququ_continuations"] == [record]
    assert "claim_token" not in body["ququ_continuations"][0]


async def test_general_chat_routes_keep_ququ_storage_server_owned(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """List/update hide claim state and create cannot seed private records."""

    private_records = [
        {
            "continuation_id": "ququ-continuation:private",
            "status": "generating",
            "claim_token": "server-secret",
        }
    ]
    await chat_manager.create_chat(
        ChatSpec(
            id="chat-private-ququ",
            name="已有蛐蛐续念",
            session_id="console:private-ququ",
            user_id="default",
            channel="console",
            meta={
                "visible": "kept",
                "_ququ_continuations_v1": private_records,
            },
        )
    )

    async with api_client:
        listed = await api_client.get("/api/chats")
        updated = await api_client.put(
            "/api/chats/chat-private-ququ",
            json={"name": "重命名后仍保留"},
        )
        created = await api_client.post(
            "/api/chats",
            json={
                "name": "不能伪造续念",
                "session_id": "console:forged-ququ",
                "user_id": "default",
                "channel": "console",
                "meta": {
                    "visible": "accepted",
                    "_ququ_continuations_v1": private_records,
                },
            },
        )

    assert listed.status_code == 200
    listed_chat = next(
        item for item in listed.json() if item["id"] == "chat-private-ququ"
    )
    assert listed_chat["meta"]["visible"] == "kept"
    assert "_ququ_continuations_v1" not in listed_chat["meta"]

    assert updated.status_code == 200
    assert updated.json()["meta"]["visible"] == "kept"
    assert "_ququ_continuations_v1" not in updated.json()["meta"]
    persisted = await chat_manager.get_chat("chat-private-ququ")
    assert persisted is not None
    assert persisted.meta["_ququ_continuations_v1"] == private_records

    assert created.status_code == 200
    assert "_ququ_continuations_v1" not in created.json()["meta"]
    created_persisted = await chat_manager.get_chat(created.json()["id"])
    assert created_persisted is not None
    assert "_ququ_continuations_v1" not in created_persisted.meta


async def test_topic_workspace_prefers_explicit_existing_chat_id(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """A direct route must not fork a second chat before session hydration."""
    chat = await _seed_chat(chat_manager)

    async with api_client:
        response = await api_client.post(
            "/api/chats/topic-workspace",
            json={
                "chat_id": chat.id,
                # This is the transient route id a fresh renderer may use
                # before it learns the chat's persisted session id.
                "session_id": chat.id,
                "user_id": "default",
                "channel": "console",
                "name": "新会话",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["chat"]["id"] == chat.id
    assert body["chat"]["session_id"] == "console:default"
    assert len(await chat_manager.list_chats()) == 1


async def test_topic_workspace_persists_valid_knowledge_source_binding(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    workspace_root = chat_manager._repo.path.parent
    source_path = workspace_root / ".wiki" / "raw" / "sources" / "budget.xlsx"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"workbook")

    async with api_client:
        response = await api_client.post(
            "/api/chats/topic-workspace",
            json={
                "session_id": "console:knowledge-budget",
                "user_id": "default",
                "channel": "console",
                "name": "关于 budget.xlsx",
                "knowledge_source": {
                    "path": ".wiki/raw/sources/budget.xlsx",
                    "filename": "budget.xlsx",
                    "kind": "office",
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["chat"]["meta"]["knowledge_source"] == {
        "path": ".wiki/raw/sources/budget.xlsx",
        "filename": "budget.xlsx",
        "kind": "office",
    }


async def test_topic_workspace_persists_multiple_exact_knowledge_sources(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    workspace_root = chat_manager._repo.path.parent
    references = []
    for filename in ("alpha.txt", "beta.txt"):
        source_path = workspace_root / ".wiki" / "raw" / "sources" / filename
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(filename, encoding="utf-8")
        references.append(
            {
                "path": f".wiki/raw/sources/{filename}",
                "filename": filename,
                "kind": "text",
            }
        )

    async with api_client:
        response = await api_client.post(
            "/api/chats/topic-workspace",
            json={
                "session_id": "console:knowledge-scope",
                "user_id": "default",
                "channel": "console",
                "name": "基于 2 份资料",
                "knowledge_sources": references,
                "knowledge_scope_mode": "exact",
            },
        )

    assert response.status_code == 200
    meta = response.json()["chat"]["meta"]
    assert meta["knowledge_sources"] == references
    assert meta["knowledge_scope_mode"] == "exact"
    assert meta["knowledge_source"] is None


async def test_topic_workspace_rejects_non_knowledge_workspace_files(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    workspace_root = chat_manager._repo.path.parent
    unrelated = workspace_root / "private-notes.txt"
    unrelated.write_text("not a knowledge source", encoding="utf-8")

    async with api_client:
        response = await api_client.post(
            "/api/chats/topic-workspace",
            json={
                "session_id": "console:invalid-knowledge-scope",
                "user_id": "default",
                "channel": "console",
                "knowledge_sources": [
                    {
                        "path": "private-notes.txt",
                        "filename": "private-notes.txt",
                    }
                ],
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid knowledge source"


async def test_topic_workspace_atomically_binds_grant_without_moving_state(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "user-projects" / "external-project"
    project_root.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    registry = WorkspaceGrantRegistry(
        tmp_path / "managed" / "workspace-grants.json",
        clock=lambda: now,
    )
    grant = registry.claim_selection(
        create_workspace_selection_receipt(
            project_root=project_root,
            agent_id="default",
            desktop_token="desktop-secret",
            issued_at=now,
            nonce="bind-selection-1",
        ),
        desktop_token="desktop-secret",
        agent_id="default",
    )
    api_client._transport.app.dependency_overrides[  # type: ignore[attr-defined]
        get_workspace_grant_registry
    ] = lambda: registry

    async with api_client:
        response = await api_client.post(
            "/api/chats/topic-workspace",
            headers={"X-Agent-Id": "default"},
            json={
                "session_id": "console:coding:grant-test",
                "user_id": "default",
                "channel": "console",
                "name": "Coding · external-project",
                "coding_mode": True,
                "workspace_grant_id": grant.id,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_grant_id"] == grant.id
    assert body["project_workspace_dir"] == str(project_root.resolve())
    assert body["state_workspace_dir"] == body["topic_workspace_dir"]
    assert Path(body["state_workspace_dir"]).is_relative_to(
        chat_manager._repo.path.parent,
    )
    assert not (project_root / ".lantai").exists()
    assert body["chat"]["meta"]["workspace_grant_id"] == grant.id
    assert body["chat"]["meta"]["session_state_id"] == (
        f"console:chat:{body['chat']['id']}"
    )
    assert "manual_workspace_dir" not in body["chat"]["meta"]

    follow_up_client = AsyncClient(
        transport=api_client._transport,  # type: ignore[attr-defined]
        base_url="http://test",
    )
    async with follow_up_client:
        follow_up = await follow_up_client.post(
            "/api/chats/topic-workspace",
            json={
                "chat_id": body["chat"]["id"],
                "session_id": body["chat"]["session_id"],
                "user_id": "default",
                "channel": "console",
            },
        )
    assert follow_up.status_code == 200
    assert follow_up.json()["workspace_grant_id"] == grant.id
    assert follow_up.json()["project_workspace_dir"] == str(
        project_root.resolve(),
    )


async def test_topic_workspace_accepts_desktop_grant_header_for_normal_chat(
    api_client: AsyncClient,
    tmp_path: Path,
) -> None:
    """Normal chat sends the active grant as a desktop header, not coding state."""
    project_root = tmp_path / "header-bound-project"
    project_root.mkdir()
    now = datetime.now(timezone.utc)
    registry = WorkspaceGrantRegistry(
        tmp_path / "managed" / "workspace-grants.json",
        clock=lambda: now,
    )
    grant = registry.claim_selection(
        create_workspace_selection_receipt(
            project_root=project_root,
            agent_id="default",
            desktop_token="desktop-secret",
            issued_at=now,
            nonce="header-bind-selection",
        ),
        desktop_token="desktop-secret",
        agent_id="default",
    )
    api_client._transport.app.dependency_overrides[  # type: ignore[attr-defined]
        get_workspace_grant_registry
    ] = lambda: registry

    async with api_client:
        response = await api_client.post(
            "/api/chats/topic-workspace",
            headers={
                "X-Agent-Id": "default",
                "X-Workspace-Grant-Id": grant.id,
            },
            json={
                "session_id": "console:normal:header-grant",
                "user_id": "default",
                "channel": "console",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_grant_id"] == grant.id
    assert body["project_workspace_dir"] == str(project_root.resolve())
    assert body["chat"]["meta"]["workspace_grant_id"] == grant.id


async def test_topic_workspace_accepts_read_only_grant_without_upgrading_it(
    api_client: AsyncClient,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "read-only-project"
    project_root.mkdir()
    now = datetime.now(timezone.utc)
    registry = WorkspaceGrantRegistry(
        tmp_path / "managed" / "workspace-grants.json",
        clock=lambda: now,
    )
    grant = registry.claim_selection(
        create_workspace_selection_receipt(
            project_root=project_root,
            agent_id="default",
            desktop_token="desktop-secret",
            issued_at=now,
            nonce="read-only-bind-selection",
            access_mode="read_only",
        ),
        desktop_token="desktop-secret",
        agent_id="default",
    )
    api_client._transport.app.dependency_overrides[  # type: ignore[attr-defined]
        get_workspace_grant_registry
    ] = lambda: registry

    async with api_client:
        response = await api_client.post(
            "/api/chats/topic-workspace",
            headers={"X-Agent-Id": "default"},
            json={
                "session_id": "console:read-only-grant",
                "user_id": "default",
                "channel": "console",
                "workspace_grant_id": grant.id,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_grant_id"] == grant.id
    assert body["workspace_access_mode"] == "read_only"
    assert body["project_workspace_dir"] == str(project_root.resolve())


async def test_legacy_external_raw_path_is_migrated_to_reauthorization_state(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    tmp_path: Path,
) -> None:
    external = tmp_path.parent / "legacy-external-project"
    external.mkdir(exist_ok=True)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="legacy-external-chat",
            name="旧 Coding 会话",
            session_id="console:coding:legacy-external",
            user_id="default",
            channel="console",
            meta={
                "coding_mode": True,
                "manual_workspace_dir": str(external),
                "coding_workspace_dir": str(external),
            },
        ),
    )

    async with api_client:
        response = await api_client.post(
            "/api/chats/topic-workspace",
            json={
                "chat_id": chat.id,
                "session_id": chat.session_id,
                "user_id": "default",
                "channel": "console",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["effective_workspace_dir"] == body["topic_workspace_dir"]
    assert body["workspace_reauthorization_required"] is True
    assert body["chat"]["meta"]["workspace_reauthorization_required"] is True
    assert (
        body["chat"]["meta"]["workspace_reauthorization_name"] == external.name
    )
    assert "manual_workspace_dir" not in body["chat"]["meta"]
    assert "coding_workspace_dir" not in body["chat"]["meta"]


async def test_invalid_grant_does_not_create_a_partial_chat(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    tmp_path: Path,
) -> None:
    registry = WorkspaceGrantRegistry(
        tmp_path / "managed" / "workspace-grants.json",
    )
    api_client._transport.app.dependency_overrides[  # type: ignore[attr-defined]
        get_workspace_grant_registry
    ] = lambda: registry

    async with api_client:
        response = await api_client.post(
            "/api/chats/topic-workspace",
            json={
                "session_id": "console:coding:invalid-grant",
                "user_id": "default",
                "channel": "console",
                "workspace_grant_id": "wsg_missing",
            },
        )

    assert response.status_code == 404
    assert await chat_manager.list_chats() == []


async def test_get_chat_reads_legacy_session_memory_state(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    fake_session: FakeSession,
) -> None:
    """Old persisted memory state must still render as chat history."""
    from lantai.runtime.converters.legacy_messages import LegacyMsg

    chat = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-memory-chat",
            name="旧会话",
            session_id="console:legacy-memory",
            user_id="default",
            channel="console",
        ),
    )
    user_msg = LegacyMsg("user", "旧问题", "user")
    assistant_msg = LegacyMsg(
        "Friday",
        [{"type": "text", "text": "旧回答"}],
        "assistant",
    )
    fake_session.state = {
        "agent": {
            "memory": {
                "_compressed_summary": "",
                "content": [
                    [user_msg.to_dict(), []],
                    [assistant_msg.to_dict(), []],
                ],
            },
        },
    }

    async with api_client:
        response = await api_client.get(f"/api/chats/{chat.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["chat_id"] == chat.id
    assert [message["role"] for message in body["messages"]] == [
        "user",
        "assistant",
    ]
    assert body["messages"][0]["content"][0]["text"] == "旧问题"
    assert body["messages"][1]["content"][0]["text"] == "旧回答"
    assert fake_session.read_session_ids[-1] == chat.session_id


async def test_ambiguous_legacy_shared_state_is_quarantined_per_chat(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    fake_session: FakeSession,
) -> None:
    """Two old chat rows must not both adopt one unowned state file."""
    from lantai.runtime.converters.legacy_messages import LegacyMsg

    transport_session_id = "console:legacy-shared-alias"
    chat_a = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-shared-a",
            name="把这个做成PPT",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
        ),
    )
    chat_b = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-shared-b",
            name="把这个做成PPT",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
        ),
    )
    leaked = LegacyMsg("user", "ONLY_OLD_SHARED_不可自动归属.docx", "user")
    fake_session.state = {
        "agent": {
            "memory": {
                "_compressed_summary": "",
                "content": [[leaked.to_dict(), []]],
            },
        },
    }
    state_a = f"console:chat:{chat_a.id}"
    state_b = f"console:chat:{chat_b.id}"
    fake_session.states_by_session = {state_a: {}, state_b: {}}

    async with api_client:
        history_a = await api_client.get(f"/api/chats/{chat_a.id}")
        history_b = await api_client.get(f"/api/chats/{chat_b.id}")

    assert history_a.status_code == 200
    assert history_b.status_code == 200
    assert history_a.json()["messages"] == []
    assert history_b.json()["messages"] == []
    assert fake_session.read_session_ids[-2:] == [state_a, state_b]
    for history in (history_a.json(), history_b.json()):
        assert history["meta"]["state_binding_version"] == 1
        assert (
            history["meta"]["state_migration_state"]
            == "needs_manual_recovery"
        )
        assert history["meta"]["legacy_source_reference"] == {
            "session_id": transport_session_id,
            "user_id": "default",
            "channel": "console",
        }
        assert history["meta"]["legacy_history_notice"] == (
            "检测到旧版混合历史，已隔离，原始内容仍可恢复"
        )

    preserved_source = await fake_session.get_session_state_dict(
        transport_session_id,
        "default",
        "console",
    )
    assert preserved_source == fake_session.state
    assert preserved_source["agent"]["memory"]["content"][0][0][
        "content"
    ] == "ONLY_OLD_SHARED_不可自动归属.docx"


async def test_quarantined_legacy_history_requires_explicit_copy(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    fake_session: FakeSession,
) -> None:
    """Recovery creates a canonical copy and preserves the shared source."""
    from lantai.runtime.converters.legacy_messages import LegacyMsg

    transport_session_id = "console:legacy-explicit-recovery"
    selected = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-recovery-selected",
            name="待确认归属的历史",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
        ),
    )
    await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-recovery-peer",
            name="同源旧会话",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
        ),
    )
    user_message = LegacyMsg("user", "恢复我选中的这份旧历史", "user")
    source_state = {
        "agent": {
            "memory": {
                "_compressed_summary": "",
                "content": [[user_message.to_dict(), []]],
            },
            "plan_notebook": {"legacy": True},
            "last_context_manifest": {"legacy": True},
        },
    }
    fake_session.state = source_state
    fake_session.states_by_session = {
        f"console:chat:{selected.id}": {},
        "console:chat:legacy-recovery-peer": {},
    }

    async with api_client:
        isolated = await api_client.get(f"/api/chats/{selected.id}")
        recovered = await api_client.post(
            f"/api/chats/{selected.id}/legacy-history-recovery"
        )

    assert isolated.status_code == 200
    assert isolated.json()["messages"] == []
    assert recovered.status_code == 200
    body = recovered.json()
    assert body["chat_id"] not in {
        selected.id,
        "legacy-recovery-peer",
    }
    assert body["name"] == "待确认归属的历史 · 恢复副本"
    assert body["messages"][0]["content"][0]["text"] == (
        "恢复我选中的这份旧历史"
    )
    provenance = body["meta"]["legacy_recovery_provenance"]
    assert provenance == {
        **provenance,
        "method": "explicit_copy",
        "selection": "all_preserved_messages",
        "selected_from_chat_id": selected.id,
        "source_session_id": transport_session_id,
        "source_user_id": "default",
        "source_channel": "console",
    }
    assert len(provenance["source_memory_sha256"]) == 64
    assert provenance["recovered_at"].endswith("+00:00")
    recovered_state_id = body["meta"]["session_state_id"]
    assert recovered_state_id == f"console:chat:{body['chat_id']}"
    recovered_agent = fake_session.states_by_session[recovered_state_id][
        "agent"
    ]
    assert recovered_agent["memory"] == source_state["agent"]["memory"]
    assert recovered_agent["plan_notebook"] != {"legacy": True}
    assert recovered_agent["last_context_manifest"] is None
    assert fake_session.state is source_state
    assert fake_session.state["agent"]["plan_notebook"] == {"legacy": True}
    selected_after = await chat_manager.get_chat(selected.id)
    assert selected_after is not None
    assert (
        selected_after.meta["state_migration_state"]
        == "needs_manual_recovery"
    )


async def test_normal_chat_cannot_invoke_legacy_history_recovery(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    chat = await _seed_chat(chat_manager)

    async with api_client:
        response = await api_client.post(
            f"/api/chats/{chat.id}/legacy-history-recovery"
        )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "This chat has no quarantined legacy history to recover"
    )


async def test_legacy_client_meta_cannot_claim_another_chat_state(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    fake_session: FakeSession,
) -> None:
    """An old arbitrary metadata value is not a durable ownership grant."""
    transport_session_id = "console:legacy-client-meta-alias"
    chat_a = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-client-meta-a",
            name="旧会话 A",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
            meta={"session_state_id": "console:chat:legacy-client-meta-b"},
        ),
    )
    chat_b = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-client-meta-b",
            name="旧会话 B",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
        ),
    )
    state_a = f"console:chat:{chat_a.id}"
    state_b = f"console:chat:{chat_b.id}"
    fake_session.states_by_session = {
        state_a: {},
        state_b: {
            "agent": {
                "memory": {
                    "_compressed_summary": "",
                    "content": [],
                },
            },
        },
    }

    async with api_client:
        history_a = await api_client.get(f"/api/chats/{chat_a.id}")
        history_b = await api_client.get(f"/api/chats/{chat_b.id}")

    assert history_a.status_code == 200
    assert history_b.status_code == 200
    assert fake_session.read_session_ids[-2:] == [state_a, state_b]


async def test_deleting_one_ambiguous_legacy_chat_persists_survivor_owner(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    fake_session: FakeSession,
) -> None:
    """Deletion must not make the survivor fall back to the shared old key."""
    transport_session_id = "console:legacy-delete-shared"
    chat_a = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-delete-a",
            name="旧会话 A",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
        ),
    )
    chat_b = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-delete-b",
            name="旧会话 B",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
        ),
    )
    state_b = f"console:chat:{chat_b.id}"
    fake_session.states_by_session = {state_b: {}}

    async with api_client:
        deleted = await api_client.delete(f"/api/chats/{chat_a.id}")
        history_b = await api_client.get(f"/api/chats/{chat_b.id}")

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert history_b.status_code == 200
    assert fake_session.read_session_ids[-1] == state_b
    saved_b = await chat_manager.get_chat(chat_b.id)
    assert saved_b is not None
    assert saved_b.meta["session_state_id"] == state_b


async def test_migrating_one_ambiguous_legacy_chat_persists_peer_owner(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    fake_session: FakeSession,
) -> None:
    """Migrating A must not make B adopt their formerly shared old key."""
    transport_session_id = "console:legacy-migrate-shared"
    chat_a = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-migrate-a",
            name="旧会话 A",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
        ),
    )
    chat_b = await _seed_legacy_chat(
        chat_manager,
        ChatSpec(
            id="legacy-migrate-b",
            name="旧会话 B",
            session_id=transport_session_id,
            user_id="default",
            channel="console",
        ),
    )
    state_a = f"console:chat:{chat_a.id}"
    state_b = f"console:chat:{chat_b.id}"
    fake_session.states_by_session = {state_b: {}}

    migrated_a = await chat_manager.merge_chat_meta(
        chat_a.id,
        {"session_state_id": state_a},
    )
    assert migrated_a is not None

    async with api_client:
        history_b = await api_client.get(f"/api/chats/{chat_b.id}")

    assert history_b.status_code == 200
    assert fake_session.read_session_ids[-1] == state_b
    saved_b = await chat_manager.get_chat(chat_b.id)
    assert saved_b is not None
    assert saved_b.meta["session_state_id"] == state_b


async def test_get_or_create_chat_persists_canonical_state_owner(
    chat_manager: ChatManager,
) -> None:
    chat = await chat_manager.get_or_create_chat(
        session_id="console:auto-created",
        user_id="default",
        channel="console",
    )

    assert chat.meta["session_state_id"] == f"console:chat:{chat.id}"
    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    assert saved.meta["session_state_id"] == f"console:chat:{chat.id}"


async def test_create_chat_replaces_unowned_state_key_with_canonical_owner(
    chat_manager: ChatManager,
) -> None:
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="server-owned-create",
            name="新会话",
            session_id="console:server-owned-create",
            user_id="default",
            channel="console",
            meta={"session_state_id": "client-controlled"},
        ),
    )

    expected = f"console:chat:{chat.id}"
    assert chat.meta["session_state_id"] == expected
    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    assert saved.meta["session_state_id"] == expected


async def test_repository_transaction_serializes_cross_process_writers(
    tmp_path: Path,
) -> None:
    """A second writer must load only after the first committed its row."""

    repo_path = tmp_path / "shared-chats.json"
    first_ready = tmp_path / "first.ready"
    first_entered = tmp_path / "first.entered"
    first_release = tmp_path / "first.release"
    second_ready = tmp_path / "second.ready"
    second_entered = tmp_path / "second.entered"
    second_release = tmp_path / "second.release"

    def start_writer(
        chat_id: str,
        ready: Path,
        entered: Path,
        release: Path,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                _TRANSACTION_WRITER_SCRIPT,
                str(repo_path),
                chat_id,
                str(ready),
                str(entered),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    first: subprocess.Popen[str] | None = None
    second: subprocess.Popen[str] | None = None

    try:
        first = start_writer(
            "cross-process-first",
            first_ready,
            first_entered,
            first_release,
        )
        assert _wait_for_path(first_ready)
        assert _wait_for_path(first_entered)
        second = start_writer(
            "cross-process-second",
            second_ready,
            second_entered,
            second_release,
        )
        assert _wait_for_path(second_ready)
        time.sleep(0.25)
        assert not second_entered.exists()

        first_release.touch()
        assert _wait_for_path(second_entered)
        second_release.touch()
        first_stdout, first_stderr = first.communicate(timeout=10)
        second_stdout, second_stderr = second.communicate(timeout=10)
        assert first.returncode == 0, first_stdout + first_stderr
        assert second.returncode == 0, second_stdout + second_stderr
    finally:
        first_release.touch()
        second_release.touch()
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    stored = await JsonChatRepository(repo_path).load()
    assert {chat.id for chat in stored.chats} == {
        "cross-process-first",
        "cross-process-second",
    }


async def test_independent_chats_with_shared_transport_session_keep_distinct_history(
    api_client: AsyncClient,
    fake_session: FakeSession,
) -> None:
    """A transport alias must never become the owner of two chat histories."""
    from lantai.runtime.converters.legacy_messages import LegacyMsg

    transport_session_id = "console:shared-desktop-alias"

    def memory_state(text: str) -> dict:
        message = LegacyMsg("user", text, "user")
        return {
            "agent": {
                "memory": {
                    "_compressed_summary": "",
                    "content": [[message.to_dict(), []]],
                },
            },
        }

    async with api_client:
        created_a = await api_client.post(
            "/api/chats",
            json={
                "name": "把这个做成PPT",
                "session_id": transport_session_id,
                "user_id": "default",
                "channel": "console",
                "meta": {"session_state_id": "client-controlled-state"},
            },
        )
        created_b = await api_client.post(
            "/api/chats",
            json={
                "name": "把这个做成PPT",
                "session_id": transport_session_id,
                "user_id": "default",
                "channel": "console",
            },
        )

        assert created_a.status_code == 200
        assert created_b.status_code == 200
        chat_a = created_a.json()
        chat_b = created_b.json()
        assert chat_a["id"] != chat_b["id"]
        state_a = chat_a["meta"]["session_state_id"]
        state_b = chat_b["meta"]["session_state_id"]
        assert state_a != state_b
        assert state_a != "client-controlled-state"

        fake_session.state = memory_state("不应被自动归属的旧共享数据")
        fake_session.states_by_session = {
            state_a: memory_state("ONLY_A_博士选题报告.docx"),
            state_b: memory_state("ONLY_B_财务报表.xlsx"),
        }

        history_a = await api_client.get(f"/api/chats/{chat_a['id']}")
        history_b = await api_client.get(f"/api/chats/{chat_b['id']}")

    assert history_a.status_code == 200
    assert history_b.status_code == 200
    assert history_a.json()["messages"][0]["content"][0]["text"] == (
        "ONLY_A_博士选题报告.docx"
    )
    assert history_b.json()["messages"][0]["content"][0]["text"] == (
        "ONLY_B_财务报表.xlsx"
    )
    assert fake_session.read_session_ids[-2:] == [state_a, state_b]


async def test_create_chat_drops_client_supplied_external_attachment_paths(
    api_client: AsyncClient,
) -> None:
    """Only the upload backend may attest canonical attachment paths."""

    forged_path = "/Users/example/.lantai/media/" + "a" * 32 + "_forged.docx"
    async with api_client:
        response = await api_client.post(
            "/api/chats",
            json={
                "name": "带附件的新会话",
                "session_id": "console:attachment-meta-injection",
                "user_id": "default",
                "channel": "console",
                "meta": {
                    "source": "test-client",
                    "external_attachment_paths": [forged_path],
                },
            },
        )

    assert response.status_code == 200
    created = response.json()
    assert created["meta"]["source"] == "test-client"
    assert "external_attachment_paths" not in created["meta"]


async def test_search_chats_builds_a_reusable_server_side_index(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    fake_session: FakeSession,
) -> None:
    """Global search must not rehydrate every chat after each keystroke."""
    from lantai.runtime.converters.legacy_messages import LegacyMsg

    await chat_manager.create_chat(
        ChatSpec(
            id="searchable-chat",
            name="Codex 协作",
            session_id="console:searchable",
            user_id="default",
            channel="console",
        ),
    )
    assistant = LegacyMsg(
        "Friday",
        [{"type": "text", "text": "Codex 已经把文件做出来了。"}],
        "assistant",
    )
    assistant.timestamp = "2026-09-06T12:34:56Z"
    fake_session.state = {
        "agent": {
            "memory": {
                "_compressed_summary": "",
                "content": [[assistant.to_dict(), []]],
            },
        },
    }

    async with api_client:
        first = await api_client.get("/api/chats/search?q=Codex")
        second = await api_client.get("/api/chats/search?q=文件")

    assert first.status_code == 200
    assert second.status_code == 200
    first_hits = first.json()
    assert [hit["kind"] for hit in first_hits] == ["title", "message"]
    assert first_hits[1]["excerpt"] == "Codex 已经把文件做出来了。"
    assert first_hits[1]["timestamp"].startswith("2026-09-06T12:34:56")
    assert second.json()[0]["chat_id"] == "searchable-chat"
    assert fake_session.read_count == 1


async def test_search_chats_includes_archived_and_coding_conversations(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    fake_session: FakeSession,
) -> None:
    fake_session.state = {}
    await chat_manager.create_chat(
        ChatSpec(
            id="search-archived",
            name="归档检索标题",
            session_id="console:archived",
            user_id="default",
            channel="console",
            archived=True,
        )
    )
    await chat_manager.create_chat(
        ChatSpec(
            id="search-coding",
            name="编程检索标题",
            session_id="console:coding:workspace:test",
            user_id="default",
            channel="console",
        )
    )

    async with api_client:
        archived = await api_client.get("/api/chats/search?q=归档检索")
        coding = await api_client.get("/api/chats/search?q=编程检索")

    assert archived.status_code == 200
    assert archived.json()[0]["archived"] is True
    assert coding.status_code == 200
    assert coding.json()[0]["coding"] is True


async def test_search_chat_cache_has_a_global_lru_bound(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    fake_session: FakeSession,
    monkeypatch,
) -> None:
    from lantai.app.runner import api as runner_api

    runner_api._CHAT_SEARCH_INDEX.clear()
    runner_api._CHAT_SEARCH_INFLIGHT.clear()
    monkeypatch.setattr(runner_api, "_CHAT_SEARCH_INDEX_MAX_ENTRIES", 1)
    monkeypatch.setattr(
        runner_api,
        "_CHAT_SEARCH_INDEX_MAX_CHARACTERS",
        1_000_000,
    )
    fake_session.state = {}
    for index in range(2):
        await chat_manager.create_chat(
            ChatSpec(
                id=f"bounded-search-{index}",
                name=f"Bounded {index}",
                session_id=f"console:bounded:{index}",
                user_id="default",
                channel="console",
            )
        )

    async with api_client:
        response = await api_client.get("/api/chats/search?q=no-match")

    assert response.status_code == 200
    assert len(runner_api._CHAT_SEARCH_INDEX) <= 1
    assert not runner_api._CHAT_SEARCH_INFLIGHT


async def test_list_chats_can_separate_coding_sessions(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """The ordinary chat list should be separable from Coding sessions."""
    await chat_manager.create_chat(
        ChatSpec(
            id="chat-normal",
            name="普通聊天",
            session_id="console:normal",
            user_id="default",
            channel="console",
        ),
    )
    await chat_manager.create_chat(
        ChatSpec(
            id="chat-coding",
            name="新会话",
            session_id="console:coding:workspace-1",
            user_id="default",
            channel="console",
            meta={"coding_mode": True},
        ),
    )

    async with api_client:
        ordinary = await api_client.get("/api/chats?mode=chat")
        coding = await api_client.get("/api/chats?mode=coding")
        all_chats = await api_client.get("/api/chats")

    assert ordinary.status_code == 200
    assert [chat["id"] for chat in ordinary.json()] == ["chat-normal"]
    assert coding.status_code == 200
    assert [chat["id"] for chat in coding.json()] == ["chat-coding"]
    assert all_chats.status_code == 200
    assert {chat["id"] for chat in all_chats.json()} == {
        "chat-normal",
        "chat-coding",
    }


async def test_list_chats_does_not_scan_runtime_ledgers_for_idle_chats(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lightweight session list must not hydrate every event ledger."""
    await chat_manager.create_chat(
        ChatSpec(
            id="chat-idle-ledger",
            name="闲置会话",
            session_id="console:idle-ledger",
            user_id="default",
            channel="console",
        ),
    )

    async def fail_if_loaded(*_args, **_kwargs):
        raise AssertionError("idle chat event ledger should not be scanned")

    monkeypatch.setattr(
        chat_api_module,
        "_load_turn_event_preview",
        fail_if_loaded,
    )

    async with api_client:
        response = await api_client.get("/api/chats")

    assert response.status_code == 200
    assert response.json()[0]["id"] == "chat-idle-ledger"


async def test_list_chats_repairs_verified_legacy_prompt_title(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    prompt = "请让4个子Agent并行检查滚动锁死问题，然后汇总结论。"
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="legacy-title-chat",
            name=prompt[:10],
            session_id="console:legacy-title",
            user_id="default",
            channel="console",
            meta={"last_turn_text": prompt},
        ),
    )

    async with api_client:
        response = await api_client.get("/api/chats")

    assert response.status_code == 200
    repaired = next(item for item in response.json() if item["id"] == chat.id)
    assert repaired["name"] == "滚动锁死问题排查"

    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    assert saved.name == "滚动锁死问题排查"
    assert saved.updated_at == chat.updated_at


async def test_list_chats_batches_legacy_title_repairs_into_one_save(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening the session drawer must not rewrite chats.json once per chat."""
    prompts = [
        "请让4个子Agent并行检查滚动锁死问题，然后汇总结论。",
        "请检查新建会话卡顿问题，并说明根因。",
    ]
    for index, prompt in enumerate(prompts):
        await chat_manager.create_chat(
            ChatSpec(
                id=f"legacy-title-batch-{index}",
                name=prompt[:10],
                session_id=f"console:legacy-title-batch-{index}",
                user_id="default",
                channel="console",
                meta={"last_turn_text": prompt},
            ),
        )

    save_calls = 0
    original_save = chat_manager._repo.save

    async def counted_save(chats_file):
        nonlocal save_calls
        save_calls += 1
        await original_save(chats_file)

    monkeypatch.setattr(chat_manager._repo, "save", counted_save)

    async with api_client:
        response = await api_client.get("/api/chats")

    assert response.status_code == 200
    assert save_calls == 1


@pytest.mark.asyncio
async def test_get_chat_includes_turn_event_preview(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """GET history should surface the recent runtime event preview."""
    from lantai.app.experience.events import record_chat_turn_started

    topic_workspace = (
        chat_manager._repo.path.parent / "topic-workspaces" / "chat-1"
    )
    topic_workspace.mkdir(parents=True, exist_ok=True)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="chat-events-1",
            name="运行中的会话",
            session_id="console:event-1",
            user_id="default",
            channel="console",
            meta={"topic_workspace_dir": str(topic_workspace)},
        ),
    )
    await record_chat_turn_started(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        text_preview="请继续整理这个方案",
        effective_workspace_dir=str(topic_workspace),
        run_id="run-preview-1",
    )

    async with api_client:
        response = await api_client.get(f"/api/chats/{chat.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["turn_event_preview"]["summary_line"] == "本轮已开始"
    assert len(body["turn_event_preview"]["events"]) == 1
    assert body["turn_event_preview"]["events"][0]["label"] == "开始运行"
    assert body["runtime_state"]["status"] == "idle"
    assert body["runtime_state"]["summary"] == "本轮已开始"
    assert body["runtime_state"]["can_reconnect"] is False


@pytest.mark.asyncio
async def test_get_chat_runtime_state_recovers_pending_running_turn(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """GET history should expose one typed runtime_state for UI recovery."""
    from lantai.app.experience.events import record_chat_turn_started

    topic_workspace = (
        chat_manager._repo.path.parent / "topic-workspaces" / "chat-2"
    )
    topic_workspace.mkdir(parents=True, exist_ok=True)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="chat-runtime-state-1",
            name="未完成会话",
            session_id="console:runtime-1",
            user_id="default",
            channel="console",
            meta={
                "topic_workspace_dir": str(topic_workspace),
                "pending_turn": {
                    "text": "试一下吧",
                    "run_id": "run-runtime-1",
                    "client_turn_id": "client-runtime-1",
                },
            },
        ),
    )
    await record_chat_turn_started(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        text_preview="试一下吧",
        effective_workspace_dir=str(topic_workspace),
        run_id="run-runtime-1",
    )

    async with api_client:
        response = await api_client.get(f"/api/chats/{chat.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["pending_user_message"] == "试一下吧"
    assert body["runtime_state"]["status"] == "running"
    assert body["runtime_state"]["delivery_status"] == "pending"
    assert body["runtime_state"]["run_id"] == "run-runtime-1"
    assert body["runtime_state"]["client_turn_id"] == "client-runtime-1"
    assert body["runtime_state"]["attempt_id"] == (
        "run-runtime-1:chat-attempt-1"
    )
    assert body["runtime_state"]["summary"] == "本轮已开始"
    assert body["runtime_state"]["workspace_dir"] == str(topic_workspace)
    assert body["runtime_state"]["events_preview"][0]["label"] == "开始运行"


@pytest.mark.asyncio
async def test_get_chat_runtime_state_never_exposes_tracker_identity_aliases(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """Tracker chat/run aliases must be replaced by the stored contract."""
    from lantai.app.experience.events import record_chat_turn_started

    root_workspace = chat_manager._repo.path.parent
    topic_workspace = root_workspace / "topic-workspaces" / "chat-alias"
    topic_workspace.mkdir(parents=True, exist_ok=True)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="chat-runtime-alias",
            name="运行身份别名",
            session_id="console:runtime-alias",
            user_id="default",
            channel="console",
            meta={
                "topic_workspace_dir": str(topic_workspace),
                "last_turn_run_id": "run-runtime-alias",
                "pending_turn": {
                    "text": "继续执行",
                    "run_id": "run-runtime-alias",
                    "client_turn_id": "client-runtime-alias",
                },
            },
        ),
    )
    await record_chat_turn_started(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        text_preview="继续执行",
        effective_workspace_dir=str(topic_workspace),
        run_id="run-runtime-alias",
    )
    workspace = FakeWorkspace(root_workspace)
    workspace.task_tracker.snapshots[chat.id] = {
        "run_key": chat.id,
        "status": "running",
        "attempt_id": "run-runtime-alias",
        "generation": 4,
        "root_session_id": chat.session_id,
        "started_at": "2026-09-20T08:00:00+00:00",
    }
    api_client._transport.app.dependency_overrides[  # type: ignore[attr-defined]
        get_workspace
    ] = lambda: workspace

    async with api_client:
        response = await api_client.get(f"/api/chats/{chat.id}")

    assert response.status_code == 200
    runtime_state = response.json()["runtime_state"]
    assert runtime_state["run_id"] == "run-runtime-alias"
    assert runtime_state["run_id"] != chat.id
    assert runtime_state["attempt_id"] == (
        "run-runtime-alias:chat-attempt-1"
    )
    assert runtime_state["attempt_id"] != "run-runtime-alias"
    assert runtime_state["can_reconnect"] is True
    assert runtime_state["can_stop"] is True


@pytest.mark.asyncio
async def test_get_chat_marks_orphaned_pending_turn_stalled(
    api_client: AsyncClient,
    chat_manager: ChatManager,
) -> None:
    """GET history should turn orphaned pending turns into a visible stall."""
    topic_workspace = (
        chat_manager._repo.path.parent / "topic-workspaces" / "chat-3"
    )
    topic_workspace.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="chat-stalled-1",
            name="卡住的会话",
            session_id="console:stalled-1",
            user_id="default",
            channel="console",
            meta={
                "topic_workspace_dir": str(topic_workspace),
                "pending_turn": {
                    "text": "继续修前端预览",
                    "run_id": "run-stalled-1",
                    "status": "running",
                    "created_at": created_at.isoformat(),
                },
            },
        ),
    )

    async with api_client:
        response = await api_client.get(f"/api/chats/{chat.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stalled"
    assert body["pending_user_message"] == "继续修前端预览"
    assert body["runtime_state"]["status"] == "stalled"
    assert body["runtime_state"]["run_id"] == "run-stalled-1"
    assert body["runtime_state"]["can_reconnect"] is False
    assert body["turn_event_preview"]["events"][-1]["type"] == "turn.stalled"
    assert body["turn_event_preview"]["events"][-1]["status"] == "stalled"

    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    pending_turn = saved.meta["pending_turn"]
    assert pending_turn["status"] == "stalled"
    assert pending_turn["stalled_reason"] == "pending_turn_without_active_task"


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "chat-1"},
        {"session_id": "console:other"},
    ],
)
async def test_put_chat_rejects_read_only_fields(
    api_client: AsyncClient,
    chat_manager: ChatManager,
    payload: dict[str, str],
) -> None:
    """PUT should reject read-only chat identity fields."""
    chat = await _seed_chat(chat_manager)

    async with api_client:
        response = await api_client.put(
            f"/api/chats/{chat.id}",
            json=payload,
        )

    assert response.status_code == 422

    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    assert saved.session_id == chat.session_id


async def test_patch_chat_if_name_matches_applies_when_name_matches(
    chat_manager: ChatManager,
) -> None:
    """Atomic compare-and-set succeeds when the persisted name matches."""
    chat = await _seed_chat(chat_manager)

    updated = await chat_manager.patch_chat_if_name_matches(
        chat.id,
        expected_name=chat.name,
        patch=ChatUpdate(name="Generated Title"),
    )

    assert updated is not None
    assert updated.name == "Generated Title"

    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    assert saved.name == "Generated Title"


async def test_patch_chat_if_name_matches_skips_when_name_differs(
    chat_manager: ChatManager,
) -> None:
    """Stale ``expected_name`` must leave the persisted name untouched.

    Without this guard a slow background title task can race against a
    user rename and overwrite it.
    """
    chat = await _seed_chat(chat_manager)
    await chat_manager.patch_chat(chat.id, ChatUpdate(name="User Pick"))

    updated = await chat_manager.patch_chat_if_name_matches(
        chat.id,
        expected_name=chat.name,  # stale: original placeholder
        patch=ChatUpdate(name="Generated Title"),
    )

    assert updated is None

    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    assert saved.name == "User Pick"


async def test_patch_chat_if_name_matches_returns_none_for_missing_chat(
    chat_manager: ChatManager,
) -> None:
    updated = await chat_manager.patch_chat_if_name_matches(
        "does-not-exist",
        expected_name="anything",
        patch=ChatUpdate(name="x"),
    )
    assert updated is None


async def test_repair_auto_title_preserves_original_activity_timestamp(
    chat_manager: ChatManager,
) -> None:
    """A legacy title repair must not make an old chat look newly active."""
    chat = await _seed_chat(chat_manager)

    updated = await chat_manager.repair_auto_title_if_name_matches(
        chat.id,
        expected_name=chat.name,
        semantic_name="语义标题",
    )

    assert updated is not None
    assert updated.name == "语义标题"
    assert updated.updated_at == chat.updated_at


async def test_touch_chat_updates_timestamp_without_overwriting_name(
    chat_manager: ChatManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Touching a chat for activity bookkeeping should preserve title."""
    chat = await _seed_chat(chat_manager)

    renamed = await chat_manager.patch_chat(
        chat.id,
        ChatUpdate(name="Renamed Chat"),
    )
    assert renamed is not None

    renamed_after_update = await chat_manager.get_chat(chat.id)
    assert renamed_after_update is not None
    previous_updated_at = renamed_after_update.updated_at

    class FixedDateTime:
        """Deterministic clock for timestamp-sensitive assertions."""

        @classmethod
        def now(cls, tz=None):
            assert tz == previous_updated_at.tzinfo
            return previous_updated_at + timedelta(seconds=1)

    monkeypatch.setattr(chat_manager_module, "datetime", FixedDateTime)

    touch_chat = getattr(chat_manager, "touch_chat", None)
    assert callable(touch_chat)
    await touch_chat(chat.id)

    saved = await chat_manager.get_chat(chat.id)
    assert saved is not None
    assert saved.name == "Renamed Chat"
    assert saved.updated_at > previous_updated_at


@pytest.mark.parametrize("cursor_transport", ["query", "last-event-id"])
async def test_durable_reconnect_keeps_critical_events_without_dispatch(
    chat_manager: ChatManager,
    monkeypatch,
    cursor_transport: str,
) -> None:
    """Resume across the page boundary without losing results or rerunning work."""
    import json
    from lantai.app.runner.runner import AgentRunner

    dispatches = []

    async def forbidden_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))
        raise AssertionError("durable replay must not dispatch a model turn")
        yield  # Make the trap an async iterator, like the real runner.

    monkeypatch.setattr(AgentRunner, "stream_query", forbidden_dispatch)
    workspace_dir = chat_manager._repo.path.parent
    topic_workspace = workspace_dir / "topic-workspaces" / "critical-replay"
    topic_workspace.mkdir(parents=True)
    chat = await chat_manager.create_chat(
        ChatSpec(
            id="critical-replay",
            name="Critical replay",
            session_id="console:critical-replay",
            user_id="default",
            channel="console",
            meta={"topic_workspace_dir": str(topic_workspace)},
        )
    )
    common = dict(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="turn-critical",
    )
    for index in range(248):
        await append_chat_event(
            **common,
            kind="chat.turn.started",
            status="running",
            summary=f"status-{index}",
        )
    await append_chat_event(
        **common,
        kind="chat.tool_call.started",
        item_id="tool-critical",
        status="running",
        summary="tool started",
        payload={"tool_name": "write_file"},
    )
    critical = [
        (
            "chat.approval.requested",
            "running",
            "approval",
            {
                "approval_request_id": "approval-critical",
                "tool_name": "write_file",
            },
        ),
        (
            "chat.approval.resolved",
            "completed",
            "approved",
            {
                "approval_request_id": "approval-critical",
                "decision": "approved",
            },
        ),
        (
            "chat.tool_call.finished",
            "completed",
            "effect",
            {
                "tool_name": "write_file",
                "effect_id": "effect-critical",
                "result_summary": "saved",
            },
        ),
        ("chat.final_answer", "completed", "answer", {"text": "saved result"}),
        ("chat.turn.settled", "completed", "terminal", {}),
    ]
    for kind, status, summary, payload in critical:
        await append_chat_event(
            **common,
            kind=kind,
            status=status,
            summary=summary,
            payload=payload,
            item_id="tool-critical"
            if kind == "chat.tool_call.finished"
            else None,
        )

    class Request:
        def __init__(self, headers=None):
            self.headers = headers or {}

        async def is_disconnected(self):
            return True

    async def connect(cursor=0, epoch=None, headers=None):
        return await stream_chat_runtime_events(
            request=Request(headers),
            chat_id=chat.id,
            after_seq=cursor,
            stream_epoch=epoch,
            mgr=chat_manager,
            workspace=FakeWorkspace(workspace_dir),
        )

    def payload(chunk):
        return json.loads(
            next(
                line[6:]
                for line in chunk.splitlines()
                if line.startswith("data: ")
            )
        )

    first = await connect()
    first_page = payload(await anext(first.body_iterator))
    assert len(first_page["events"]) == 250
    await first.body_iterator.aclose()  # Disconnect before page two.
    cursor = first_page["next_after_seq"]
    epoch = first_page["stream_epoch"]
    assert cursor == 250
    resumed = await connect(
        cursor=cursor if cursor_transport == "query" else 0,
        epoch=epoch if cursor_transport == "query" else None,
        headers={"last-event-id": f"{epoch}:{cursor}"}
        if cursor_transport == "last-event-id"
        else None,
    )
    remaining = []
    async for chunk in resumed.body_iterator:
        remaining.extend(payload(chunk)["events"])
    events = first_page["events"] + remaining
    assert [event["seq"] for event in events] == list(range(1, 255))
    assert [event["type"] for event in events[-5:]] == [
        "approval.requested",
        "approval.resolved",
        "tool.completed",
        "item.completed",
        "turn.completed",
    ]
    assert events[-5]["payload"]["approval_request_id"] == "approval-critical"
    assert events[-3]["payload"]["effect_id"] == "effect-critical"
    assert events[-2]["payload"]["text"] == "saved result"
    assert all(event["turn_id"] == "turn-critical" for event in events)
    assert dispatches == []


async def test_topic_workspace_persists_research_binding(api_client, chat_manager):
    from lantai.research.projects import ResearchService
    from lantai.research.chat import bind_chat
    service = ResearchService(chat_manager._repo.path.parent)
    project = service.create_project("q", [], idempotency_key="project")
    async with api_client:
        response = await api_client.post("/api/chats/topic-workspace", json={
            "session_id": "research-session", "research_project_id": project["project_id"]})
        assert response.status_code == 200
        chat_id = response.json()["chat"]["id"]
        chat = await chat_manager.get_chat(chat_id)
        assert chat.meta["research_project_id"] == project["project_id"]
        assert bind_chat(chat, service.workspace).project == project["project_id"]
        # Normal pre-send ensure is idempotent and cannot remove the association.
        repeated = await api_client.post("/api/chats/topic-workspace", json={
            "chat_id": chat_id, "session_id": "research-session"})
        assert repeated.json()["chat"]["meta"]["research_project_id"] == project["project_id"]


@pytest.mark.parametrize("kind", ["missing", "foreign", "offline", "empty"])
async def test_topic_workspace_rejects_unusable_research_binding(api_client, chat_manager, kind):
    from lantai.research.projects import ResearchService
    root = chat_manager._repo.path.parent
    service = ResearchService(root / "foreign" if kind == "foreign" else root)
    project = service.create_project("q", [], policy="offline" if kind == "offline" else "local_sources", idempotency_key="project")
    project_id = {"missing": "project-missing", "empty": " "}.get(kind, project["project_id"])
    async with api_client:
        response = await api_client.post("/api/chats/topic-workspace", json={
            "session_id": "research-rejected", "research_project_id": project_id})
    assert response.status_code == 400
    assert await chat_manager.list_chats() == []


async def test_topic_workspace_cannot_race_research_rebinding(api_client, chat_manager, monkeypatch):
    import asyncio
    from lantai.app.runner import api as runner_api
    from lantai.research.projects import ResearchService

    service = ResearchService(chat_manager._repo.path.parent)
    projects = [service.create_project("q", [], idempotency_key=f"project-{i}")["project_id"] for i in range(2)]
    chat = await _seed_chat(chat_manager)
    original = runner_api.ensure_chat_topic_workspace
    both_entered = asyncio.Event()
    entered = 0

    async def synchronized_workspace(*args, **kwargs):
        nonlocal entered
        result = await original(*args, **kwargs)
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), 5)
        return result

    monkeypatch.setattr(runner_api, "ensure_chat_topic_workspace", synchronized_workspace)
    async with api_client:
        results = await asyncio.gather(*[api_client.post("/api/chats/topic-workspace", json={
            "chat_id": chat.id, "session_id": chat.session_id, "research_project_id": project_id,
        }) for project_id in projects])
    assert sorted(response.status_code for response in results) == [200, 409]
    winner = next(response.json()["chat"]["meta"]["research_project_id"] for response in results if response.status_code == 200)
    assert (await chat_manager.get_chat(chat.id)).meta["research_project_id"] == winner
