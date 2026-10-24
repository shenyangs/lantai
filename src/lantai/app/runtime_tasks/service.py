# -*- coding: utf-8 -*-
"""Persistent task creation and managed Console execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from ...runtime.runs import (
    RuntimeRunContract,
    RuntimeRunOwner,
    build_chat_task_contract,
)
from ...runtime.store import RuntimeRunStore, RuntimeRunStoreConflictError
from ...security.beta_access import AccessBoundary
from ..beta_access.wiring import require_workspace_beta_access_http
from ..runner.models import ChatSpec
from .schemas import CreateRuntimeTaskRequest, RuntimeTaskStartResponse

logger = logging.getLogger(__name__)

_TASK_START_DRAINS: set[asyncio.Task[Any]] = set()
_MAX_TASK_START_DRAINS = 8


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _task_identity(workspace_dir: Path, agent_id: str, client_task_id: str) -> str:
    digest = hashlib.sha256(
        f"{workspace_dir.resolve()}\n{agent_id}\n{client_task_id}".encode("utf-8")
    ).hexdigest()
    return digest[:24]


def _task_title(body: CreateRuntimeTaskRequest) -> str:
    if body.title:
        return body.title
    first_line = body.objective.splitlines()[0].strip()
    return first_line[:80] or "新任务"


async def create_runtime_task(
    workspace: Any,
    body: CreateRuntimeTaskRequest,
) -> tuple[RuntimeRunContract, ChatSpec, Path, bool]:
    """Create an idempotent queued RuntimeRun and its conversation anchor."""

    require_workspace_beta_access_http(
        workspace,
        boundary=AccessBoundary.WORK_START,
        capability="runtime.task.create",
    )

    root = Path(workspace.workspace_dir).expanduser()
    identity = _task_identity(root, workspace.agent_id, body.client_task_id)
    run_id = f"task-{identity}"
    chat_id = f"task-chat-{identity}"
    session_id = f"console:task:{identity}"
    topic_workspace = root / "topic-workspaces" / chat_id
    topic_workspace.mkdir(parents=True, exist_ok=True)
    store = RuntimeRunStore(topic_workspace)

    existing = await store.load_contract(run_id)
    if existing is not None:
        existing_objective = (
            existing.task_contract.objective if existing.task_contract else ""
        )
        if existing_objective != body.objective:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "client_task_id_conflict",
                    "message": "client_task_id already belongs to another objective",
                    "run_id": run_id,
                },
            )
        chat = await workspace.chat_manager.get_chat(chat_id)
        if chat is None:
            chat = await workspace.chat_manager.create_chat(
                ChatSpec(
                    id=chat_id,
                    name=_task_title(body),
                    session_id=session_id,
                    user_id="default",
                    channel="console",
                    meta={
                        "topic_workspace_dir": str(topic_workspace),
                        "task_center_run_id": run_id,
                        "task_center_client_task_id": body.client_task_id,
                        "task_center_title": _task_title(body),
                        "work_method_id": body.work_method_id,
                    },
                )
            )
        return existing, chat, topic_workspace, False

    chat = await workspace.chat_manager.get_chat(chat_id)
    if chat is None:
        chat = await workspace.chat_manager.create_chat(
            ChatSpec(
                id=chat_id,
                name=_task_title(body),
                session_id=session_id,
                user_id="default",
                channel="console",
                meta={
                    "topic_workspace_dir": str(topic_workspace),
                    "task_center_run_id": run_id,
                    "task_center_client_task_id": body.client_task_id,
                    "task_center_title": _task_title(body),
                    "work_method_id": body.work_method_id,
                },
            )
        )

    attempt_id = f"{run_id}:chat-attempt-1"
    task_contract = build_chat_task_contract(
        objective=body.objective,
        client_turn_id=body.client_task_id,
        complexity=None,
        work_method_id=body.work_method_id,
        run_id=run_id,
        attempt_id=attempt_id,
        acceptance=body.acceptance,
    )
    now = datetime.now(timezone.utc)
    contract = RuntimeRunContract(
        run_id=run_id,
        attempt_id=attempt_id,
        source="chat",
        owner=RuntimeRunOwner(
            user_id=chat.user_id,
            agent_id=workspace.agent_id,
            session_id=chat.session_id,
            workspace_id=str(topic_workspace),
            channel=chat.channel,
        ),
        status="queued",
        revision=0,
        created_at=now,
        budget=task_contract.budget,
        task_contract=task_contract,
        contract_hash=_stable_hash(
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "chat_id": chat.id,
                "session_id": chat.session_id,
                "task_contract": task_contract.model_dump(mode="json"),
            }
        ),
        policy_snapshot_hash=_stable_hash(
            {
                "source": "task_center",
                "work_method_id": body.work_method_id,
                "release_edition": "official",
            }
        ),
        release_edition="official",
    )
    try:
        saved = await store.save_contract(contract, expected_revision=None)
    except RuntimeRunStoreConflictError:
        raced = await store.load_contract(run_id)
        if raced is None:
            raise
        saved = raced
        return saved, chat, topic_workspace, False
    return saved, chat, topic_workspace, True


async def _find_task_chat(workspace: Any, run_id: str) -> ChatSpec | None:
    for chat in await workspace.chat_manager.list_chats():
        if str((chat.meta or {}).get("task_center_run_id") or "") == run_id:
            return chat
    return None


async def _drain_console_task(request: Request, payload: dict[str, Any]) -> None:
    from ..routers.console import post_console_chat

    try:
        response = await post_console_chat(payload, request)
        async for _chunk in response.body_iterator:
            pass
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Managed task-center run failed before Console settlement: run_id=%s",
            payload.get("queued_run_id"),
        )


def _track_drain(task: asyncio.Task[Any]) -> None:
    _TASK_START_DRAINS.add(task)
    task.add_done_callback(_TASK_START_DRAINS.discard)


async def start_runtime_task(
    workspace: Any,
    request: Request,
    run_id: str,
) -> RuntimeTaskStartResponse:
    """Launch a queued task through the existing managed Console runtime."""

    require_workspace_beta_access_http(
        workspace,
        boundary=AccessBoundary.WORK_START,
        capability="runtime.task.start",
    )

    chat = await _find_task_chat(workspace, run_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Task conversation not found")
    workspace_dir = Path(
        str((chat.meta or {}).get("topic_workspace_dir") or workspace.workspace_dir)
    ).expanduser()
    contract = await RuntimeRunStore(workspace_dir).load_contract(run_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Runtime task not found")
    if contract.status not in {"queued", "claimed", "running"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "task_not_startable",
                "status": contract.status,
                "run_id": run_id,
            },
        )
    tracker_running = await workspace.task_tracker.get_status(chat.id) == "running"
    already_running = contract.status in {"claimed", "running"} or tracker_running
    if contract.status == "queued" and not tracker_running:
        active_drains = sum(1 for task in _TASK_START_DRAINS if not task.done())
        if active_drains >= _MAX_TASK_START_DRAINS:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "runtime_task_start_queue_full",
                    "message": "Too many task starts are already being prepared; retry shortly.",
                    "active_drains": active_drains,
                    "max_drains": _MAX_TASK_START_DRAINS,
                },
            )
        objective = contract.task_contract.objective if contract.task_contract else chat.name
        client_task_id = (
            contract.task_contract.client_turn_id
            if contract.task_contract is not None
            else f"task-start-{run_id}"
        )
        payload = {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": objective}],
                }
            ],
            "session_id": chat.session_id,
            "user_id": chat.user_id,
            "channel": chat.channel,
            "stream": True,
            "queued_run_id": run_id,
            "meta": {
                "client_turn_id": client_task_id,
                "work_method_id": (
                    contract.task_contract.work_method_id
                    if contract.task_contract is not None
                    else "standard"
                ),
                "queued_run_id": run_id,
            },
        }
        task = asyncio.create_task(
            _drain_console_task(request, payload),
            name=f"lantai-task-center-{run_id}",
        )
        _track_drain(task)
    return RuntimeTaskStartResponse(
        run_id=run_id,
        chat_id=chat.id,
        session_id=chat.session_id,
        status=contract.status,
        already_running=already_running,
    )
