# -*- coding: utf-8 -*-
"""Behavior locks for the authoritative Runtime Run reducer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lantai.runtime.runs import (
    RuntimeRunFencingError,
    RuntimeRunReducerAction,
    RuntimeRunTransitionError,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeRunReason,
    RuntimeStepRef,
    RuntimeStallAssessment,
    assess_runtime_run_heartbeat,
    build_chat_task_contract,
    dominant_runtime_run_status,
    reduce_runtime_run,
    revise_chat_task_contract,
    start_runtime_run_attempt,
)


NOW = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)


def _owner() -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="user-1",
        agent_id="agent-default",
        session_id="session-1",
        workspace_id="workspace-1",
        channel="console",
    )


def _run(
    *,
    status: str = "queued",
    revision: int = 0,
    attempt_id: str = "attempt-1",
    lease_owner: str | None = None,
    heartbeat_at: datetime | None = None,
    settled_at: datetime | None = None,
    task_contract=None,
) -> RuntimeRunContract:
    return RuntimeRunContract(
        run_id="run-1",
        attempt_id=attempt_id,
        source="chat",
        owner=_owner(),
        status=status,  # type: ignore[arg-type]
        revision=revision,
        lease_owner=lease_owner,
        lease_expires_at=NOW + timedelta(minutes=5) if lease_owner else None,
        created_at=NOW,
        started_at=NOW if status != "queued" else None,
        heartbeat_at=heartbeat_at,
        settled_at=settled_at,
        release_edition="official",
        task_contract=task_contract,
    )


def test_runtime_run_reducer_allows_authoritative_path_and_increments_revision() -> None:
    claimed = reduce_runtime_run(
        _run(),
        RuntimeRunReducerAction(
            type="claim",
            expected_attempt_id="attempt-1",
            expected_revision=0,
            lease_owner="worker-a",
            now=NOW,
        ),
    ).run
    running = reduce_runtime_run(
        claimed,
        RuntimeRunReducerAction(
            type="start",
            expected_revision=1,
            lease_owner="worker-a",
            now=NOW + timedelta(seconds=1),
        ),
    ).run
    waiting = reduce_runtime_run(
        running,
        RuntimeRunReducerAction(type="wait_approval", now=NOW + timedelta(seconds=2)),
    ).run
    resumed = reduce_runtime_run(
        waiting,
        RuntimeRunReducerAction(type="resume", lease_owner="worker-a", now=NOW + timedelta(seconds=3)),
    ).run
    stop_requested = reduce_runtime_run(
        resumed,
        RuntimeRunReducerAction(
            type="request_stop",
            reason=RuntimeRunReason(code="user_stop", category="user"),
            now=NOW + timedelta(seconds=4),
        ),
    ).run
    succeeded = reduce_runtime_run(
        stop_requested,
        RuntimeRunReducerAction(
            type="settle",
            status="succeeded",
            now=NOW + timedelta(seconds=5),
        ),
    ).run

    assert [claimed.status, running.status, waiting.status, resumed.status] == [
        "claimed",
        "running",
        "waiting_approval",
        "running",
    ]
    assert stop_requested.status == "stop_requested"
    assert stop_requested.stop_reason is not None
    assert succeeded.status == "succeeded"
    assert succeeded.revision == 6
    assert succeeded.settled_at == NOW + timedelta(seconds=5)


def test_runtime_run_reducer_persists_budget_wait_and_resume_clears_it() -> None:
    paused = reduce_runtime_run(
        _run(status="running", revision=3),
        RuntimeRunReducerAction(
            type="pause",
            wait_reason="budget",
            expected_revision=3,
            now=NOW,
        ),
    ).run

    assert paused.status == "paused"
    assert paused.wait_reason == "budget"
    assert paused.next_eligible_at is None

    resumed = reduce_runtime_run(
        paused,
        RuntimeRunReducerAction(
            type="resume",
            expected_revision=4,
            now=NOW + timedelta(seconds=1),
        ),
    ).run

    assert resumed.status == "running"
    assert resumed.wait_reason is None
    assert resumed.next_eligible_at is None


def test_runtime_run_reducer_rejects_terminal_revival_in_same_attempt() -> None:
    failed = reduce_runtime_run(
        _run(status="running", revision=3),
        RuntimeRunReducerAction(
            type="settle",
            status="failed",
            reason=RuntimeRunReason(code="tool_failed", category="tool"),
            now=NOW,
        ),
    ).run

    with pytest.raises(RuntimeRunTransitionError, match="terminal"):
        reduce_runtime_run(
            failed,
            RuntimeRunReducerAction(type="resume", now=NOW + timedelta(seconds=1)),
        )

    with pytest.raises(RuntimeRunTransitionError, match="terminal"):
        reduce_runtime_run(
            failed,
            RuntimeRunReducerAction(type="start", now=NOW + timedelta(seconds=1)),
        )


def test_runtime_run_reducer_fences_attempt_revision_and_lease_owner() -> None:
    run = _run(status="running", revision=4, lease_owner="worker-a")

    with pytest.raises(RuntimeRunFencingError, match="attempt"):
        reduce_runtime_run(
            run,
            RuntimeRunReducerAction(type="pause", expected_attempt_id="attempt-2"),
        )

    with pytest.raises(RuntimeRunFencingError, match="revision"):
        reduce_runtime_run(
            run,
            RuntimeRunReducerAction(type="pause", expected_revision=3),
        )

    with pytest.raises(RuntimeRunFencingError, match="lease"):
        reduce_runtime_run(
            run,
            RuntimeRunReducerAction(type="pause", lease_owner="worker-b"),
        )


def test_runtime_run_reducer_persists_one_fenced_task_revision() -> None:
    original = build_chat_task_contract(
        objective="先回答问题",
        client_turn_id="turn-1",
        complexity="standard",
        work_method_id="standard",
        run_id="run-1",
        attempt_id="attempt-1",
    )
    revised = revise_chat_task_contract(
        original,
        client_turn_id="steer-1",
        command="再生成 Word 文档",
    )
    run = _run(status="running", revision=4, task_contract=original)

    reduced = reduce_runtime_run(
        run,
        RuntimeRunReducerAction(
            type="revise_task",
            task_contract=revised,
            expected_attempt_id="attempt-1",
            expected_revision=4,
            now=NOW,
        ),
    ).run

    assert reduced.status == "running"
    assert reduced.revision == 5
    assert reduced.task_contract == revised


def test_runtime_run_reducer_rejects_task_revision_that_rewrites_base_contract() -> None:
    original = build_chat_task_contract(
        objective="先回答问题",
        client_turn_id="turn-1",
        complexity="standard",
        work_method_id="standard",
        run_id="run-1",
        attempt_id="attempt-1",
    )
    rewritten = revise_chat_task_contract(
        original,
        client_turn_id="steer-1",
        command="再生成 Word 文档",
    ).model_copy(update={"objective": "偷偷替换原始目标"})

    with pytest.raises(RuntimeRunTransitionError, match="base contract"):
        reduce_runtime_run(
            _run(status="running", revision=4, task_contract=original),
            RuntimeRunReducerAction(
                type="revise_task",
                task_contract=rewritten,
                expected_revision=4,
                now=NOW,
            ),
        )


def test_retry_and_takeover_create_new_attempts_instead_of_reopening_terminal() -> None:
    terminal = _run(
        status="failed",
        revision=7,
        attempt_id="attempt-1",
        lease_owner="worker-a",
        settled_at=NOW,
    )

    retry = start_runtime_run_attempt(
        terminal,
        attempt_id="attempt-2",
        attempt_reason="retry",
        lease_owner="worker-b",
        now=NOW + timedelta(seconds=1),
    )
    takeover = start_runtime_run_attempt(
        terminal,
        attempt_id="attempt-3",
        attempt_reason="takeover",
        lease_owner="worker-c",
        now=NOW + timedelta(seconds=2),
    )

    assert retry.run_id == terminal.run_id
    assert retry.attempt_id == "attempt-2"
    assert retry.previous_attempt_id == "attempt-1"
    assert retry.status == "queued"
    assert retry.revision == 0
    assert retry.settled_at is None
    assert retry.error is None
    assert retry.lease_owner == "worker-b"
    assert takeover.previous_attempt_id == "attempt-1"

    with pytest.raises(RuntimeRunTransitionError, match="new attempt_id"):
        start_runtime_run_attempt(
            terminal,
            attempt_id="attempt-1",
            attempt_reason="retry",
            now=NOW + timedelta(seconds=3),
        )


def test_heartbeat_timeout_only_suspects_before_explicit_stalled_terminal() -> None:
    running = _run(
        status="running",
        revision=2,
        lease_owner="worker-a",
        heartbeat_at=NOW,
    )
    waiting = _run(status="waiting_approval", revision=5, heartbeat_at=NOW)
    heartbeat = reduce_runtime_run(
        waiting,
        RuntimeRunReducerAction(type="heartbeat", now=NOW + timedelta(seconds=1)),
    ).run

    assessment = assess_runtime_run_heartbeat(
        running,
        now=NOW + timedelta(seconds=90),
        timeout_seconds=60,
    )
    stalled = reduce_runtime_run(
        assessment.run,
        RuntimeRunReducerAction(
            type="settle",
            status="stalled",
            reason=RuntimeRunReason(code="heartbeat_timeout", category="runtime", retryable=True),
            now=NOW + timedelta(seconds=91),
        ),
    ).run

    assert isinstance(assessment, RuntimeStallAssessment)
    assert heartbeat.status == "waiting_approval"
    assert assessment.suspected_stalled is True
    assert assessment.run.status == "running"
    assert stalled.status == "stalled"
    assert stalled.error is not None
    assert stalled.error.code == "heartbeat_timeout"


def test_heartbeat_can_update_current_step_without_status_change() -> None:
    running = _run(
        status="running",
        revision=2,
        lease_owner="worker-a",
        heartbeat_at=NOW,
    )
    step = RuntimeStepRef(
        step_id="tool:search",
        index=1,
        kind="tool",
        title="Search docs",
    )

    updated = reduce_runtime_run(
        running,
        RuntimeRunReducerAction(
            type="heartbeat",
            expected_attempt_id=running.attempt_id,
            expected_revision=running.revision,
            lease_owner="worker-a",
            current_step=step,
            now=NOW + timedelta(seconds=10),
        ),
    ).run

    assert updated.status == "running"
    assert updated.revision == 3
    assert updated.heartbeat_at == NOW + timedelta(seconds=10)
    assert updated.current_step == step


def test_projection_priority_is_shared_and_terminal_monotonic() -> None:
    assert dominant_runtime_run_status(
        ["queued", "running", "succeeded", "cancelled", "failed"],
    ) == "failed"
    assert dominant_runtime_run_status(["running", "waiting_approval"]) == "waiting_approval"
    assert dominant_runtime_run_status(["stop_requested", "succeeded"]) == "succeeded"
