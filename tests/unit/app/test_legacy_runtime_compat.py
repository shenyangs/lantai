# -*- coding: utf-8 -*-
"""Behavior locks for read-only legacy Runtime compatibility adapters."""

from __future__ import annotations

from datetime import datetime, timezone

from lantai.app.capabilities.models import TaskRun
from lantai.app.experience.legacy_adapter import LegacyExperienceEventAdapter
from lantai.app.capabilities.legacy_adapter import (
    LegacyTaskRunCompatibilityAdapter,
    compare_legacy_task_run_projection,
)


NOW = datetime(2026, 7, 16, 11, 0, tzinfo=timezone.utc)


def test_legacy_experience_event_adapter_is_read_only_and_canonical() -> None:
    adapter = LegacyExperienceEventAdapter()

    runtime_event = adapter.to_runtime_event(
        {
            "id": "evt-continue",
            "seq": 7,
            "ts": NOW.isoformat(),
            "agent_id": "default",
            "chat_id": "chat-1",
            "session_id": "console:chat-1",
            "run_id": "run-1",
            "kind": "chat.turn.continue_requested",
            "status": "info",
            "summary": "请求继续",
            "payload": {
                "request_id": "continue-1",
                "mode": "retry",
                "run_id": "run-1",
                "reason": "retry_requires_frontend_resubmit",
            },
        },
    )

    assert adapter.side_effect_free is True
    assert not hasattr(adapter, "run_turn")
    assert runtime_event.type == "turn.continue_requested"
    assert runtime_event.payload["mode"] == "retry"
    assert runtime_event.payload["legacy_kind"] == "chat.turn.continue_requested"


def test_legacy_task_run_adapter_converts_snapshot_to_runtime_contract() -> None:
    adapter = LegacyTaskRunCompatibilityAdapter(release_edition="mas")
    task_run = TaskRun(
        id="task-1",
        agent_id="agent-default",
        source="pipeline",
        chat_id="chat-1",
        session_id="console:chat-1",
        title="整理日报",
        status="timed_out",
        started_at=NOW,
        ended_at=NOW,
        failure_reason="执行超时",
        metadata={"workspace_id": "workspace-1"},
    )

    contract = adapter.to_runtime_run_contract(task_run)

    assert adapter.side_effect_free is True
    assert contract.run_id == "task-1"
    assert contract.attempt_id == "task-1:legacy-attempt"
    assert contract.source == "pipeline"
    assert contract.status == "stalled"
    assert contract.owner.agent_id == "agent-default"
    assert contract.owner.session_id == "console:chat-1"
    assert contract.owner.workspace_id == "workspace-1"
    assert contract.release_edition == "mas"
    assert contract.error is not None
    assert contract.error.code == "legacy_task_run_failure"


def test_shadow_task_run_projection_compare_has_no_side_effects() -> None:
    legacy = TaskRun(
        id="task-1",
        agent_id="agent-default",
        source="capability",
        chat_id="chat-1",
        session_id="console:chat-1",
        title="能力执行",
        status="succeeded",
        started_at=NOW,
        ended_at=NOW,
    )
    adapter = LegacyTaskRunCompatibilityAdapter()
    canonical = adapter.to_runtime_run_contract(legacy)

    report = compare_legacy_task_run_projection([legacy], [canonical])

    assert report.side_effect_free is True
    assert report.side_effects_executed == 0
    assert report.matched_run_ids == ["task-1"]
    assert report.missing_run_ids == []
    assert report.status_mismatches == []
