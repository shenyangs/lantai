# -*- coding: utf-8 -*-
"""Behavior locks for RuntimeRunStore read-time stop assessment refresh."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.app.experience.events import append_chat_event
from lantai.app.runner.models import ChatSpec
from lantai.app.runner.manager import ChatManager
from lantai.app.runner.repo.json_repo import JsonChatRepository
from lantai.app.runner.stall_watchdog import (
    mark_chat_stalled_if_needed,
    refresh_stalled_chats,
    refresh_stalled_runtime_runs,
)
from lantai.runtime.runs import (
    RuntimeBudget,
    RuntimeRunContract,
    RuntimeRunOwner,
)
from lantai.runtime.store import RuntimeRunStore


NOW = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[3]
RESTART_SMOKE = ROOT / "scripts/run_runtime_watchdog_restart_smoke.py"


@pytest.mark.asyncio
async def test_force_stall_reconciles_idle_backend_before_timeout(
    tmp_path,
) -> None:
    topic_workspace = tmp_path / "topic-workspace"
    topic_workspace.mkdir()
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-force-stall",
            session_id="console:force-stall",
            user_id="default",
            channel="console",
            meta={
                "topic_workspace_dir": str(topic_workspace),
                "pending_turn": {
                    "text": "调整当前方向",
                    "run_id": "run-force-stall",
                    "status": "running",
                    "created_at": NOW.isoformat(),
                },
            },
        ),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_manager=manager,
    )

    updated, marked = await mark_chat_stalled_if_needed(
        mgr=manager,
        workspace=workspace,
        chat_spec=chat,
        tracker_status="idle",
        active_run=None,
        turn_event_preview=None,
        now=NOW,
        force=True,
    )

    assert marked is True
    assert updated.meta["pending_turn"]["status"] == "stalled"


@pytest.mark.asyncio
async def test_chat_watchdog_skips_event_ledgers_for_chats_without_pending_turns(
    monkeypatch,
    tmp_path,
) -> None:
    from lantai.app.runner import stall_watchdog

    chat = ChatSpec(
        id="idle-chat",
        session_id="console:idle-chat",
        user_id="default",
        channel="console",
    )

    class Manager:
        async def list_chats(self):
            return [chat]

    class Tracker:
        async def get_status(self, _chat_id):
            raise AssertionError("idle chat queried the task tracker")

        async def get_snapshot(self, _chat_id):
            raise AssertionError("idle chat queried the task tracker")

    async def fail_if_preview_is_loaded(*_args, **_kwargs):
        raise AssertionError("idle chat replayed its runtime event ledger")

    monkeypatch.setattr(
        stall_watchdog,
        "load_turn_event_preview",
        fail_if_preview_is_loaded,
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        chat_manager=Manager(),
        task_tracker=Tracker(),
    )

    assert await refresh_stalled_chats(workspace) == [chat]


def _owner(workspace_dir) -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="default",
        agent_id="agent-default",
        session_id="console:chat-budget",
        workspace_id=str(workspace_dir),
        channel="console",
    )


@pytest.mark.asyncio
async def test_refresh_stalled_runtime_runs_settles_token_budget_from_event_usage(
    tmp_path,
) -> None:
    contract = RuntimeRunContract(
        run_id="run-token-budget",
        attempt_id="run-token-budget:attempt-1",
        source="chat",
        owner=_owner(tmp_path),
        status="running",
        revision=2,
        lease_owner="chat:chat-budget",
        created_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        budget=RuntimeBudget(max_tokens=5),
        contract_hash="sha256:contract",
        policy_snapshot_hash="sha256:policy",
        release_edition="official",
    )
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(contract, expected_revision=None)
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        chat_id="chat-budget",
        session_id="console:chat-budget",
        run_id=contract.run_id,
        kind="chat.turn.started",
        status="running",
        summary="预算测试开始",
        payload={"attempt_id": contract.attempt_id},
        usage={"input_tokens": 4, "output_tokens": 2},
    )

    settled = await refresh_stalled_runtime_runs(
        [tmp_path],
        now=NOW + timedelta(seconds=5),
    )
    saved = await store.load_contract(contract.run_id)

    assert len(settled) == 1
    assert saved is not None
    assert saved.status == "stalled"
    assert saved.revision == 3
    assert saved.error is not None
    assert saved.error.code == "loop_token_budget_exhausted"


@pytest.mark.asyncio
async def test_refresh_stalled_runtime_runs_settles_tool_call_budget_from_events(
    tmp_path,
) -> None:
    contract = RuntimeRunContract(
        run_id="run-tool-budget",
        attempt_id="run-tool-budget:attempt-1",
        source="chat",
        owner=_owner(tmp_path),
        status="running",
        revision=2,
        lease_owner="chat:chat-budget",
        created_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        budget=RuntimeBudget(max_tool_calls=1),
        contract_hash="sha256:contract",
        policy_snapshot_hash="sha256:policy",
        release_edition="official",
    )
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(contract, expected_revision=None)
    for index in range(2):
        await append_chat_event(
            workspace_dir=tmp_path,
            agent_id="agent-default",
            chat_id="chat-budget",
            session_id="console:chat-budget",
            run_id=contract.run_id,
            item_id=f"tool-{index}",
            kind="chat.tool_call.started",
            status="running",
            summary="工具调用",
            payload={
                "attempt_id": contract.attempt_id,
                "tool_call_id": f"tool-{index}",
                "tool_name": "search",
            },
        )

    settled = await refresh_stalled_runtime_runs(
        [tmp_path],
        now=NOW + timedelta(seconds=5),
    )
    saved = await store.load_contract(contract.run_id)

    assert len(settled) == 1
    assert saved is not None
    assert saved.status == "stalled"
    assert saved.error is not None
    assert saved.error.code == "loop_tool_call_budget_exhausted"


@pytest.mark.asyncio
async def test_refresh_stalled_runtime_runs_settles_artifact_budget_from_events(
    tmp_path,
) -> None:
    contract = RuntimeRunContract(
        run_id="run-artifact-budget",
        attempt_id="run-artifact-budget:attempt-1",
        source="chat",
        owner=_owner(tmp_path),
        status="running",
        revision=2,
        lease_owner="chat:chat-budget",
        created_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        budget=RuntimeBudget(max_artifact_bytes=10),
        contract_hash="sha256:contract",
        policy_snapshot_hash="sha256:policy",
        release_edition="official",
    )
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(contract, expected_revision=None)
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        chat_id="chat-budget",
        session_id="console:chat-budget",
        run_id=contract.run_id,
        kind="chat.artifact.created",
        status="finished",
        summary="产物",
        payload={
            "attempt_id": contract.attempt_id,
            "artifact_id": "artifact-report",
            "size_bytes": 12,
        },
    )

    settled = await refresh_stalled_runtime_runs(
        [tmp_path],
        now=NOW + timedelta(seconds=5),
    )
    saved = await store.load_contract(contract.run_id)

    assert len(settled) == 1
    assert saved is not None
    assert saved.status == "stalled"
    assert saved.error is not None
    assert saved.error.code == "loop_artifact_budget_exhausted"


def test_runtime_watchdog_restart_smoke_settles_expired_runs(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(RESTART_SMOKE),
            "--workspace",
            str(tmp_path / "runtime-watchdog-restart"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    by_run = {item["run_id"]: item for item in result["runs"]}

    assert result["ok"] is True
    assert result["writer_process"] != result["watchdog_process"]
    assert result["watchdog_process"] != result["reader_process"]
    assert by_run["run-expired-lease"]["status"] == "stalled"
    assert by_run["run-expired-lease"]["error_code"] == "loop_lease_expired"
    assert by_run["run-expired-deadline"]["status"] == "paused"
    assert by_run["run-expired-deadline"]["error_code"] is None
    assert by_run["run-healthy"]["status"] == "running"
    assert by_run["run-healthy"]["error_code"] is None
