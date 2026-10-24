# -*- coding: utf-8 -*-
"""Behavior locks for versioned Runtime Run control contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from lantai.runtime.runs import (
    RuntimeAttemptContract,
    RuntimeBudget,
    RuntimeCheckpoint,
    RuntimeEffectRecord,
    RuntimeEffectStatus,
    RuntimeRunContract,
    RuntimeRunContractMigrationError,
    RuntimeRunOwner,
    RuntimeRunReason,
    RuntimeRunSource,
    RuntimeRunStatus,
    RuntimeStepContract,
    RuntimeStepRef,
    migrate_runtime_run_contract,
)


NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


def _owner() -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="user-1",
        agent_id="agent-default",
        session_id="session-1",
        workspace_id="workspace-1",
        channel="console",
    )


def _contract_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "source": "mission",
        "owner": _owner().model_dump(mode="json"),
        "status": "running",
        "revision": 7,
        "lease_owner": "worker-a",
        "lease_expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "created_at": NOW.isoformat(),
        "started_at": NOW.isoformat(),
        "heartbeat_at": (NOW + timedelta(seconds=30)).isoformat(),
        "settled_at": None,
        "budget": {
            "max_turns": 8,
            "max_steps": 50,
            "max_tool_calls": 12,
            "max_seconds": 3600.0,
            "max_tokens": 200000,
            "max_cost_points": 120.0,
            "max_artifact_bytes": 1048576,
            "deadline_at": (NOW + timedelta(hours=1)).isoformat(),
        },
        "checkpoint": {
            "checkpoint_id": "checkpoint-1",
            "event_id": "event-15",
            "seq": 15,
            "artifact_id": "artifact-1",
            "summary": "repo scan complete",
        },
        "current_step": {
            "step_id": "step-1",
            "index": 1,
            "kind": "tool",
            "title": "Inspect runtime contracts",
        },
        "error": None,
        "stop_reason": None,
        "contract_hash": "sha256:contract",
        "policy_snapshot_hash": "sha256:policy",
        "release_edition": "official",
    }


def test_runtime_run_contract_round_trips_required_control_fields() -> None:
    contract = RuntimeRunContract.model_validate(_contract_payload())

    restored = RuntimeRunContract.model_validate(contract.model_dump(mode="json"))

    assert restored.schema_version == 1
    assert restored.run_id == "run-1"
    assert restored.attempt_id == "attempt-1"
    assert restored.source == "mission"
    assert restored.status == "running"
    assert restored.revision == 7
    assert restored.lease_owner == "worker-a"
    assert restored.lease_expires_at == NOW + timedelta(minutes=5)
    assert restored.budget.max_steps == 50
    assert restored.budget.max_tool_calls == 12
    assert restored.budget.max_artifact_bytes == 1048576
    assert restored.checkpoint is not None
    assert restored.checkpoint.seq == 15
    assert restored.current_step is not None
    assert restored.current_step.step_id == "step-1"
    assert restored.contract_hash == "sha256:contract"
    assert restored.policy_snapshot_hash == "sha256:policy"
    assert restored.release_edition == "official"


def test_runtime_run_source_and_status_are_strict_authoritative_enums() -> None:
    assert set(get_args(RuntimeRunSource)) == {
        "chat",
        "mission",
        "browser",
        "capability",
        "pipeline",
        "cron",
        "reminder",
    }
    assert set(get_args(RuntimeRunStatus)) == {
        "queued",
        "claimed",
        "running",
        "waiting_approval",
        "paused",
        "stop_requested",
        "succeeded",
        "failed",
        "cancelled",
        "stalled",
    }

    payload = _contract_payload()
    payload["source"] = "browser_task"
    with pytest.raises(ValidationError):
        RuntimeRunContract.model_validate(payload)

    payload = _contract_payload()
    payload["status"] = "completed"
    with pytest.raises(ValidationError):
        RuntimeRunContract.model_validate(payload)


def test_runtime_run_contract_rejects_synthetic_or_unversioned_extras() -> None:
    payload = _contract_payload()
    payload["synthetic_task_run_id"] = "task-run-1"

    with pytest.raises(ValidationError):
        RuntimeRunContract.model_validate(payload)


def test_runtime_attempt_step_and_effect_contracts_are_versioned() -> None:
    attempt = RuntimeAttemptContract(
        run_id="run-1",
        attempt_id="attempt-2",
        source="browser",
        owner=_owner(),
        status="claimed",
        revision=1,
        attempt_reason="takeover",
        previous_attempt_id="attempt-1",
        lease_owner="worker-b",
        lease_expires_at=NOW + timedelta(minutes=10),
        created_at=NOW,
        started_at=None,
        heartbeat_at=None,
        settled_at=None,
        budget=RuntimeBudget(max_steps=10),
        checkpoint=RuntimeCheckpoint(
            checkpoint_id="checkpoint-1",
            event_id="event-15",
            seq=15,
            next_execution_position="before_tool",
            pending_tool_call={
                "call_id": "call-15",
                "tool_name": "browser.click",
                "arguments": {"selector": "#submit"},
                "metadata": {"provider": "test"},
            },
        ),
        current_step=None,
        error=None,
        stop_reason=None,
        contract_hash="sha256:contract",
        policy_snapshot_hash="sha256:policy",
        release_edition="mas",
    )
    assert attempt.checkpoint is not None
    assert attempt.checkpoint.next_execution_position == "before_tool"
    assert attempt.checkpoint.pending_tool_call is not None
    assert attempt.checkpoint.pending_tool_call.call_id == "call-15"
    step = RuntimeStepContract(
        run_id="run-1",
        attempt_id="attempt-2",
        step_id="step-1",
        source="browser",
        owner=_owner(),
        index=1,
        kind="browser_action",
        title="Open account page",
        status="running",
        revision=1,
        created_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        settled_at=None,
        checkpoint=None,
        error=None,
        release_edition="mas",
    )
    effect = RuntimeEffectRecord(
        run_id="run-1",
        attempt_id="attempt-2",
        step_id="step-1",
        effect_id="effect-1",
        source="browser",
        owner=_owner(),
        status="intent",
        tool_name="browser.click",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="run-1:attempt-2:step-1:effect-1",
        approval_id=None,
        created_at=NOW,
        started_at=None,
        settled_at=None,
        result_hash=None,
        ambiguity_reason=None,
        release_edition="mas",
    )

    assert attempt.schema_version == 1
    assert step.schema_version == 1
    assert effect.schema_version == 1
    assert effect.status in get_args(RuntimeEffectStatus)


def test_runtime_run_contract_core_models_do_not_use_any_dict_fields() -> None:
    models = (
        RuntimeRunOwner,
        RuntimeBudget,
        RuntimeCheckpoint,
        RuntimeStepRef,
        RuntimeRunReason,
        RuntimeRunContract,
        RuntimeAttemptContract,
        RuntimeStepContract,
        RuntimeEffectRecord,
    )

    for model in models:
        for field_name, field in model.model_fields.items():
            annotation = repr(field.annotation)
            assert "typing.Any" not in annotation, (model.__name__, field_name)
            assert "dict" not in annotation, (model.__name__, field_name)


def test_runtime_run_contract_migration_is_explicit() -> None:
    migrated = migrate_runtime_run_contract(_contract_payload())

    assert isinstance(migrated, RuntimeRunContract)
    assert migrated.run_id == "run-1"

    payload = _contract_payload()
    payload["schema_version"] = 99
    with pytest.raises(RuntimeRunContractMigrationError):
        migrate_runtime_run_contract(payload)
