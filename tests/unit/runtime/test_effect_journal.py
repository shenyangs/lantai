# -*- coding: utf-8 -*-
"""Behavior locks for Runtime Effect reducer and journal."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectJournalConflictError,
    RuntimeEffectJournalCorruptionError,
    RuntimeEffectReducerAction,
    RuntimeEffectRecoveryCapability,
    RuntimeEffectTransitionError,
    _checksum_payload,
    assess_runtime_effect_kill_point_replay,
    assess_runtime_effect_replay,
    reduce_runtime_effect,
)
from lantai.runtime.runs import RuntimeEffectRecord, RuntimeRunOwner
from tests.support.runtime_faults import FakeExternalEffect


NOW = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)


def _owner() -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="user-1",
        agent_id="agent-default",
        session_id="session-1",
        workspace_id="workspace-1",
        channel="console",
    )


def _effect(*, status: str = "intent", revision: int = 0) -> RuntimeEffectRecord:
    return RuntimeEffectRecord(
        run_id="run-1",
        attempt_id="attempt-1",
        step_id="step-1",
        effect_id="effect-1",
        source="browser",
        owner=_owner(),
        status=status,  # type: ignore[arg-type]
        revision=revision,
        tool_name="browser.click",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="run-1:attempt-1:step-1:effect-1",
        created_at=NOW,
        release_edition="official",
    )


def test_runtime_effect_reducer_tracks_approval_execution_and_ambiguity() -> None:
    waiting = reduce_runtime_effect(
        _effect(),
        RuntimeEffectReducerAction(
            type="require_approval",
            approval_id="approval-1",
            now=NOW,
        ),
    ).effect
    approved = reduce_runtime_effect(
        waiting,
        RuntimeEffectReducerAction(type="approve", now=NOW),
    ).effect
    running = reduce_runtime_effect(
        approved,
        RuntimeEffectReducerAction(type="start", now=NOW),
    ).effect
    ambiguous = reduce_runtime_effect(
        running,
        RuntimeEffectReducerAction(
            type="mark_ambiguous",
            ambiguity_reason="process died after browser click",
            now=NOW,
        ),
    ).effect
    resolved = reduce_runtime_effect(
        ambiguous,
        RuntimeEffectReducerAction(
            type="probe_committed",
            result_hash="sha256:dom-observation",
            now=NOW,
        ),
    ).effect

    assert [waiting.status, approved.status, running.status] == [
        "waiting_approval",
        "approved",
        "running",
    ]
    assert waiting.approval_id == "approval-1"
    assert running.started_at == NOW
    assert ambiguous.status == "ambiguous"
    assert ambiguous.ambiguity_reason == "process died after browser click"
    assert resolved.status == "probed_committed"
    assert resolved.result_hash == "sha256:dom-observation"
    assert resolved.revision == 5


def test_runtime_effect_reducer_is_idempotent_for_same_terminal_result() -> None:
    running = _effect(status="running", revision=3)
    committed = reduce_runtime_effect(
        running,
        RuntimeEffectReducerAction(
            type="commit",
            result_hash="sha256:result",
            now=NOW,
        ),
    ).effect

    repeated = reduce_runtime_effect(
        committed,
        RuntimeEffectReducerAction(
            type="commit",
            result_hash="sha256:result",
            now=NOW,
        ),
    )

    assert repeated.idempotent is True
    assert repeated.effect == committed

    with pytest.raises(RuntimeEffectTransitionError, match="terminal"):
        reduce_runtime_effect(
            committed,
            RuntimeEffectReducerAction(
                type="commit",
                result_hash="sha256:different",
                now=NOW,
            ),
        )


def test_runtime_effect_reducer_rejects_execution_before_required_approval() -> None:
    waiting = reduce_runtime_effect(
        _effect(),
        RuntimeEffectReducerAction(type="require_approval", now=NOW),
    ).effect

    with pytest.raises(RuntimeEffectTransitionError, match="approval"):
        reduce_runtime_effect(
            waiting,
            RuntimeEffectReducerAction(type="start", now=NOW),
        )


def test_runtime_effect_journal_appends_checksum_envelopes_and_reads_latest(tmp_path):
    journal = RuntimeEffectJournal(tmp_path)
    initial = _effect()
    running = reduce_runtime_effect(
        initial,
        RuntimeEffectReducerAction(type="start", now=NOW),
    ).effect
    committed = reduce_runtime_effect(
        running,
        RuntimeEffectReducerAction(
            type="commit",
            result_hash="sha256:result",
            now=NOW,
        ),
    ).effect

    journal.append_effect(initial)
    journal.append_effect(running)
    journal.append_effect(committed)

    ledger_path = journal.ledger_path("run-1", "attempt-1")
    raw_lines = ledger_path.read_text(encoding="utf-8").splitlines()
    envelopes = [json.loads(line) for line in raw_lines]
    records = journal.list_effects("run-1", "attempt-1")

    assert len(raw_lines) == 3
    assert all(item["checksum"].startswith("sha256:") for item in envelopes)
    assert [record.revision for record in records] == [0, 1, 2]
    assert journal.latest_effect("run-1", "attempt-1", "effect-1") == committed
    assert ledger_path.stat().st_mode & 0o777 == 0o600


def test_runtime_effect_journal_rejects_stale_revisions(tmp_path):
    journal = RuntimeEffectJournal(tmp_path)
    initial = _effect()
    running = reduce_runtime_effect(
        initial,
        RuntimeEffectReducerAction(type="start", now=NOW),
    ).effect

    journal.append_effect(initial)
    journal.append_effect(running)

    with pytest.raises(RuntimeEffectJournalConflictError, match="revision"):
        journal.append_effect(initial)


def test_runtime_effect_journal_rejects_run_attempt_mismatch(tmp_path):
    journal = RuntimeEffectJournal(tmp_path)
    journal.append_effect(_effect())
    ledger_path = journal.ledger_path("run-1", "attempt-1")
    raw = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    raw["payload"]["run_id"] = "different-run"
    raw["payload"]["attempt_id"] = "different-attempt"
    raw["checksum"] = _checksum_payload(raw["payload"])
    ledger_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RuntimeEffectJournalCorruptionError, match="run_id mismatch"):
        journal.list_effects("run-1", "attempt-1")


def test_runtime_effect_journal_rejects_duplicate_revision_on_read(tmp_path):
    journal = RuntimeEffectJournal(tmp_path)
    initial = _effect()
    running = reduce_runtime_effect(
        initial,
        RuntimeEffectReducerAction(type="start", now=NOW),
    ).effect
    journal.append_effect(initial)
    journal.append_effect(running)

    duplicate = running.model_copy(update={"status": "ambiguous"})
    payload = duplicate.model_dump(mode="json")
    envelope = {
        "schema_version": 1,
        "writer_version": "test/manual-corruption",
        "checksum": _checksum_payload(payload),
        "payload": payload,
    }
    ledger_path = journal.ledger_path("run-1", "attempt-1")
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope) + "\n")

    with pytest.raises(RuntimeEffectJournalCorruptionError, match="revision"):
        journal.list_effects("run-1", "attempt-1")


def test_runtime_effect_replay_assessment_treats_missing_intent_as_not_started() -> None:
    assessment = assess_runtime_effect_replay(
        None,
        RuntimeEffectRecoveryCapability(
            external_side_effect=True,
            idempotency="not_idempotent",
            probe="none",
            compensation="none",
            on_ambiguity="manual_disambiguation",
        ),
    )

    assert assessment.observed_status == "missing"
    assert assessment.crash_window == "before_intent"
    assert assessment.automatic_external_call_allowed is True
    assert assessment.requires_human is False
    assert assessment.recommended_reducer_actions == ()


def test_runtime_effect_replay_assessment_allows_intent_before_execution() -> None:
    assessment = assess_runtime_effect_replay(
        _effect(status="intent"),
        RuntimeEffectRecoveryCapability(
            external_side_effect=True,
            idempotency="not_idempotent",
            probe="none",
            compensation="none",
            on_ambiguity="manual_disambiguation",
        ),
    )

    assert assessment.crash_window == "after_intent_before_execution"
    assert assessment.automatic_external_call_allowed is True
    assert assessment.recommended_reducer_actions == ("start",)


def test_runtime_effect_replay_assessment_blocks_non_idempotent_running_effect() -> None:
    assessment = assess_runtime_effect_replay(
        _effect(status="running", revision=1),
        RuntimeEffectRecoveryCapability(
            external_side_effect=True,
            idempotency="not_idempotent",
            probe="none",
            compensation="none",
            on_ambiguity="manual_disambiguation",
        ),
    )

    assert assessment.crash_window == "during_or_after_external_call"
    assert assessment.automatic_external_call_allowed is False
    assert assessment.requires_human is True
    assert assessment.requires_probe is False
    assert assessment.recommended_reducer_actions == ("mark_ambiguous",)


def test_runtime_effect_replay_assessment_requires_probe_when_declared() -> None:
    assessment = assess_runtime_effect_replay(
        _effect(status="running", revision=1),
        RuntimeEffectRecoveryCapability(
            external_side_effect=True,
            idempotency="not_idempotent",
            probe="tool_specific",
            compensation="none",
            on_ambiguity="probe_required",
        ),
    )

    assert assessment.automatic_external_call_allowed is False
    assert assessment.requires_probe is True
    assert assessment.requires_human is False
    assert assessment.recommended_reducer_actions == ("mark_ambiguous",)


def test_runtime_effect_replay_assessment_allows_safe_running_retry() -> None:
    read_only = assess_runtime_effect_replay(
        _effect(status="running", revision=1),
        RuntimeEffectRecoveryCapability(),
    )
    idempotent = assess_runtime_effect_replay(
        _effect(status="running", revision=1),
        RuntimeEffectRecoveryCapability(
            external_side_effect=True,
            idempotency="idempotency_key",
            idempotency_key_transport="runtime_context",
            probe="tool_specific",
            compensation="none",
            on_ambiguity="safe_to_retry",
        ),
    )

    assert read_only.automatic_external_call_allowed is True
    assert idempotent.automatic_external_call_allowed is True
    assert read_only.recommended_reducer_actions == (
        "mark_ambiguous",
        "mark_safe_to_retry",
    )
    assert idempotent.recommended_reducer_actions == (
        "mark_ambiguous",
        "mark_safe_to_retry",
    )


def test_runtime_effect_replay_assessment_resumes_after_commit_without_reexecution() -> None:
    committed = _effect(status="committed", revision=2)

    assessment = assess_runtime_effect_replay(committed, RuntimeEffectRecoveryCapability())

    assert assessment.crash_window == "after_commit_before_checkpoint"
    assert assessment.automatic_external_call_allowed is False
    assert assessment.requires_human is False
    assert assessment.recommended_reducer_actions == ()


def test_runtime_effect_kill_point_matrix_blocks_non_idempotent_replay() -> None:
    recovery = RuntimeEffectRecoveryCapability(
        external_side_effect=True,
        idempotency="not_idempotent",
        probe="none",
        compensation="none",
        on_ambiguity="manual_disambiguation",
    )

    cases = [
        (
            "before_intent_write",
            None,
            True,
            False,
            False,
            False,
            False,
        ),
        (
            "after_intent_fsync_before_execution",
            _effect(status="intent"),
            True,
            False,
            False,
            False,
            False,
        ),
        (
            "after_external_call_before_response",
            _effect(status="running", revision=1),
            False,
            True,
            True,
            False,
            False,
        ),
        (
            "after_external_success_before_commit",
            _effect(status="running", revision=1),
            False,
            True,
            True,
            False,
            False,
        ),
        (
            "after_commit_before_checkpoint",
            _effect(status="committed", revision=2),
            False,
            True,
            True,
            True,
            False,
        ),
        (
            "after_checkpoint_before_terminal",
            _effect(status="committed", revision=2),
            False,
            True,
            True,
            False,
            True,
        ),
    ]

    for (
        kill_point,
        effect,
        automatic_allowed,
        may_have_called,
        must_not_replay,
        needs_checkpoint,
        needs_terminal,
    ) in cases:
        assessment = assess_runtime_effect_kill_point_replay(
            kill_point=kill_point,  # type: ignore[arg-type]
            effect=effect,
            recovery=recovery,
        )

        assert assessment.kill_point == kill_point
        assert assessment.automatic_external_call_allowed is automatic_allowed
        assert assessment.external_call_may_have_been_issued is may_have_called
        assert assessment.external_call_must_not_be_replayed is must_not_replay
        assert assessment.needs_checkpoint_rebuild is needs_checkpoint
        assert assessment.needs_terminal_rewrite is needs_terminal


@pytest.mark.asyncio
async def test_runtime_effect_kill_point_replay_does_not_duplicate_external_effect() -> None:
    external = FakeExternalEffect()
    running = _effect(status="running", revision=1)
    await external.execute(running.effect_id, payload={"message": "sent"})

    assessment = assess_runtime_effect_kill_point_replay(
        kill_point="after_external_success_before_commit",
        effect=running,
        recovery=RuntimeEffectRecoveryCapability(
            external_side_effect=True,
            idempotency="not_idempotent",
            probe="none",
            compensation="none",
            on_ambiguity="manual_disambiguation",
        ),
    )

    if assessment.automatic_external_call_allowed:
        await external.execute(running.effect_id, payload={"message": "sent"})

    assert assessment.external_call_must_not_be_replayed is True
    assert assessment.requires_human is True
    assert external.count(running.effect_id) == 1
