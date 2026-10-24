# -*- coding: utf-8 -*-
"""Runtime event compatibility layer tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lantai.app.experience.events import (
    append_chat_event,
    append_task_event,
    build_runtime_notification_payload,
    build_runtime_events_response,
    build_runtime_run_events_response,
    build_turn_event_preview,
    collect_runtime_runs,
    record_loop_budget_changed,
    record_runtime_notification,
    record_skills_changed,
    record_chat_turn_started,
    record_chat_turn_settled,
    record_chat_turn_stop_requested,
    RuntimeEventSequenceAppendError,
)
from lantai.app.experience import events as experience_events
from lantai.app.experience.schemas import TurnEventPreview
from lantai.app.runner.models import ChatSpec
from lantai.app.runner.runtime_state import (
    CanonicalChatRuntimeIdentity,
    build_chat_runtime_state,
    resolve_canonical_chat_runtime_identity,
)
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.runs import (
    RuntimeBudget,
    RuntimeCheckpoint,
    RuntimeEffectRecord,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeStepRef,
    RuntimeTaskContract,
)
from lantai.runtime.loops import (
    LoopBudget,
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopRubric,
    LoopRubricDimension,
    build_loop_budget_change_record,
)
from lantai.runtime.store import RuntimeRunStore


def _canonical_chat_identity(
    run_id: str,
    attempt_id: str,
) -> CanonicalChatRuntimeIdentity:
    return CanonicalChatRuntimeIdentity(
        run_id=run_id,
        attempt_id=attempt_id,
        control_revision=2,
        generation=1,
        task_revision=0,
        lease_owner="chat:chat-active",
        workspace_dir="/tmp/chat-active",
        source="chat",
        owner_session_id="console:active",
        owner_agent_id="default",
    )


def test_unknown_experience_kind_maps_to_warning_not_completed() -> None:
    assert (
        experience_events._runtime_type_for_kind(  # pylint: disable=protected-access
            "future.provider.unknown",
            "info",
        )
        == "runtime.warning"
    )


def test_experience_terminal_types_are_reserved_for_turn_endings() -> None:
    terminal_types = {
        "turn.completed",
        "turn.failed",
        "turn.interrupted",
        "turn.stalled",
    }

    assert (
        experience_events._runtime_type_for_kind(  # pylint: disable=protected-access
            "chat.turn.settled",
            "finished",
        )
        == "turn.completed"
    )
    assert (
        experience_events._runtime_type_for_kind(  # pylint: disable=protected-access
            "chat.turn.settled",
            "failed",
        )
        == "turn.failed"
    )
    assert (
        experience_events._runtime_type_for_kind(  # pylint: disable=protected-access
            "chat.turn.settled",
            "interrupted",
        )
        == "turn.interrupted"
    )
    assert (
        experience_events._runtime_type_for_kind(  # pylint: disable=protected-access
            "chat.turn.settled",
            "stalled",
        )
        == "turn.stalled"
    )
    assert (
        experience_events._runtime_type_for_kind(  # pylint: disable=protected-access
            "chat.system.event",
            "stalled",
        )
        == "turn.stalled"
    )

    control_or_item_events = [
        ("chat.error", "failed"),
        ("chat.turn.stop_requested", "interrupted"),
        ("chat.message.completed", "completed"),
        ("chat.final_answer", "completed"),
        ("chat.tool_call.finished", "failed"),
        ("cron.job.settled", "interrupted"),
        ("image.task.settled", "failed"),
        ("skills.changed", "completed"),
    ]
    for kind, status in control_or_item_events:
        assert (
            experience_events._runtime_type_for_kind(  # pylint: disable=protected-access
                kind,
                status,
            )
            not in terminal_types
        )


@pytest.mark.asyncio
async def test_chat_error_does_not_create_duplicate_turn_terminal(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        kind="chat.error",
        status="failed",
        summary="本轮回复失败",
        payload={"status": "failed"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        kind="chat.turn.settled",
        status="failed",
        summary="运行失败",
        payload={"final_task_status": "failed"},
    )

    response = build_runtime_events_response(workspace_dir, "chat-runtime")

    assert [event.type for event in response.events] == [
        "runtime.error",
        "turn.failed",
    ]


@pytest.mark.asyncio
async def test_runtime_events_can_filter_collaboration_tool_types(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        item_id="call-1",
        kind="chat.tool_call.started",
        type="tool.call.started",
        status="running",
        summary="协作进行中",
        payload={"collaboration": {"batch_id": "batch-1", "revision": 1}},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        kind="chat.message.delta",
        type="message.delta",
        status="running",
        summary="正文增量",
        payload={"delta": "不应返回"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        item_id="call-1",
        kind="chat.tool_call.finished",
        type="tool.call.completed",
        status="completed",
        summary="协作完成",
        payload={"collaboration": {"batch_id": "batch-1", "revision": 2}},
    )

    response = build_runtime_events_response(
        workspace_dir,
        "chat-runtime",
        event_types={
            "tool.call.started",
            "tool.call.delta",
            "tool.call.completed",
        },
    )

    assert [event.type for event in response.events] == [
        "tool.call.started",
        "tool.call.completed",
    ]
    assert response.latest_seq == response.events[-1].seq


@pytest.mark.asyncio
async def test_runtime_events_roundtrip_and_after_seq(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    first = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "hello"},
    )
    second = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        kind="chat.final_answer",
        status="completed",
        summary="本轮回复完成",
        payload={
            "usage": {
                "input_tokens": 123,
                "output_tokens": 45,
                "cache_tokens": 67,
                "context_tokens": 890,
                "cost_points": 9,
            },
        },
    )

    assert first.seq == 1
    assert second.seq == 2

    response = build_runtime_events_response(workspace_dir, "chat-runtime")

    assert response.latest_seq == 2
    assert [event.type for event in response.events] == [
        "turn.started",
        "item.completed",
    ]
    assert response.events[-1].usage == {
        "input_tokens": 123,
        "output_tokens": 45,
        "cache_tokens": 67,
        "context_tokens": 890,
        "cost_points": 9,
    }

    resumed = build_runtime_events_response(
        workspace_dir,
        "chat-runtime",
        after_seq=1,
    )

    assert resumed.after_seq == 1
    assert resumed.latest_seq == 2
    assert len(resumed.events) == 1
    assert resumed.events[0].seq == 2


@pytest.mark.asyncio
async def test_runtime_event_stream_epoch_and_cursor_are_stable(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        kind="chat.turn.started",
        status="running",
        summary="started",
    )

    first = build_runtime_events_response(workspace_dir, "chat-runtime")
    resumed = build_runtime_events_response(
        workspace_dir,
        "chat-runtime",
        after_seq=first.next_after_seq or 0,
        expected_stream_epoch=first.stream_epoch,
    )

    assert len(first.stream_epoch) == 24
    assert first.cursor == f"{first.stream_epoch}:1"
    assert first.earliest_seq == 1
    assert first.ledger_latest_seq == 1
    assert resumed.stream_epoch == first.stream_epoch
    assert resumed.resync_required is False
    assert resumed.events == []


@pytest.mark.asyncio
async def test_runtime_event_stream_requests_resync_for_stale_generation(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    event = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        kind="chat.turn.started",
        status="running",
        summary="started",
    )

    response = build_runtime_events_response(
        workspace_dir,
        "chat-runtime",
        after_seq=99,
        expected_stream_epoch="previous-ledger",
    )

    assert response.resync_required is True
    assert response.resync_reason == "stream_epoch_mismatch"
    assert response.after_seq == 99
    assert response.next_after_seq == event.seq
    assert [item.id for item in response.events] == [event.id]


@pytest.mark.asyncio
async def test_runtime_event_stream_requests_resync_when_cursor_is_ahead(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    event = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="turn-1",
        kind="chat.turn.started",
        status="running",
        summary="started",
    )
    initial = build_runtime_events_response(workspace_dir, "chat-runtime")

    response = build_runtime_events_response(
        workspace_dir,
        "chat-runtime",
        after_seq=99,
        expected_stream_epoch=initial.stream_epoch,
    )

    assert response.resync_required is True
    assert response.resync_reason == "cursor_ahead_of_ledger"
    assert response.next_after_seq == event.seq
    assert [item.id for item in response.events] == [event.id]


@pytest.mark.asyncio
async def test_loop_budget_changed_records_canonical_runtime_event(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    previous = LoopContract(
        contract_id="loop-budget-1",
        objective="Keep budget changes auditable",
        scope=["Runtime migration"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="audit",
                title="Budget audit",
                acceptance=["Budget changes are evented"],
            )
        ],
        acceptance=["Every budget change writes an event"],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="runtime-tests",
                name="Runtime tests",
                kind="test",
                command="uv run pytest tests/unit/runtime -q",
            )
        ],
        rubric=LoopRubric(
            rubric_id="budget-audit",
            dimensions=[
                LoopRubricDimension(
                    dimension_id="correctness",
                    name="Correctness",
                    weight=1,
                )
            ],
        ),
        budgets=LoopBudget(max_steps=5),
    )
    next_contract = previous.model_copy(
        update={"budgets": previous.budgets.model_copy(update={"max_steps": 8})}
    )
    record = build_loop_budget_change_record(
        previous,
        next_contract,
        changed_by="user",
        reason="User approved a larger step budget.",
    )

    event = await record_loop_budget_changed(
        workspace_dir=workspace_dir,
        agent_id="default",
        target_id="loop:loop-budget-1",
        session_id="console:loop-budget",
        run_id="loop-run-1",
        record=record,
    )
    details = build_runtime_run_events_response([workspace_dir], "loop-run-1")

    assert event.kind == "loop.budget.changed"
    assert event.type == "runtime.budget_changed"
    assert event.payload["changed_by"] == "user"
    assert details.events[-1].type == "runtime.budget_changed"
    assert details.events[-1].source == "system"
    assert details.events[-1].payload["event_hash"] == record.event_hash
    assert details.events[-1].payload["previous_budget_hash"] != (
        details.events[-1].payload["next_budget_hash"]
    )


@pytest.mark.asyncio
async def test_runtime_notification_records_canonical_advisory_event(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    run = RuntimeRunContract(
        run_id="run-notify",
        attempt_id="run-notify:attempt-1",
        source="capability",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id="default",
            session_id="console:notify",
            workspace_id=str(workspace_dir),
            channel="console",
        ),
        status="waiting_approval",
        revision=4,
        current_step=RuntimeStepRef(
            step_id="approval-1",
            index=2,
            kind="approval",
            title="等待发布确认",
        ),
        contract_hash="sha256:contract",
        release_edition="official",
    )

    event = await record_runtime_notification(
        workspace_dir=workspace_dir,
        agent_id="default",
        target_id="runtime:run-notify",
        session_id="console:notify",
        run=run,
        notification_kind="waiting_approval",
        contract_id="loop-notify",
        trigger_reason="human_checkpoint",
        action_url="/tasks?run=run-notify",
    )
    details = build_runtime_run_events_response([workspace_dir], "run-notify")

    assert event.kind == "runtime.notification"
    assert event.type == "runtime.notification"
    assert event.payload["notification_kind"] == "waiting_approval"
    assert event.payload["title"] == "运行等待审批"
    assert event.payload["message"] == "等待发布确认"
    assert event.payload["contract_id"] == "loop-notify"
    assert event.payload["contract_hash"] == "sha256:contract"
    assert event.payload["export_recommended"] is False
    assert details.events[-1].type == "runtime.notification"
    assert details.events[-1].payload["dedupe_key"] == (
        "runtime-notification:run-notify:run-notify:attempt-1:"
        "waiting_approval:waiting_approval:human_checkpoint"
    )


@pytest.mark.parametrize(
    ("notification_kind", "status", "export_recommended"),
    [
        ("waiting_approval", "waiting_approval", False),
        ("ambiguous_effect", "running", True),
        ("budget_exhausted", "failed", True),
        ("final_complete", "succeeded", True),
    ],
)
def test_runtime_notification_payload_defaults_cover_loop04_triggers(
    tmp_path,
    notification_kind,
    status,
    export_recommended,
):
    run = RuntimeRunContract(
        run_id=f"run-{notification_kind}",
        attempt_id=f"run-{notification_kind}:attempt-1",
        source="capability",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id="default",
            session_id="console:notify",
            workspace_id=str(tmp_path),
            channel="console",
        ),
        status=status,
        revision=4,
        release_edition="official",
    )

    payload = build_runtime_notification_payload(
        run=run,
        notification_kind=notification_kind,
        contract_id="loop-notify",
        trigger_reason="loop04",
    )

    assert payload.schema_version == 1
    assert payload.notification_kind == notification_kind
    assert payload.run_id == run.run_id
    assert payload.attempt_id == run.attempt_id
    assert payload.contract_id == "loop-notify"
    assert payload.source_status == status
    assert payload.export_recommended is export_recommended
    assert payload.dedupe_key.endswith(f"{status}:loop04")


@pytest.mark.asyncio
async def test_continue_requested_experience_event_maps_to_canonical_runtime_event(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runtime",
        session_id="console:test",
        run_id="run-continue",
        item_id="continue-1",
        kind="chat.turn.continue_requested",
        status="info",
        summary="请求继续",
        payload={
            "request_id": "continue-1",
            "mode": "retry",
            "run_id": "run-continue",
            "attempt_id": "attempt-1",
            "requested_by": "user-1",
            "revision": 3,
        },
    )

    response = build_runtime_events_response(workspace_dir, "chat-runtime")

    assert response.events[0].schema_version == 1
    assert response.events[0].type == "turn.continue_requested"
    assert response.events[0].source == "user"
    assert response.events[0].payload["mode"] == "retry"


@pytest.mark.asyncio
async def test_runtime_events_filter_other_chats(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-a",
        session_id="console:a",
        run_id="turn-a",
        kind="chat.turn.started",
        status="running",
        summary="a",
        payload={},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-b",
        session_id="console:b",
        run_id="turn-b",
        kind="chat.turn.started",
        status="running",
        summary="b",
        payload={},
    )

    response = build_runtime_events_response(workspace_dir, "chat-b")

    assert len(response.events) == 1
    assert response.events[0].turn_id == "turn-b"
    assert response.latest_seq == 2


@pytest.mark.asyncio
async def test_turn_stop_requested_is_control_event_not_terminal(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await record_chat_turn_stop_requested(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-stop",
        session_id="console:stop",
        requested_chat_id="turn-from-ui",
        resolved_chat_id="chat-stop",
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-stop",
        session_id="console:stop",
        run_id="chat-stop",
        kind="chat.turn.settled",
        status="interrupted",
        summary="运行已中断",
        payload={"final_task_status": "interrupted"},
    )

    response = build_runtime_events_response(workspace_dir, "chat-stop")
    assert [event.type for event in response.events] == [
        "runtime.warning",
        "turn.interrupted",
    ]
    event = response.events[0]

    assert event.status == "interrupted"
    assert event.payload["phase"] == "model"
    assert event.payload["requested_chat_id"] == "turn-from-ui"
    assert event.payload["resolved_chat_id"] == "chat-stop"
    assert event.payload["detail"] == "停止请求已转到当前会话"


@pytest.mark.asyncio
async def test_stalled_turn_settled_maps_to_stalled_terminal(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await record_chat_turn_settled(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-stalled",
        session_id="console:stalled",
        final_task_status="stalled",
        pending_turn_cleared=True,
        active_run_event_count=4,
        run_id="turn-stalled",
    )

    response = build_runtime_events_response(workspace_dir, "chat-stalled")

    assert [event.type for event in response.events] == ["turn.stalled"]
    assert response.events[0].status == "stalled"
    assert response.events[0].turn_id == "turn-stalled"
    assert response.events[0].payload["final_task_status"] == "stalled"


@pytest.mark.asyncio
async def test_delivered_outcome_survives_settle_preview_and_runtime_state(
    tmp_path,
):
    workspace_dir = tmp_path / "topic-workspace"

    await record_chat_turn_settled(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-delivered",
        session_id="console:delivered",
        final_task_status="finished",
        outcome_status="delivered",
        pending_turn_cleared=True,
        active_run_event_count=4,
        run_id="turn-delivered",
    )

    preview = build_turn_event_preview(workspace_dir, "chat-delivered")
    assert preview is not None
    assert preview.events[-1].payload["outcome_status"] == "delivered"

    state = build_chat_runtime_state(
        chat_spec=ChatSpec(
            id="chat-delivered",
            session_id="console:delivered",
            user_id="default",
            channel="console",
        ),
        tracker_status="idle",
        active_run=None,
        turn_event_preview=preview,
        pending_turn={},
    )

    assert state is not None
    assert state.status == "completed"
    assert state.run_id == "turn-delivered"
    assert state.outcome_status == "delivered"


def test_active_run_does_not_inherit_previous_run_delivery_outcome() -> None:
    state = build_chat_runtime_state(
        chat_spec=ChatSpec(
            id="chat-active",
            session_id="console:active",
            user_id="default",
            channel="console",
            meta={
                "last_turn_run_id": "run-current",
                "last_turn_outcome_run_id": "run-previous",
                "last_turn_outcome_status": "delivered",
                "last_turn_settled_at": "2026-08-09T08:03:15Z",
                "pending_turn": {
                    "run_id": "run-current",
                    "status": "running",
                    "text": "build a product",
                },
            },
        ),
        tracker_status="running",
        active_run={
            "run_key": "run-current",
            "status": "running",
            "attempt_id": "run-current:attempt-2",
        },
        turn_event_preview=None,
        pending_turn={
            "run_id": "run-current",
            "client_turn_id": "client-current",
            "status": "running",
            "text": "build a product",
        },
        canonical_identity=_canonical_chat_identity(
            "run-current",
            "run-current:attempt-2",
        ),
    )

    assert state is not None
    assert state.status == "running"
    assert state.run_id == "run-current"
    assert state.client_turn_id == "client-current"
    assert state.attempt_id == "run-current:attempt-2"
    assert state.outcome_status == "unknown"


def test_active_run_does_not_mix_client_turn_from_different_pending_run() -> None:
    state = build_chat_runtime_state(
        chat_spec=ChatSpec(
            id="chat-active-pending-conflict",
            session_id="console:active-pending-conflict",
            user_id="default",
            channel="console",
        ),
        tracker_status="running",
        active_run={
            "run_key": "run-active",
            "status": "running",
            "attempt_id": "run-active:attempt-1",
        },
        turn_event_preview=None,
        pending_turn={
            "run_id": "run-stale-pending",
            "client_turn_id": "client-stale-pending",
            "status": "running",
            "text": "stale pending text",
        },
        canonical_identity=_canonical_chat_identity(
            "run-active",
            "run-active:attempt-1",
        ),
    )

    assert state is not None
    assert state.run_id == "run-active"
    assert state.client_turn_id is None
    assert state.attempt_id == "run-active:attempt-1"


@pytest.mark.asyncio
async def test_chat_runtime_identity_resolves_tracker_aliases_from_store(
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    chat = ChatSpec(
        id="chat-alias-lock",
        session_id="console:alias-lock",
        user_id="default",
        channel="console",
        meta={
            "topic_workspace_dir": str(workspace_dir),
            "last_turn_run_id": "run-alias-lock",
            "pending_turn": {
                "run_id": "run-alias-lock",
                "status": "running",
                "text": "continue",
            },
        },
    )
    await record_chat_turn_started(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        text_preview="continue",
        effective_workspace_dir=str(workspace_dir),
        run_id="run-alias-lock",
        task_contract=RuntimeTaskContract(
            objective="Continue the active chat turn.",
            budget=RuntimeBudget(),
        ),
    )
    tracker_alias = {
        "run_key": chat.id,
        "status": "running",
        "attempt_id": "run-alias-lock",
        "generation": 7,
        "root_session_id": chat.session_id,
    }

    identity = await resolve_canonical_chat_runtime_identity(
        chat_spec=chat,
        tracker_status="running",
        active_run=tracker_alias,
        run_hint="run-alias-lock",
        agent_id="default",
        root_workspace_dir=tmp_path,
    )

    assert identity is not None
    assert identity.run_id == "run-alias-lock"
    assert identity.attempt_id == "run-alias-lock:chat-attempt-1"
    assert identity.control_revision == 2
    assert identity.task_revision == 0
    assert identity.generation == 7
    assert identity.lease_owner == f"chat:{chat.id}"
    assert identity.workspace_dir == str(workspace_dir)
    assert identity.source == "chat"
    assert identity.owner_session_id == chat.session_id
    assert identity.owner_agent_id == "default"

    preview = build_turn_event_preview(workspace_dir, chat.id)
    state = build_chat_runtime_state(
        chat_spec=chat,
        tracker_status="running",
        active_run=tracker_alias,
        turn_event_preview=preview,
        pending_turn=chat.meta["pending_turn"],
        canonical_identity=identity,
    )
    assert state is not None
    assert state.run_id == "run-alias-lock"
    assert state.run_id != chat.id
    assert state.attempt_id == "run-alias-lock:chat-attempt-1"
    assert state.attempt_id != tracker_alias["attempt_id"]


@pytest.mark.asyncio
async def test_chat_runtime_identity_rejects_foreign_and_terminal_contracts(
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "topic-workspace"
    chat = ChatSpec(
        id="chat-owner",
        session_id="console:owner",
        user_id="default",
        channel="console",
        meta={
            "topic_workspace_dir": str(workspace_dir),
            "last_turn_run_id": "run-owner",
        },
    )
    await record_chat_turn_started(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        text_preview="continue",
        effective_workspace_dir=str(workspace_dir),
        run_id="run-owner",
    )
    active_alias = {
        "status": "running",
        "attempt_id": "run-owner",
        "generation": 1,
        "root_session_id": chat.session_id,
    }
    foreign_chat = chat.model_copy(update={"id": "chat-foreign"})

    assert await resolve_canonical_chat_runtime_identity(
        chat_spec=foreign_chat,
        tracker_status="running",
        active_run=active_alias,
        run_hint="run-owner",
        agent_id="default",
        root_workspace_dir=tmp_path,
    ) is None

    await record_chat_turn_settled(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        final_task_status="finished",
        pending_turn_cleared=True,
        active_run_event_count=1,
        run_id="run-owner",
    )
    assert await resolve_canonical_chat_runtime_identity(
        chat_spec=chat,
        tracker_status="running",
        active_run=active_alias,
        run_hint="run-owner",
        agent_id="default",
        root_workspace_dir=tmp_path,
    ) is None


def test_child_failure_does_not_settle_pending_parent_runtime_state() -> None:
    state = build_chat_runtime_state(
        chat_spec=ChatSpec(
            id="chat-parent-with-child",
            session_id="console:parent-with-child",
            user_id="default",
            channel="console",
            meta={"last_turn_run_id": "run-parent"},
        ),
        tracker_status="idle",
        active_run=None,
        turn_event_preview=TurnEventPreview.model_validate(
            {
                "summary_line": "子任务失败",
                "events": [
                    {
                        "id": "parent-started",
                        "ts": "2026-09-05T08:00:00Z",
                        "run_id": "run-parent",
                        "kind": "chat.turn.started",
                        "type": "turn.started",
                        "status": "running",
                        "label": "本轮开始",
                        "detail": "父任务已开始",
                    },
                    {
                        "id": "child-failed",
                        "ts": "2026-09-05T08:00:01Z",
                        "run_id": "run-child",
                        "kind": "chat.tool_call.finished",
                        "type": "item.completed",
                        "status": "failed",
                        "label": "子任务失败",
                        "detail": "子任务没有完成",
                        "payload": {
                            "parent_run_id": "run-parent",
                            "outcome_status": "blocked",
                            "delivery_status": "not_delivered",
                        },
                    },
                ],
            }
        ),
        pending_turn={
            "run_id": "run-parent",
            "status": "running",
            "text": "继续完成父任务",
        },
    )

    assert state is not None
    assert state.status == "running"
    assert state.run_id == "run-parent"
    assert state.outcome_status == "unknown"
    assert state.delivery_status == "pending"
    assert state.summary == "本轮运行中"
    assert [event.run_id for event in state.events_preview] == [
        "run-parent",
        "run-child",
    ]


def test_parent_tool_completion_does_not_settle_pending_turn_while_tracker_idle(
) -> None:
    state = build_chat_runtime_state(
        chat_spec=ChatSpec(
            id="chat-parent-tool-finished",
            session_id="console:parent-tool-finished",
            user_id="default",
            channel="console",
            meta={"last_turn_run_id": "run-parent"},
        ),
        tracker_status="idle",
        active_run=None,
        turn_event_preview=TurnEventPreview.model_validate(
            {
                "summary_line": "工具执行完成",
                "events": [
                    {
                        "id": "parent-started",
                        "ts": "2026-09-05T08:00:00Z",
                        "run_id": "run-parent",
                        "kind": "chat.turn.started",
                        "type": "turn.started",
                        "status": "running",
                        "label": "本轮开始",
                        "detail": "父任务已开始",
                    },
                    {
                        "id": "parent-tool-finished",
                        "ts": "2026-09-05T08:00:01Z",
                        "run_id": "run-parent",
                        "kind": "chat.tool_call.finished",
                        "type": "item.completed",
                        "status": "completed",
                        "label": "工具执行完成",
                        "detail": "工具完成后仍需模型继续处理",
                        "payload": {
                            "outcome_status": "blocked",
                            "delivery_status": "not_delivered",
                        },
                    },
                ],
            }
        ),
        pending_turn={
            "run_id": "run-parent",
            "status": "running",
            "text": "继续完成父任务",
        },
    )

    assert state is not None
    assert state.status == "running"
    assert state.run_id == "run-parent"
    assert state.outcome_status == "unknown"
    assert state.delivery_status == "pending"
    assert state.summary == "工具执行完成"


def test_legacy_completed_preview_does_not_invent_chat_id_as_run_id() -> None:
    state = build_chat_runtime_state(
        chat_spec=ChatSpec(
            id="chat-is-not-a-run",
            session_id="console:legacy-completed",
            user_id="default",
            channel="console",
        ),
        tracker_status="idle",
        active_run=None,
        turn_event_preview=TurnEventPreview.model_validate(
            {
                "summary_line": "本轮已结束",
                "events": [
                    {
                        "id": "legacy-settled",
                        "ts": "2026-09-04T12:00:00Z",
                        "kind": "chat.turn.settled",
                        "type": "turn.completed",
                        "status": "completed",
                        "label": "本轮结束",
                        "detail": "历史事件没有运行标识",
                        "payload": {"outcome_status": "delivered"},
                    }
                ],
            }
        ),
        pending_turn={},
    )

    assert state is not None
    assert state.status == "completed"
    assert state.run_id is None


@pytest.mark.asyncio
async def test_approval_events_keep_runtime_contract_and_presentation(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-approval",
        session_id="console:approval",
        run_id="turn-approval",
        item_id="approval-1",
        kind="chat.approval.requested",
        status="running",
        summary="请求确认 shell",
        payload={
            "approval_request_id": "approval-1",
            "tool_name": "execute_shell_command",
            "severity": "high",
            "findings_count": 2,
            "policy_decision": {"decision": "prompt"},
        },
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-approval",
        session_id="console:approval",
        run_id="turn-approval",
        item_id="approval-1",
        kind="chat.approval.resolved",
        status="completed",
        summary="确认已完成",
        payload={
            "approval_request_id": "approval-1",
            "tool_name": "execute_shell_command",
            "decision": "approved",
        },
    )

    response = build_runtime_events_response(workspace_dir, "chat-approval")

    assert [event.type for event in response.events] == [
        "approval.requested",
        "approval.resolved",
    ]
    requested = response.events[0]
    assert requested.item_id == "approval-1"
    assert requested.payload["phase"] == "approval"
    assert requested.payload["presentation"]["surface"] == "approval"
    assert requested.payload["presentation"]["risk_level"] == "medium"
    assert requested.presentation is not None
    assert requested.presentation.surface == "approval"
    assert response.events[1].payload["decision"] == "approved"


@pytest.mark.asyncio
async def test_runtime_checkpoint_events_get_checkpoint_phase(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-checkpoint",
        session_id="console:checkpoint",
        run_id="turn-checkpoint",
        kind="chat.checkpoint.created",
        status="completed",
        summary="已创建检查点",
        payload={
            "checkpoint_id": "before-preview-fix",
            "source_seq": 12,
        },
    )

    response = build_runtime_events_response(workspace_dir, "chat-checkpoint")

    assert response.events[0].payload["phase"] == "checkpoint"
    assert response.events[0].payload["checkpoint_id"] == "before-preview-fix"


@pytest.mark.asyncio
async def test_runtime_tool_presentation_policy_marks_memory_and_subagents(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-policy",
        session_id="console:policy",
        run_id="turn-policy",
        item_id="tool-memory",
        kind="chat.tool_call.started",
        status="running",
        summary="开始调用 memory_search",
        payload={"tool_name": "memory_search"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-policy",
        session_id="console:policy",
        run_id="turn-policy",
        item_id="tool-agent",
        kind="chat.tool_call.started",
        status="running",
        summary="开始调用 chat_with_agent",
        payload={"tool_name": "chat_with_agent"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-policy",
        session_id="console:policy",
        run_id="turn-policy",
        item_id="tool-agent-batch",
        kind="chat.tool_call.started",
        status="running",
        summary="开始并行协作",
        payload={"tool_name": "collaborate_with_agents"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="Lantai_Puer_Agent",
        chat_id="chat-policy",
        session_id="console:policy",
        run_id="turn-policy",
        item_id="tool-private-skill",
        kind="chat.tool_call.started",
        status="running",
        summary="开始调用 read_skill",
        payload={"tool_name": "read_skill"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="Lantai_Puer_Agent",
        chat_id="chat-policy",
        session_id="console:policy",
        run_id="turn-policy",
        item_id="tool-explicit-skill",
        kind="chat.tool_call.started",
        status="running",
        summary="用户主动调用 read_skill",
        payload={
            "tool_name": "read_skill",
            "metadata": {
                "skill_exposure": "explicit",
                "skill_invocation_origin": "user_explicit",
            },
        },
    )

    response = build_runtime_events_response(workspace_dir, "chat-policy")
    by_item = {event.item_id: event for event in response.events}

    assert by_item["tool-memory"].payload["presentation"]["visibility"] == "hidden"
    assert by_item["tool-memory"].payload["presentation"]["surface"] == "debug"
    assert by_item["tool-agent"].payload["presentation"]["visibility"] == "prominent"
    assert by_item["tool-agent"].payload["presentation"]["surface"] == "subagent"
    assert (
        by_item["tool-agent-batch"].payload["presentation"]["visibility"]
        == "prominent"
    )
    assert (
        by_item["tool-agent-batch"].payload["presentation"]["surface"]
        == "subagent"
    )
    assert (
        by_item["tool-private-skill"].payload["presentation"]["visibility"]
        == "hidden"
    )
    assert (
        by_item["tool-private-skill"].payload["presentation"]["surface"]
        == "debug"
    )
    assert (
        by_item["tool-explicit-skill"].payload["presentation"]["visibility"]
        == "visible"
    )
    assert (
        by_item["tool-explicit-skill"].payload["presentation"]["surface"]
        == "tool"
    )

    preview = build_turn_event_preview(workspace_dir, "chat-policy")
    assert preview is not None
    preview_ids = {event.id for event in preview.events}
    runtime_event_ids = {event.item_id: event.id for event in response.events}
    assert runtime_event_ids["tool-private-skill"] not in preview_ids
    assert runtime_event_ids["tool-explicit-skill"] in preview_ids


@pytest.mark.asyncio
async def test_runtime_runs_group_events_and_usage(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runs",
        session_id="console:runs",
        run_id="run-1",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "写一份周报"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-runs",
        session_id="console:runs",
        run_id="run-1",
        kind="chat.final_answer",
        status="completed",
        summary="本轮回复完成",
        payload={
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "context_tokens": 300,
            },
        },
    )
    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="cron:daily",
        kind="cron.job.started",
        status="running",
        summary="定时任务已开始",
        payload={"job_name": "日报整理", "task_type": "agent"},
    )
    await record_skills_changed(
        workspace_dir=workspace_dir,
        agent_id="default",
        change_type="refresh",
        skill_name="wps365",
    )

    response = collect_runtime_runs([workspace_dir])

    assert response.total == 3
    run = next(item for item in response.items if item.id == "run-1")
    assert run.source == "chat"
    assert run.status == "completed"
    assert run.title == "写一份周报"
    assert run.chat_id == "chat-runs"
    assert run.session_id == "console:runs"
    assert run.workspace_dir == str(workspace_dir)
    assert run.can_interrupt is False
    assert run.can_retry is True
    assert run.usage.input_tokens == 100
    assert run.usage.output_tokens == 20
    assert run.usage.context_tokens == 300

    child_lookup = collect_runtime_runs(
        [workspace_dir],
        requested_session_id="console:runs",
    )
    assert child_lookup.total == 1
    assert child_lookup.items[0].id == "run-1"

    details = build_runtime_run_events_response([workspace_dir], "run-1")
    assert details.run_id == "run-1"
    assert [event.type for event in details.events] == [
        "turn.started",
        "item.completed",
    ]


@pytest.mark.asyncio
async def test_runtime_runs_mark_stalled_chat_terminal_and_retryable(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-stalled",
        session_id="console:stalled",
        run_id="run-stalled",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "修复前端预览"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-stalled",
        session_id="console:stalled",
        run_id="run-stalled",
        kind="chat.system.event",
        type="turn.stalled",
        status="stalled",
        summary="本轮可能卡住",
        payload={
            "phase": "failure",
            "trace_title": "本轮可能卡住",
            "trace_detail": "90 秒内没有新的运行事件，且后端没有活动任务",
        },
    )

    response = collect_runtime_runs([workspace_dir])
    run = response.items[0]

    assert run.id == "run-stalled"
    assert run.status == "stalled"
    assert run.can_interrupt is False
    assert run.can_retry is True
    assert run.summary == "90 秒内没有新的运行事件，且后端没有活动任务"

    details = build_runtime_run_events_response([workspace_dir], "run-stalled")
    assert details.events[-1].type == "turn.stalled"
    assert details.events[-1].status == "stalled"
    assert details.events[-1].payload["failure_diagnosis"]["category"] == "timeout"
    assert (
        details.events[-1].payload["failure_diagnosis"]["summary"]
        == "执行超时或运行卡住"
    )

    preview = build_turn_event_preview(workspace_dir, "chat-stalled")
    assert preview is not None
    assert (
        preview.events[-1].payload["failure_diagnosis"]["next_action"]
        == "先确认进程是否还活着和最后输出点；若无活动任务，从最后已完成步骤继续。"
    )


@pytest.mark.asyncio
async def test_preview_harness_event_payload_reaches_chat_preview(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-preview-harness",
        session_id="console:preview-harness",
        run_id="run-preview-harness",
        kind="chat.system.event",
        type="preview.inspected",
        status="failed",
        summary="本地预览不可达。",
        payload={
            "phase": "preview",
            "trace_title": "预览诊断：localhost:5173",
            "trace_detail": "本地预览不可达。",
            "preview_url": "http://localhost:5173/",
            "failure_reason": "本地预览不可达。",
            "preview_harness": {
                "url": "http://localhost:5173/",
                "fetch_url": "http://localhost:5173/",
                "ok": False,
                "summary": "本地预览不可达。",
                "findings": [
                    {
                        "severity": "critical",
                        "category": "availability",
                        "message": "本地预览不可达。",
                        "evidence": "Connection refused",
                    },
                ],
            },
        },
    )

    preview = build_turn_event_preview(workspace_dir, "chat-preview-harness")

    assert preview is not None
    payload = preview.events[-1].payload
    assert payload["preview_harness"]["summary"] == "本地预览不可达。"
    assert payload["failure_diagnosis"]["category"] == "preview"


@pytest.mark.asyncio
async def test_runtime_run_events_include_child_agent_runs(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-child-runs",
        session_id="console:child-runs",
        run_id="run-parent",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "并行检查实现"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="water",
        chat_id="chat-child-runs",
        session_id="console:child-runs:water",
        run_id="run-child",
        kind="chat.tool_call.finished",
        status="completed",
        summary="子任务完成",
        payload={
            "phase": "subtask",
            "parent_run_id": "run-parent",
            "agent_name": "白水",
            "agent_role": "代码与自动化",
            "delegation_reason": "并行检查实现",
            "permission_profile": "workspace",
            "current_tool": "chat_with_agent",
            "result_summary": "已完成实现检查",
            "sub_run": {
                "parent_run_id": "run-parent",
                "agent_name": "白水",
                "status": "completed",
            },
        },
    )

    runs = collect_runtime_runs([workspace_dir])
    parent = next(item for item in runs.items if item.id == "run-parent")
    assert parent.event_count == 2
    assert parent.source == "chat"

    details = build_runtime_run_events_response([workspace_dir], "run-parent")
    assert [event.payload["summary"] for event in details.events] == [
        "本轮已开始",
        "子任务完成",
    ]
    child = details.events[1]
    assert child.payload["parent_run_id"] == "run-parent"
    assert child.payload["agent_name"] == "白水"
    assert child.payload["sub_run"]["status"] == "completed"
    assert child.type == "item.completed"


@pytest.mark.asyncio
async def test_chat_tool_completion_without_identity_is_rejected(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    with pytest.raises(RuntimeEventSequenceAppendError) as exc_info:
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-missing-tool-id",
            session_id="console:missing-tool-id",
            run_id="run-missing-tool-id",
            kind="chat.tool_call.finished",
            status="completed",
            summary="工具完成但没有 id",
            payload={"tool_name": "execute_shell_command"},
        )

    assert "tool_completion_missing_id" in exc_info.value.codes


@pytest.mark.asyncio
async def test_task_event_settled_without_started_gets_stable_item_id(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="cron:weekly",
        run_id="cron-weekly",
        kind="cron.job.settled",
        status="finished",
        summary="定时任务已结束",
        payload={
            "job_name": "周报整理",
            "task_type": "agent",
            "final_status": "completed",
        },
    )

    details = build_runtime_run_events_response([workspace_dir], "cron-weekly")

    assert [event.type for event in details.events] == ["tool.completed"]
    assert details.events[0].item_id == "cron:weekly"


@pytest.mark.asyncio
async def test_cross_workspace_runtime_run_projection_is_input_order_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    parent_workspace = tmp_path / "parent-workspace"
    child_workspace = tmp_path / "child-workspace"
    fixed_ts = "2026-07-10T08:40:00Z"
    monkeypatch.setattr(experience_events, "_now_iso", lambda: fixed_ts)

    await append_chat_event(
        workspace_dir=parent_workspace,
        agent_id="default",
        chat_id="chat-cross-workspace",
        session_id="console:cross-workspace",
        run_id="run-cross-workspace",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "跨工作区聚合"},
    )
    await append_chat_event(
        workspace_dir=child_workspace,
        agent_id="default",
        chat_id="chat-cross-workspace",
        session_id="console:cross-workspace",
        run_id="run-cross-workspace",
        kind="chat.turn.settled",
        status="finished",
        summary="本轮已结束",
        payload={
            "final_task_status": "finished",
            "pending_turn_cleared": True,
            "active_run_event_count": 2,
        },
    )

    parent_first = collect_runtime_runs([parent_workspace, child_workspace])
    child_first = collect_runtime_runs([child_workspace, parent_workspace])

    parent_first_run = next(
        item for item in parent_first.items if item.id == "run-cross-workspace"
    )
    child_first_run = next(
        item for item in child_first.items if item.id == "run-cross-workspace"
    )

    assert parent_first_run.status == "completed"
    assert child_first_run.status == "completed"
    assert parent_first_run.source == "chat"
    assert child_first_run.source == "chat"
    assert parent_first_run.workspace_dir == str(child_workspace)
    assert child_first_run.workspace_dir == str(child_workspace)
    assert (
        parent_first_run.debug_refs["projection_warning"]
        == "cross_workspace_local_seq_tie"
    )
    assert (
        child_first_run.debug_refs["projection_warning"]
        == "cross_workspace_local_seq_tie"
    )

    details_parent_first = build_runtime_run_events_response(
        [parent_workspace, child_workspace],
        "run-cross-workspace",
    )
    details_child_first = build_runtime_run_events_response(
        [child_workspace, parent_workspace],
        "run-cross-workspace",
    )

    assert [event.type for event in details_parent_first.events] == [
        "turn.started",
        "turn.completed",
    ]
    assert [event.type for event in details_child_first.events] == [
        "turn.started",
        "turn.completed",
    ]
    assert details_parent_first.workspace_dir == str(child_workspace)
    assert details_child_first.workspace_dir == str(child_workspace)


@pytest.mark.asyncio
async def test_retired_image_runtime_events_remain_readable_without_active_producers(
    tmp_path,
):
    workspace_dir = tmp_path / "topic-workspace"

    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="image:gw-task-123456",
        run_id="image-run",
        kind="image.task.started",
        status="running",
        summary="历史图片任务已开始",
        payload={
            "task_id": "gw-task-123456",
            "model": "wanx-image",
            "prompt_preview": "温暖办公室里的沈知远水彩封面",
            "quantity": 4,
            "size": "1024x1024",
            "quality": "high",
        },
    )
    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="image:gw-task-123456",
        run_id="image-run",
        kind="image.task.settled",
        status="finished",
        summary="历史图片任务已完成",
        payload={
            "task_id": "gw-task-123456",
            "final_status": "completed",
            "model": "wanx-image",
            "asset_count": 4,
            "remaining_points": 640,
        },
        usage={"cost_points": 360},
    )

    response = collect_runtime_runs([workspace_dir], source="image")

    assert response.total == 1
    run = response.items[0]
    assert run.id == "image-run"
    assert run.source == "image"
    assert run.status == "completed"
    assert run.title == "已退役图片任务：温暖办公室里的沈知远水彩封面"
    assert "gw-task" not in run.title
    assert run.summary == "已完成 · 4 张 · 360 点 · 剩余 640 点"
    assert run.usage.cost_points == 360

    details = build_runtime_run_events_response([workspace_dir], "image-run")
    assert [event.type for event in details.events] == [
        "tool.started",
        "tool.completed",
    ]
    assert details.events[-1].usage == {"cost_points": 360}


@pytest.mark.asyncio
async def test_retired_image_pending_event_stays_running_when_read_from_history(
    tmp_path,
):
    workspace_dir = tmp_path / "topic-workspace"

    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="image:gateway-task-queued",
        run_id="image:gateway-task-queued",
        kind="image.task.settled",
        status="running",
        summary="历史图片任务进行中",
        payload={
            "task_id": "gateway-task-queued",
            "final_status": "queued",
            "model": "wanx-image",
        },
    )

    response = collect_runtime_runs([workspace_dir], source="image")

    assert response.total == 1
    assert response.items[0].status == "running"
    assert response.items[0].summary == "生成中"
    assert response.items[0].usage.cost_points == 0


@pytest.mark.asyncio
async def test_runtime_product_smoke_covers_user_visible_sources(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-product",
        session_id="console:product",
        run_id="turn-product",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "整理今天的工作"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-product",
        session_id="console:product",
        run_id="turn-product",
        item_id="tool-1",
        kind="chat.tool_call.started",
        status="running",
        summary="工具开始",
        payload={"tool_name": "execute_shell_command"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-product",
        session_id="console:product",
        run_id="turn-product",
        kind="chat.final_answer",
        status="completed",
        summary="本轮回复完成",
        payload={
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 240,
                "cache_tokens": 900,
                "context_tokens": 6400,
            },
        },
    )
    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="cron:weekly",
        run_id="cron-weekly",
        kind="cron.job.settled",
        status="finished",
        summary="定时任务已结束",
        payload={
            "job_name": "周报整理",
            "task_type": "agent",
            "final_status": "completed",
        },
    )
    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="heartbeat:default",
        run_id="heartbeat-default",
        kind="heartbeat.run.settled",
        status="finished",
        summary="心跳任务已结束",
        payload={"target": "silent", "final_status": "completed"},
    )
    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="image:image-1",
        run_id="image-smoke",
        kind="image.task.started",
        status="running",
        summary="历史图片任务已开始",
        payload={
            "task_id": "image-1",
            "model": "wanx-image",
            "prompt_preview": "封面图",
        },
    )
    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="image:image-1",
        run_id="image-smoke",
        kind="image.task.settled",
        status="finished",
        summary="历史图片任务已完成",
        payload={
            "task_id": "image-1",
            "final_status": "completed",
        },
        usage={"cost_points": 90},
    )
    await record_skills_changed(
        workspace_dir=workspace_dir,
        agent_id="default",
        change_type="refresh",
        skill_name="wps365",
    )

    response = collect_runtime_runs([workspace_dir])
    by_source = {item.source: item for item in response.items}

    assert {"chat", "cron", "heartbeat", "image", "skills"} <= set(by_source)
    assert by_source["chat"].usage.input_tokens == 1200
    assert by_source["chat"].usage.cache_tokens == 900
    assert by_source["chat"].usage.context_tokens == 6400
    assert by_source["image"].usage.cost_points == 90
    assert by_source["image"].title == "已退役图片任务：封面图"

    resumed = build_runtime_run_events_response(
        [workspace_dir],
        "turn-product",
        after_seq=1,
    )
    assert resumed.after_seq == 1
    assert resumed.latest_seq >= 3
    assert [event.seq for event in resumed.events] == [2, 3]


@pytest.mark.asyncio
async def test_runtime_runs_filter_source_and_status(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="cron:daily",
        kind="cron.job.started",
        status="running",
        summary="定时任务已开始",
        payload={"job_name": "日报整理"},
    )
    await append_task_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        task_id="heartbeat:default",
        kind="heartbeat.run.settled",
        status="finished",
        summary="心跳已结束",
        payload={"target": "main", "final_status": "completed"},
    )

    response = collect_runtime_runs(
        [workspace_dir],
        source="heartbeat",
        status="completed",
    )

    assert response.total == 1
    assert response.items[0].source == "heartbeat"


@pytest.mark.asyncio
async def test_runtime_runs_include_store_contract_projection_fields(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    contract = RuntimeRunContract(
        run_id="capability-store-run",
        attempt_id="capability-store-run:attempt-1",
        source="capability",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id="agent-default",
            session_id="capability-session",
            workspace_id=str(workspace_dir),
            channel="console",
        ),
        status="waiting_approval",
        revision=4,
        lease_owner="capability-worker",
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        started_at=now,
        heartbeat_at=now + timedelta(seconds=30),
        budget=RuntimeBudget(max_steps=12, max_seconds=900),
        checkpoint=RuntimeCheckpoint(
            checkpoint_id="checkpoint-1",
            event_id="event-8",
            seq=8,
            artifact_id="artifact-1",
            summary="draft created",
        ),
        current_step=RuntimeStepRef(
            step_id="approval-1",
            index=2,
            kind="approval",
            title="等待人工确认",
        ),
        contract_hash="sha256:contract",
        policy_snapshot_hash="sha256:policy",
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(
        contract,
        expected_revision=None,
    )

    response = collect_runtime_runs([workspace_dir])
    run = response.items[0]

    assert response.total == 1
    assert run.id == "capability-store-run"
    assert run.source == "capability"
    assert run.status == "running"
    assert run.attempt_id == "capability-store-run:attempt-1"
    assert run.authority_status == "waiting_approval"
    assert run.transport_status == "unknown"
    assert run.revision == 4
    assert run.lease_owner == "capability-worker"
    assert run.pending_approval is True
    assert run.current_step == {
        "step_id": "approval-1",
        "index": 2,
        "kind": "approval",
        "title": "等待人工确认",
    }
    assert run.budget["max_steps"] == 12
    assert run.checkpoint is not None
    assert run.checkpoint["checkpoint_id"] == "checkpoint-1"
    assert run.recovery["can_takeover"] is True
    assert run.projection_source == "runtime_store"


@pytest.mark.asyncio
async def test_append_chat_event_updates_runtime_run_heartbeat_step_and_checkpoint(
    tmp_path,
):
    workspace_dir = tmp_path / "topic-workspace"
    now = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
    contract = RuntimeRunContract(
        run_id="run-progress",
        attempt_id="run-progress:attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            user_id="default",
            agent_id="default",
            session_id="console:progress",
            workspace_id=str(workspace_dir),
            channel="console",
        ),
        status="running",
        revision=2,
        lease_owner="chat:chat-progress",
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        contract_hash="sha256:contract",
        policy_snapshot_hash="sha256:policy",
        release_edition="official",
    )
    store = RuntimeRunStore(workspace_dir)
    await store.save_contract(contract, expected_revision=None)
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-progress",
        session_id="console:progress",
        run_id=contract.run_id,
        kind="chat.turn.started",
        status="running",
        summary="开始",
        payload={"attempt_id": contract.attempt_id},
    )

    started = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-progress",
        session_id="console:progress",
        run_id=contract.run_id,
        item_id="tool-search",
        kind="chat.tool_call.started",
        status="running",
        summary="调用搜索",
        payload={
            "attempt_id": contract.attempt_id,
            "tool_call_id": "tool-search",
            "tool_name": "search",
        },
    )
    after_started = await store.load_contract(contract.run_id)

    assert after_started is not None
    assert after_started.revision == 3
    assert after_started.heartbeat_at is not None
    assert after_started.heartbeat_at.isoformat().startswith(started.ts[:19])
    assert after_started.current_step is not None
    assert after_started.current_step.step_id == "tool:tool-search"
    assert after_started.current_step.kind == "tool"
    assert after_started.current_step.index == 0

    finished = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-progress",
        session_id="console:progress",
        run_id=contract.run_id,
        item_id="tool-search",
        kind="chat.tool_call.finished",
        status="finished",
        summary="搜索完成",
        payload={
            "attempt_id": contract.attempt_id,
            "tool_call_id": "tool-search",
            "tool_name": "search",
        },
    )
    after_finished = await store.load_contract(contract.run_id)

    assert after_finished is not None
    assert after_finished.revision == 4
    assert after_finished.current_step is not None
    assert after_finished.current_step.step_id == "tool:tool-search"
    assert after_finished.current_step.index == 0
    assert after_finished.checkpoint is not None
    assert after_finished.checkpoint.checkpoint_id == f"event:{finished.id}"
    assert after_finished.checkpoint.event_id == finished.id
    assert after_finished.checkpoint.seq == finished.seq
    assert after_finished.checkpoint.summary == "搜索完成"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-progress",
        session_id="console:progress",
        run_id=contract.run_id,
        item_id="tool-failed",
        kind="chat.tool_call.started",
        status="running",
        summary="再次搜索",
        payload={
            "attempt_id": contract.attempt_id,
            "tool_call_id": "tool-failed",
            "tool_name": "search",
        },
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-progress",
        session_id="console:progress",
        run_id=contract.run_id,
        item_id="tool-failed",
        kind="chat.tool_call.finished",
        status="failed",
        summary="搜索失败",
        payload={
            "attempt_id": contract.attempt_id,
            "tool_call_id": "tool-failed",
            "tool_name": "search",
        },
    )
    after_failure = await store.load_contract(contract.run_id)

    assert after_failure is not None
    assert after_failure.checkpoint == after_finished.checkpoint


@pytest.mark.asyncio
async def test_runtime_runs_mark_running_browser_contract_pauseable(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    await RuntimeRunStore(workspace_dir).save_contract(
        RuntimeRunContract(
            run_id="browser-store-run",
            attempt_id="browser-store-run:attempt-1",
            source="browser",
            owner=RuntimeRunOwner(
                user_id="default",
                agent_id="browser",
                session_id="browser-session",
                workspace_id=str(workspace_dir),
                channel="browser",
            ),
            status="running",
            revision=3,
            created_at=now,
            started_at=now,
            heartbeat_at=now,
            contract_hash="sha256:browser-contract",
            policy_snapshot_hash="sha256:browser-policy",
            release_edition="official",
        ),
        expected_revision=None,
    )

    response = collect_runtime_runs([workspace_dir])
    run = response.items[0]

    assert response.total == 1
    assert run.id == "browser-store-run"
    assert run.source == "browser"
    assert run.authority_status == "running"
    assert run.recovery["can_pause"] is True
    assert run.recovery["can_resume"] is False


@pytest.mark.asyncio
async def test_runtime_runs_do_not_advertise_chat_same_attempt_resume(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    await RuntimeRunStore(workspace_dir).save_contract(
        RuntimeRunContract(
            run_id="chat-stalled-run",
            attempt_id="chat-stalled-run:attempt-1",
            source="chat",
            owner=RuntimeRunOwner(
                user_id="default",
                agent_id="default",
                session_id="console:chat",
                workspace_id=str(workspace_dir),
                channel="console",
            ),
            status="stalled",
            revision=3,
            created_at=now,
            started_at=now,
            heartbeat_at=now,
            contract_hash="sha256:chat-contract",
            policy_snapshot_hash="sha256:chat-policy",
            release_edition="official",
        ),
        expected_revision=None,
    )

    response = collect_runtime_runs([workspace_dir])
    run = response.items[0]

    assert response.total == 1
    assert run.id == "chat-stalled-run"
    assert run.source == "chat"
    assert run.authority_status == "stalled"
    assert run.recovery["can_resume"] is False
    assert run.recovery["can_takeover"] is True
    assert run.recovery["resume_requires_durable_lease"] is True


@pytest.mark.asyncio
async def test_runtime_runs_surface_ambiguous_browser_effect_recovery(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    owner = RuntimeRunOwner(
        user_id="default",
        agent_id="browser",
        session_id="browser-session",
        workspace_id=str(workspace_dir),
        channel="browser",
    )
    contract = RuntimeRunContract(
        run_id="browser-ambiguous-run",
        attempt_id="browser-ambiguous-run:attempt-1",
        source="browser",
        owner=owner,
        status="running",
        revision=3,
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        contract_hash="sha256:browser-contract",
        policy_snapshot_hash="sha256:browser-policy",
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(contract, expected_revision=None)
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
        idempotency_key="browser-ambiguous-run:attempt-1:effect-click-publish",
        created_at=now,
        release_edition="official",
    )
    journal = RuntimeEffectJournal(workspace_dir)
    journal.append_effect(effect)
    journal.append_effect(
        reduce_runtime_effect(
            effect,
            RuntimeEffectReducerAction(type="start", now=now),
        ).effect
    )

    response = collect_runtime_runs([workspace_dir])
    run = response.items[0]
    recovery_effect = run.recovery["effects"][0]

    assert run.id == "browser-ambiguous-run"
    assert run.ambiguous_effect is True
    assert run.recovery["effect_replay_forbidden"] is True
    assert run.recovery["effect_requires_probe"] is True
    assert run.recovery["can_takeover"] is True
    assert recovery_effect["effect_id"] == "effect-click-publish"
    assert recovery_effect["status"] == "running"
    assert recovery_effect["tool_name"] == "browser.click"
    assert recovery_effect["external_call_must_not_be_replayed"] is True
    assert recovery_effect["automatic_external_call_allowed"] is False
    assert recovery_effect["probe"] == "browser_observation"


@pytest.mark.asyncio
async def test_runtime_runs_surface_committed_effect_checkpoint_repair(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    owner = RuntimeRunOwner(
        user_id="default",
        agent_id="browser",
        session_id="browser-session",
        workspace_id=str(workspace_dir),
        channel="browser",
    )
    contract = RuntimeRunContract(
        run_id="browser-repair-run",
        attempt_id="browser-repair-run:attempt-1",
        source="browser",
        owner=owner,
        status="running",
        revision=3,
        created_at=now,
        started_at=now,
        heartbeat_at=now,
        contract_hash="sha256:browser-contract",
        policy_snapshot_hash="sha256:browser-policy",
        release_edition="official",
    )
    await RuntimeRunStore(workspace_dir).save_contract(contract, expected_revision=None)
    effect = RuntimeEffectRecord(
        run_id=contract.run_id,
        attempt_id=contract.attempt_id,
        step_id="browser-action-0",
        effect_id="effect-click-save",
        source="browser",
        owner=owner,
        status="intent",
        tool_name="browser.click",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="browser-repair-run:attempt-1:effect-click-save",
        created_at=now,
        release_edition="official",
    )
    running = reduce_runtime_effect(
        effect,
        RuntimeEffectReducerAction(type="start", now=now),
    ).effect
    committed = reduce_runtime_effect(
        running,
        RuntimeEffectReducerAction(
            type="commit",
            result_hash="sha256:result",
            now=now,
        ),
    ).effect
    journal = RuntimeEffectJournal(workspace_dir)
    journal.append_effect(effect)
    journal.append_effect(running)
    journal.append_effect(committed)

    response = collect_runtime_runs([workspace_dir])
    run = response.items[0]
    recovery_effect = run.recovery["effects"][0]

    assert run.id == "browser-repair-run"
    assert run.ambiguous_effect is False
    assert run.recovery["effect_replay_forbidden"] is True
    assert run.recovery["effect_checkpoint_rebuild_required"] is True
    assert recovery_effect["status"] == "committed"
    assert recovery_effect["checkpoint_rebuild_required"] is True
    assert recovery_effect["external_call_must_not_be_replayed"] is True
