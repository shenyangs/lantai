"""Behavior locks for Lantai-owned runtime contracts."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from lantai.runtime import (
    RUNTIME_EVENT_PAYLOAD_MODELS,
    RUNTIME_EVENT_SCHEMA_VERSION,
    ContentBlock,
    RuntimeApprovalEventPayload,
    RuntimeBudgetChangedEventPayload,
    RuntimeControlInputEventPayload,
    RuntimeEvent,
    RuntimeEventPayloadValidationError,
    RuntimeEventSequenceValidator,
    RuntimeEventType,
    RuntimeInput,
    RuntimeMessage,
    RuntimeMessageEventPayload,
    RuntimeNotificationEventPayload,
    RuntimePresentation,
    RuntimeRequestContext,
    RuntimeRunner,
    RuntimeToolEventPayload,
    RuntimeTurnContinueRequestedPayload,
    RuntimeUsage,
    validate_runtime_event_payload,
)


def _contract_event(
    seq: int,
    event_type: str,
    *,
    id: str | None = None,
    item_id: str | None = None,
    payload: dict | None = None,
    status: str = "completed",
    turn_id: str = "turn-1",
) -> RuntimeEvent:
    return RuntimeEvent(
        id=id or f"evt-{seq}",
        seq=seq,
        type=event_type,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        source="system",
        session_id="session-1",
        turn_id=turn_id,
        item_id=item_id,
        payload=payload or {},
    )


def test_runtime_message_text_round_trip() -> None:
    msg = RuntimeMessage.text(
        "user",
        "hello",
        id="msg-1",
        metadata={"source": "console"},
    )

    restored = RuntimeMessage.model_validate(msg.model_dump(mode="json"))

    assert restored.role == "user"
    assert restored.text_content() == "hello"
    assert restored.metadata == {"source": "console"}


def test_runtime_message_preserves_structured_blocks() -> None:
    msg = RuntimeMessage(
        id="assistant-1",
        role="assistant",
        content=[
            ContentBlock(type="thinking", text="checking tools"),
            ContentBlock(
                type="tool_call",
                data={
                    "id": "tool-1",
                    "name": "wiki_search",
                    "arguments": {"query": "Runtime Kernel"},
                },
            ),
        ],
    )

    payload = msg.model_dump(mode="json")
    restored = RuntimeMessage.model_validate(payload)

    assert restored.content[0].type == "thinking"
    assert restored.content[1].data["arguments"]["query"] == "Runtime Kernel"


def test_runtime_event_round_trip_keeps_usage_and_presentation() -> None:
    event = RuntimeEvent(
        id="evt-1",
        seq=7,
        type="tool.completed",
        status="completed",
        source="tool",
        session_id="session-1",
        turn_id="turn-1",
        item_id="tool-1",
        summary="tool done",
        payload={"tool_name": "wiki_search"},
        usage=RuntimeUsage(input_tokens=10, output_tokens=3),
        presentation=RuntimePresentation(
            surface="tool",
            display_name="wiki_search",
        ),
        created_at="2026-05-29T00:00:00Z",
    )

    restored = RuntimeEvent.model_validate(event.model_dump(mode="json"))

    assert restored.type == "tool.completed"
    assert restored.usage is not None
    assert restored.usage.input_tokens == 10
    assert restored.presentation is not None
    assert restored.presentation.surface == "tool"


def test_runtime_event_schema_version_and_continue_payload_registry() -> None:
    assert RUNTIME_EVENT_SCHEMA_VERSION == 1
    assert "turn.continue_requested" in get_args(RuntimeEventType)

    event = RuntimeEvent(
        id="evt-continue",
        seq=8,
        type="turn.continue_requested",
        status="info",
        source="user",
        session_id="session-1",
        turn_id="run-1",
        item_id="continue-1",
        payload={
            "request_id": "continue-1",
            "mode": "retry",
            "run_id": "run-1",
            "attempt_id": "attempt-1",
            "requested_by": "user-1",
            "revision": 7,
            "checkpoint_id": "checkpoint-1",
        },
        created_at="2026-07-16T08:00:00Z",
    )

    payload = event.model_dump(mode="json")
    assert payload["schema_version"] == 1
    assert payload["payload"]["mode"] == "retry"
    assert isinstance(
        validate_runtime_event_payload(event.type, event.payload),
        RuntimeTurnContinueRequestedPayload,
    )

    payload["payload"]["raw_provider_body"] = {"secret": "not allowed"}
    with pytest.raises(RuntimeEventPayloadValidationError):
        validate_runtime_event_payload("turn.continue_requested", payload["payload"])
    with pytest.raises(ValidationError):
        RuntimeEvent.model_validate(payload)

    payload = event.model_dump(mode="json")
    payload["payload"]["mode"] = "continue"
    with pytest.raises(RuntimeEventPayloadValidationError):
        validate_runtime_event_payload("turn.continue_requested", payload["payload"])
    with pytest.raises(ValidationError):
        RuntimeEvent.model_validate(payload)


def test_runtime_warning_accepts_tool_names_from_unsettled_effect_recovery() -> None:
    payload = validate_runtime_event_payload(
        "runtime.warning",
        {
            "kind": "unfinished_tool_effects",
            "decision": "continue",
            "tool_names": ["generate_image", "edit_file"],
        },
    )

    assert payload.tool_names == ["generate_image", "edit_file"]


def test_control_input_admission_has_a_strict_observation_contract() -> None:
    payload = validate_runtime_event_payload(
        "turn.control_input.admitted",
        {
            "command_id": "command-1",
            "client_turn_id": "command-1",
            "intent": "redirect_now",
            "admission_status": "admitted",
            "execution_status": "pending",
            "attempt_id": "attempt-1",
            "server_seq": 2,
            "next_waiting_reason": "safe_boundary",
            "replaces_command_id": "command-0",
        },
    )

    assert isinstance(payload, RuntimeControlInputEventPayload)
    assert payload.command_id == "command-1"
    assert payload.replaces_command_id == "command-0"

    with pytest.raises(RuntimeEventPayloadValidationError):
        validate_runtime_event_payload(
            "turn.control_input.admitted",
            {
                "command_id": "command-1",
                "client_turn_id": "command-1",
                "intent": "redirect_now",
                "admission_status": "admitted",
                "execution_status": "pending",
                "server_seq": 2,
                "raw_provider_body": {"secret": "not allowed"},
            },
        )


def test_turn_started_payload_accepts_long_task_contract() -> None:
    event = RuntimeEvent(
        id="evt-long-turn",
        seq=9,
        type="turn.started",
        status="running",
        source="system",
        session_id="session-1",
        turn_id="run-1",
        payload={
            "attempt_id": "attempt-1",
            "task_contract": {
                "schema_version": 1,
                "classification": "long",
                "budget": {"max_active_seconds": 1800.0},
            },
        },
        created_at="2026-08-09T08:00:00Z",
    )

    restored = RuntimeEvent.model_validate(event.model_dump(mode="json"))

    assert restored.payload["task_contract"]["classification"] == "long"


def test_runtime_error_payload_accepts_delivery_status() -> None:
    event = RuntimeEvent(
        id="evt-undelivered-error",
        seq=10,
        type="runtime.error",
        status="failed",
        source="system",
        session_id="session-1",
        turn_id="run-1",
        payload={
            "status": "failed",
            "outcome_status": "incomplete",
            "delivery_status": "not_delivered",
        },
        created_at="2026-08-09T08:00:00Z",
    )

    restored = RuntimeEvent.model_validate(event.model_dump(mode="json"))

    assert restored.payload["delivery_status"] == "not_delivered"


def test_runtime_event_payload_registry_covers_all_canonical_types() -> None:
    assert set(RUNTIME_EVENT_PAYLOAD_MODELS) == set(get_args(RuntimeEventType))
    assert validate_runtime_event_payload(
        "turn.started",
        {
            "agent_id": "default",
            "session_id": "session-1",
            "root_session_id": "root-1",
            "chat_id": "chat-1",
            "run_id": "run-1",
            "attempt_id": "attempt-1",
            "turn_id": "turn-1",
            "text_preview": "hello",
        },
    )
    assert isinstance(
        validate_runtime_event_payload(
            "message.delta",
            {
                "source_message_id": "source-msg-1",
                "message": RuntimeMessage.text(
                    "assistant",
                    "hello",
                    id="msg-1",
                ).model_dump(mode="json"),
            },
        ),
        RuntimeMessageEventPayload,
    )
    assert isinstance(
        validate_runtime_event_payload(
            "tool.call.started",
            {
                "id": "call-1",
                "tool_name": "lookup",
                "arguments": {"query": "runtime"},
            },
        ),
        RuntimeToolEventPayload,
    )
    assert isinstance(
        validate_runtime_event_payload(
            "approval.resolved",
            {
                "request_id": "approval-1",
                "tool_name": "shell",
                "decision": "approved",
            },
        ),
        RuntimeApprovalEventPayload,
    )
    assert isinstance(
        validate_runtime_event_payload(
            "runtime.budget_changed",
            {
                "schema_version": 1,
                "contract_id": "loop-1",
                "previous_contract_hash": "sha256:previous-contract",
                "next_contract_hash": "sha256:next-contract",
                "previous_budget_hash": "sha256:previous-budget",
                "next_budget_hash": "sha256:next-budget",
                "changed_by": "user",
                "reason": "Approved more steps.",
                "event_hash": "sha256:event",
            },
        ),
        RuntimeBudgetChangedEventPayload,
    )
    assert isinstance(
        validate_runtime_event_payload(
            "runtime.notification",
            {
                "schema_version": 1,
                "notification_id": "run-1:attempt-1:waiting_approval",
                "notification_kind": "waiting_approval",
                "priority": "high",
                "title": "运行等待审批",
                "message": "当前步骤需要人工确认。",
                "dedupe_key": "runtime-notification:run-1:attempt-1:waiting_approval",
                "run_id": "run-1",
                "attempt_id": "attempt-1",
                "contract_id": "loop-1",
                "contract_hash": "sha256:contract",
                "source_status": "waiting_approval",
                "trigger_reason": "human_checkpoint",
                "export_recommended": False,
                "action_url": "/tasks?run=run-1",
            },
        ),
        RuntimeNotificationEventPayload,
    )

    with pytest.raises(RuntimeEventPayloadValidationError):
        validate_runtime_event_payload(
            "runtime.budget_changed",
            {
                "schema_version": 1,
                "contract_id": "loop-1",
                "previous_contract_hash": "sha256:previous-contract",
                "next_contract_hash": "sha256:next-contract",
                "previous_budget_hash": "sha256:previous-budget",
                "next_budget_hash": "sha256:next-budget",
                "changed_by": "prompt",
                "reason": "Prompt tried to override the budget.",
                "event_hash": "sha256:event",
            },
        )


def test_registered_runtime_event_payloads_reject_unknown_raw_fields() -> None:
    for event_type in (
        "message.delta",
        "tool.call.started",
        "approval.requested",
        "runtime.notification",
        "runtime.error",
    ):
        with pytest.raises(RuntimeEventPayloadValidationError):
            validate_runtime_event_payload(
                event_type,
                {"raw_provider_body": {"secret": "not allowed"}},
            )


def test_shared_runtime_event_v1_fixture_matches_python_contract() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2] / "fixtures" / "runtime_event_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert fixture["schema_version"] == RUNTIME_EVENT_SCHEMA_VERSION
    events = [RuntimeEvent.model_validate(item) for item in fixture["events"]]

    assert events[0].schema_version == RUNTIME_EVENT_SCHEMA_VERSION
    assert events[0].type == "turn.continue_requested"
    assert (
        validate_runtime_event_payload(
            events[0].type,
            events[0].payload,
        ).mode
        == "retry"
    )


def test_runtime_input_is_independent_from_framework_types() -> None:
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
            chat_id="chat-1",
            run_id="turn-1",
            workspace_dir="/tmp/workspace",
        ),
        messages=[RuntimeMessage.text("user", "ping")],
        model_slot="active",
        tool_policy_context={"permission_profile": "workspace"},
    )

    payload = runtime_input.model_dump(mode="json")

    assert payload["request"]["agent_id"] == "default"
    assert payload["messages"][0]["content"][0]["text"] == "ping"
    assert payload["tool_policy_context"]["permission_profile"] == "workspace"


@pytest.mark.asyncio
async def test_runtime_runner_delegates_to_adapter() -> None:
    runtime_input = RuntimeInput(
        request=RuntimeRequestContext(
            agent_id="default",
            session_id="session-1",
        ),
        messages=[RuntimeMessage.text("user", "hello")],
    )

    class FakeAdapter:
        def __init__(self) -> None:
            self.seen: list[RuntimeInput] = []

        async def run_turn(
            self,
            item: RuntimeInput,
        ) -> AsyncIterator[RuntimeEvent]:
            self.seen.append(item)
            yield RuntimeEvent(
                id="evt-start",
                type="turn.started",
                status="running",
                session_id=item.request.session_id,
                turn_id=item.request.run_id,
            )

    adapter = FakeAdapter()
    runner = RuntimeRunner(adapter)

    events = [event async for event in runner.run_turn(runtime_input)]

    assert adapter.seen == [runtime_input]
    assert events[0].type == "turn.started"
    assert events[0].session_id == "session-1"


def test_runtime_event_sequence_validator_accepts_canonical_turn() -> None:
    events = [
        _contract_event(1, "turn.started", status="running"),
        _contract_event(2, "message.delta", status="running", item_id="msg-1"),
        _contract_event(
            3,
            "tool.call.started",
            status="running",
            item_id="call-1",
            payload={
                "id": "call-1",
                "tool_name": "lookup",
                "arguments": {"query": "澜台"},
            },
        ),
        _contract_event(
            4,
            "tool.call.completed",
            item_id="call-1",
            payload={
                "id": "call-1",
                "tool_name": "lookup",
                "ok": True,
                "output": [{"type": "text", "text": "found"}],
            },
        ),
        _contract_event(
            5,
            "approval.requested",
            status="running",
            item_id="approval-1",
            payload={"request_id": "approval-1", "tool_name": "shell"},
        ),
        _contract_event(
            6,
            "approval.resolved",
            item_id="approval-1",
            payload={"request_id": "approval-1", "decision": "approved"},
        ),
        _contract_event(7, "turn.completed"),
    ]

    assert RuntimeEventSequenceValidator().validate(events) == []


def test_runtime_event_sequence_validator_reports_contract_violations() -> None:
    events = [
        _contract_event(1, "turn.started", id="dup", status="running"),
        _contract_event(1, "message.delta", id="dup", status="running"),
        _contract_event(2, "turn.completed"),
        _contract_event(
            3,
            "tool.call.started",
            status="running",
            item_id="call-1",
            payload={"id": "call-1", "tool_name": "lookup", "arguments": "bad"},
        ),
        _contract_event(
            4,
            "tool.completed",
            item_id="call-1",
            payload={"id": "call-1", "tool_name": "lookup", "ok": True},
        ),
        _contract_event(
            5,
            "approval.resolved",
            item_id="approval-1",
            payload={"request_id": "approval-1", "decision": "approved"},
        ),
        _contract_event(6, "turn.failed"),
    ]

    codes = {
        violation.code for violation in RuntimeEventSequenceValidator().validate(events)
    }

    assert "duplicate_event_id" in codes
    assert "non_increasing_seq" in codes
    assert "side_effect_after_terminal" in codes
    assert "tool_arguments_not_object" in codes
    assert "legacy_tool_completed_type" in codes
    assert "approval_resolved_without_request" in codes
    assert "multiple_terminal_events" in codes


def test_runtime_event_sequence_validator_rejects_unpaired_tool_and_approval() -> None:
    events = [
        _contract_event(1, "turn.started", status="running"),
        _contract_event(
            2,
            "tool.call.started",
            status="running",
            item_id="call-1",
            payload={"id": "call-1", "tool_name": "lookup", "arguments": {}},
        ),
        _contract_event(
            3,
            "approval.requested",
            status="running",
            item_id="approval-1",
            payload={"request_id": "approval-1", "tool_name": "shell"},
        ),
        _contract_event(4, "turn.completed"),
    ]

    codes = {
        violation.code for violation in RuntimeEventSequenceValidator().validate(events)
    }

    assert "tool_started_without_completion" in codes
    assert "approval_requested_without_resolution" in codes
