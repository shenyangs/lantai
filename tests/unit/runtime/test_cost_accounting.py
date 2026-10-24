"""WP-04 cost, budget and billing evidence contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from lantai.runtime.cost_accounting import (
    BalanceRecoveryFacts,
    BudgetLedger,
    BudgetMode,
    CostEvidence,
    CostEvidenceLedger,
    CostStatus,
    RuntimeBudgetAdmissionError,
    decide_balance_recovery,
    finalize_model_dispatch_budget,
    micro_cny,
    record_model_usage_cost,
    reserve_model_dispatch_budget,
)
from lantai.runtime.models import ModelRequest, ModelUsage
from lantai.runtime.runs import (
    RuntimeAutonomyGrant,
    RuntimeBudget,
    RuntimeRunContract,
    RuntimeRunOwner,
    RuntimeTaskContract,
)
from lantai.runtime.store import RuntimeRunStore


NOW = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)


async def _store_budgeted_autonomy_run(
    root,
    *,
    limit_micro_cny: int = 10_000,
    revoked: bool = False,
) -> RuntimeRunContract:
    grant = RuntimeAutonomyGrant(
        grant_id="grant-budget",
        approved=True,
        issued_by="user",
        run_id="run-budget",
        chat_id="chat-budget",
        workspace_id=str(root.resolve()),
        target_task_revision=0,
        issued_at=NOW,
        starts_at=NOW,
        expires_at=NOW + timedelta(days=1),
        allowed_operations=("model_call",),
        allowed_provider_ids=("provider-a",),
        allowed_model_ids=("model-a",),
        budget_currency="CNY",
        max_cost_micro_cny=limit_micro_cny,
        model_price_bounds=(
            {
                "provider_id": "provider-a",
                "model_id": "model-a",
                "pricing_version": "provider-a-2026-09-20",
                "max_dispatch_micro_cny": 6_000,
                "upper_bound_proven": True,
            },
        ),
        revision=2 if revoked else 1,
        revocation_generation=1 if revoked else 0,
        revoked_at=NOW + timedelta(minutes=1) if revoked else None,
        revocation_reason="user revoked" if revoked else None,
    )
    task = RuntimeTaskContract(
        run_id="run-budget",
        attempt_id="attempt-budget",
        objective="bounded unattended task",
        budget=RuntimeBudget(max_cost_points=10),
        autonomy_grant=grant,
    )
    run = RuntimeRunContract(
        run_id="run-budget",
        attempt_id="attempt-budget",
        source="chat",
        owner=RuntimeRunOwner(
            agent_id="default",
            session_id="console:chat-budget",
            workspace_id=str(root.resolve()),
        ),
        status="running",
        task_contract=task,
        release_edition="official",
    )
    await RuntimeRunStore(root).save_contract(run, expected_revision=None)
    return run


def _budgeted_request(root, *, dispatch_id: str = "dispatch-1") -> ModelRequest:
    return ModelRequest(
        metadata={
            "state_workspace_dir": str(root),
            "run_id": "run-budget",
            "model_request_id": "run-budget:model:1",
            "runtime_model_dispatch_id": dispatch_id,
        },
    )


def test_t038_unknown_cost_is_not_free() -> None:
    evidence = CostEvidence.unknown(run_id="run-1", effect_id="effect-1")
    assert evidence.status is CostStatus.UNKNOWN
    assert evidence.amount_micro_cny is None
    assert evidence.is_free is False


def test_t039_money_uses_integer_micro_cny_without_float_rounding() -> None:
    assert micro_cny(Decimal("0.000001")) == 1
    assert micro_cny(Decimal("1.234567")) == 1_234_567
    with pytest.raises(ValueError):
        micro_cny(Decimal("0.0000001"))


def test_t040_t041_duplicate_and_parent_child_evidence_are_not_double_counted(
    tmp_path,
) -> None:
    ledger = CostEvidenceLedger(tmp_path)
    parent = CostEvidence.exact(
        run_id="run-1",
        effect_id="parent",
        trace_id="trace-1",
        amount_micro_cny=30,
    )
    child = CostEvidence.exact(
        run_id="run-1",
        effect_id="child",
        parent_effect_id="parent",
        trace_id="trace-1",
        amount_micro_cny=30,
    )

    assert ledger.record(parent).inserted is True
    assert ledger.record(parent).inserted is False
    assert ledger.record(child).inserted is True
    assert ledger.total_for_run("run-1") == 30


def test_t042_usage_without_a_trace_is_unassigned(tmp_path) -> None:
    ledger = CostEvidenceLedger(tmp_path)
    item = CostEvidence.exact(
        run_id=None,
        effect_id=None,
        trace_id=None,
        amount_micro_cny=99,
        upstream_record_id="usage-9",
    )

    ledger.record(item)

    assert ledger.total_for_run("run-1") == 0
    assert ledger.unassigned_total() == 99


def test_t043_reservation_is_atomic(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path, limit_micro_cny=100)

    def reserve(key: str) -> bool:
        return ledger.reserve(
            reservation_id=key,
            run_id=key,
            amount_micro_cny=60,
            mode=BudgetMode.ESTIMATE_GUARD,
            upper_bound_proven=False,
        ).accepted

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(reserve, ["a", "b"]))

    assert accepted.count(True) == 1
    assert ledger.reserved_micro_cny == 60


def test_t044_ambiguous_result_retains_reservation(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path, limit_micro_cny=100)
    ledger.reserve(
        reservation_id="r1",
        run_id="run-1",
        amount_micro_cny=40,
        mode=BudgetMode.ESTIMATE_GUARD,
        upper_bound_proven=False,
    )

    ledger.mark_ambiguous("r1")

    assert ledger.reserved_micro_cny == 40
    assert ledger.get("r1").status == "ambiguous"


def test_t045_hard_budget_requires_provable_upstream_bound(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path, limit_micro_cny=100)
    with pytest.raises(ValueError, match="upper bound"):
        ledger.reserve(
            reservation_id="hard",
            run_id="run-1",
            amount_micro_cny=40,
            mode=BudgetMode.VERIFIED_UPPER_BOUND,
            upper_bound_proven=False,
        )


def test_t047_overrun_is_disputed_instead_of_rewriting_history(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path, limit_micro_cny=100)
    ledger.reserve(
        reservation_id="r1",
        run_id="run-1",
        amount_micro_cny=40,
        mode=BudgetMode.ESTIMATE_GUARD,
        upper_bound_proven=False,
    )

    entry = ledger.settle("r1", actual_micro_cny=75)

    assert entry.status == "disputed_overrun"
    assert entry.reserved_micro_cny == 40
    assert entry.actual_micro_cny == 75


def test_settled_cost_remains_committed_against_the_task_budget(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path, limit_micro_cny=100, scope_run_id="run-1")
    ledger.reserve(
        reservation_id="attempt-1:dispatch-1",
        run_id="run-1",
        amount_micro_cny=60,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=True,
    )

    ledger.settle("attempt-1:dispatch-1", actual_micro_cny=40)
    rejected = ledger.reserve(
        reservation_id="attempt-2:dispatch-2",
        run_id="run-1",
        amount_micro_cny=70,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=True,
    )

    assert rejected.accepted is False
    assert ledger.reserved_micro_cny == 0
    assert ledger.committed_micro_cny == 40
    assert ledger.consumed_micro_cny == 40


def test_task_budget_is_shared_across_attempts_but_not_unrelated_runs(tmp_path) -> None:
    unrelated = BudgetLedger(tmp_path, limit_micro_cny=100, scope_run_id="run-2")
    unrelated.reserve(
        reservation_id="run-2:attempt-1:dispatch-1",
        run_id="run-2",
        amount_micro_cny=90,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=True,
    )
    ledger = BudgetLedger(tmp_path, limit_micro_cny=100, scope_run_id="run-1")

    first = ledger.reserve(
        reservation_id="run-1:attempt-1:dispatch-1",
        run_id="run-1",
        amount_micro_cny=60,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=True,
    )
    second = ledger.reserve(
        reservation_id="run-1:attempt-2:dispatch-2",
        run_id="run-1",
        amount_micro_cny=50,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=True,
    )

    assert first.accepted is True
    assert second.accepted is False
    assert ledger.consumed_micro_cny == 60
    with pytest.raises(ValueError, match="scope"):
        ledger.reserve(
            reservation_id="wrong-run",
            run_id="run-2",
            amount_micro_cny=1,
            mode=BudgetMode.VERIFIED_UPPER_BOUND,
            upper_bound_proven=True,
        )


def test_ambiguous_and_overrun_costs_remain_conservatively_consumed(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path, limit_micro_cny=200, scope_run_id="run-1")
    ledger.reserve(
        reservation_id="ambiguous",
        run_id="run-1",
        amount_micro_cny=40,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=True,
    )
    ledger.mark_ambiguous("ambiguous")
    ledger.reserve(
        reservation_id="overrun",
        run_id="run-1",
        amount_micro_cny=50,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=True,
    )
    ledger.settle("overrun", actual_micro_cny=70)

    assert ledger.reserved_micro_cny == 40
    assert ledger.committed_micro_cny == 70
    assert ledger.consumed_micro_cny == 110


def test_t046_recharge_does_not_expand_the_task_budget(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path, limit_micro_cny=100)
    ledger.reserve(
        reservation_id="r1",
        run_id="run-1",
        amount_micro_cny=40,
        mode=BudgetMode.ESTIMATE_GUARD,
        upper_bound_proven=False,
    )

    decision = decide_balance_recovery(
        BalanceRecoveryFacts(
            requested_run_id="run-1",
            paused_run_id="run-1",
            task_status="paused_insufficient_balance",
            user_confirmed=True,
            binding_version_matches=True,
            budget_revision_matches=True,
            effect_reconciled=True,
        )
    )

    assert decision.may_resume is True
    assert decision.budget_delta_micro_cny == 0
    assert ledger.limit_micro_cny == 100
    assert ledger.reserved_micro_cny == 40


@pytest.mark.parametrize("status", ["cancelled", "succeeded", "failed"])
def test_t056_balance_change_cannot_wake_terminal_or_cancelled_task(status) -> None:
    decision = decide_balance_recovery(
        BalanceRecoveryFacts(
            requested_run_id="run-1",
            paused_run_id="run-1",
            task_status=status,
            user_confirmed=True,
            binding_version_matches=True,
            budget_revision_matches=True,
            effect_reconciled=True,
        )
    )

    assert decision.may_resume is False
    assert decision.resume_run_id is None


def test_t057_balance_change_resumes_only_the_explicit_reconciled_task() -> None:
    wrong_run = decide_balance_recovery(
        BalanceRecoveryFacts(
            requested_run_id="run-2",
            paused_run_id="run-1",
            task_status="paused_insufficient_balance",
            user_confirmed=True,
            binding_version_matches=True,
            budget_revision_matches=True,
            effect_reconciled=True,
        )
    )
    unconfirmed = decide_balance_recovery(
        BalanceRecoveryFacts(
            requested_run_id="run-1",
            paused_run_id="run-1",
            task_status="paused_insufficient_balance",
            user_confirmed=False,
            binding_version_matches=True,
            budget_revision_matches=True,
            effect_reconciled=True,
        )
    )

    assert wrong_run.may_resume is False
    assert unconfirmed.may_resume is False


def test_model_usage_cost_is_bound_to_run_effect_and_upstream_record(tmp_path) -> None:
    request = ModelRequest(
        provider_id="tokendance",
        metadata={
            "state_workspace_dir": str(tmp_path),
            "run_id": "run-usage",
            "model_request_id": "effect-model-1",
        },
    )

    evidence = record_model_usage_cost(
        provider_id="tokendance",
        request=request,
        usage=ModelUsage(
            cost_cny=0.012345,
            raw={"billing_record_id": "billing-1"},
        ),
    )

    assert evidence is not None
    assert evidence.status is CostStatus.EXACT
    assert evidence.amount_micro_cny == 12_345
    assert evidence.run_id == "run-usage"
    assert evidence.effect_id == "effect-model-1"
    assert evidence.upstream_record_id == "billing-1"
    assert CostEvidenceLedger(tmp_path).total_for_run("run-usage") == 12_345


def test_model_usage_without_verified_price_record_remains_unknown(tmp_path) -> None:
    request = ModelRequest(
        provider_id="tokendance",
        metadata={
            "state_workspace_dir": str(tmp_path),
            "run_id": "run-unknown",
            "model_request_id": "effect-model-unknown",
        },
    )

    evidence = record_model_usage_cost(
        provider_id="tokendance",
        request=request,
        usage=ModelUsage(cost_cny=0.5, raw={}),
    )

    assert evidence is not None
    assert evidence.status is CostStatus.UNKNOWN
    assert evidence.is_free is False
    assert CostEvidenceLedger(tmp_path).total_for_run("run-unknown") == 0


def test_omitted_model_cost_remains_unknown_and_is_not_free(
    tmp_path,
) -> None:
    request = ModelRequest(
        provider_id="tokendance",
        metadata={
            "state_workspace_dir": str(tmp_path),
            "run_id": "run-omitted-cost",
            "model_request_id": "effect-model-omitted-cost",
        },
    )
    usage = ModelUsage(raw={"billing_record_id": "billing-without-price"})

    assert usage.cost_cny is None

    evidence = record_model_usage_cost(
        provider_id="tokendance",
        request=request,
        usage=usage,
    )

    assert evidence is not None
    assert evidence.status is CostStatus.UNKNOWN
    assert evidence.is_free is False


def test_missing_model_usage_remains_unknown_and_is_not_free(tmp_path) -> None:
    request = ModelRequest(
        provider_id="tokendance",
        metadata={
            "state_workspace_dir": str(tmp_path),
            "run_id": "run-missing-usage",
            "model_request_id": "effect-model-missing-usage",
        },
    )

    evidence = record_model_usage_cost(
        provider_id="tokendance",
        request=request,
        usage=None,
    )

    assert evidence is not None
    assert evidence.status is CostStatus.UNKNOWN
    assert evidence.is_free is False


@pytest.mark.asyncio
async def test_model_dispatch_claims_verified_bound_before_provider_call(
    tmp_path,
) -> None:
    await _store_budgeted_autonomy_run(tmp_path)

    reservation = await reserve_model_dispatch_budget(
        request=_budgeted_request(tmp_path),
        provider_id="provider-a",
        model_id="model-a",
        now=NOW + timedelta(minutes=2),
    )

    assert reservation is not None
    assert reservation.amount_micro_cny == 6_000
    ledger = BudgetLedger(
        tmp_path,
        limit_micro_cny=10_000,
        scope_run_id="run-budget",
    )
    assert ledger.get("dispatch-1").status == "dispatched"
    assert ledger.consumed_micro_cny == 6_000
    with pytest.raises(RuntimeBudgetAdmissionError) as exc_info:
        await reserve_model_dispatch_budget(
            request=_budgeted_request(tmp_path),
            provider_id="provider-a",
            model_id="model-a",
            now=NOW + timedelta(minutes=2),
        )
    assert exc_info.value.code == "runtime_budget_dispatch_already_claimed"


@pytest.mark.asyncio
async def test_model_dispatch_fails_closed_before_call_when_budget_is_exhausted(
    tmp_path,
) -> None:
    await _store_budgeted_autonomy_run(tmp_path, limit_micro_cny=5_999)

    with pytest.raises(RuntimeBudgetAdmissionError) as exc_info:
        await reserve_model_dispatch_budget(
            request=_budgeted_request(tmp_path),
            provider_id="provider-a",
            model_id="model-a",
            now=NOW + timedelta(minutes=2),
        )

    assert exc_info.value.code == "runtime_budget_exhausted"
    assert BudgetLedger(
        tmp_path,
        limit_micro_cny=5_999,
        scope_run_id="run-budget",
    ).get("dispatch-1").accepted is False


@pytest.mark.asyncio
async def test_model_dispatch_rechecks_revocation_and_settles_exact_evidence(
    tmp_path,
) -> None:
    await _store_budgeted_autonomy_run(tmp_path, revoked=True)
    with pytest.raises(RuntimeBudgetAdmissionError) as exc_info:
        await reserve_model_dispatch_budget(
            request=_budgeted_request(tmp_path),
            provider_id="provider-a",
            model_id="model-a",
            now=NOW + timedelta(minutes=2),
        )
    assert exc_info.value.code == "autonomy_grant_revoked"

    fresh_root = tmp_path / "fresh"
    fresh_root.mkdir()
    await _store_budgeted_autonomy_run(fresh_root)
    reservation = await reserve_model_dispatch_budget(
        request=_budgeted_request(fresh_root),
        provider_id="provider-a",
        model_id="model-a",
        now=NOW + timedelta(minutes=2),
    )
    assert reservation is not None
    evidence = CostEvidence.exact(
        run_id="run-budget",
        effect_id="dispatch-1",
        trace_id="trace-1",
        amount_micro_cny=1_250,
    )

    finalized = finalize_model_dispatch_budget(reservation, evidence=evidence)

    assert finalized.status == "settled"
    assert finalized.actual_micro_cny == 1_250
    assert BudgetLedger(
        fresh_root,
        limit_micro_cny=10_000,
        scope_run_id="run-budget",
    ).consumed_micro_cny == 1_250
