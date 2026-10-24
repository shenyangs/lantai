# -*- coding: utf-8 -*-
"""Behavior locks for the authoritative Agent task board."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app.experience.schemas import RuntimeRunSummary
from lantai.app.runner.manager import ChatManager
from lantai.app.runner.repo.json_repo import JsonChatRepository
from lantai.app.runtime_tasks.projection import (
    build_runtime_task_board,
    project_runtime_task,
)
from lantai.app.runtime_tasks.schemas import CreateRuntimeTaskRequest
from lantai.app.runtime_tasks.service import create_runtime_task, start_runtime_task
from lantai.runtime.runs import (
    RuntimeAutonomyGrant,
    RuntimeProgressReport,
    RuntimeRunContract,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from lantai.runtime.store import RuntimeRunStore


def _contract(
    tmp_path: Path,
    *,
    status: str,
    delivery_status: str = "pending",
) -> RuntimeRunContract:
    task = build_chat_task_contract(
        objective="整理发布前检查清单",
        client_turn_id="client-task-1",
        complexity=None,
        work_method_id="standard",
        run_id="task-1",
        attempt_id="task-1:chat-attempt-1",
    )
    return RuntimeRunContract(
        run_id="task-1",
        attempt_id="task-1:chat-attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            agent_id="default",
            session_id="console:task:1",
            workspace_id=str(tmp_path),
            channel="console",
        ),
        status=status,  # type: ignore[arg-type]
        delivery_status=delivery_status,  # type: ignore[arg-type]
        budget=task.budget,
        task_contract=task,
        release_edition="official",
    )


def _summary(contract: RuntimeRunContract, tmp_path: Path) -> RuntimeRunSummary:
    return RuntimeRunSummary(
        id=contract.run_id,
        source="chat",
        title="chat run",
        status="running" if contract.status == "running" else "completed",
        authority_status=contract.status,
        workspace_dir=str(tmp_path),
    )


@pytest.mark.parametrize(
    ("status", "delivery", "lane", "outcome"),
    [
        ("queued", "pending", "pending", "queued"),
        ("running", "pending", "active", "in_progress"),
        ("waiting_approval", "pending", "needs_user", "needs_input"),
        ("succeeded", "verified", "done", "delivered"),
        ("succeeded", "pending", "needs_action", "unknown"),
        ("failed", "not_delivered", "needs_action", "blocked"),
    ],
)
def test_projection_uses_authority_and_delivery_evidence(
    tmp_path: Path,
    status: str,
    delivery: str,
    lane: str,
    outcome: str,
) -> None:
    contract = _contract(tmp_path, status=status, delivery_status=delivery)
    item = project_runtime_task(
        contract,
        _summary(contract, tmp_path),
        chat_meta={"chat_id": "task-chat-1", "task_center_title": "发布检查"},
    )

    assert item.lane == lane
    assert item.outcome_status == outcome
    assert item.title == "发布检查"
    assert item.chat_id == "task-chat-1"


def test_cancelled_task_is_history_not_done(tmp_path: Path) -> None:
    contract = _contract(
        tmp_path,
        status="cancelled",
        delivery_status="not_delivered",
    )
    item = project_runtime_task(contract, _summary(contract, tmp_path))

    assert item.lane == "needs_action"
    assert item.is_history is True
    assert item.outcome_status == "incomplete"


def test_task_board_exposes_task_scoped_autonomy_grant(tmp_path: Path) -> None:
    contract = _contract(tmp_path, status="running")
    now = datetime.now(timezone.utc)
    grant = RuntimeAutonomyGrant(
        grant_id="grant-task-1",
        approved=True,
        issued_by="user",
        run_id=contract.run_id,
        chat_id="task-chat-1",
        workspace_id=str(tmp_path),
        target_task_revision=0,
        issued_at=now,
        starts_at=now,
        expires_at=now + timedelta(hours=8),
        allowed_operations=("automatic_resume", "local_read"),
        allowed_paths=(str(tmp_path),),
        auto_resume_conditions=("app_reopen",),
    )
    assert contract.task_contract is not None
    contract = contract.model_copy(
        update={
            "task_contract": contract.task_contract.model_copy(
                update={"autonomy_grant": grant}
            )
        }
    )

    item = project_runtime_task(
        contract,
        _summary(contract, tmp_path),
        chat_meta={"chat_id": "task-chat-1"},
    )

    assert item.autonomy_grant is not None
    assert item.autonomy_grant["grant_id"] == "grant-task-1"
    assert item.autonomy_grant["allowed_paths"] == [str(tmp_path)]
    assert item.autonomy_grant["revocation_generation"] == 0


def test_task_board_projects_persisted_progress_report_without_recomputing(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path, status="paused")
    report = RuntimeProgressReport(
        report_id="report-task-1",
        kind="deadline_snapshot",
        generated_at=datetime.now(timezone.utc),
        run_id=contract.run_id,
        attempt_id=contract.attempt_id,
        contract_revision=contract.revision,
        status="paused",
        terminal=False,
        objective="整理发布前检查清单",
        open_acceptance_items=("确认发布证据",),
        artifact_refs=("artifact://release-check",),
        blocked_reasons=("watchdog deadline expired",),
        unknown_facts=("provider invoice is not locally observable",),
        wait_reason="deadline",
    )
    contract = contract.model_copy(update={"progress_report": report})

    item = project_runtime_task(contract, _summary(contract, tmp_path))

    assert item.progress_report is not None
    assert item.progress_report["report_id"] == "report-task-1"
    assert item.progress_report["terminal"] is False
    assert item.progress_report["open_acceptance_items"] == ["确认发布证据"]
    assert item.progress_report["wait_reason"] == "deadline"


def test_task_board_projects_contracts_without_replaying_event_ledgers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The list surface must stay cheap even when event history is enormous."""

    from lantai.app.experience import events as experience_events

    contract = _contract(
        tmp_path,
        status="succeeded",
        delivery_status="verified",
    )
    store = RuntimeRunStore(tmp_path)
    path = store.contract_path(contract.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    store._write_contract_unlocked(path, contract)

    def fail_if_event_history_is_replayed(*_args, **_kwargs):
        raise AssertionError("task-board list replayed runtime event history")

    monkeypatch.setattr(
        experience_events,
        "_iter_events_for_workspace",
        fail_if_event_history_is_replayed,
    )

    board = build_runtime_task_board([tmp_path], limit=1)

    assert board.total == 1
    assert board.visible_total == 1
    assert board.items[0].id == contract.run_id
    assert board.items[0].lane == "done"


@pytest.mark.asyncio
async def test_task_creation_is_idempotent_by_client_task_id(tmp_path: Path) -> None:
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_manager=manager,
    )
    body = CreateRuntimeTaskRequest(
        objective="整理发布前检查清单",
        title="发布检查",
        acceptance=["检查项可以逐条确认"],
        work_method_id="standard",
        client_task_id="client-task-release-check",
    )

    first, first_chat, first_dir, first_created = await create_runtime_task(
        workspace,
        body,
    )
    second, second_chat, second_dir, second_created = await create_runtime_task(
        workspace,
        body,
    )

    assert first_created is True
    assert second_created is False
    assert second.run_id == first.run_id
    assert second_chat.id == first_chat.id
    assert second_dir == first_dir
    stored = await RuntimeRunStore(first_dir).load_contract(first.run_id)
    assert stored is not None
    assert stored.status == "queued"
    assert stored.task_contract is not None
    assert stored.task_contract.objective == body.objective
    assert first_chat.meta["session_state_id"] == (
        f"console:chat:{first_chat.id}"
    )
    assert len(await manager.list_chats()) == 1


@pytest.mark.asyncio
async def test_start_uses_managed_console_path_and_keeps_run_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from lantai.app.runtime_tasks import service as task_service

    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))

    class Tracker:
        async def get_status(self, _run_key: str) -> str:
            return "idle"

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_manager=manager,
        task_tracker=Tracker(),
    )
    body = CreateRuntimeTaskRequest(
        objective="整理发布前检查清单",
        client_task_id="client-task-start",
    )
    contract, chat, _workspace_dir, _created = await create_runtime_task(
        workspace,
        body,
    )
    seen: list[dict] = []

    async def fake_drain(_request, payload):
        seen.append(payload)

    monkeypatch.setattr(task_service, "_drain_console_task", fake_drain)

    result = await start_runtime_task(
        workspace,
        SimpleNamespace(),
        contract.run_id,
    )
    await asyncio.sleep(0)

    assert result.accepted is True
    assert result.chat_id == chat.id
    assert seen[0]["queued_run_id"] == contract.run_id
    assert seen[0]["meta"]["client_turn_id"] == body.client_task_id


@pytest.mark.asyncio
async def test_start_does_not_duplicate_a_claimed_or_running_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from lantai.app.runtime_tasks import service as task_service

    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))

    class Tracker:
        async def get_status(self, _run_key: str) -> str:
            return "idle"

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_manager=manager,
        task_tracker=Tracker(),
    )
    body = CreateRuntimeTaskRequest(
        objective="整理发布前检查清单",
        client_task_id="client-task-running",
    )
    contract, _chat, task_workspace, _created = await create_runtime_task(
        workspace,
        body,
    )
    running = contract.model_copy(
        update={"status": "running", "revision": contract.revision + 1}
    )
    await RuntimeRunStore(task_workspace).save_contract(
        running,
        expected_revision=contract.revision,
    )
    seen: list[dict] = []

    async def fake_drain(_request, payload):
        seen.append(payload)

    monkeypatch.setattr(task_service, "_drain_console_task", fake_drain)

    result = await start_runtime_task(
        workspace,
        SimpleNamespace(),
        contract.run_id,
    )
    await asyncio.sleep(0)

    assert result.already_running is True
    assert seen == []


@pytest.mark.asyncio
async def test_start_rejects_an_unbounded_task_start_queue(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from lantai.app.runtime_tasks import service as task_service

    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))

    class Tracker:
        async def get_status(self, _run_key: str) -> str:
            return "idle"

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_manager=manager,
        task_tracker=Tracker(),
    )
    body = CreateRuntimeTaskRequest(
        objective="验证有界任务启动队列",
        client_task_id="client-task-queue-full",
    )
    contract, _chat, _workspace_dir, _created = await create_runtime_task(
        workspace,
        body,
    )
    blocker = asyncio.Event()

    async def blocked() -> None:
        await blocker.wait()

    drains = {asyncio.create_task(blocked()) for _ in range(2)}
    monkeypatch.setattr(task_service, "_TASK_START_DRAINS", drains)
    monkeypatch.setattr(task_service, "_MAX_TASK_START_DRAINS", 2)

    try:
        with pytest.raises(HTTPException) as caught:
            await start_runtime_task(workspace, SimpleNamespace(), contract.run_id)
        assert caught.value.status_code == 429
        assert caught.value.detail["code"] == "runtime_task_start_queue_full"
    finally:
        blocker.set()
        await asyncio.gather(*drains)
