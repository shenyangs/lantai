# -*- coding: utf-8 -*-
"""Ownership contracts for Runtime router lookup helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from lantai.app.routers import runtime as runtime_router
from lantai.app.experience.events import build_runtime_run_events_response
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.runs import (
    RuntimeEffectRecord,
    RuntimeBudget,
    RuntimeCheckpoint,
    RuntimePendingToolCall,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunReason,
)
from lantai.runtime.models import ModelContentBlock, ModelMessage
from lantai.runtime.store import RuntimeRunStore


def _chat(chat_id: str, session_id: str, run_id: str):
    return SimpleNamespace(
        id=chat_id,
        session_id=session_id,
        meta={"last_turn_run_id": run_id},
    )


def test_runtime_chat_lookup_does_not_self_match_requested_identifiers() -> None:
    chat_a = _chat("chat-a", "session-a", "run-a")
    chat_b = _chat("chat-b", "session-b", "run-b")

    assert not runtime_router._chat_matches_identifier(
        chat_a,
        run_id="run-b",
        chat_id="chat-b",
        session_id="session-b",
    )
    assert runtime_router._chat_matches_identifier(
        chat_b,
        run_id="run-b",
        chat_id="chat-b",
        session_id="session-b",
    )


def test_runtime_chat_lookup_matches_pending_turn_owner_only() -> None:
    chat = SimpleNamespace(
        id="chat-a",
        session_id="session-a",
        meta={
            "pending_turn": {
                "run_id": "pending-run-a",
                "turn_id": "pending-turn-a",
            },
        },
    )

    assert runtime_router._chat_matches_identifier(
        chat,
        run_id="pending-turn-a",
    )
    assert not runtime_router._chat_matches_identifier(
        chat,
        run_id="pending-turn-b",
        chat_id="chat-b",
        session_id="session-b",
    )


def test_runtime_chat_lookup_ignores_stale_last_turn_when_pending_owner_exists() -> None:
    chat = SimpleNamespace(
        id="chat-a",
        session_id="session-a",
        meta={
            "last_turn_run_id": "old-run",
            "pending_turn": {
                "run_id": "current-run",
                "turn_id": "current-turn",
            },
        },
    )

    assert not runtime_router._chat_matches_identifier(chat, run_id="old-run")
    assert runtime_router._chat_matches_identifier(chat, run_id="current-run")
    assert runtime_router._chat_matches_identifier(chat, run_id="current-turn")


class _FakeRuntimeChatManager:
    def __init__(self, chats) -> None:
        self.chats = list(chats)

    async def list_chats(self):
        return list(self.chats)


def _effect_resolution_owner(workspace_dir):
    return RuntimeRunOwner(
        user_id="default",
        agent_id="browser",
        session_id="browser-session",
        workspace_id=str(workspace_dir),
        channel="browser",
    )


async def _install_effect_resolution_workspace(monkeypatch, workspace_dir):
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=workspace_dir,
        chat_manager=_FakeRuntimeChatManager([]),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)


async def _write_running_effect_contract(workspace_dir):
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    owner = _effect_resolution_owner(workspace_dir)
    contract = RuntimeRunContract(
        run_id="browser-effect-run",
        attempt_id="browser-effect-run:attempt-1",
        source="browser",
        owner=owner,
        status="running",
        revision=2,
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        contract_hash="sha256:contract",
        policy_snapshot_hash="sha256:policy",
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )
    effect = RuntimeEffectRecord(
        run_id=contract.run_id,
        attempt_id=contract.attempt_id,
        step_id="browser-action-0",
        effect_id="effect-click-publish",
        source="browser",
        owner=owner,
        status="intent",
        tool_name="browser.click",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="browser-effect-run:attempt-1:effect-click-publish",
        created_at=now,
        release_edition="official",
    )
    journal = RuntimeEffectJournal(workspace_dir)
    journal.append_effect(effect)
    running = reduce_runtime_effect(
        effect,
        RuntimeEffectReducerAction(type="start", now=now),
    ).effect
    journal.append_effect(running)
    return contract, journal


async def _write_committed_effect_contract(workspace_dir):
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    contract, journal = await _write_running_effect_contract(workspace_dir)
    running = journal.latest_effect(
        contract.run_id,
        contract.attempt_id,
        "effect-click-publish",
    )
    assert running is not None
    committed = reduce_runtime_effect(
        running,
        RuntimeEffectReducerAction(
            type="commit",
            result_hash="sha256:browser-result",
            now=now,
        ),
    ).effect
    journal.append_effect(committed)
    return contract, journal


class _FakeRuntimeTaskTracker:
    def __init__(
        self,
        *,
        snapshot,
        stopped: bool = True,
        status: str = "running",
    ) -> None:
        self.snapshot = snapshot
        self.stopped = stopped
        self.status = status
        self.snapshot_requests: list[str] = []
        self.status_requests: list[str] = []
        self.fenced_stop_requests: list[tuple[str, int]] = []
        self.raw_stop_requests: list[str] = []

    async def get_status(self, run_key: str):
        self.status_requests.append(run_key)
        return self.status

    async def get_snapshot(self, run_key: str):
        self.snapshot_requests.append(run_key)
        return self.snapshot

    async def request_stop_if_generation(self, run_key: str, generation: int) -> bool:
        self.fenced_stop_requests.append((run_key, generation))
        return self.stopped

    async def request_stop(self, run_key: str) -> bool:
        self.raw_stop_requests.append(run_key)
        return self.stopped


@pytest.mark.asyncio
async def test_resolve_runtime_run_effect_marks_running_effect_human_resolved(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, journal = await _write_running_effect_contract(workspace_dir)

    result = await runtime_router.resolve_runtime_run_effect(
        SimpleNamespace(),
        contract.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectResolveRequest(
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
            expected_effect_revision=1,
            workspace_dir=str(workspace_dir),
            resolution="human_resolved",
            reason="用户确认外部点击已经完成",
            result_hash="sha256:manual-result",
        ),
    )

    records = journal.list_effects(contract.run_id, contract.attempt_id)
    assert [record.status for record in records] == [
        "intent",
        "running",
        "ambiguous",
        "human_resolved",
    ]
    assert records[-1].revision == 3
    assert records[-1].ambiguity_reason == "用户确认外部点击已经完成"
    assert records[-1].result_hash == "sha256:manual-result"
    assert result["resolved"] is True
    assert result["effect_status"] == "human_resolved"
    assert result["effect_revision"] == 3
    assert result["applied_actions"] == ["mark_ambiguous", "human_resolve"]


@pytest.mark.asyncio
async def test_resolve_runtime_run_effect_rejects_stale_effect_revision(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, _journal = await _write_running_effect_contract(workspace_dir)

    with pytest.raises(HTTPException) as exc:
        await runtime_router.resolve_runtime_run_effect(
            SimpleNamespace(),
            contract.run_id,
            "effect-click-publish",
            runtime_router.RuntimeEffectResolveRequest(
                expected_attempt_id=contract.attempt_id,
                expected_revision=contract.revision,
                expected_effect_revision=0,
                workspace_dir=str(workspace_dir),
                resolution="mark_ambiguous",
                reason="旧界面状态",
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "stale_runtime_effect"
    assert exc.value.detail["expected_effect_revision"] == 0
    assert exc.value.detail["current_effect_revision"] == 1


@pytest.mark.asyncio
async def test_repair_runtime_run_effect_rebuilds_checkpoint_without_replay(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, journal = await _write_committed_effect_contract(workspace_dir)

    result = await runtime_router.repair_runtime_run_effect(
        SimpleNamespace(),
        contract.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectRepairRequest(
            action="rebuild_checkpoint",
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
            expected_effect_revision=2,
            workspace_dir=str(workspace_dir),
            checkpoint_id="checkpoint-after-click",
            checkpoint_seq=12,
            checkpoint_summary="browser click committed",
        ),
    )

    saved = await RuntimeRunStore(workspace_dir).load_contract(contract.run_id)
    records = journal.list_effects(contract.run_id, contract.attempt_id)

    assert result["repaired"] is True
    assert result["action"] == "rebuild_checkpoint"
    assert result["previous_revision"] == 2
    assert result["revision"] == 3
    assert result["effect_status"] == "committed"
    assert result["checkpoint"]["checkpoint_id"] == "checkpoint-after-click"
    assert result["checkpoint"]["seq"] == 12
    assert saved is not None
    assert saved.revision == 3
    assert saved.status == "running"
    assert saved.checkpoint is not None
    assert saved.checkpoint.checkpoint_id == "checkpoint-after-click"
    assert saved.current_step is not None
    assert saved.current_step.kind == "checkpoint"
    assert [record.status for record in records] == [
        "intent",
        "running",
        "committed",
    ]


@pytest.mark.asyncio
async def test_repair_runtime_run_effect_rebuilds_checkpoint_for_unacknowledged_effect(
    monkeypatch,
    tmp_path,
) -> None:
    """A checkpoint for another effect is not an idempotent repair result."""

    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, _journal = await _write_committed_effect_contract(workspace_dir)
    other_checkpoint = contract.model_copy(
        update={
            "revision": 3,
            "checkpoint": RuntimeCheckpoint(
                checkpoint_id="checkpoint-other-effect",
                seq=11,
                effect_ids=("effect-other",),
                summary="other tool complete",
            ),
        },
    )
    store = RuntimeRunStore(workspace_dir)
    await store.save_contract(
        other_checkpoint,
        expected_revision=contract.revision,
        expected_attempt_id=contract.attempt_id,
    )

    result = await runtime_router.repair_runtime_run_effect(
        SimpleNamespace(),
        contract.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectRepairRequest(
            action="rebuild_checkpoint",
            expected_attempt_id=contract.attempt_id,
            expected_revision=other_checkpoint.revision,
            expected_effect_revision=2,
            workspace_dir=str(workspace_dir),
        ),
    )

    assert result["repaired"] is True
    assert result["idempotent"] is False
    assert result["checkpoint"]["effect_ids"] == [
        "effect-click-publish",
        "effect-other",
    ]


@pytest.mark.asyncio
async def test_repair_runtime_run_effect_requires_bound_result_for_before_tool(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, _journal = await _write_committed_effect_contract(workspace_dir)
    pending = RuntimePendingToolCall(
        call_id="call-click-publish",
        tool_name="browser.click",
        arguments={"selector": "#publish"},
    )
    checkpointed = contract.model_copy(
        update={
            "revision": 3,
            "checkpoint": RuntimeCheckpoint(
                checkpoint_id="checkpoint-before-click",
                seq=11,
                stage="tool_boundary",
                next_execution_position="before_tool",
                pending_tool_call=pending,
                compressed_context=(
                    ModelMessage.text("user", "publish it").model_dump_json(),
                ),
                model_rounds=2,
                tool_calls=1,
            ),
        },
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        checkpointed,
        expected_revision=contract.revision,
        expected_attempt_id=contract.attempt_id,
    )

    with pytest.raises(HTTPException) as exc:
        await runtime_router.repair_runtime_run_effect(
            SimpleNamespace(),
            contract.run_id,
            "effect-click-publish",
            runtime_router.RuntimeEffectRepairRequest(
                action="rebuild_checkpoint",
                expected_attempt_id=contract.attempt_id,
                expected_revision=checkpointed.revision,
                expected_effect_revision=2,
                workspace_dir=str(workspace_dir),
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "runtime_effect_repair_requires_tool_result"
    saved = await RuntimeRunStore(workspace_dir).load_contract(contract.run_id)
    assert saved == checkpointed


@pytest.mark.asyncio
async def test_repair_runtime_run_effect_preserves_checkpoint_and_binds_probe_result(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, _journal = await _write_committed_effect_contract(workspace_dir)
    pending = RuntimePendingToolCall(
        call_id="call-click-publish",
        tool_name="browser.click",
        arguments={"selector": "#publish"},
    )
    assistant_call = ModelMessage(
        id="assistant-click",
        role="assistant",
        content=[
            ModelContentBlock(
                type="tool_call",
                data={
                    "id": pending.call_id,
                    "name": pending.tool_name,
                    "arguments": dict(pending.arguments),
                },
            )
        ],
    )
    checkpointed = contract.model_copy(
        update={
            "revision": 3,
            "checkpoint": RuntimeCheckpoint(
                checkpoint_id="checkpoint-before-click",
                seq=11,
                stage="tool_boundary",
                next_execution_position="before_tool",
                pending_tool_call=pending,
                accepted_steer_ids=("steer-1",),
                artifact_digests=("sha256:artifact",),
                compressed_context=(assistant_call.model_dump_json(),),
                active_seconds=4.5,
                model_rounds=2,
                tool_calls=1,
                used_tokens=31,
                inbox_after_seq=7,
                segment_index=1,
                segment_tool_calls=1,
                segment_tokens=31,
            ),
        },
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        checkpointed,
        expected_revision=contract.revision,
        expected_attempt_id=contract.attempt_id,
    )

    result = await runtime_router.repair_runtime_run_effect(
        SimpleNamespace(),
        contract.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectRepairRequest(
            action="rebuild_checkpoint",
            expected_attempt_id=contract.attempt_id,
            expected_revision=checkpointed.revision,
            expected_effect_revision=2,
            workspace_dir=str(workspace_dir),
            tool_result={
                "call_id": pending.call_id,
                "name": pending.tool_name,
                "ok": True,
                "content": [{"type": "text", "text": "published receipt #7"}],
                "metadata": {"probe_receipt": "receipt-7"},
            },
        ),
    )

    saved = await RuntimeRunStore(workspace_dir).load_contract(contract.run_id)
    assert saved is not None and saved.checkpoint is not None
    repaired = saved.checkpoint
    assert result["checkpoint"]["next_execution_position"] == "after_tool"
    assert repaired.pending_tool_call is None
    assert repaired.effect_ids == ("effect-click-publish",)
    assert repaired.accepted_steer_ids == ("steer-1",)
    assert repaired.artifact_digests == ("sha256:artifact",)
    assert repaired.active_seconds == 4.5
    assert repaired.model_rounds == 2
    assert repaired.tool_calls == 1
    assert repaired.used_tokens == 31
    assert repaired.inbox_after_seq == 7
    assert repaired.segment_index == 1
    assert repaired.segment_tool_calls == 1
    assert repaired.segment_tokens == 31
    restored_messages = [
        ModelMessage.model_validate_json(raw) for raw in repaired.compressed_context
    ]
    assert restored_messages[-1].role == "tool"
    assert restored_messages[-1].tool_call_id == pending.call_id
    assert restored_messages[-1].content[0].data["output"][0]["text"] == (
        "published receipt #7"
    )


@pytest.mark.asyncio
async def test_repair_runtime_run_effect_rebuilds_stalled_checkpoint_after_probe(
    monkeypatch,
    tmp_path,
) -> None:
    """A crash-stalled run may bind a proved result before a fenced takeover."""

    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, _journal = await _write_committed_effect_contract(workspace_dir)
    pending = RuntimePendingToolCall(
        call_id="call-click-publish",
        tool_name="browser.click",
        arguments={"selector": "#publish"},
    )
    checkpointed = contract.model_copy(
        update={
            "revision": 3,
            "checkpoint": RuntimeCheckpoint(
                checkpoint_id="checkpoint-before-click",
                seq=11,
                stage="tool_boundary",
                next_execution_position="before_tool",
                pending_tool_call=pending,
                compressed_context=(
                    ModelMessage.text("user", "publish it").model_dump_json(),
                ),
            ),
        },
    )
    store = RuntimeRunStore(workspace_dir)
    await store.save_contract(
        checkpointed,
        expected_revision=contract.revision,
        expected_attempt_id=contract.attempt_id,
    )
    stalled = checkpointed.model_copy(
        update={
            "revision": 4,
            "status": "stalled",
            "lease_owner": None,
            "lease_expires_at": None,
            "settled_at": datetime(2026, 7, 16, 12, 1, tzinfo=timezone.utc),
            "stop_reason": RuntimeRunReason(
                code="recovery_effect_unknown",
                message="Process exited after external mutation.",
                category="runtime",
                retryable=False,
            ),
        },
    )
    await store.save_contract(
        stalled,
        expected_revision=checkpointed.revision,
        expected_attempt_id=checkpointed.attempt_id,
    )

    result = await runtime_router.repair_runtime_run_effect(
        SimpleNamespace(),
        stalled.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectRepairRequest(
            action="rebuild_checkpoint",
            expected_attempt_id=stalled.attempt_id,
            expected_revision=stalled.revision,
            expected_effect_revision=2,
            workspace_dir=str(workspace_dir),
            tool_result={
                "call_id": pending.call_id,
                "name": pending.tool_name,
                "ok": True,
                "content": [{"type": "text", "text": "probe receipt #8"}],
                "metadata": {"probe_receipt": "receipt-8"},
            },
        ),
    )

    saved = await store.load_contract(stalled.run_id)
    assert result["status"] == "stalled"
    assert saved is not None and saved.checkpoint is not None
    assert saved.status == "stalled"
    assert saved.checkpoint.next_execution_position == "after_tool"
    assert saved.checkpoint.pending_tool_call is None
    assert saved.checkpoint.effect_ids == ("effect-click-publish",)


@pytest.mark.asyncio
async def test_repair_runtime_run_effect_rewrites_terminal_after_checkpoint(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, journal = await _write_committed_effect_contract(workspace_dir)
    checkpointed = contract.model_copy(
        update={
            "revision": 3,
            "checkpoint": RuntimeCheckpoint(
                checkpoint_id="checkpoint-after-click",
                seq=12,
                effect_ids=("effect-click-publish",),
                summary="browser click committed",
            ),
        },
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        checkpointed,
        expected_revision=contract.revision,
        expected_attempt_id=contract.attempt_id,
    )

    result = await runtime_router.repair_runtime_run_effect(
        SimpleNamespace(),
        contract.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectRepairRequest(
            action="rewrite_terminal",
            expected_attempt_id=contract.attempt_id,
            expected_revision=3,
            expected_effect_revision=2,
            workspace_dir=str(workspace_dir),
            terminal_status="succeeded",
            reason="checkpoint repaired and no further work remains",
        ),
    )

    saved = await RuntimeRunStore(workspace_dir).load_contract(contract.run_id)
    records = journal.list_effects(contract.run_id, contract.attempt_id)

    assert result["repaired"] is True
    assert result["action"] == "rewrite_terminal"
    assert result["previous_revision"] == 3
    assert result["revision"] == 4
    assert result["status"] == "succeeded"
    assert saved is not None
    assert saved.revision == 4
    assert saved.status == "succeeded"
    assert saved.stop_reason is not None
    assert saved.stop_reason.code == "runtime_effect_terminal_repair"
    assert saved.stop_reason.message == "checkpoint repaired and no further work remains"
    assert [record.status for record in records] == [
        "intent",
        "running",
        "committed",
    ]


@pytest.mark.parametrize(
    (
        "resolution",
        "expected_status",
        "expected_actions",
        "result_hash",
        "checkpoint_id",
        "checkpoint_summary",
        "terminal_reason",
    ),
    [
        (
            "probe_committed",
            "probed_committed",
            ["mark_ambiguous", "probe_committed"],
            "sha256:probe-result",
            "checkpoint-after-probe",
            "probe confirmed click effect",
            "probe and checkpoint repair completed the run",
        ),
        (
            "human_resolved",
            "human_resolved",
            ["mark_ambiguous", "human_resolve"],
            "sha256:manual-result",
            "checkpoint-after-human-resolution",
            "operator confirmed click effect",
            "manual resolution and checkpoint repair completed the run",
        ),
    ],
)
@pytest.mark.asyncio
async def test_runtime_run_effect_resolution_repair_flow_refreshes_fences_without_replay(
    monkeypatch,
    tmp_path,
    resolution,
    expected_status,
    expected_actions,
    result_hash,
    checkpoint_id,
    checkpoint_summary,
    terminal_reason,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, journal = await _write_running_effect_contract(workspace_dir)

    resolved = await runtime_router.resolve_runtime_run_effect(
        SimpleNamespace(),
        contract.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectResolveRequest(
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
            expected_effect_revision=1,
            workspace_dir=str(workspace_dir),
            resolution=resolution,
            reason=checkpoint_summary,
            result_hash=result_hash,
        ),
    )

    assert resolved["resolved"] is True
    assert resolved["effect_status"] == expected_status
    assert resolved["effect_revision"] == 3
    assert resolved["revision"] == contract.revision
    assert resolved["applied_actions"] == expected_actions

    checkpointed = await runtime_router.repair_runtime_run_effect(
        SimpleNamespace(),
        contract.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectRepairRequest(
            action="rebuild_checkpoint",
            expected_attempt_id=contract.attempt_id,
            expected_revision=resolved["revision"],
            expected_effect_revision=resolved["effect_revision"],
            workspace_dir=str(workspace_dir),
            checkpoint_id=checkpoint_id,
            checkpoint_seq=27,
            checkpoint_summary=checkpoint_summary,
        ),
    )

    assert checkpointed["repaired"] is True
    assert checkpointed["idempotent"] is False
    assert checkpointed["revision"] == contract.revision + 1
    assert checkpointed["effect_revision"] == resolved["effect_revision"]
    assert checkpointed["checkpoint"]["checkpoint_id"] == checkpoint_id

    terminal = await runtime_router.repair_runtime_run_effect(
        SimpleNamespace(),
        contract.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectRepairRequest(
            action="rewrite_terminal",
            expected_attempt_id=contract.attempt_id,
            expected_revision=checkpointed["revision"],
            expected_effect_revision=resolved["effect_revision"],
            workspace_dir=str(workspace_dir),
            terminal_status="succeeded",
            reason=terminal_reason,
        ),
    )

    saved = await RuntimeRunStore(workspace_dir).load_contract(contract.run_id)
    records = journal.list_effects(contract.run_id, contract.attempt_id)

    assert terminal["repaired"] is True
    assert terminal["idempotent"] is False
    assert terminal["revision"] == checkpointed["revision"] + 1
    assert terminal["status"] == "succeeded"
    assert saved is not None
    assert saved.revision == terminal["revision"]
    assert saved.status == "succeeded"
    assert saved.checkpoint is not None
    assert saved.checkpoint.checkpoint_id == checkpoint_id
    assert saved.stop_reason is not None
    assert saved.stop_reason.code == "runtime_effect_terminal_repair"
    assert saved.stop_reason.message == terminal_reason
    assert [record.status for record in records] == [
        "intent",
        "running",
        "ambiguous",
        expected_status,
    ]

    repeated_terminal = await runtime_router.repair_runtime_run_effect(
        SimpleNamespace(),
        contract.run_id,
        "effect-click-publish",
        runtime_router.RuntimeEffectRepairRequest(
            action="rewrite_terminal",
            expected_attempt_id=contract.attempt_id,
            expected_revision=terminal["revision"],
            expected_effect_revision=resolved["effect_revision"],
            workspace_dir=str(workspace_dir),
            terminal_status="succeeded",
            reason="repeat from refreshed UI state",
        ),
    )

    assert repeated_terminal["repaired"] is True
    assert repeated_terminal["idempotent"] is True
    assert repeated_terminal["revision"] == terminal["revision"]
    assert [record.status for record in journal.list_effects(contract.run_id, contract.attempt_id)] == [
        "intent",
        "running",
        "ambiguous",
        expected_status,
    ]


@pytest.mark.asyncio
async def test_repair_runtime_run_effect_rejects_terminal_without_checkpoint(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    contract, _journal = await _write_committed_effect_contract(workspace_dir)

    with pytest.raises(HTTPException) as exc:
        await runtime_router.repair_runtime_run_effect(
            SimpleNamespace(),
            contract.run_id,
            "effect-click-publish",
            runtime_router.RuntimeEffectRepairRequest(
                action="rewrite_terminal",
                expected_attempt_id=contract.attempt_id,
                expected_revision=contract.revision,
                expected_effect_revision=2,
                workspace_dir=str(workspace_dir),
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "runtime_effect_repair_requires_checkpoint"


@pytest.mark.asyncio
async def test_interrupt_runtime_run_uses_generation_fence(
    monkeypatch,
    tmp_path,
) -> None:
    topic_workspace = tmp_path / "topic-workspace"
    chat = SimpleNamespace(
        id="chat-a",
        session_id="session-a",
        meta={
            "topic_workspace_dir": str(topic_workspace),
            "pending_turn": {
                "run_id": "current-run",
                "turn_id": "current-turn",
            },
        },
    )
    tracker = _FakeRuntimeTaskTracker(
        snapshot={"status": "running", "generation": 7},
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=_FakeRuntimeChatManager([chat]),
        task_tracker=tracker,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.interrupt_runtime_run(
        SimpleNamespace(),
        "current-run",
    )

    assert result == {
        "stopped": True,
        "run_id": "current-run",
        "chat_id": "chat-a",
    }
    assert tracker.snapshot_requests == ["chat-a"]
    assert tracker.fenced_stop_requests == [("chat-a", 7)]
    assert tracker.raw_stop_requests == []


@pytest.mark.asyncio
async def test_list_runtime_runs_refreshes_expired_runtime_contract(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    old = datetime(2000, 1, 1, 10, 0, tzinfo=timezone.utc)
    contract = RuntimeRunContract(
        run_id="expired-runtime-run",
        attempt_id="expired-runtime-run:attempt-1",
        source="capability",
        owner=_effect_resolution_owner(workspace_dir),
        status="running",
        revision=2,
        lease_owner="expired-worker",
        lease_expires_at=old,
        created_at=old - timedelta(minutes=30),
        started_at=old - timedelta(minutes=29),
        heartbeat_at=old - timedelta(minutes=10),
        budget=RuntimeBudget(),
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=workspace_dir,
        chat_manager=_FakeRuntimeChatManager([]),
        task_tracker=_FakeRuntimeTaskTracker(snapshot=None, status="idle"),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.list_runtime_runs(
        SimpleNamespace(),
        source=None,
        status=None,
        limit=100,
    )
    saved = await RuntimeRunStore(workspace_dir).load_contract(contract.run_id)

    assert saved is not None
    assert saved.status == "stalled"
    assert saved.revision == 3
    assert saved.error is not None
    assert saved.error.code == "loop_lease_expired"
    assert result["items"][0]["id"] == contract.run_id
    assert result["items"][0]["authority_status"] == "stalled"


@pytest.mark.asyncio
async def test_get_runtime_run_events_returns_store_only_contract_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
    contract = RuntimeRunContract(
        run_id="store-only-run",
        attempt_id="store-only-run:attempt-1",
        source="capability",
        owner=_effect_resolution_owner(workspace_dir),
        status="running",
        revision=2,
        lease_owner="worker",
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        budget=RuntimeBudget(),
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=workspace_dir,
        chat_manager=_FakeRuntimeChatManager([]),
        task_tracker=_FakeRuntimeTaskTracker(snapshot=None, status="idle"),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.get_runtime_run_events(
        SimpleNamespace(),
        contract.run_id,
        after_seq=0,
        limit=None,
        attempt_id=None,
    )

    assert result["run_id"] == contract.run_id
    assert result["attempt_id"] == contract.attempt_id
    assert result["workspace_dir"] == str(workspace_dir)
    assert result["events"] == []


@pytest.mark.asyncio
async def test_update_runtime_run_budget_persists_and_records_event(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    now = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
    owner = _effect_resolution_owner(workspace_dir)
    contract = RuntimeRunContract(
        run_id="budget-run",
        attempt_id="budget-run:attempt-1",
        source="capability",
        owner=owner,
        status="running",
        revision=2,
        lease_owner="budget-worker",
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        budget=RuntimeBudget(max_steps=5, max_seconds=300),
        contract_hash="sha256:contract",
        policy_snapshot_hash="sha256:policy",
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )

    result = await runtime_router.update_runtime_run_budget(
        SimpleNamespace(),
        contract.run_id,
        runtime_router.RuntimeRunBudgetUpdateRequest(
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
            workspace_dir=str(workspace_dir),
            budget=RuntimeBudget(max_steps=9, max_seconds=600),
            changed_by="user",
            reason="User approved a larger step budget.",
        ),
    )

    saved = await RuntimeRunStore(workspace_dir).load_contract(contract.run_id)
    events = build_runtime_run_events_response([workspace_dir], contract.run_id)

    assert result["changed"] is True
    assert result["previous_revision"] == 2
    assert result["revision"] == 3
    assert result["budget"]["max_steps"] == 9
    assert saved is not None
    assert saved.revision == 3
    assert saved.budget.max_steps == 9
    assert events.events[-1].type == "runtime.budget_changed"
    assert events.events[-1].payload["changed_by"] == "user"
    assert events.events[-1].payload["event_hash"] == result["budget_event"]["event_hash"]
    assert events.events[-1].payload["previous_budget_hash"] != (
        events.events[-1].payload["next_budget_hash"]
    )


@pytest.mark.asyncio
async def test_update_runtime_run_budget_rejects_stale_revision(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    now = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
    contract = RuntimeRunContract(
        run_id="budget-stale-run",
        attempt_id="budget-stale-run:attempt-1",
        source="capability",
        owner=_effect_resolution_owner(workspace_dir),
        status="running",
        revision=4,
        lease_owner="budget-worker",
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        budget=RuntimeBudget(max_steps=5),
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(contract, expected_revision=None)

    with pytest.raises(HTTPException) as exc:
        await runtime_router.update_runtime_run_budget(
            SimpleNamespace(),
            contract.run_id,
            runtime_router.RuntimeRunBudgetUpdateRequest(
                expected_attempt_id=contract.attempt_id,
                expected_revision=3,
                workspace_dir=str(workspace_dir),
                budget=RuntimeBudget(max_steps=9),
                changed_by="user",
                reason="Stale UI request.",
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "stale_attempt"
    assert exc.value.detail["current_revision"] == 4


@pytest.mark.asyncio
async def test_update_runtime_run_budget_rejects_terminal_run(
    monkeypatch,
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    await _install_effect_resolution_workspace(monkeypatch, workspace_dir)
    now = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
    contract = RuntimeRunContract(
        run_id="budget-terminal-run",
        attempt_id="budget-terminal-run:attempt-1",
        source="capability",
        owner=_effect_resolution_owner(workspace_dir),
        status="succeeded",
        revision=4,
        lease_owner="budget-worker",
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        settled_at=now,
        budget=RuntimeBudget(max_steps=5),
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(contract, expected_revision=None)

    with pytest.raises(HTTPException) as exc:
        await runtime_router.update_runtime_run_budget(
            SimpleNamespace(),
            contract.run_id,
            runtime_router.RuntimeRunBudgetUpdateRequest(
                expected_attempt_id=contract.attempt_id,
                expected_revision=contract.revision,
                workspace_dir=str(workspace_dir),
                budget=RuntimeBudget(max_steps=9),
                changed_by="policy",
                reason="Terminal budget update should be rejected.",
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "runtime_transition_rejected"
    assert "terminal" in exc.value.detail["message"]


def test_update_runtime_run_budget_rejects_prompt_actor() -> None:
    with pytest.raises(ValidationError):
        runtime_router.RuntimeRunBudgetUpdateRequest(
            budget=RuntimeBudget(max_steps=9),
            changed_by="prompt",
            reason="Prompt tried to override the budget.",
        )
