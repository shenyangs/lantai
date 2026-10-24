# -*- coding: utf-8 -*-
"""Health levels and deterministic self-recovery for Coherence Harness."""

from datetime import datetime, timezone

import pytest

from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.runtime.coherence import (
    ExecutionFence,
    HarnessHealthService,
)
from lantai.runtime.context_guard import prepare_model_request_for_context
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectReducerAction,
    reduce_runtime_effect,
)
from lantai.runtime.runs import (
    RuntimeEffectRecord,
    RuntimeRunContract,
    RuntimeRunOwner,
)
from lantai.runtime.store import RuntimeRunStore
from lantai.runtime.turn_control import RuntimeTurnControlStore
from lantai.runtime.models import ModelMessage, ModelRequest


NOW = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)


def _owner() -> RuntimeRunOwner:
    return RuntimeRunOwner(
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        workspace_id="workspace-1",
        channel="console",
    )


def _contract(*, status: str = "running") -> RuntimeRunContract:
    return RuntimeRunContract(
        run_id="run-1",
        attempt_id="attempt-1",
        source="chat",
        owner=_owner(),
        status=status,  # type: ignore[arg-type]
        created_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        release_edition="official",
    )


@pytest.mark.asyncio
async def test_health_snapshot_is_l0_for_consistent_runtime(tmp_path) -> None:
    await RuntimeRunStore(tmp_path).save_contract(
        _contract(),
        expected_revision=None,
    )

    snapshot = await HarnessHealthService(tmp_path).audit(
        run_id="run-1",
        attempt_id="attempt-1",
    )

    assert snapshot.level == "L0"
    assert snapshot.findings == ()
    assert snapshot.model_capsule() is None
    assert snapshot.verify_integrity()


@pytest.mark.asyncio
async def test_orphaned_pre_dispatch_input_claim_is_released_at_l1(
    tmp_path,
) -> None:
    await RuntimeRunStore(tmp_path).save_contract(
        _contract(),
        expected_revision=None,
    )
    turn_store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1", generation=1)
    _, item = turn_store.append_steer(
        client_turn_id="steer-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="继续",
        fence=fence,
    )
    turn_store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="native:attempt-1:1",
        fence=fence,
    )
    health = HarnessHealthService(tmp_path)

    snapshot = await health.audit(
        run_id="run-1",
        attempt_id="attempt-1",
        stale_claim_after_seconds=0,
    )
    result = health.execute_safe_recovery(snapshot.recovery_actions[0])
    repeated = health.execute_safe_recovery(snapshot.recovery_actions[0])

    assert snapshot.level == "L1"
    assert snapshot.findings[0].safe_automatic_repair is True
    assert result.status == "executed"
    assert repeated == result
    assert (
        turn_store.current_dispositions("run-1")[item.seq].status == "released"
    )
    assert turn_store.read_admissible_inbox("run-1") == [item]


@pytest.mark.asyncio
async def test_ambiguous_external_effect_is_l3_and_never_auto_repaired(
    tmp_path,
) -> None:
    await RuntimeRunStore(tmp_path).save_contract(
        _contract(),
        expected_revision=None,
    )
    effect = RuntimeEffectRecord(
        run_id="run-1",
        attempt_id="attempt-1",
        step_id="step-1",
        effect_id="effect-1",
        source="chat",
        owner=_owner(),
        status="intent",
        tool_name="send_message",
        args_hash="sha256:args",
        policy_snapshot_hash="sha256:policy",
        idempotency_key="run-1:attempt-1:effect-1",
        created_at=NOW,
        release_edition="official",
    )
    journal = RuntimeEffectJournal(tmp_path)
    journal.append_effect(effect)
    running = reduce_runtime_effect(
        effect,
        RuntimeEffectReducerAction(type="start", now=NOW),
    ).effect
    journal.append_effect(running)
    ambiguous = reduce_runtime_effect(
        running,
        RuntimeEffectReducerAction(
            type="mark_ambiguous",
            ambiguity_reason="transport ended after send",
            now=NOW,
        ),
    ).effect
    journal.append_effect(ambiguous)

    snapshot = await HarnessHealthService(tmp_path).audit(
        run_id="run-1",
        attempt_id="attempt-1",
    )
    capsule = snapshot.model_capsule()

    assert snapshot.level == "L3"
    assert snapshot.recovery_actions == ()
    assert capsule is not None
    assert capsule["stop_external_effects"] is True
    assert "effect_ambiguous" in capsule["codes"]


@pytest.mark.asyncio
async def test_consumed_input_without_model_surface_degrades_to_l2(
    tmp_path,
) -> None:
    await RuntimeRunStore(tmp_path).save_contract(
        _contract(),
        expected_revision=None,
    )
    turn_store = RuntimeTurnControlStore(tmp_path)
    fence = ExecutionFence(attempt_id="attempt-1")
    _, item = turn_store.append_steer(
        client_turn_id="steer-1",
        run_id="run-1",
        attempt_id="attempt-1",
        chat_id="chat-1",
        text="继续",
        fence=fence,
    )
    turn_store.claim_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="worker-1",
        fence=fence,
    )
    turn_store.settle_input(
        run_id="run-1",
        item_seq=item.seq,
        actor_id="worker-1",
        fence=fence,
        status="consumed",
    )

    snapshot = await HarnessHealthService(tmp_path).audit(
        run_id="run-1",
        attempt_id="attempt-1",
    )

    assert snapshot.level == "L2"
    assert snapshot.findings[0].code == "input_consumed_without_surface"
    assert snapshot.model_capsule() is not None


@pytest.mark.asyncio
async def test_tampered_model_surface_is_l3_authority_failure(
    tmp_path,
) -> None:
    await RuntimeRunStore(tmp_path).save_contract(
        _contract(),
        expected_revision=None,
    )
    prepared = prepare_model_request_for_context(
        ModelRequest(
            messages=[ModelMessage.text("user", "hello")],
            metadata={
                "state_workspace_dir": str(tmp_path),
                "run_id": "run-1",
                "turn_id": "run-1",
                "attempt_id": "attempt-1",
                "model_request_id": "run-1:model:1",
                "model_request_created_at": "2026-08-15T00:00:00+00:00",
                "harness_mode": "enforced",
            },
        ),
        ProviderModelEndpoint(provider_id="test", model_id="test-model"),
    )
    manifest_path = (
        tmp_path
        / ".lantai"
        / "runtime"
        / "v1"
        / prepared.metadata["model_request_manifest_ref"]
    )
    manifest_path.write_text('{"tampered":true}', encoding="utf-8")

    snapshot = await HarnessHealthService(tmp_path).audit(
        run_id="run-1",
        attempt_id="attempt-1",
    )

    assert snapshot.level == "L3"
    assert any(
        finding.code == "model_surface_corrupt"
        for finding in snapshot.findings
    )
