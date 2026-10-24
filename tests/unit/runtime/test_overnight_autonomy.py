# -*- coding: utf-8 -*-
"""Behavior locks for task-scoped long-horizon authorization and wake intent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from lantai.runtime.runs import (
    RuntimeAutonomyGrant,
    RuntimeBudget,
    RuntimeModelPriceBound,
    RuntimeRunContract,
    RuntimeRunFencingError,
    RuntimeRunOwner,
    RuntimeRunReducerAction,
    RuntimeRunTransitionError,
    RuntimeTaskContract,
    RuntimeWakeIntent,
    build_runtime_wake_key,
    evaluate_runtime_autonomy,
    evaluate_runtime_wake,
    reduce_runtime_run,
    revise_chat_task_contract,
)


NOW = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


def _grant(**updates: object) -> RuntimeAutonomyGrant:
    payload: dict[str, object] = {
        "grant_id": "grant-1",
        "approved": True,
        "issued_by": "user",
        "run_id": "run-1",
        "chat_id": "chat-1",
        "workspace_id": "/workspace/one",
        "target_task_revision": 0,
        "issued_at": NOW,
        "starts_at": NOW,
        "expires_at": NOW + timedelta(hours=8),
        "allowed_operations": (
            "automatic_resume",
            "model_call",
            "network_read",
        ),
        "allowed_paths": ("/workspace/one",),
        "allowed_provider_ids": ("provider-a",),
        "allowed_model_ids": ("model-a",),
        "allow_network": True,
        "allow_gui": False,
        "allow_external_delivery": False,
        "auto_resume_conditions": ("app_reopen", "network_recovery"),
        "budget_currency": "CNY",
        "max_cost_micro_cny": 25_000_000,
        "model_price_bounds": (
            {
                "provider_id": "provider-a",
                "model_id": "model-a",
                "pricing_version": "provider-a-2026-09-20",
                "max_dispatch_micro_cny": 2_000_000,
                "upper_bound_proven": True,
            },
        ),
        "revision": 1,
        "revocation_generation": 0,
    }
    payload.update(updates)
    return RuntimeAutonomyGrant.model_validate(payload)


def _run(
    *,
    grant: RuntimeAutonomyGrant | None,
    status: str = "running",
    task_revision: int = 0,
    wake_intent: RuntimeWakeIntent | None = None,
) -> RuntimeRunContract:
    task = RuntimeTaskContract(
        run_id="run-1",
        attempt_id="attempt-1",
        revision=task_revision,
        commands=()
        if task_revision == 0
        else tuple(
            {
                "revision": index,
                "client_turn_id": f"steer-{index}",
                "text": f"command {index}",
            }
            for index in range(1, task_revision + 1)
        ),
        client_turn_id=None if task_revision == 0 else f"steer-{task_revision}",
        objective="continue the existing task",
        budget=RuntimeBudget(max_cost_points=25),
        autonomy_grant=grant,
    )
    return RuntimeRunContract(
        run_id="run-1",
        attempt_id="attempt-1",
        source="chat",
        owner=RuntimeRunOwner(
            user_id="user-1",
            agent_id="default",
            session_id="console:chat-1",
            workspace_id="/workspace/one",
        ),
        status=status,
        task_contract=task,
        wake_intent=wake_intent,
        release_edition="official",
    )


def test_ordinary_task_is_fail_closed_without_autonomy_grant() -> None:
    decision = evaluate_runtime_autonomy(
        _run(grant=None),
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="app_reopen",
        now=NOW + timedelta(minutes=1),
    )

    assert decision.allowed is False
    assert decision.code == "autonomy_grant_missing"


def test_valid_user_grant_is_bound_to_task_chat_workspace_and_revision() -> None:
    run = _run(grant=_grant())

    decision = evaluate_runtime_autonomy(
        run,
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="app_reopen",
        now=NOW + timedelta(minutes=1),
    )

    assert decision.allowed is True
    assert decision.code == "autonomy_allowed"

    wrong_chat = evaluate_runtime_autonomy(
        run,
        operation="automatic_resume",
        chat_id="chat-2",
        workspace_id="/workspace/one",
        resume_condition="app_reopen",
        now=NOW + timedelta(minutes=1),
    )
    wrong_workspace = evaluate_runtime_autonomy(
        run,
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/two",
        resume_condition="app_reopen",
        now=NOW + timedelta(minutes=1),
    )

    assert wrong_chat.code == "autonomy_chat_mismatch"
    assert wrong_workspace.code == "autonomy_workspace_mismatch"


def test_expiry_revocation_and_task_revision_never_extend_across_attempts() -> None:
    expired = evaluate_runtime_autonomy(
        _run(grant=_grant(expires_at=NOW + timedelta(minutes=5))),
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="app_reopen",
        now=NOW + timedelta(minutes=5),
    )
    revoked = evaluate_runtime_autonomy(
        _run(
            grant=_grant(
                revision=2,
                revocation_generation=1,
                revoked_at=NOW + timedelta(minutes=2),
                revocation_reason="user revoked",
            )
        ),
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="app_reopen",
        now=NOW + timedelta(minutes=3),
    )
    revised = evaluate_runtime_autonomy(
        _run(grant=_grant(target_task_revision=0), task_revision=1),
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="app_reopen",
        now=NOW + timedelta(minutes=1),
    )

    assert expired.code == "autonomy_grant_expired"
    assert revoked.code == "autonomy_grant_revoked"
    assert revised.code == "autonomy_task_revision_mismatch"


def test_provider_network_gui_and_delivery_require_explicit_scope() -> None:
    run = _run(grant=_grant())

    provider = evaluate_runtime_autonomy(
        run,
        operation="model_call",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        provider_id="provider-b",
        now=NOW + timedelta(minutes=1),
    )
    gui = evaluate_runtime_autonomy(
        run,
        operation="gui",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        now=NOW + timedelta(minutes=1),
    )
    delivery = evaluate_runtime_autonomy(
        run,
        operation="external_delivery",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        now=NOW + timedelta(minutes=1),
    )

    assert provider.code == "autonomy_provider_not_allowed"
    assert gui.code == "autonomy_operation_not_allowed"
    assert delivery.code == "autonomy_operation_not_allowed"


def test_billable_grant_requires_exact_money_and_verified_model_price_bounds() -> None:
    with pytest.raises(ValidationError, match="exact task budget"):
        _grant(max_cost_micro_cny=None)
    with pytest.raises(ValidationError, match="price bound"):
        _grant(model_price_bounds=())
    with pytest.raises(ValidationError, match="allowlists"):
        _grant(
            model_price_bounds=(
                RuntimeModelPriceBound(
                    provider_id="provider-b",
                    model_id="model-a",
                    pricing_version="provider-b-2026-09-20",
                    max_dispatch_micro_cny=1,
                    upper_bound_proven=True,
                ),
            ),
        )


def test_model_call_requires_allowed_model_and_exact_budget() -> None:
    run = _run(grant=_grant())

    allowed = evaluate_runtime_autonomy(
        run,
        operation="model_call",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        provider_id="provider-a",
        model_id="model-a",
        now=NOW + timedelta(minutes=1),
    )
    wrong_model = evaluate_runtime_autonomy(
        run,
        operation="model_call",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        provider_id="provider-a",
        model_id="model-b",
        now=NOW + timedelta(minutes=1),
    )

    assert allowed.allowed is True
    assert wrong_model.code == "autonomy_model_not_allowed"


def test_automatic_resume_requires_the_exact_user_approved_condition() -> None:
    run = _run(grant=_grant(auto_resume_conditions=("app_reopen",)))

    app_reopen = evaluate_runtime_autonomy(
        run,
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="app_reopen",
        now=NOW + timedelta(minutes=1),
    )
    network_recovery = evaluate_runtime_autonomy(
        run,
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="network_recovery",
        now=NOW + timedelta(minutes=1),
    )
    unspecified = evaluate_runtime_autonomy(
        run,
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        now=NOW + timedelta(minutes=1),
    )

    assert app_reopen.allowed is True
    assert network_recovery.code == "autonomy_resume_condition_not_allowed"
    assert unspecified.code == "autonomy_resume_condition_required"


def test_local_operation_rechecks_canonical_target_path(tmp_path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (allowed_root / "escape").symlink_to(outside_root, target_is_directory=True)
    run = _run(
        grant=_grant(
            allowed_operations=("local_read", "local_write_new"),
            allowed_paths=(str(allowed_root),),
            auto_resume_conditions=(),
        )
    )

    inside = evaluate_runtime_autonomy(
        run,
        operation="local_write_new",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        target_path=str(allowed_root / "new.txt"),
        now=NOW + timedelta(minutes=1),
    )
    missing = evaluate_runtime_autonomy(
        run,
        operation="local_read",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        now=NOW + timedelta(minutes=1),
    )
    escaped = evaluate_runtime_autonomy(
        run,
        operation="local_write_new",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        target_path=str(allowed_root / "escape" / "new.txt"),
        now=NOW + timedelta(minutes=1),
    )

    assert inside.allowed is True
    assert missing.code == "autonomy_target_path_required"
    assert escaped.code == "autonomy_path_not_allowed"


def test_unapproved_or_internally_issued_grant_payload_is_rejected() -> None:
    payload = _grant().model_dump(mode="python")
    payload["approved"] = False
    with pytest.raises(ValidationError):
        RuntimeAutonomyGrant.model_validate(payload)

    payload = _grant().model_dump(mode="python")
    payload["issued_by"] = "model"
    with pytest.raises(ValidationError):
        RuntimeAutonomyGrant.model_validate(payload)


def test_wake_key_is_stable_and_binds_revision_generation_and_reason() -> None:
    baseline = build_runtime_wake_key(
        run_id="run-1",
        task_revision=0,
        continuation_generation=3,
        reason_revision=2,
        wait_reason="provider_backoff",
    )

    assert baseline == build_runtime_wake_key(
        run_id="run-1",
        task_revision=0,
        continuation_generation=3,
        reason_revision=2,
        wait_reason="provider_backoff",
    )
    assert baseline != build_runtime_wake_key(
        run_id="run-1",
        task_revision=0,
        continuation_generation=4,
        reason_revision=2,
        wait_reason="provider_backoff",
    )
    assert baseline != build_runtime_wake_key(
        run_id="run-1",
        task_revision=0,
        continuation_generation=3,
        reason_revision=2,
        wait_reason="network",
    )


def test_persisted_wake_is_not_eligible_early_and_rechecks_current_grant() -> None:
    wake = RuntimeWakeIntent.create(
        run_id="run-1",
        task_revision=0,
        continuation_generation=3,
        reason_revision=2,
        wait_reason="provider_backoff",
        next_eligible_at=NOW + timedelta(minutes=10),
        grant=_grant(),
    )
    run = _run(grant=_grant(), wake_intent=wake)

    early = evaluate_runtime_wake(
        run,
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="network_recovery",
        now=NOW + timedelta(minutes=9),
    )
    due = evaluate_runtime_wake(
        run,
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="network_recovery",
        now=NOW + timedelta(minutes=10),
    )
    revoked_run = _run(
        grant=_grant(
            revision=2,
            revocation_generation=1,
            revoked_at=NOW + timedelta(minutes=5),
            revocation_reason="user revoked",
        ),
        wake_intent=wake,
    )
    revoked = evaluate_runtime_wake(
        revoked_run,
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="network_recovery",
        now=NOW + timedelta(minutes=10),
    )

    assert early.code == "wake_not_due"
    assert due.allowed is True
    assert revoked.code == "wake_grant_revision_mismatch"


@pytest.mark.parametrize("status", ["paused", "waiting_approval", "stop_requested"])
def test_automatic_resume_does_not_override_user_or_approval_boundaries(
    status: str,
) -> None:
    decision = evaluate_runtime_autonomy(
        _run(grant=_grant(), status=status),
        operation="automatic_resume",
        chat_id="chat-1",
        workspace_id="/workspace/one",
        resume_condition="app_reopen",
        now=NOW + timedelta(minutes=1),
    )

    assert decision.allowed is False
    assert decision.code == "autonomy_run_status_blocked"


def test_reducer_persists_grant_and_wake_under_existing_run_fences() -> None:
    run = _run(grant=None)
    granted = reduce_runtime_run(
        run,
        RuntimeRunReducerAction(
            type="set_autonomy_grant",
            autonomy_grant=_grant(),
            expected_attempt_id=run.attempt_id,
            expected_revision=run.revision,
            now=NOW,
        ),
    ).run
    assert granted.task_contract is not None
    assert granted.task_contract.autonomy_grant == _grant()
    assert granted.task_contract.revision == 0

    wake = RuntimeWakeIntent.create(
        run_id="run-1",
        task_revision=0,
        continuation_generation=1,
        reason_revision=1,
        wait_reason="network",
        next_eligible_at=NOW + timedelta(minutes=1),
        grant=_grant(),
    )
    waiting = reduce_runtime_run(
        granted,
        RuntimeRunReducerAction(
            type="set_wake_intent",
            wake_intent=wake,
            expected_attempt_id=granted.attempt_id,
            expected_revision=granted.revision,
            now=NOW,
        ),
    ).run

    assert waiting.wake_intent == wake
    with pytest.raises(RuntimeRunFencingError, match="revision"):
        reduce_runtime_run(
            granted,
            RuntimeRunReducerAction(
                type="set_wake_intent",
                wake_intent=wake,
                expected_revision=granted.revision - 1,
                now=NOW,
            ),
        )


def test_stop_and_task_revision_invalidate_old_wake() -> None:
    grant = _grant()
    wake = RuntimeWakeIntent.create(
        run_id="run-1",
        task_revision=0,
        continuation_generation=1,
        reason_revision=1,
        wait_reason="network",
        next_eligible_at=NOW + timedelta(minutes=1),
        grant=grant,
    )
    run = _run(grant=grant, wake_intent=wake)
    stopped = reduce_runtime_run(
        run,
        RuntimeRunReducerAction(
            type="request_stop",
            expected_revision=run.revision,
            now=NOW,
        ),
    ).run

    assert stopped.wake_intent is None
    stopped_task = stopped.task_contract
    assert stopped_task is not None
    stopped_grant = stopped_task.autonomy_grant
    assert stopped_grant is not None
    assert stopped_grant.revocation_generation == 1
    assert stopped_grant.revision == grant.revision + 1
    assert stopped_grant.revoked_at == NOW
    assert stopped_grant.revocation_reason == "runtime_stop_requested"

    current_task = run.task_contract
    assert current_task is not None
    revised_task = revise_chat_task_contract(
        current_task,
        client_turn_id="steer-1",
        command="change target",
    )
    revised = reduce_runtime_run(
        run,
        RuntimeRunReducerAction(
            type="revise_task",
            task_contract=revised_task,
            expected_revision=run.revision,
            now=NOW,
        ),
    ).run
    assert revised.wake_intent is None


def test_grant_update_requires_monotonic_revision_and_revocation_generation() -> None:
    run = _run(grant=_grant())

    with pytest.raises(RuntimeRunTransitionError, match="revision"):
        reduce_runtime_run(
            run,
            RuntimeRunReducerAction(
                type="set_autonomy_grant",
                autonomy_grant=_grant(revision=1),
                now=NOW,
            ),
        )

    revoked_grant = _grant(
        revision=2,
        revocation_generation=1,
        revoked_at=NOW + timedelta(minutes=2),
        revocation_reason="user revoked",
    )
    revoked = reduce_runtime_run(
        run,
        RuntimeRunReducerAction(
            type="set_autonomy_grant",
            autonomy_grant=revoked_grant,
            expected_revision=run.revision,
            now=NOW + timedelta(minutes=2),
        ),
    ).run
    assert revoked.task_contract is not None
    assert revoked.task_contract.autonomy_grant == revoked_grant
    assert revoked.wake_intent is None
