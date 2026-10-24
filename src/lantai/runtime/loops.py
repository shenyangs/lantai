# -*- coding: utf-8 -*-
"""Versioned long-running Loop contracts.

The structured contract is the authority. A ``contract.md`` file should be
treated only as a readable projection of this model.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


RUNTIME_LOOP_CONTRACT_SCHEMA_VERSION = 1
RUNTIME_LOOP_EVALUATION_SCHEMA_VERSION = 1
RUNTIME_LOOP_BUDGET_EVENT_SCHEMA_VERSION = 1

LoopInputKind = Literal["file", "url", "text", "runtime_run", "artifact", "manual"]
LoopGateKind = Literal[
    "command",
    "test",
    "http",
    "artifact_exists",
    "metric_threshold",
    "manual_check",
]
LoopPolicyMode = Literal["enabled", "read_only", "interactive_only", "disabled"]
LoopReleaseEditionScope = Literal["official", "mas", "both"]
LoopGateResultStatus = Literal["passed", "failed", "skipped"]
LoopEvaluationConclusion = Literal["pass", "needs_revision", "blocked", "waiting_human"]
LoopDeterministicGateOutcome = Literal["passed", "blocked"]
LoopHumanGateDecision = Literal["not_required", "pending", "approved", "rejected"]
LoopStopReasonCategory = Literal[
    "budget",
    "lease",
    "watchdog",
    "progress",
    "rubric",
    "human",
    "gate",
]
LoopStopReasonCode = Literal[
    "attempt_budget_exhausted",
    "step_budget_exhausted",
    "tool_call_budget_exhausted",
    "token_budget_exhausted",
    "cost_budget_exhausted",
    "wall_time_budget_exhausted",
    "artifact_budget_exhausted",
    "lease_expired",
    "heartbeat_stale",
    "watchdog_deadline_expired",
    "consecutive_failures_exhausted",
    "consecutive_same_failures_exhausted",
    "no_progress_exhausted",
    "rubric_target_met",
    "human_stop_requested",
    "deterministic_gate_blocked",
]
LoopBudgetChangeActor = Literal["user", "system", "policy", "runtime"]

_STRICT_CONFIG = ConfigDict(extra="forbid", frozen=True)


def _non_empty_text(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    return text


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _hash_payload(value: object) -> str:
    return f"sha256:{hashlib.sha256(_stable_json(value).encode('utf-8')).hexdigest()}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class _StrictLoopModel(BaseModel):
    model_config = _STRICT_CONFIG


class LoopInput(_StrictLoopModel):
    input_id: str
    kind: LoopInputKind
    title: str
    uri: str | None = None
    evidence_hash: str | None = None

    @field_validator("input_id", "title")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)


class LoopDeliverable(_StrictLoopModel):
    deliverable_id: str
    title: str
    acceptance: list[str] = Field(min_length=1)
    artifact_hint: str | None = None

    @field_validator("deliverable_id", "title")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("acceptance")
    @classmethod
    def validate_acceptance(cls, values: list[str]) -> list[str]:
        return [_non_empty_text(value, "acceptance") for value in values]


class LoopDeterministicGate(_StrictLoopModel):
    gate_id: str
    name: str
    kind: LoopGateKind
    command: str | None = None
    expected: str | None = None
    required: bool = True
    timeout_seconds: int | None = Field(default=None, gt=0)

    @field_validator("gate_id", "name")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)

    @model_validator(mode="after")
    def validate_gate_payload(self) -> "LoopDeterministicGate":
        if self.kind in {"command", "test"} and not (self.command or "").strip():
            raise ValueError("command/test gates require command")
        return self


class LoopRubricDimension(_StrictLoopModel):
    dimension_id: str
    name: str
    weight: float = Field(gt=0)
    target_score: float = Field(default=4.0, ge=0, le=5)
    description: str = ""

    @field_validator("dimension_id", "name")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)


class LoopRubric(_StrictLoopModel):
    rubric_id: str
    version: str = "1"
    passing_score: float = Field(default=4.0, ge=0, le=5)
    dimensions: list[LoopRubricDimension] = Field(min_length=1)

    @field_validator("rubric_id", "version")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)

    @model_validator(mode="after")
    def validate_unique_dimensions(self) -> "LoopRubric":
        ids = [item.dimension_id for item in self.dimensions]
        if len(ids) != len(set(ids)):
            raise ValueError("rubric dimension ids must be unique")
        return self


class LoopEditionPolicy(_StrictLoopModel):
    release_scope: LoopReleaseEditionScope = "both"
    tool_policy: LoopPolicyMode = "enabled"
    network_policy: LoopPolicyMode = "enabled"
    file_policy: LoopPolicyMode = "read_only"
    browser_policy: LoopPolicyMode = "interactive_only"
    shell_policy: LoopPolicyMode = "disabled"


class LoopBudget(_StrictLoopModel):
    max_attempts: int | None = Field(default=None, ge=1)
    max_steps: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_points: float | None = Field(default=None, ge=0)
    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_artifact_bytes: int | None = Field(default=None, ge=1)


class LoopStopConditions(_StrictLoopModel):
    rubric_score_at_least: float | None = Field(default=None, ge=0, le=5)
    max_consecutive_failures: int | None = Field(default=None, ge=1)
    max_consecutive_same_failures: int | None = Field(default=None, ge=1)
    max_no_progress_iterations: int | None = Field(default=None, ge=1)
    stop_on_deterministic_gate_failure: bool = True
    human_stop_allowed: bool = True


class LoopBudgetUsage(_StrictLoopModel):
    attempts: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_points: float = Field(default=0, ge=0)
    wall_seconds: float = Field(default=0, ge=0)
    artifact_bytes: int = Field(default=0, ge=0)


class LoopLeaseState(_StrictLoopModel):
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    heartbeat_timeout_seconds: float | None = Field(default=None, gt=0)
    watchdog_deadline_at: datetime | None = None

    @field_validator("lease_owner")
    @classmethod
    def validate_optional_owner(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _non_empty_text(value, "lease_owner")


class LoopProgressWindow(_StrictLoopModel):
    consecutive_failures: int = Field(default=0, ge=0)
    consecutive_same_failures: int = Field(default=0, ge=0)
    consecutive_no_progress_iterations: int = Field(default=0, ge=0)
    last_failure_fingerprint: str | None = None

    @field_validator("last_failure_fingerprint")
    @classmethod
    def validate_optional_fingerprint(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _non_empty_text(value, "last_failure_fingerprint")


def advance_loop_progress_window(
    previous: LoopProgressWindow | None = None,
    *,
    iteration_failed: bool,
    made_progress: bool,
    failure_fingerprint: str | None = None,
) -> LoopProgressWindow:
    """Return the next persisted progress window for one loop iteration."""

    window = previous or LoopProgressWindow()
    fingerprint = (
        _non_empty_text(failure_fingerprint, "failure_fingerprint")
        if failure_fingerprint is not None
        else None
    )
    no_progress = (
        0
        if made_progress
        else window.consecutive_no_progress_iterations + 1
    )
    if not iteration_failed:
        return LoopProgressWindow(
            consecutive_no_progress_iterations=no_progress,
        )

    same_failure = (
        fingerprint is not None
        and fingerprint == window.last_failure_fingerprint
    )
    return LoopProgressWindow(
        consecutive_failures=window.consecutive_failures + 1,
        consecutive_same_failures=(
            window.consecutive_same_failures + 1
            if same_failure
            else (1 if fingerprint is not None else 0)
        ),
        consecutive_no_progress_iterations=no_progress,
        last_failure_fingerprint=fingerprint,
    )


class LoopStopReason(_StrictLoopModel):
    code: LoopStopReasonCode
    category: LoopStopReasonCategory
    message: str
    current: float | None = None
    limit: float | None = None
    retryable: bool = False

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _non_empty_text(value, "message")


class LoopStopAssessment(_StrictLoopModel):
    contract_id: str
    contract_hash: str
    should_stop: bool
    reasons: list[LoopStopReason] = Field(default_factory=list)
    budget_exhausted: bool = False
    lease_expired: bool = False
    heartbeat_stale: bool = False
    watchdog_expired: bool = False
    progress_stalled: bool = False
    rubric_target_met: bool = False
    human_stop_requested: bool = False

    @field_validator("contract_id", "contract_hash")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)


class LoopBudgetChangeRecord(_StrictLoopModel):
    schema_version: Literal[1] = RUNTIME_LOOP_BUDGET_EVENT_SCHEMA_VERSION
    contract_id: str
    previous_contract_hash: str
    next_contract_hash: str
    previous_budget_hash: str
    next_budget_hash: str
    changed_by: LoopBudgetChangeActor
    reason: str
    event_hash: str = ""

    @field_validator(
        "contract_id",
        "previous_contract_hash",
        "next_contract_hash",
        "previous_budget_hash",
        "next_budget_hash",
        "reason",
    )
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)


class LoopHumanCheckpoint(_StrictLoopModel):
    checkpoint_id: str
    title: str
    required_before: str

    @field_validator("checkpoint_id", "title", "required_before")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)


class LoopArtifactPolicy(_StrictLoopModel):
    retention_days: int = Field(default=30, ge=0)
    export_requires_approval: bool = True
    allow_sensitive_payloads: bool = False
    delete_artifacts_on_contract_delete: bool = False


class LoopContract(_StrictLoopModel):
    schema_version: Literal[1] = RUNTIME_LOOP_CONTRACT_SCHEMA_VERSION
    contract_id: str
    objective: str
    scope: list[str] = Field(min_length=1)
    out_of_scope: list[str] = Field(default_factory=list)
    inputs: list[LoopInput] = Field(default_factory=list)
    deliverables: list[LoopDeliverable] = Field(min_length=1)
    acceptance: list[str] = Field(min_length=1)
    deterministic_gates: list[LoopDeterministicGate] = Field(min_length=1)
    rubric: LoopRubric
    edition_policy: LoopEditionPolicy = Field(default_factory=LoopEditionPolicy)
    budgets: LoopBudget = Field(default_factory=LoopBudget)
    stop_conditions: LoopStopConditions = Field(default_factory=LoopStopConditions)
    human_checkpoints: list[LoopHumanCheckpoint] = Field(default_factory=list)
    artifact_policy: LoopArtifactPolicy = Field(default_factory=LoopArtifactPolicy)
    approved_by: str | None = None
    contract_version: str = "1"
    contract_hash: str = ""

    @field_validator("contract_id", "objective", "contract_version")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("scope", "out_of_scope", "acceptance")
    @classmethod
    def validate_text_list(cls, values: list[str], info) -> list[str]:
        return [_non_empty_text(value, info.field_name) for value in values]

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "LoopContract":
        groups = {
            "input ids": [item.input_id for item in self.inputs],
            "deliverable ids": [item.deliverable_id for item in self.deliverables],
            "gate ids": [item.gate_id for item in self.deterministic_gates],
            "checkpoint ids": [item.checkpoint_id for item in self.human_checkpoints],
        }
        for label, ids in groups.items():
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} must be unique")
        return self


class LoopDeterministicGateResult(_StrictLoopModel):
    gate_id: str
    status: LoopGateResultStatus
    summary: str = ""
    evidence_hash: str | None = None

    @field_validator("gate_id")
    @classmethod
    def validate_gate_id(cls, value: str) -> str:
        return _non_empty_text(value, "gate_id")


class LoopRubricDimensionScore(_StrictLoopModel):
    dimension_id: str
    score: float = Field(ge=0, le=5)
    rationale_summary: str = ""
    evidence_hash: str | None = None

    @field_validator("dimension_id")
    @classmethod
    def validate_dimension_id(cls, value: str) -> str:
        return _non_empty_text(value, "dimension_id")


class LoopEvaluationReport(_StrictLoopModel):
    schema_version: Literal[1] = RUNTIME_LOOP_EVALUATION_SCHEMA_VERSION
    report_id: str
    contract_id: str
    contract_hash: str
    evaluator: str = "loop_evaluator"
    evaluator_version: str = "1"
    evidence_summary_hash: str
    deterministic_gate_result: LoopDeterministicGateOutcome
    gate_results: list[LoopDeterministicGateResult] = Field(default_factory=list)
    rubric_id: str
    rubric_version: str
    rubric_scores: list[LoopRubricDimensionScore] = Field(default_factory=list)
    weighted_score: float | None = None
    conclusion: LoopEvaluationConclusion
    summary: str
    human_gate_decision: LoopHumanGateDecision = "not_required"

    @field_validator(
        "report_id",
        "contract_id",
        "contract_hash",
        "evaluator",
        "evaluator_version",
        "evidence_summary_hash",
        "rubric_id",
        "rubric_version",
        "summary",
    )
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty_text(value, info.field_name)


def compute_loop_contract_hash(contract: LoopContract) -> str:
    payload = contract.model_dump(mode="json")
    payload["contract_hash"] = ""
    return _hash_payload(payload)


def loop_contract_with_hash(contract: LoopContract) -> LoopContract:
    return contract.model_copy(
        update={"contract_hash": compute_loop_contract_hash(contract)}
    )


def compute_loop_evaluation_evidence_hash(
    *,
    contract_hash: str,
    gate_results: list[LoopDeterministicGateResult],
    rubric_scores: list[LoopRubricDimensionScore],
    human_gate_decision: LoopHumanGateDecision,
) -> str:
    payload = {
        "contract_hash": contract_hash,
        "gate_results": [item.model_dump(mode="json") for item in gate_results],
        "rubric_scores": [item.model_dump(mode="json") for item in rubric_scores],
        "human_gate_decision": human_gate_decision,
    }
    return _hash_payload(payload)


def compute_loop_budget_hash(budget: LoopBudget) -> str:
    return _hash_payload(budget.model_dump(mode="json"))


def compute_loop_budget_change_event_hash(record: LoopBudgetChangeRecord) -> str:
    payload = record.model_dump(mode="json")
    payload["event_hash"] = ""
    return _hash_payload(payload)


def build_loop_budget_change_record(
    previous_contract: LoopContract,
    next_contract: LoopContract,
    *,
    changed_by: LoopBudgetChangeActor,
    reason: str,
) -> LoopBudgetChangeRecord:
    previous_hashed = loop_contract_with_hash(previous_contract)
    next_hashed = loop_contract_with_hash(next_contract)
    if previous_hashed.contract_id != next_hashed.contract_id:
        raise ValueError("budget change requires the same contract id")

    previous_budget_hash = compute_loop_budget_hash(previous_hashed.budgets)
    next_budget_hash = compute_loop_budget_hash(next_hashed.budgets)
    if previous_budget_hash == next_budget_hash:
        raise ValueError("budget change event requires an actual budget change")

    record = LoopBudgetChangeRecord(
        contract_id=previous_hashed.contract_id,
        previous_contract_hash=previous_hashed.contract_hash,
        next_contract_hash=next_hashed.contract_hash,
        previous_budget_hash=previous_budget_hash,
        next_budget_hash=next_budget_hash,
        changed_by=changed_by,
        reason=reason,
    )
    return record.model_copy(
        update={"event_hash": compute_loop_budget_change_event_hash(record)}
    )


def _budget_reason(
    *,
    code: LoopStopReasonCode,
    message: str,
    current: float,
    limit: float,
) -> LoopStopReason:
    return LoopStopReason(
        code=code,
        category="budget",
        message=message,
        current=current,
        limit=limit,
        retryable=False,
    )


def assess_loop_stop_conditions(
    contract: LoopContract,
    *,
    usage: LoopBudgetUsage,
    lease_state: LoopLeaseState | None = None,
    progress: LoopProgressWindow | None = None,
    latest_evaluation: LoopEvaluationReport | None = None,
    human_stop_requested: bool = False,
    now: datetime | None = None,
) -> LoopStopAssessment:
    """Evaluate budget, lease, watchdog, progress, rubric, and human stops."""

    hashed = loop_contract_with_hash(contract)
    reasons: list[LoopStopReason] = []
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    progress = progress or LoopProgressWindow()

    budget_checks: tuple[
        tuple[str, str, LoopStopReasonCode, str],
        ...,
    ] = (
        ("max_attempts", "attempts", "attempt_budget_exhausted", "attempts"),
        ("max_steps", "steps", "step_budget_exhausted", "steps"),
        ("max_tool_calls", "tool_calls", "tool_call_budget_exhausted", "tool calls"),
        ("max_tokens", "tokens", "token_budget_exhausted", "tokens"),
        ("max_cost_points", "cost_points", "cost_budget_exhausted", "cost points"),
        ("max_wall_seconds", "wall_seconds", "wall_time_budget_exhausted", "wall seconds"),
        (
            "max_artifact_bytes",
            "artifact_bytes",
            "artifact_budget_exhausted",
            "artifact bytes",
        ),
    )
    for limit_attr, current_attr, code, label in budget_checks:
        limit = getattr(hashed.budgets, limit_attr)
        current = getattr(usage, current_attr)
        if limit is not None and current >= limit:
            reasons.append(
                _budget_reason(
                    code=code,
                    message=f"Loop {label} budget exhausted.",
                    current=float(current),
                    limit=float(limit),
                )
            )

    if lease_state is not None:
        if lease_state.lease_expires_at is not None:
            lease_expires_at = _as_utc(lease_state.lease_expires_at)
            if lease_expires_at <= now_utc:
                reasons.append(
                    LoopStopReason(
                        code="lease_expired",
                        category="lease",
                        message="Loop lease expired.",
                        retryable=True,
                    )
                )
        if (
            lease_state.last_heartbeat_at is not None
            and lease_state.heartbeat_timeout_seconds is not None
        ):
            heartbeat_age = (
                now_utc - _as_utc(lease_state.last_heartbeat_at)
            ).total_seconds()
            if heartbeat_age >= lease_state.heartbeat_timeout_seconds:
                reasons.append(
                    LoopStopReason(
                        code="heartbeat_stale",
                        category="watchdog",
                        message="Loop heartbeat is stale.",
                        current=max(heartbeat_age, 0),
                        limit=lease_state.heartbeat_timeout_seconds,
                        retryable=True,
                    )
                )
        if lease_state.watchdog_deadline_at is not None:
            watchdog_deadline_at = _as_utc(lease_state.watchdog_deadline_at)
            if watchdog_deadline_at <= now_utc:
                reasons.append(
                    LoopStopReason(
                        code="watchdog_deadline_expired",
                        category="watchdog",
                        message="Loop watchdog deadline expired.",
                        retryable=True,
                    )
                )

    if (
        hashed.stop_conditions.max_consecutive_failures is not None
        and progress.consecutive_failures
        >= hashed.stop_conditions.max_consecutive_failures
    ):
        reasons.append(
            LoopStopReason(
                code="consecutive_failures_exhausted",
                category="progress",
                message="Loop consecutive failure budget exhausted.",
                current=progress.consecutive_failures,
                limit=hashed.stop_conditions.max_consecutive_failures,
                retryable=False,
            )
        )
    if (
        hashed.stop_conditions.max_consecutive_same_failures is not None
        and progress.consecutive_same_failures
        >= hashed.stop_conditions.max_consecutive_same_failures
    ):
        reasons.append(
            LoopStopReason(
                code="consecutive_same_failures_exhausted",
                category="progress",
                message="Loop repeated identical failure budget exhausted.",
                current=progress.consecutive_same_failures,
                limit=hashed.stop_conditions.max_consecutive_same_failures,
                retryable=False,
            )
        )
    if (
        hashed.stop_conditions.max_no_progress_iterations is not None
        and progress.consecutive_no_progress_iterations
        >= hashed.stop_conditions.max_no_progress_iterations
    ):
        reasons.append(
            LoopStopReason(
                code="no_progress_exhausted",
                category="progress",
                message="Loop no-progress budget exhausted.",
                current=progress.consecutive_no_progress_iterations,
                limit=hashed.stop_conditions.max_no_progress_iterations,
                retryable=False,
            )
        )

    if latest_evaluation is not None:
        if latest_evaluation.contract_id != hashed.contract_id:
            raise ValueError("evaluation report contract id does not match contract")
        if latest_evaluation.contract_hash != hashed.contract_hash:
            raise ValueError("evaluation report contract hash does not match contract")
        if (
            hashed.stop_conditions.stop_on_deterministic_gate_failure
            and latest_evaluation.deterministic_gate_result == "blocked"
        ):
            reasons.append(
                LoopStopReason(
                    code="deterministic_gate_blocked",
                    category="gate",
                    message="Loop deterministic gate blocked evaluation.",
                    retryable=False,
                )
            )

        rubric_target = hashed.stop_conditions.rubric_score_at_least
        rubric_met = False
        if latest_evaluation.conclusion == "pass":
            rubric_met = rubric_target is None or (
                latest_evaluation.weighted_score is not None
                and latest_evaluation.weighted_score >= rubric_target
            )
        elif (
            latest_evaluation.conclusion == "waiting_human"
            and rubric_target is not None
            and latest_evaluation.weighted_score is not None
            and latest_evaluation.weighted_score >= rubric_target
        ):
            rubric_met = True
        if rubric_met:
            reasons.append(
                LoopStopReason(
                    code="rubric_target_met",
                    category="rubric",
                    message="Loop rubric target met.",
                    current=latest_evaluation.weighted_score,
                    limit=rubric_target,
                    retryable=False,
                )
            )

    if human_stop_requested and hashed.stop_conditions.human_stop_allowed:
        reasons.append(
            LoopStopReason(
                code="human_stop_requested",
                category="human",
                message="Human requested loop stop.",
                retryable=False,
            )
        )

    codes = {reason.code for reason in reasons}
    categories = {reason.category for reason in reasons}
    return LoopStopAssessment(
        contract_id=hashed.contract_id,
        contract_hash=hashed.contract_hash,
        should_stop=bool(reasons),
        reasons=reasons,
        budget_exhausted="budget" in categories,
        lease_expired="lease_expired" in codes,
        heartbeat_stale="heartbeat_stale" in codes,
        watchdog_expired="watchdog_deadline_expired" in codes,
        progress_stalled="progress" in categories,
        rubric_target_met="rubric_target_met" in codes,
        human_stop_requested="human_stop_requested" in codes,
    )


def _unique_by_id(items: list, attr: str, label: str) -> dict[str, object]:
    values: dict[str, object] = {}
    for item in items:
        item_id = getattr(item, attr)
        if item_id in values:
            raise ValueError(f"{label} ids must be unique: {item_id}")
        values[item_id] = item
    return values


def _weighted_rubric_score(
    contract: LoopContract,
    scores_by_id: dict[str, LoopRubricDimensionScore],
) -> float:
    total_weight = sum(item.weight for item in contract.rubric.dimensions)
    if total_weight <= 0:
        raise ValueError("rubric dimensions must have positive total weight")
    return sum(
        scores_by_id[item.dimension_id].score * item.weight
        for item in contract.rubric.dimensions
    ) / total_weight


def evaluate_loop_contract(
    contract: LoopContract,
    *,
    gate_results: list[LoopDeterministicGateResult],
    rubric_scores: list[LoopRubricDimensionScore],
    human_gate_decision: LoopHumanGateDecision = "not_required",
    evaluator: str = "loop_evaluator",
    evaluator_version: str = "1",
    report_id: str | None = None,
) -> LoopEvaluationReport:
    """Evaluate a LoopContract from allowed evidence summaries only."""

    hashed = loop_contract_with_hash(contract)
    gates_by_id = _unique_by_id(gate_results, "gate_id", "gate result")
    contract_gates_by_id = {gate.gate_id: gate for gate in hashed.deterministic_gates}
    unknown_gates = sorted(set(gates_by_id) - set(contract_gates_by_id))
    if unknown_gates:
        raise ValueError(f"unknown deterministic gate ids: {', '.join(unknown_gates)}")

    missing_required = [
        gate.gate_id
        for gate in hashed.deterministic_gates
        if gate.required and gate.gate_id not in gates_by_id
    ]
    failed_required = [
        gate.gate_id
        for gate in hashed.deterministic_gates
        if gate.required
        and gate.gate_id in gates_by_id
        and gates_by_id[gate.gate_id].status != "passed"  # type: ignore[index]
    ]

    scores_by_id = _unique_by_id(rubric_scores, "dimension_id", "rubric score")
    dimension_ids = {item.dimension_id for item in hashed.rubric.dimensions}
    unknown_scores = sorted(set(scores_by_id) - dimension_ids)
    if unknown_scores:
        raise ValueError(f"unknown rubric dimension ids: {', '.join(unknown_scores)}")
    missing_scores = [
        item.dimension_id
        for item in hashed.rubric.dimensions
        if item.dimension_id not in scores_by_id
    ]

    weighted_score: float | None = None
    deterministic_result: LoopDeterministicGateOutcome = "passed"
    conclusion: LoopEvaluationConclusion
    summary: str

    if missing_required:
        deterministic_result = "blocked"
        conclusion = "blocked"
        summary = f"Missing required deterministic gate results: {', '.join(missing_required)}"
    elif failed_required:
        deterministic_result = "blocked"
        conclusion = "blocked"
        summary = f"Required deterministic gates failed: {', '.join(failed_required)}"
    elif missing_scores:
        conclusion = "blocked"
        summary = f"Missing rubric dimension scores: {', '.join(missing_scores)}"
    else:
        weighted_score = _weighted_rubric_score(
            hashed,
            scores_by_id,  # type: ignore[arg-type]
        )
        below_target = [
            item.dimension_id
            for item in hashed.rubric.dimensions
            if scores_by_id[item.dimension_id].score < item.target_score  # type: ignore[index]
        ]
        if weighted_score < hashed.rubric.passing_score or below_target:
            conclusion = "needs_revision"
            summary = (
                f"Rubric score {weighted_score:.2f} did not satisfy contract targets."
            )
        elif human_gate_decision == "pending":
            conclusion = "waiting_human"
            summary = "Rubric passed; waiting for human gate."
        elif human_gate_decision == "rejected":
            conclusion = "blocked"
            summary = "Human gate rejected the loop result."
        else:
            conclusion = "pass"
            summary = f"Loop contract passed with rubric score {weighted_score:.2f}."

    evidence_hash = compute_loop_evaluation_evidence_hash(
        contract_hash=hashed.contract_hash,
        gate_results=gate_results,
        rubric_scores=rubric_scores,
        human_gate_decision=human_gate_decision,
    )
    return LoopEvaluationReport(
        report_id=report_id or f"{hashed.contract_id}:evaluation:{evidence_hash[-12:]}",
        contract_id=hashed.contract_id,
        contract_hash=hashed.contract_hash,
        evaluator=evaluator,
        evaluator_version=evaluator_version,
        evidence_summary_hash=evidence_hash,
        deterministic_gate_result=deterministic_result,
        gate_results=gate_results,
        rubric_id=hashed.rubric.rubric_id,
        rubric_version=hashed.rubric.version,
        rubric_scores=rubric_scores,
        weighted_score=weighted_score,
        conclusion=conclusion,
        summary=summary,
        human_gate_decision=human_gate_decision,
    )


def render_loop_contract_markdown(contract: LoopContract) -> str:
    """Render a readable projection suitable for ``contract.md``."""

    hashed = loop_contract_with_hash(contract)
    lines = [
        f"# {hashed.objective}",
        "",
        f"- Contract ID: `{hashed.contract_id}`",
        f"- Schema Version: `{hashed.schema_version}`",
        f"- Contract Version: `{hashed.contract_version}`",
        f"- Contract Hash: `{hashed.contract_hash}`",
        "",
        "## Scope",
        *[f"- {item}" for item in hashed.scope],
        "",
        "## Out Of Scope",
        *([f"- {item}" for item in hashed.out_of_scope] or ["- None"]),
        "",
        "## Deliverables",
    ]
    for deliverable in hashed.deliverables:
        lines.append(f"- `{deliverable.deliverable_id}` {deliverable.title}")
        lines.extend(f"  - {item}" for item in deliverable.acceptance)
    lines.extend(["", "## Deterministic Gates"])
    for gate in hashed.deterministic_gates:
        detail = gate.command or gate.expected or gate.kind
        lines.append(f"- `{gate.gate_id}` {gate.name}: {detail}")
    lines.extend(
        [
            "",
            "## Rubric",
            f"- Rubric: `{hashed.rubric.rubric_id}` v{hashed.rubric.version}",
            f"- Passing Score: {hashed.rubric.passing_score:g}",
        ]
    )
    for dimension in hashed.rubric.dimensions:
        lines.append(
            f"- `{dimension.dimension_id}` {dimension.name}: weight {dimension.weight:g}, target {dimension.target_score:g}",
        )
    lines.extend(
        [
            "",
            "## Policies",
            f"- Release Scope: `{hashed.edition_policy.release_scope}`",
            f"- Tool Policy: `{hashed.edition_policy.tool_policy}`",
            f"- Network Policy: `{hashed.edition_policy.network_policy}`",
            f"- File Policy: `{hashed.edition_policy.file_policy}`",
            f"- Browser Policy: `{hashed.edition_policy.browser_policy}`",
            f"- Shell Policy: `{hashed.edition_policy.shell_policy}`",
            "",
            "## Stop Conditions",
            f"- Rubric Score At Least: `{hashed.stop_conditions.rubric_score_at_least}`",
            f"- Max Consecutive Failures: `{hashed.stop_conditions.max_consecutive_failures}`",
            f"- Max No Progress Iterations: `{hashed.stop_conditions.max_no_progress_iterations}`",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
