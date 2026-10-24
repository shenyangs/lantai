# -*- coding: utf-8 -*-
"""Auditable task cost evidence and conservative local budget reservations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterator, Mapping

from ..utils.file_lock import locked_file_descriptor

from .models import ModelRequest, ModelUsage
from .runs import evaluate_runtime_autonomy
from .store import RuntimeRunStore


class CostStatus(str, Enum):
    EXACT = "exact"
    ESTIMATE = "estimate"
    PENDING = "pending"
    UNKNOWN = "unknown"


class BudgetMode(str, Enum):
    VERIFIED_UPPER_BOUND = "verified_upper_bound"
    ESTIMATE_GUARD = "estimate_guard"


@dataclass(frozen=True, slots=True)
class BalanceRecoveryFacts:
    (
        "Facts required before one paused run may continue after "
        "a balance change.\n\n    A TokenDance account balance is"
        " external billing state. It never mutates a\n    Lantai "
        "task budget and it is not, by itself, authority to wake "
        "a run.\n    "
    )

    requested_run_id: str
    paused_run_id: str
    task_status: str
    user_confirmed: bool
    binding_version_matches: bool
    budget_revision_matches: bool
    effect_reconciled: bool


@dataclass(frozen=True, slots=True)
class BalanceRecoveryDecision:
    may_resume: bool
    resume_run_id: str | None
    reason: str
    budget_delta_micro_cny: int = 0


# pylint: disable=R0911
def decide_balance_recovery(
    facts: BalanceRecoveryFacts,
) -> BalanceRecoveryDecision:
    """Fail closed unless the user selected the exact, still-paused run."""

    if facts.task_status != "paused_insufficient_balance":
        return BalanceRecoveryDecision(False, None, "task_not_balance_paused")
    if facts.requested_run_id != facts.paused_run_id:
        return BalanceRecoveryDecision(False, None, "run_mismatch")
    if not facts.user_confirmed:
        return BalanceRecoveryDecision(
            False,
            None,
            "user_confirmation_required",
        )
    if not facts.binding_version_matches:
        return BalanceRecoveryDecision(False, None, "binding_changed")
    if not facts.budget_revision_matches:
        return BalanceRecoveryDecision(False, None, "budget_revision_changed")
    if not facts.effect_reconciled:
        return BalanceRecoveryDecision(
            False,
            None,
            "effect_reconciliation_required",
        )
    return BalanceRecoveryDecision(
        True,
        facts.paused_run_id,
        "resume_exact_run",
    )


# pylint: enable=R0911


def micro_cny(value: Decimal | str | int) -> int:
    amount = Decimal(value)
    scaled = amount * Decimal(1_000_000)
    if scaled != scaled.to_integral_value():
        raise ValueError("CNY amount has more than six decimal places")
    result = int(scaled)
    if result < 0:
        raise ValueError("cost cannot be negative")
    return result


@dataclass(frozen=True, slots=True)
class CostEvidence:
    evidence_id: str
    status: CostStatus
    amount_micro_cny: int | None
    run_id: str | None
    effect_id: str | None
    trace_id: str | None = None
    upstream_record_id: str | None = None
    parent_effect_id: str | None = None
    currency: str = "CNY"
    source_unit: str = "micro_cny"

    @property
    def is_free(self) -> bool:
        return self.status is CostStatus.EXACT and self.amount_micro_cny == 0

    @staticmethod
    def _id(parts: tuple[object, ...]) -> str:
        return hashlib.sha256(
            json.dumps(parts, separators=(",", ":"), default=str).encode(),
        ).hexdigest()

    @classmethod
    def unknown(cls, *, run_id: str, effect_id: str) -> "CostEvidence":
        return cls(
            evidence_id=cls._id(("unknown", run_id, effect_id)),
            status=CostStatus.UNKNOWN,
            amount_micro_cny=None,
            run_id=run_id,
            effect_id=effect_id,
        )

    @classmethod
    def exact(
        cls,
        *,
        run_id: str | None,
        effect_id: str | None,
        trace_id: str | None,
        amount_micro_cny: int,
        upstream_record_id: str | None = None,
        parent_effect_id: str | None = None,
    ) -> "CostEvidence":
        if amount_micro_cny < 0:
            raise ValueError("cost cannot be negative")
        identity = upstream_record_id or trace_id or effect_id
        if not identity:
            raise ValueError("cost evidence requires a stable source identity")
        return cls(
            evidence_id=cls._id(
                (
                    "exact",
                    identity,
                    run_id,
                    effect_id,
                    parent_effect_id,
                    amount_micro_cny,
                ),
            ),
            status=CostStatus.EXACT,
            amount_micro_cny=amount_micro_cny,
            run_id=run_id,
            effect_id=effect_id,
            trace_id=trace_id,
            upstream_record_id=upstream_record_id,
            parent_effect_id=parent_effect_id,
        )


@dataclass(frozen=True, slots=True)
class CostRecordResult:
    inserted: bool
    evidence: CostEvidence


_PROCESS_LOCK = threading.RLock()


class _JsonStore:
    def __init__(self, root: str | Path, name: str) -> None:
        self.root = Path(root) / ".lantai" / "runtime" / "v1"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / name
        self.lock_path = self.root / f"{name}.lock"

    @contextmanager
    def locked(self) -> Iterator[None]:
        with _PROCESS_LOCK, self.lock_path.open("a+b") as lock:
            with locked_file_descriptor(lock.fileno()):
                yield

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"invalid ledger payload: {self.path}")
        return payload

    def write(self, rows: list[dict]) -> None:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.root,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class CostEvidenceLedger:
    def __init__(self, root: str | Path) -> None:
        self._store = _JsonStore(root, "cost-evidence.json")

    @staticmethod
    def _decode(row: dict) -> CostEvidence:
        return CostEvidence(**{**row, "status": CostStatus(row["status"])})

    def record(self, evidence: CostEvidence) -> CostRecordResult:
        with self._store.locked():
            rows = self._store.read()
            if any(row["evidence_id"] == evidence.evidence_id for row in rows):
                return CostRecordResult(False, evidence)
            row = asdict(evidence)
            row["status"] = evidence.status.value
            rows.append(row)
            self._store.write(rows)
        return CostRecordResult(True, evidence)

    def _items(self) -> list[CostEvidence]:
        with self._store.locked():
            return [self._decode(row) for row in self._store.read()]

    @staticmethod
    def _sum(items: list[CostEvidence]) -> int:
        total = 0
        seen_billing: set[str] = set()
        for item in items:
            if (
                item.status is not CostStatus.EXACT
                or item.amount_micro_cny is None
            ):
                continue
            billing_key = (
                item.upstream_record_id or item.trace_id or item.evidence_id
            )
            if billing_key in seen_billing:
                continue
            seen_billing.add(billing_key)
            total += item.amount_micro_cny
        return total

    def total_for_run(self, run_id: str) -> int:
        return self._sum(
            [item for item in self._items() if item.run_id == run_id],
        )

    def unassigned_total(self) -> int:
        return self._sum(
            [item for item in self._items() if item.run_id is None],
        )


def _nonempty_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def record_model_usage_cost(
    *,
    provider_id: str,
    request: ModelRequest,
    usage: ModelUsage | Mapping[str, Any] | None,
) -> CostEvidence | None:
    """Persist provider cost evidence at the model-response boundary.

    A numeric amount is exact only when the provider also returned a stable
    billing or trace identity.  Otherwise the linked task/effect remains
    explicitly unknown rather than being interpreted as free.
    """

    if not str(provider_id).strip():
        return None
    metadata = request.metadata
    workspace = _nonempty_text(metadata.get("state_workspace_dir"))
    run_id = _nonempty_text(metadata.get("run_id"))
    effect_id = _nonempty_text(
        metadata.get("runtime_model_dispatch_id")
        or metadata.get("model_request_id")
        or metadata.get("model_round_lineage_sha256"),
    )
    if workspace is None or run_id is None or effect_id is None:
        return None

    if usage is None:
        cost_cny = None
        raw: Mapping[str, Any] = {}
    elif isinstance(usage, ModelUsage):
        cost_cny = (
            usage.cost_cny if "cost_cny" in usage.model_fields_set else None
        )
        raw = usage.raw
    else:
        cost_cny = usage.get("cost_cny")
        candidate_raw = usage.get("raw")
        raw = candidate_raw if isinstance(candidate_raw, Mapping) else usage

    trace_id = _nonempty_text(raw.get("trace_id"))
    upstream_record_id = next(
        (
            identity
            for key in ("billing_record_id", "usage_id", "request_id")
            if (identity := _nonempty_text(raw.get(key))) is not None
        ),
        None,
    )
    evidence: CostEvidence
    if (
        cost_cny is not None
        and not isinstance(cost_cny, bool)
        and (upstream_record_id is not None or trace_id is not None)
    ):
        try:
            amount_micro_cny = micro_cny(Decimal(str(cost_cny)))
        except (ArithmeticError, ValueError):
            evidence = CostEvidence.unknown(run_id=run_id, effect_id=effect_id)
        else:
            evidence = CostEvidence.exact(
                run_id=run_id,
                effect_id=effect_id,
                trace_id=trace_id,
                upstream_record_id=upstream_record_id,
                amount_micro_cny=amount_micro_cny,
            )
    else:
        evidence = CostEvidence.unknown(run_id=run_id, effect_id=effect_id)

    CostEvidenceLedger(workspace).record(evidence)
    return evidence


@dataclass(frozen=True, slots=True)
class BudgetEntry:
    reservation_id: str
    run_id: str
    reserved_micro_cny: int
    mode: BudgetMode
    upper_bound_proven: bool
    status: str
    accepted: bool
    actual_micro_cny: int | None = None


class BudgetLedger:
    def __init__(
        self,
        root: str | Path,
        *,
        limit_micro_cny: int,
        scope_run_id: str | None = None,
    ) -> None:
        if limit_micro_cny < 0:
            raise ValueError("budget limit cannot be negative")
        self.limit_micro_cny = limit_micro_cny
        self.scope_run_id = (
            str(scope_run_id).strip() if scope_run_id is not None else None
        )
        if scope_run_id is not None and not self.scope_run_id:
            raise ValueError("budget run scope cannot be empty")
        self._store = _JsonStore(root, "budget-reservations.json")

    @staticmethod
    def _decode(row: dict) -> BudgetEntry:
        return BudgetEntry(**{**row, "mode": BudgetMode(row["mode"])})

    @staticmethod
    def _encode(entry: BudgetEntry) -> dict:
        row = asdict(entry)
        row["mode"] = entry.mode.value
        return row

    def _in_scope(self, row: Mapping[str, Any]) -> bool:
        return (
            self.scope_run_id is None or row.get("run_id") == self.scope_run_id
        )

    @staticmethod
    def _pending_amount(row: Mapping[str, Any]) -> int:
        if not bool(row.get("accepted")):
            return 0
        if row.get("status") not in {"reserved", "dispatched", "ambiguous"}:
            return 0
        return int(row["reserved_micro_cny"])

    @staticmethod
    def _committed_amount(row: Mapping[str, Any]) -> int:
        if not bool(row.get("accepted")):
            return 0
        if row.get("status") not in {"settled", "disputed_overrun"}:
            return 0
        actual = row.get("actual_micro_cny")
        return int(row["reserved_micro_cny"] if actual is None else actual)

    def _consumed_amount(self, rows: list[dict]) -> int:
        return sum(
            self._pending_amount(row) + self._committed_amount(row)
            for row in rows
            if self._in_scope(row)
        )

    def reserve(
        self,
        *,
        reservation_id: str,
        run_id: str,
        amount_micro_cny: int,
        mode: BudgetMode,
        upper_bound_proven: bool,
    ) -> BudgetEntry:
        if amount_micro_cny < 0:
            raise ValueError("reservation cannot be negative")
        if self.scope_run_id is not None and run_id != self.scope_run_id:
            raise ValueError("reservation run is outside this budget scope")
        if mode is BudgetMode.VERIFIED_UPPER_BOUND and not upper_bound_proven:
            raise ValueError(
                "verified hard budget requires a provable upper bound",
            )
        with self._store.locked():
            rows = self._store.read()
            for row in rows:
                if row["reservation_id"] == reservation_id:
                    current = self._decode(row)
                    if (
                        current.run_id != run_id
                        or current.reserved_micro_cny != amount_micro_cny
                    ):
                        raise ValueError(
                            "reservation id was reused with new facts",
                        )
                    return current
            consumed = self._consumed_amount(rows)
            accepted = consumed + amount_micro_cny <= self.limit_micro_cny
            entry = BudgetEntry(
                reservation_id=reservation_id,
                run_id=run_id,
                reserved_micro_cny=amount_micro_cny,
                mode=mode,
                upper_bound_proven=upper_bound_proven,
                status="reserved" if accepted else "rejected",
                accepted=accepted,
            )
            rows.append(self._encode(entry))
            self._store.write(rows)
            return entry

    def _update(self, reservation_id: str, **changes: object) -> BudgetEntry:
        with self._store.locked():
            rows = self._store.read()
            for index, row in enumerate(rows):
                if row["reservation_id"] != reservation_id:
                    continue
                if not self._in_scope(row):
                    raise KeyError(reservation_id)
                current = self._decode(row)
                updated = BudgetEntry(
                    **{
                        **asdict(current),
                        "mode": current.mode,
                        **changes,
                    },
                )
                rows[index] = self._encode(updated)
                self._store.write(rows)
                return updated
        raise KeyError(reservation_id)

    def mark_ambiguous(self, reservation_id: str) -> BudgetEntry:
        return self._update(reservation_id, status="ambiguous")

    def claim_dispatch(self, reservation_id: str) -> BudgetEntry:
        """Atomically consume a fresh reservation for exactly one dispatch."""

        with self._store.locked():
            rows = self._store.read()
            for index, row in enumerate(rows):
                if row["reservation_id"] != reservation_id:
                    continue
                if not self._in_scope(row):
                    raise KeyError(reservation_id)
                current = self._decode(row)
                if not current.accepted or current.status != "reserved":
                    raise ValueError(
                        "reservation is not fresh and dispatchable",
                    )
                updated = BudgetEntry(
                    **{
                        **asdict(current),
                        "mode": current.mode,
                        "status": "dispatched",
                    },
                )
                rows[index] = self._encode(updated)
                self._store.write(rows)
                return updated
        raise KeyError(reservation_id)

    def settle(
        self,
        reservation_id: str,
        *,
        actual_micro_cny: int,
    ) -> BudgetEntry:
        if actual_micro_cny < 0:
            raise ValueError("actual cost cannot be negative")
        current = self.get(reservation_id)
        status = (
            "disputed_overrun"
            if actual_micro_cny > current.reserved_micro_cny
            else "settled"
        )
        return self._update(
            reservation_id,
            status=status,
            actual_micro_cny=actual_micro_cny,
        )

    def get(self, reservation_id: str) -> BudgetEntry:
        with self._store.locked():
            for row in self._store.read():
                if row["reservation_id"] == reservation_id and self._in_scope(
                    row,
                ):
                    return self._decode(row)
        raise KeyError(reservation_id)

    def entries(self) -> tuple[BudgetEntry, ...]:
        """Return an immutable task-scoped ledger snapshot for reporting."""

        with self._store.locked():
            return tuple(
                self._decode(row)
                for row in self._store.read()
                if self._in_scope(row)
            )

    @property
    def reserved_micro_cny(self) -> int:
        with self._store.locked():
            return sum(
                self._pending_amount(row)
                for row in self._store.read()
                if self._in_scope(row)
            )

    @property
    def committed_micro_cny(self) -> int:
        with self._store.locked():
            return sum(
                self._committed_amount(row)
                for row in self._store.read()
                if self._in_scope(row)
            )

    @property
    def consumed_micro_cny(self) -> int:
        with self._store.locked():
            return self._consumed_amount(self._store.read())


class RuntimeBudgetAdmissionError(RuntimeError):
    """Fail-closed rejection before an unattended billable dispatch."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
        reservation_id: str | None = None,
    ) -> None:
        self.code = code
        self.run_id = run_id
        self.reservation_id = reservation_id
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ModelBudgetReservation:
    workspace_dir: str
    run_id: str
    reservation_id: str
    limit_micro_cny: int
    amount_micro_cny: int
    pricing_version: str


async def reserve_model_dispatch_budget(
    *,
    request: ModelRequest,
    provider_id: str,
    model_id: str,
    now: datetime | None = None,
) -> ModelBudgetReservation | None:
    """Recheck current authority and claim budget immediately before dispatch.

    Ordinary requests without a persisted autonomy grant remain unaffected.
    Once a task has such a grant, missing identity, policy drift, revocation,
    and unavailable budget all fail closed before the provider is called.
    """

    metadata = request.metadata
    workspace = _nonempty_text(metadata.get("state_workspace_dir"))
    run_id = _nonempty_text(metadata.get("run_id"))
    if workspace is None or run_id is None:
        return None
    workspace_path = Path(workspace).expanduser().resolve(strict=False)
    run = await RuntimeRunStore(workspace_path).load_contract(run_id)
    if run is None or run.task_contract is None:
        return None
    grant = run.task_contract.autonomy_grant
    if grant is None:
        return None

    provider = str(provider_id or "").strip()
    model = str(model_id or "").strip()
    decision = evaluate_runtime_autonomy(
        run,
        operation="model_call",
        chat_id=grant.chat_id,
        workspace_id=str(workspace_path),
        provider_id=provider,
        model_id=model,
        now=now,
    )
    if not decision.allowed:
        raise RuntimeBudgetAdmissionError(
            decision.code,
            decision.message,
            run_id=run_id,
        )

    dispatch_id = _nonempty_text(metadata.get("runtime_model_dispatch_id"))
    if dispatch_id is None:
        raise RuntimeBudgetAdmissionError(
            "runtime_budget_dispatch_identity_missing",
            (
                "Billable unattended dispatch requires a durable dispatch"
                " identity."
            ),
            run_id=run_id,
        )
    price_bound = next(
        (
            item
            for item in grant.model_price_bounds
            if item.provider_id == provider and item.model_id == model
        ),
        None,
    )
    if price_bound is None or not price_bound.upper_bound_proven:
        raise RuntimeBudgetAdmissionError(
            "autonomy_model_price_bound_missing",
            (
                "The selected provider/model has no verified dispatch pri"
                "ce bound."
            ),
            run_id=run_id,
            reservation_id=dispatch_id,
        )
    if grant.max_cost_micro_cny is None:
        raise RuntimeBudgetAdmissionError(
            "autonomy_budget_missing",
            "Billable unattended work requires an exact task budget.",
            run_id=run_id,
            reservation_id=dispatch_id,
        )

    ledger = BudgetLedger(
        workspace_path,
        limit_micro_cny=grant.max_cost_micro_cny,
        scope_run_id=run_id,
    )
    entry = ledger.reserve(
        reservation_id=dispatch_id,
        run_id=run_id,
        amount_micro_cny=price_bound.max_dispatch_micro_cny,
        mode=BudgetMode.VERIFIED_UPPER_BOUND,
        upper_bound_proven=price_bound.upper_bound_proven,
    )
    if not entry.accepted:
        raise RuntimeBudgetAdmissionError(
            "runtime_budget_exhausted",
            (
                "The next verified dispatch bound exceeds the remaining t"
                "ask budget."
            ),
            run_id=run_id,
            reservation_id=dispatch_id,
        )
    try:
        ledger.claim_dispatch(dispatch_id)
    except ValueError as exc:
        raise RuntimeBudgetAdmissionError(
            "runtime_budget_dispatch_already_claimed",
            (
                "This dispatch identity was already consumed or remains a"
                "mbiguous."
            ),
            run_id=run_id,
            reservation_id=dispatch_id,
        ) from exc
    return ModelBudgetReservation(
        workspace_dir=str(workspace_path),
        run_id=run_id,
        reservation_id=dispatch_id,
        limit_micro_cny=grant.max_cost_micro_cny,
        amount_micro_cny=price_bound.max_dispatch_micro_cny,
        pricing_version=price_bound.pricing_version,
    )


def mark_model_dispatch_budget_ambiguous(
    reservation: ModelBudgetReservation | None,
) -> BudgetEntry | None:
    if reservation is None:
        return None
    return BudgetLedger(
        reservation.workspace_dir,
        limit_micro_cny=reservation.limit_micro_cny,
        scope_run_id=reservation.run_id,
    ).mark_ambiguous(reservation.reservation_id)


def finalize_model_dispatch_budget(
    reservation: ModelBudgetReservation | None,
    *,
    evidence: CostEvidence | None,
) -> BudgetEntry | None:
    (
        "Settle exact evidence; retain the full bound for every u"
        "nknown result."
    )

    if reservation is None:
        return None
    ledger = BudgetLedger(
        reservation.workspace_dir,
        limit_micro_cny=reservation.limit_micro_cny,
        scope_run_id=reservation.run_id,
    )
    if (
        evidence is not None
        and evidence.status is CostStatus.EXACT
        and evidence.amount_micro_cny is not None
        and evidence.run_id == reservation.run_id
        and evidence.effect_id == reservation.reservation_id
    ):
        return ledger.settle(
            reservation.reservation_id,
            actual_micro_cny=evidence.amount_micro_cny,
        )
    return ledger.mark_ambiguous(reservation.reservation_id)


__all__ = [
    "BalanceRecoveryDecision",
    "BalanceRecoveryFacts",
    "BudgetEntry",
    "BudgetLedger",
    "BudgetMode",
    "CostEvidence",
    "CostEvidenceLedger",
    "CostStatus",
    "ModelBudgetReservation",
    "RuntimeBudgetAdmissionError",
    "decide_balance_recovery",
    "finalize_model_dispatch_budget",
    "mark_model_dispatch_budget_ambiguous",
    "micro_cny",
    "record_model_usage_cost",
    "reserve_model_dispatch_budget",
]
