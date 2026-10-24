# -*- coding: utf-8 -*-
"""Behavior locks for Chat RuntimeRunStore adapter writes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lantai.app.experience.events import (
    record_chat_turn_settled,
    record_chat_turn_started,
)
from lantai.app.experience.runtime_runs import touch_chat_runtime_run_heartbeat
from lantai.runtime.runs import (
    RuntimeRunContract,
    RuntimeRunOwner,
    start_runtime_run_attempt,
)
from lantai.runtime.store import RuntimeRunStore


@pytest.mark.asyncio
async def test_chat_turn_started_writes_runtime_run_contract(tmp_path):
    await record_chat_turn_started(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        chat_id="chat-1",
        session_id="console:chat-1",
        text_preview="hello",
        effective_workspace_dir=str(tmp_path),
        run_id="run-chat-1",
    )

    contract = await RuntimeRunStore(tmp_path).load_contract("run-chat-1")

    assert contract is not None
    assert contract.run_id == "run-chat-1"
    assert contract.attempt_id == "run-chat-1:chat-attempt-1"
    assert contract.source == "chat"
    assert contract.status == "running"
    assert contract.revision == 2
    assert contract.owner.agent_id == "agent-default"
    assert contract.owner.session_id == "console:chat-1"
    assert contract.owner.workspace_id == str(tmp_path)
    assert contract.contract_hash.startswith("sha256:")
    assert contract.policy_snapshot_hash.startswith("sha256:")
    assert contract.release_edition == "official"


@pytest.mark.asyncio
async def test_chat_turn_started_consumes_prepared_retry_attempt(tmp_path):
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    original = RuntimeRunContract(
        run_id="run-chat-retry",
        attempt_id="run-chat-retry:chat-attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id="agent-default",
            session_id="console:chat-1",
            workspace_id=str(tmp_path),
            channel="console",
        ),
        status="failed",
        revision=4,
        created_at=now,
        started_at=now,
        settled_at=now,
        contract_hash="sha256:contract",
        policy_snapshot_hash="sha256:policy",
        release_edition="official",
    )
    store = RuntimeRunStore(tmp_path)
    await store.save_contract(original, expected_revision=None)
    prepared = start_runtime_run_attempt(
        original,
        attempt_id="run-chat-retry:retry-prepared",
        attempt_reason="retry",
        now=now,
    )
    await store.save_contract(
        prepared,
        expected_revision=4,
        expected_attempt_id=original.attempt_id,
    )

    event = await record_chat_turn_started(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        chat_id="chat-1",
        session_id="console:chat-1",
        text_preview="retry hello",
        effective_workspace_dir=str(tmp_path),
        run_id="run-chat-retry",
    )

    contract = await store.load_contract("run-chat-retry")

    assert contract is not None
    assert contract.attempt_id == "run-chat-retry:retry-prepared"
    assert contract.status == "running"
    assert contract.revision == 2
    assert contract.lease_owner == "chat:chat-1"
    assert getattr(contract, "attempt_reason") == "retry"
    assert getattr(contract, "previous_attempt_id") == original.attempt_id
    assert event.payload["attempt_id"] == "run-chat-retry:retry-prepared"


@pytest.mark.asyncio
async def test_chat_turn_settled_updates_runtime_run_contract_terminal_status(tmp_path):
    await record_chat_turn_started(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        chat_id="chat-1",
        session_id="console:chat-1",
        text_preview="hello",
        effective_workspace_dir=str(tmp_path),
        run_id="run-chat-1",
    )

    await record_chat_turn_settled(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        chat_id="chat-1",
        session_id="console:chat-1",
        final_task_status="failed",
        pending_turn_cleared=True,
        active_run_event_count=3,
        run_id="run-chat-1",
    )

    contract = await RuntimeRunStore(tmp_path).load_contract("run-chat-1")

    assert contract is not None
    assert contract.status == "failed"
    assert contract.revision == 3
    assert contract.settled_at is not None
    assert contract.error is not None
    assert contract.error.code == "chat_turn_failed"


@pytest.mark.asyncio
async def test_chat_runtime_heartbeat_refreshes_running_contract(tmp_path):
    await record_chat_turn_started(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        chat_id="chat-1",
        session_id="console:chat-1",
        text_preview="hello",
        effective_workspace_dir=str(tmp_path),
        run_id="run-chat-heartbeat",
    )
    store = RuntimeRunStore(tmp_path)
    before = await store.load_contract("run-chat-heartbeat")
    assert before is not None
    assert before.heartbeat_at is not None

    now = before.heartbeat_at + timedelta(seconds=30)
    touched = await touch_chat_runtime_run_heartbeat(
        workspace_dir=tmp_path,
        run_id="run-chat-heartbeat",
        now=now,
    )
    after = await store.load_contract("run-chat-heartbeat")

    assert touched is not None
    assert after is not None
    assert after.status == "running"
    assert after.attempt_id == before.attempt_id
    assert after.revision == before.revision + 1
    assert after.heartbeat_at == now


@pytest.mark.asyncio
async def test_chat_runtime_run_adapter_marks_mas_release_edition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_APP_STORE", "1")

    await record_chat_turn_started(
        workspace_dir=tmp_path,
        agent_id="agent-default",
        chat_id="chat-1",
        session_id="console:chat-1",
        text_preview="hello",
        effective_workspace_dir=str(tmp_path),
        run_id="run-chat-mas",
    )

    contract = await RuntimeRunStore(tmp_path).load_contract("run-chat-mas")

    assert contract is not None
    assert contract.release_edition == "mas"
