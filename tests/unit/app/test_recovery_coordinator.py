# -*- coding: utf-8 -*-
"""Behavior locks for safe RuntimeRun recovery after process restart."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.app.beta_access.admission import BetaAccessAdmissionGuard
from lantai.app.experience.runtime_runs import record_chat_runtime_run_started
from lantai.app.recovery_coordinator import RecoveryCoordinator
from lantai.app.runner.models import ChatSpec
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.runs import (
    RuntimeAutonomyGrant,
    RuntimeEffectRecord,
    RuntimeRunOwner,
    RuntimeRunReducerAction,
    RuntimeWakeIntent,
    build_chat_task_contract,
    reduce_runtime_run,
)
from lantai.runtime.store import RuntimeRunStore
from lantai.security.beta_access import AccessSnapshot, BetaAccessStatus


class _ChatManager:
    def __init__(self, chat: ChatSpec) -> None:
        self.chat = chat

    async def list_chats(self):
        return [self.chat]

    async def merge_chat_meta(self, _chat_id, values, remove_keys=()):
        meta = dict(self.chat.meta)
        meta.update(values)
        for key in remove_keys:
            meta.pop(key, None)
        self.chat = self.chat.model_copy(update={"meta": meta})
        return self.chat


async def _workspace_with_run(tmp_path: Path, run_id: str):
    task_contract = build_chat_task_contract(
        objective="继续完成离线网页",
        client_turn_id="client-1",
        complexity="light",
        work_method_id="standard",
    )
    await record_chat_runtime_run_started(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_id="chat-1",
        session_id="console:chat-1",
        run_id=run_id,
        effective_workspace_dir=str(tmp_path),
        task_contract=task_contract,
    )
    chat = ChatSpec(
        id="chat-1",
        session_id="console:chat-1",
        user_id="user-1",
        channel="console",
        meta={
            "last_turn_run_id": run_id,
            "pending_turn": {"run_id": run_id, "status": "running"},
        },
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_manager=_ChatManager(chat),
    )
    return workspace


async def _grant_autonomous_resume(tmp_path: Path, run_id: str) -> None:
    store = RuntimeRunStore(tmp_path)
    contract = await store.load_contract(run_id)
    assert contract is not None
    assert contract.task_contract is not None
    now = datetime.now(timezone.utc)
    grant = RuntimeAutonomyGrant(
        grant_id=f"grant-{run_id}",
        approved=True,
        issued_by="user",
        run_id=run_id,
        chat_id="chat-1",
        workspace_id=str(tmp_path),
        target_task_revision=contract.task_contract.revision,
        issued_at=now,
        starts_at=now,
        expires_at=now + timedelta(hours=2),
        allowed_operations=("automatic_resume",),
        auto_resume_conditions=("app_reopen", "network_recovery"),
    )
    granted = reduce_runtime_run(
        contract,
        RuntimeRunReducerAction(
            type="set_autonomy_grant",
            autonomy_grant=grant,
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
            now=now,
        ),
    ).run
    await store.save_contract(
        granted,
        expected_revision=contract.revision,
        expected_attempt_id=contract.attempt_id,
    )


async def _update_run(
    tmp_path: Path,
    run_id: str,
    action: RuntimeRunReducerAction,
) -> None:
    store = RuntimeRunStore(tmp_path)
    contract = await store.load_contract(run_id)
    assert contract is not None
    updated = reduce_runtime_run(contract, action).run
    await store.save_contract(
        updated,
        expected_revision=contract.revision,
        expected_attempt_id=contract.attempt_id,
    )


@pytest.mark.asyncio
async def test_recovery_creates_resume_attempt_for_safe_unfinished_run(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-safe")

    class _Coordinator(RecoveryCoordinator):
        async def _dispatch(self, chat, contract, **_kwargs):
            self.dispatched = (chat, contract)
            return True

    coordinator = _Coordinator(workspace)
    report = await coordinator.recover(auto_resume=True)
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-safe")

    assert report.resumed == 1
    assert recovered is not None
    assert recovered.status == "running"
    assert recovered.attempt_id.startswith("run-safe:resume-")
    assert getattr(recovered, "attempt_reason", None) == "resume"


@pytest.mark.asyncio
async def test_recovery_admission_denial_does_not_prepare_or_dispatch_attempt(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-beta-denied")
    workspace.beta_access_admission_guard = BetaAccessAdmissionGuard(
        lambda: AccessSnapshot(
            True,
            BetaAccessStatus.EXPIRED,
            "account-1",
            1,
            8,
        )
    )
    before = await RuntimeRunStore(tmp_path).load_contract("run-beta-denied")

    class _Coordinator(RecoveryCoordinator):
        async def _dispatch(self, *_args, **_kwargs):
            pytest.fail("denied recovery must not execute the model")

    report = await _Coordinator(workspace).recover(auto_resume=True)
    after = await RuntimeRunStore(tmp_path).load_contract("run-beta-denied")

    assert report.resumed == 0
    assert report.stalled == 0
    assert report.skipped == 1
    assert after == before


@pytest.mark.asyncio
async def test_recovery_requires_explicit_resume_by_default(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-explicit")

    class _Coordinator(RecoveryCoordinator):
        async def _dispatch(self, *_args, **_kwargs):
            pytest.fail("startup reconciliation must not execute the model")

    report = await _Coordinator(workspace).recover()
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-explicit")

    assert report.resumed == 0
    assert report.stalled == 1
    assert recovered is not None
    assert recovered.status == "stalled"
    assert recovered.error is not None
    assert recovered.error.code == "recovery_explicit_resume_required"
    assert workspace.chat_manager.chat.meta["last_turn_outcome_status"] == "needs_input"


@pytest.mark.asyncio
async def test_startup_autonomous_recovery_fails_closed_without_user_grant(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-no-grant")

    class _Coordinator(RecoveryCoordinator):
        async def _dispatch(self, *_args, **_kwargs):
            pytest.fail("ordinary tasks must not auto-resume after restart")

    report = await _Coordinator(workspace).recover(
        auto_resume=True,
        require_autonomy_grant=True,
    )
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-no-grant")

    assert report.resumed == 0
    assert report.stalled == 1
    assert recovered is not None
    assert recovered.status == "stalled"
    assert recovered.error is not None
    assert recovered.error.code == "recovery_autonomy_grant_required"


@pytest.mark.asyncio
async def test_startup_autonomous_recovery_uses_existing_path_with_valid_grant(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-granted")
    await _grant_autonomous_resume(tmp_path, "run-granted")

    class _Coordinator(RecoveryCoordinator):
        async def _dispatch(self, chat, contract, **_kwargs):
            self.dispatched = (chat, contract)
            return True

    coordinator = _Coordinator(workspace)
    report = await coordinator.recover(
        auto_resume=True,
        require_autonomy_grant=True,
    )
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-granted")

    assert report.resumed == 1
    assert recovered is not None
    assert recovered.status == "running"
    assert recovered.attempt_id.startswith("run-granted:resume-")
    assert coordinator.dispatched[1].run_id == "run-granted"


@pytest.mark.asyncio
async def test_startup_autonomous_recovery_preserves_user_pause(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-paused")
    await _grant_autonomous_resume(tmp_path, "run-paused")
    contract = await RuntimeRunStore(tmp_path).load_contract("run-paused")
    assert contract is not None
    await _update_run(
        tmp_path,
        "run-paused",
        RuntimeRunReducerAction(
            type="pause",
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
        ),
    )

    class _Coordinator(RecoveryCoordinator):
        async def _dispatch(self, *_args, **_kwargs):
            pytest.fail("startup must not override a user pause")

    report = await _Coordinator(workspace).recover(
        auto_resume=True,
        require_autonomy_grant=True,
    )
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-paused")

    assert report.resumed == 0
    assert report.stalled == 0
    assert report.skipped == 1
    assert recovered is not None
    assert recovered.status == "paused"


@pytest.mark.asyncio
async def test_startup_autonomous_recovery_does_not_run_future_wake_early(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-future")
    await _grant_autonomous_resume(tmp_path, "run-future")
    contract = await RuntimeRunStore(tmp_path).load_contract("run-future")
    assert contract is not None
    assert contract.task_contract is not None
    assert contract.task_contract.autonomy_grant is not None
    wake = RuntimeWakeIntent.create(
        run_id=contract.run_id,
        task_revision=contract.task_contract.revision,
        continuation_generation=1,
        reason_revision=1,
        wait_reason="provider_backoff",
        next_eligible_at=datetime.now(timezone.utc) + timedelta(hours=1),
        grant=contract.task_contract.autonomy_grant,
    )
    await _update_run(
        tmp_path,
        "run-future",
        RuntimeRunReducerAction(
            type="set_wake_intent",
            wake_intent=wake,
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
        ),
    )

    class _Coordinator(RecoveryCoordinator):
        async def _dispatch(self, *_args, **_kwargs):
            pytest.fail("a durable wake must not run before next_eligible_at")

    report = await _Coordinator(workspace).recover(
        auto_resume=True,
        require_autonomy_grant=True,
    )
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-future")

    assert report.resumed == 0
    assert report.stalled == 0
    assert report.skipped == 1
    assert recovered is not None
    assert recovered.status == "running"
    assert recovered.wake_intent == wake


@pytest.mark.asyncio
async def test_concurrent_autonomous_recovery_prepares_only_one_attempt(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-concurrent")
    await _grant_autonomous_resume(tmp_path, "run-concurrent")
    arrived = 0
    both_ready = asyncio.Event()
    dispatches = 0

    class _Coordinator(RecoveryCoordinator):
        async def _settle_crash_window_effects(self, *_args, **_kwargs):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=1)
            return True, []

        async def _dispatch(self, _chat, _contract, **_kwargs):
            nonlocal dispatches
            dispatches += 1
            return True

    reports = await asyncio.gather(
        _Coordinator(workspace).recover(
            auto_resume=True,
            require_autonomy_grant=True,
        ),
        _Coordinator(workspace).recover(
            auto_resume=True,
            require_autonomy_grant=True,
        ),
    )
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-concurrent")

    assert sum(report.resumed for report in reports) == 1
    assert sum(report.skipped for report in reports) == 1
    assert sum(report.stalled for report in reports) == 0
    assert dispatches == 1
    assert recovered is not None
    assert recovered.status == "running"
    assert recovered.attempt_id.startswith("run-concurrent:resume-")


@pytest.mark.asyncio
async def test_due_wake_scan_ignores_granted_runs_without_wake_intent(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-no-wake")
    await _grant_autonomous_resume(tmp_path, "run-no-wake")

    class _Coordinator(RecoveryCoordinator):
        async def _dispatch(self, *_args, **_kwargs):
            pytest.fail("periodic due scan must not claim a run without a wake")

    report = await _Coordinator(workspace).recover(
        auto_resume=True,
        require_autonomy_grant=True,
        due_wakes_only=True,
    )
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-no-wake")

    assert report.scanned == 0
    assert report.resumed == 0
    assert recovered is not None
    assert recovered.status == "running"


@pytest.mark.asyncio
async def test_due_wake_scan_dispatches_due_intent_through_existing_path(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-due")
    await _grant_autonomous_resume(tmp_path, "run-due")
    contract = await RuntimeRunStore(tmp_path).load_contract("run-due")
    assert contract is not None
    assert contract.task_contract is not None
    assert contract.task_contract.autonomy_grant is not None
    wake = RuntimeWakeIntent.create(
        run_id=contract.run_id,
        task_revision=contract.task_contract.revision,
        continuation_generation=2,
        reason_revision=3,
        wait_reason="network",
        next_eligible_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        grant=contract.task_contract.autonomy_grant,
    )
    await _update_run(
        tmp_path,
        "run-due",
        RuntimeRunReducerAction(
            type="set_wake_intent",
            wake_intent=wake,
            expected_attempt_id=contract.attempt_id,
            expected_revision=contract.revision,
        ),
    )

    class _Coordinator(RecoveryCoordinator):
        async def _dispatch(self, _chat, contract, **_kwargs):
            self.dispatched = contract
            return True

    coordinator = _Coordinator(workspace)
    report = await coordinator.recover(
        auto_resume=True,
        require_autonomy_grant=True,
        due_wakes_only=True,
    )
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-due")

    assert report.resumed == 1
    assert coordinator.dispatched.run_id == "run-due"
    assert recovered is not None
    assert recovered.status == "running"
    assert recovered.wake_intent is None


@pytest.mark.asyncio
async def test_recovery_stalls_orphaned_running_chat_without_checkpoint(
    tmp_path: Path,
) -> None:
    chat = ChatSpec(
        id="chat-orphaned",
        session_id="console:chat-orphaned",
        user_id="user-1",
        channel="console",
        meta={
            "last_turn_run_id": "run-orphaned",
            "last_turn_status": "running",
            "pending_turn": {
                "run_id": "run-orphaned",
                "status": "running",
                "text": "继续旧任务",
            },
            "topic_workspace_dir": str(tmp_path),
        },
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_manager=_ChatManager(chat),
    )

    report = await RecoveryCoordinator(workspace).recover()

    assert report.scanned == 1
    assert report.stalled == 1
    assert workspace.chat_manager.chat.meta["last_turn_status"] == "stalled"
    assert workspace.chat_manager.chat.meta["last_turn_outcome_status"] == "needs_input"
    assert workspace.chat_manager.chat.meta["last_turn_delivery_status"] == "not_delivered"
    assert workspace.chat_manager.chat.meta["recovery_orphaned_run_id"] == "run-orphaned"
    assert "pending_turn" not in workspace.chat_manager.chat.meta


@pytest.mark.asyncio
async def test_recovery_marks_running_external_effect_ambiguous_and_stalls(
    tmp_path: Path,
) -> None:
    workspace = await _workspace_with_run(tmp_path, "run-effect")
    contract = await RuntimeRunStore(tmp_path).load_contract("run-effect")
    assert contract is not None
    effect = RuntimeEffectRecord(
        run_id=contract.run_id,
        attempt_id=contract.attempt_id,
        step_id="step-1",
        effect_id="effect-1",
        source="chat",
        owner=RuntimeRunOwner(
            agent_id="default",
            session_id="console:chat-1",
            workspace_id=str(tmp_path),
        ),
        tool_name="execute_shell_command",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="effect-1",
        release_edition="official",
    )
    journal = RuntimeEffectJournal(tmp_path)
    journal.append_effect(effect)
    journal.append_effect(
        reduce_runtime_effect(
            effect,
            RuntimeEffectReducerAction(type="start"),
        ).effect
    )

    report = await RecoveryCoordinator(workspace).recover()
    recovered = await RuntimeRunStore(tmp_path).load_contract("run-effect")
    latest_effect = journal.latest_effect(
        contract.run_id,
        contract.attempt_id,
        "effect-1",
    )

    assert report.stalled == 1
    assert recovered is not None
    assert recovered.status == "stalled"
    assert recovered.error is not None
    assert recovered.error.code == "recovery_effect_unknown"
    assert latest_effect is not None
    assert latest_effect.status == "ambiguous"
    assert workspace.chat_manager.chat.meta["last_turn_outcome_status"] == "needs_input"
