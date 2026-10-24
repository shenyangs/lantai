# -*- coding: utf-8 -*-
"""Versioned Runtime Run/Attempt/Step/Effect contracts.

These models define the control plane only. They do not create storage
locations or perform side effects; STORE/EFF phases will persist and consume
them through explicit services.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


RUNTIME_RUN_CONTRACT_SCHEMA_VERSION = 1

RuntimeRunSource = Literal[
    "chat",
    "mission",
    "browser",
    "capability",
    "pipeline",
    "cron",
    "reminder",
]
RuntimeRunStatus = Literal[
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
]
RuntimeStepKind = Literal[
    "turn",
    "plan",
    "message",
    "model_call",
    "tool",
    "browser_action",
    "approval",
    "checkpoint",
    "artifact",
    "delivery",
    "evaluation",
]
RuntimeStepStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "paused",
    "stop_requested",
    "succeeded",
    "failed",
    "cancelled",
    "stalled",
    "skipped",
]
RuntimeEffectStatus = Literal[
    "intent",
    "waiting_approval",
    "approved",
    "blocked",
    "running",
    "committed",
    "failed",
    "ambiguous",
    "probed_committed",
    "safe_to_retry",
    "compensated",
    "human_resolved",
    "cancelled",
]
RuntimeAttemptReason = Literal["start", "retry", "resume", "takeover"]
RuntimeCheckpointExecutionPosition = Literal[
    "model_boundary",
    "before_tool",
    "after_tool",
    "terminal",
]
RuntimeReleaseEdition = Literal["official", "mas"]
RuntimeTaskComplexity = Literal["light", "standard", "sustained"]
RuntimeAutonomyOperation = Literal[
    "automatic_resume",
    "model_call",
    "local_read",
    "local_write_new",
    "local_modify_existing",
    "network_read",
    "gui",
    "external_delivery",
]
RuntimeAutoResumeCondition = Literal[
    "network_recovery",
    "backend_restart",
    "app_reopen",
]
RuntimeAutonomyRequiredCapability = Literal[
    "renderer",
    "keep_awake",
    "result_verifier",
]
RuntimeAutonomyPreflightCapability = Literal[
    "user_grant",
    "workspace_access",
    "disk_capacity",
    "provider_authentication",
    "model_protocol",
    "model_context",
    "renderer",
    "keep_awake",
    "gui",
    "result_verifier",
]
RuntimeAutonomyPreflightStatus = Literal["passed", "blocked"]
RuntimeWaitReason = Literal[
    "network",
    "provider_backoff",
    "provider_auth",
    "budget",
    "approval",
    "unknown_effect",
    "system_sleep",
    "file_conflict",
    "needs_unlock",
    "durability_error",
    "milestone",
    "deadline",
]
RuntimeDeliveryStatus = Literal[
    "pending",
    "verified",
    "partial",
    "not_delivered",
]
RuntimeRunReasonCategory = Literal[
    "user",
    "policy",
    "runtime",
    "provider",
    "tool",
    "system",
    "unknown",
]
RuntimeProgressReportKind = Literal[
    "deadline_snapshot",
    "terminal",
    "manual",
]
RuntimeProgressNarrativeStatus = Literal[
    "not_requested",
    "generated",
    "failed",
]

_STRICT_CONFIG = ConfigDict(extra="forbid", frozen=True)


class RuntimeRunContractMigrationError(ValueError):
    """Raised when a stored RuntimeRunContract cannot be explicitly migrated."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _non_empty_text(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    return text


def _text_from(value: object, field_name: str) -> str:
    if value is None:
        raise RuntimeRunContractMigrationError(f"missing {field_name}")
    return _non_empty_text(str(value), field_name)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping_from(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _datetime_from(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise RuntimeRunContractMigrationError(f"unsupported datetime value: {value!r}")


def build_runtime_wake_key(
    *,
    run_id: str,
    task_revision: int,
    continuation_generation: int,
    reason_revision: int,
    wait_reason: RuntimeWaitReason,
) -> str:
    """Build the deterministic identity of one durable continuation intent."""

    payload = {
        "continuation_generation": int(continuation_generation),
        "reason_revision": int(reason_revision),
        "run_id": _non_empty_text(run_id, "run_id"),
        "task_revision": int(task_revision),
        "wait_reason": wait_reason,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"wake-v1:{hashlib.sha256(encoded).hexdigest()}"


def _run_source_from(value: object) -> RuntimeRunSource:
    text = _text_from(value, "source").lower()
    migrations = {
        "browser_task": "browser",
        "browser_workbench": "browser",
        "scheduled_task": "cron",
        "schedule": "cron",
        "loop": "mission",
    }
    text = migrations.get(text, text)
    allowed = RuntimeRunSource.__args__
    if text not in allowed:
        raise RuntimeRunContractMigrationError(f"unsupported runtime source: {text}")
    return text  # type: ignore[return-value]


def _run_status_from(value: object) -> RuntimeRunStatus:
    text = _text_from(value, "status").lower()
    migrations = {
        "dispatched": "queued",
        "acked": "claimed",
        "blocked": "waiting_approval",
        "completed": "succeeded",
        "finished": "succeeded",
        "interrupted": "cancelled",
        "timed_out": "stalled",
    }
    text = migrations.get(text, text)
    allowed = RuntimeRunStatus.__args__
    if text not in allowed:
        raise RuntimeRunContractMigrationError(f"unsupported runtime status: {text}")
    return text  # type: ignore[return-value]


def _release_edition_from(value: object) -> RuntimeReleaseEdition:
    text = str(value or "official").strip().lower()
    if text in {"app_store", "app-store", "testflight"}:
        text = "mas"
    if text not in RuntimeReleaseEdition.__args__:
        raise RuntimeRunContractMigrationError(
            f"unsupported release edition: {text}"
        )
    return text  # type: ignore[return-value]


class _StrictRuntimeModel(BaseModel):
    model_config = _STRICT_CONFIG


class RuntimeRunOwner(_StrictRuntimeModel):
    """Identity allowed to own or control a Runtime Run."""

    user_id: str = "default"
    agent_id: str
    session_id: str | None = None
    workspace_id: str | None = None
    channel: str = "console"

    @field_validator("user_id", "agent_id", "channel")
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("session_id", "workspace_id")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return _optional_text(value)


class RuntimeBudget(_StrictRuntimeModel):
    """Budget limits used by the Run reducer and takeover logic."""

    max_turns: int | None = Field(default=None, ge=1)
    max_model_rounds: int | None = Field(default=None, ge=1)
    max_steps: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_seconds: float | None = Field(default=None, gt=0)
    max_active_seconds: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, ge=1)
    max_no_progress_rounds: int | None = Field(default=None, ge=1)
    max_same_failure_count: int | None = Field(default=None, ge=1)
    max_cost_points: float | None = Field(default=None, ge=0)
    max_artifact_bytes: int | None = Field(default=None, ge=1)
    deadline_at: datetime | None = None

    @field_validator("deadline_at")
    @classmethod
    def normalize_deadline(cls, value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)


class RuntimeFailurePolicy(_StrictRuntimeModel):
    max_same_failure_count: int = Field(default=3, ge=1)
    max_no_progress_rounds: int = Field(default=5, ge=1)
    allow_one_alternative_path: bool = True


class RuntimeCheckpointPolicy(_StrictRuntimeModel):
    every_tool_calls: int = Field(default=6, ge=1)
    every_active_seconds: float = Field(default=300, gt=0)
    # Deprecated compatibility fields. Existing persisted contracts may carry
    # them, but the runtime no longer uses them to truncate or interrupt work.
    segment_active_seconds: float | None = Field(default=None, gt=0)
    segment_max_tool_calls: int | None = Field(default=None, ge=1)
    segment_max_tokens: int | None = Field(default=None, ge=1)


class RuntimeDeliverableContract(_StrictRuntimeModel):
    """Primary-model declaration that becomes immutable once bound."""

    schema_version: Literal[1] = 1
    required: Literal[True] = True
    kind: str
    accepted_extensions: tuple[str, ...]
    must_exist: Literal[True] = True
    must_open: Literal[True] = True
    visual_verification_required: bool = False
    must_be_delivered: Literal[True] = True
    declared_by: Literal["primary_model"] = "primary_model"

    @field_validator("kind")
    @classmethod
    def normalize_kind(cls, value: str) -> str:
        return _non_empty_text(value, "kind").lower()

    @field_validator("accepted_extensions")
    @classmethod
    def normalize_extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            extension = str(item or "").strip().lower()
            if extension and not extension.startswith("."):
                extension = f".{extension}"
            if (
                not extension
                or len(extension) > 16
                or not extension[1:].replace("-", "").isalnum()
            ):
                raise ValueError(f"invalid deliverable extension: {item!r}")
            if extension not in normalized:
                normalized.append(extension)
        if not normalized:
            raise ValueError("accepted_extensions must not be empty")
        if len(normalized) > 4:
            raise ValueError("accepted_extensions supports at most four values")
        return tuple(normalized)


class RuntimePendingDeliverableResolution(_StrictRuntimeModel):
    """Primary-model resolution of one prior unfinished deliverable."""

    schema_version: Literal[1] = 1
    source_run_id: str
    source_contract_revision: int = Field(default=0, ge=0)
    source_task_revision: int = Field(default=0, ge=0)
    pending_job_id: str | None = None
    decision: Literal["continue", "replace", "cancel"]
    decided_by: Literal["primary_model"] = "primary_model"

    @field_validator("source_run_id")
    @classmethod
    def normalize_source_run_id(cls, value: str) -> str:
        return _non_empty_text(value, "source_run_id")

    @field_validator("pending_job_id")
    @classmethod
    def normalize_pending_job_id(cls, value: str | None) -> str | None:
        return _optional_text(value)


class RuntimeTaskCommand(_StrictRuntimeModel):
    """One durable user command that revises an active task in place."""

    schema_version: Literal[1] = 1
    revision: int = Field(ge=1)
    client_turn_id: str
    text: str

    @field_validator("client_turn_id", "text")
    @classmethod
    def validate_command_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)


class RuntimeModelPriceBound(_StrictRuntimeModel):
    """User-approved conservative price ceiling for one provider/model pair."""

    schema_version: Literal[1] = 1
    provider_id: str
    model_id: str
    pricing_version: str
    max_dispatch_micro_cny: int = Field(gt=0)
    upper_bound_proven: Literal[True]

    @field_validator("provider_id", "model_id", "pricing_version")
    @classmethod
    def validate_identity(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)


class RuntimeAutonomyPreflightCheck(_StrictRuntimeModel):
    """One server-observed capability fact captured when a grant is issued."""

    schema_version: Literal[1] = 1
    check_id: str
    capability: RuntimeAutonomyPreflightCapability
    status: RuntimeAutonomyPreflightStatus
    checked_at: datetime
    message: str
    evidence: dict[str, str | int | bool | None] = Field(default_factory=dict)

    @field_validator("check_id", "message")
    @classmethod
    def validate_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("checked_at")
    @classmethod
    def normalize_checked_at(cls, value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class RuntimeAutonomyGrant(_StrictRuntimeModel):
    """User-issued, task-scoped authority for bounded unattended work.

    This contract records authority; it is not an execution owner.  Runtime
    callers must re-evaluate it before resume and every external operation.
    """

    schema_version: Literal[1] = 1
    grant_id: str
    approved: Literal[True]
    issued_by: Literal["user"]
    run_id: str
    chat_id: str
    workspace_id: str
    target_task_revision: int = Field(ge=0)
    issued_at: datetime
    starts_at: datetime
    expires_at: datetime
    allowed_operations: tuple[RuntimeAutonomyOperation, ...]
    allowed_paths: tuple[str, ...] = ()
    allowed_provider_ids: tuple[str, ...] = ()
    allowed_model_ids: tuple[str, ...] = ()
    allow_network: bool = False
    allow_gui: bool = False
    allow_external_delivery: bool = False
    auto_resume_conditions: tuple[RuntimeAutoResumeCondition, ...] = ()
    required_capabilities: tuple[RuntimeAutonomyRequiredCapability, ...] = ()
    preflight_checks: tuple[RuntimeAutonomyPreflightCheck, ...] = ()
    budget_currency: Literal["CNY"] | None = None
    max_cost_micro_cny: int | None = Field(default=None, ge=0)
    model_price_bounds: tuple[RuntimeModelPriceBound, ...] = ()
    revision: int = Field(default=1, ge=1)
    revocation_generation: int = Field(default=0, ge=0)
    revoked_at: datetime | None = None
    revocation_reason: str | None = None

    @field_validator("grant_id", "run_id", "chat_id", "workspace_id")
    @classmethod
    def validate_identity(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("issued_at", "starts_at", "expires_at", "revoked_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)

    @field_validator("revocation_reason")
    @classmethod
    def normalize_revocation_reason(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("allowed_operations")
    @classmethod
    def normalize_operations(
        cls,
        value: tuple[RuntimeAutonomyOperation, ...],
    ) -> tuple[RuntimeAutonomyOperation, ...]:
        if not value:
            raise ValueError("allowed_operations must not be empty")
        return tuple(dict.fromkeys(value))

    @field_validator("allowed_paths", "allowed_provider_ids", "allowed_model_ids")
    @classmethod
    def normalize_scope_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            text = _non_empty_text(item, "scope value")
            if text not in normalized:
                normalized.append(text)
        return tuple(normalized)

    @field_validator("auto_resume_conditions")
    @classmethod
    def normalize_auto_resume_conditions(
        cls,
        value: tuple[RuntimeAutoResumeCondition, ...],
    ) -> tuple[RuntimeAutoResumeCondition, ...]:
        return tuple(dict.fromkeys(value))

    @field_validator("required_capabilities")
    @classmethod
    def normalize_required_capabilities(
        cls,
        value: tuple[RuntimeAutonomyRequiredCapability, ...],
    ) -> tuple[RuntimeAutonomyRequiredCapability, ...]:
        return tuple(dict.fromkeys(value))

    @field_validator("preflight_checks")
    @classmethod
    def validate_preflight_checks(
        cls,
        value: tuple[RuntimeAutonomyPreflightCheck, ...],
    ) -> tuple[RuntimeAutonomyPreflightCheck, ...]:
        observed: set[str] = set()
        for check in value:
            if check.check_id in observed:
                raise ValueError("preflight check IDs must be unique")
            observed.add(check.check_id)
        return value

    @model_validator(mode="after")
    def validate_time_and_revocation_bounds(self):
        if self.starts_at < self.issued_at:
            raise ValueError("starts_at cannot precede issued_at")
        if self.expires_at <= self.starts_at:
            raise ValueError("expires_at must be later than starts_at")
        if self.revocation_generation == 0:
            if self.revoked_at is not None or self.revocation_reason is not None:
                raise ValueError(
                    "unrevoked grant cannot carry revocation metadata"
                )
        elif self.revoked_at is None or self.revocation_reason is None:
            raise ValueError(
                "revoked grant requires revoked_at and revocation_reason"
            )
        if self.revoked_at is not None and self.revoked_at < self.issued_at:
            raise ValueError("revoked_at cannot precede issued_at")
        if self.revocation_generation == 0 and any(
            check.status != "passed" for check in self.preflight_checks
        ):
            raise ValueError("an active grant cannot contain blocked preflight checks")
        if (
            self.auto_resume_conditions
            and "automatic_resume" not in self.allowed_operations
        ):
            raise ValueError(
                "auto_resume_conditions require the automatic_resume operation"
            )
        if "model_call" in self.allowed_operations:
            if self.budget_currency != "CNY" or self.max_cost_micro_cny is None:
                raise ValueError(
                    "model_call authority requires an exact task budget in CNY micro-units"
                )
            if not self.allowed_provider_ids or not self.allowed_model_ids:
                raise ValueError(
                    "model_call authority requires provider and model allowlists"
                )
            if not self.model_price_bounds:
                raise ValueError(
                    "model_call authority requires at least one verified price bound"
                )
            observed_pairs: set[tuple[str, str]] = set()
            for price_bound in self.model_price_bounds:
                pair = (price_bound.provider_id, price_bound.model_id)
                if (
                    price_bound.provider_id not in self.allowed_provider_ids
                    or price_bound.model_id not in self.allowed_model_ids
                ):
                    raise ValueError(
                        "model price bounds must remain inside provider/model allowlists"
                    )
                if pair in observed_pairs:
                    raise ValueError(
                        "only one active price bound is allowed per provider/model pair"
                    )
                observed_pairs.add(pair)
        return self


class RuntimeWakeIntent(_StrictRuntimeModel):
    """Durable request for the existing Run owner to reconsider a task."""

    schema_version: Literal[1] = 1
    run_id: str
    task_revision: int = Field(ge=0)
    continuation_generation: int = Field(ge=0)
    reason_revision: int = Field(ge=0)
    wait_reason: RuntimeWaitReason
    next_eligible_at: datetime
    grant_id: str
    grant_revision: int = Field(ge=1)
    revocation_generation: int = Field(ge=0)
    wake_key: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        task_revision: int,
        continuation_generation: int,
        reason_revision: int,
        wait_reason: RuntimeWaitReason,
        next_eligible_at: datetime,
        grant: RuntimeAutonomyGrant,
    ) -> "RuntimeWakeIntent":
        return cls(
            run_id=run_id,
            task_revision=task_revision,
            continuation_generation=continuation_generation,
            reason_revision=reason_revision,
            wait_reason=wait_reason,
            next_eligible_at=next_eligible_at,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            revocation_generation=grant.revocation_generation,
            wake_key=build_runtime_wake_key(
                run_id=run_id,
                task_revision=task_revision,
                continuation_generation=continuation_generation,
                reason_revision=reason_revision,
                wait_reason=wait_reason,
            ),
        )

    @field_validator("run_id", "grant_id", "wake_key")
    @classmethod
    def validate_identity(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("next_eligible_at")
    @classmethod
    def normalize_next_eligible_at(cls, value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def validate_wake_key(self):
        expected = build_runtime_wake_key(
            run_id=self.run_id,
            task_revision=self.task_revision,
            continuation_generation=self.continuation_generation,
            reason_revision=self.reason_revision,
            wait_reason=self.wait_reason,
        )
        if self.wake_key != expected:
            raise ValueError("wake_key does not match the durable wake identity")
        return self


class RuntimeTaskContract(_StrictRuntimeModel):
    """Task intent, limits, and append-only command revisions for one run."""

    schema_version: Literal[1] = 1
    run_id: str | None = None
    attempt_id: str | None = None
    client_turn_id: str | None = None
    revision: int = Field(default=0, ge=0)
    commands: tuple[RuntimeTaskCommand, ...] = ()
    complexity: RuntimeTaskComplexity = "standard"
    work_method_id: str = "standard"
    objective: str
    acceptance: tuple[str, ...] = ()
    deliverable_contract: RuntimeDeliverableContract | None = None
    pending_deliverable_resolution: RuntimePendingDeliverableResolution | None = None
    budget: RuntimeBudget
    failure_policy: RuntimeFailurePolicy = Field(default_factory=RuntimeFailurePolicy)
    checkpoint_policy: RuntimeCheckpointPolicy = Field(
        default_factory=RuntimeCheckpointPolicy,
    )
    autonomy_grant: RuntimeAutonomyGrant | None = None

    @model_validator(mode="before")
    @classmethod
    def discard_obsolete_artifact_classifier_state(cls, value: object) -> object:
        """Read old persisted runs without reviving the removed classifier."""

        if not isinstance(value, Mapping) or "deliverable_intent" not in value:
            return value
        migrated = dict(value)
        migrated.pop("deliverable_intent", None)
        return migrated

    @field_validator("run_id", "attempt_id", "client_turn_id")
    @classmethod
    def normalize_task_identity(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("work_method_id", "objective")
    @classmethod
    def validate_task_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @model_validator(mode="after")
    def validate_command_history(self):
        expected_revisions = tuple(range(1, len(self.commands) + 1))
        actual_revisions = tuple(command.revision for command in self.commands)
        if actual_revisions != expected_revisions:
            raise ValueError("TaskContract command revisions must be contiguous.")
        if self.revision != len(self.commands):
            raise ValueError("TaskContract revision must equal its command history length.")
        if self.commands and self.client_turn_id != self.commands[-1].client_turn_id:
            raise ValueError("TaskContract client_turn_id must identify the latest command.")
        return self


class RuntimeValidationMetadata(_StrictRuntimeModel):
    """Versioned provenance for system-owned completion-gate checks."""

    schema_version: Literal[1] = 1
    validator_id: str
    validator_version: str
    ruleset_id: str
    ruleset_version: str
    assessed_at: datetime = Field(default_factory=_utc_now)
    required_scope: tuple[str, ...] = ()
    observed_scope: tuple[str, ...] = ()
    invalidation_rules: tuple[str, ...] = (
        "artifact_digest_changed",
        "validator_version_changed",
        "ruleset_version_changed",
    )

    @field_validator(
        "validator_id",
        "validator_version",
        "ruleset_id",
        "ruleset_version",
    )
    @classmethod
    def normalize_validation_identity(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)


class RuntimeCompletionEvidence(_StrictRuntimeModel):
    terminal_event_id: str | None = None
    settled_effect_ids: tuple[str, ...] = ()
    acceptance_results: tuple[dict[str, object], ...] = ()
    primary_artifact_ids: tuple[str, ...] = ()
    works_item_ids: tuple[str, ...] = ()
    validation_work_item_ids: tuple[str, ...] = ()
    validation: RuntimeValidationMetadata | None = None
    final_response_id: str | None = None

    @field_validator("terminal_event_id", "final_response_id")
    @classmethod
    def normalize_evidence_id(cls, value: str | None) -> str | None:
        return _optional_text(value)


class RuntimeConvergenceFamilyCount(_StrictRuntimeModel):
    family: str
    count: int = Field(default=0, ge=0)


class RuntimeConvergenceObservation(_StrictRuntimeModel):
    tool_name: str
    family: str
    ok: bool
    intent_fingerprint: str
    result_fingerprint: str
    explicit_progress: bool | None = None
    new_sources: int = Field(default=0, ge=0)
    new_progress_keys: int = Field(default=0, ge=0)
    novel_result: bool
    made_progress: bool


class RuntimeConvergenceState(_StrictRuntimeModel):
    """Typed, bounded convergence projection persisted in a checkpoint."""

    schema_version: Literal[1] = 1
    method_id: str = "standard"
    family_counts: tuple[RuntimeConvergenceFamilyCount, ...] = ()
    consecutive_no_progress_rounds: int = Field(default=0, ge=0)
    unique_sources: int = Field(default=0, ge=0)
    unique_progress_keys: int = Field(default=0, ge=0)
    resolved_acceptance_count: int = Field(default=0, ge=0)
    review_count: int = Field(default=0, ge=0)
    mode: Literal["off", "shadow", "enforce"] = "enforce"
    seen_sources: tuple[str, ...] = ()
    seen_results: tuple[str, ...] = ()
    seen_progress_keys: tuple[str, ...] = ()
    recent: tuple[RuntimeConvergenceObservation, ...] = ()

    @field_validator("family_counts", mode="before")
    @classmethod
    def normalize_family_counts(cls, value: object) -> object:
        if isinstance(value, Mapping):
            return [
                {"family": str(family), "count": int(count)}
                for family, count in value.items()
            ]
        return value


class RuntimePendingToolCall(_StrictRuntimeModel):
    """The exact tool invocation a resumed owner must revalidate and finish."""

    call_id: str
    tool_name: str
    arguments: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("call_id", "tool_name")
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)


class RuntimeContinuationSteer(_StrictRuntimeModel):
    """One user-authored task revision retained across context rollover."""

    revision: int = Field(ge=1)
    client_turn_id: str
    text: str

    @field_validator("client_turn_id", "text")
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)


class RuntimeContinuationMilestone(_StrictRuntimeModel):
    """Model/tool-authored progress plus the evidence that supports it."""

    milestone_id: str
    statement: str
    evidence_refs: tuple[str, ...] = ()

    @field_validator("milestone_id", "statement")
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)


class RuntimeContinuationArtifact(_StrictRuntimeModel):
    """A content-bound artifact snapshot safe to carry into a new window."""

    artifact_ref: str
    relative_path: str
    content_sha256: str | None = None
    size_bytes: int = Field(ge=0)
    modified_ns: int = Field(ge=0)

    @field_validator("artifact_ref", "relative_path")
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("content_sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        normalized = _optional_text(value)
        if normalized is not None and (
            len(normalized) != 64
            or any(character not in "0123456789abcdef" for character in normalized)
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        return normalized


class RuntimeContinuationInputRef(_StrictRuntimeModel):
    """Exact pointer to one continuation-sensitive canonical Inbox record.

    The original content, attachment grant, selection and ownership stay in
    the Run Inbox.  A continuation capsule carries only enough identity to
    reload and revalidate that immutable record without admitting it again.
    """

    inbox_seq: int = Field(ge=1)
    command_id: str
    client_turn_id: str
    payload_digest: str
    source_attempt_id: str | None = None
    role: Literal["user"] = "user"
    content_types: tuple[
        Literal["image", "file", "artifact_edit_target"], ...
    ]

    @field_validator("command_id", "client_turn_id", "payload_digest")
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("source_attempt_id")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("content_types")
    @classmethod
    def validate_content_types(
        cls,
        value: tuple[
            Literal["image", "file", "artifact_edit_target"], ...
        ],
    ) -> tuple[Literal["image", "file", "artifact_edit_target"], ...]:
        if not value:
            raise ValueError(
                "content_types must include image, file, or artifact_edit_target"
            )
        return tuple(dict.fromkeys(value))


class RuntimeContinuationProviderLineage(_StrictRuntimeModel):
    """The configured route plus the grant-bounded fallback universe."""

    provider_id: str = ""
    model_id: str = ""
    route_ref: str | None = None
    allowed_provider_ids: tuple[str, ...] = ()
    allowed_model_ids: tuple[str, ...] = ()


class RuntimeContinuationCapsule(_StrictRuntimeModel):
    """Durable, source-linked handoff for one Native Runtime context window.

    The capsule is a projection of canonical task/effect/artifact state.  It
    deliberately carries references instead of credentials, tool arguments,
    approvals, budget authority, or terminal authority.
    """

    schema_version: Literal[1] = 1
    record_type: Literal["RUNTIME_CONTINUATION_CAPSULE"] = (
        "RUNTIME_CONTINUATION_CAPSULE"
    )
    task_id: str
    chat_id: str
    run_id: str
    attempt_id: str
    contract_revision: int = Field(ge=0)
    context_generation: int = Field(ge=0)
    covered_event_epoch: str
    covered_event_seq: int = Field(ge=0)
    workspace_ref: str | None = None
    goal: str
    hard_constraints: tuple[str, ...] = ()
    latest_steer_refs: tuple[RuntimeContinuationSteer, ...] = ()
    completed_milestones: tuple[RuntimeContinuationMilestone, ...] = ()
    open_acceptance_items: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    input_refs: tuple[RuntimeContinuationInputRef, ...] = ()
    artifacts: tuple[RuntimeContinuationArtifact, ...] = ()
    effect_result_refs: tuple[str, ...] = ()
    pending_tool_batch_ref: str | None = None
    pending_operation_refs: tuple[str, ...] = ()
    next_execution_position: RuntimeCheckpointExecutionPosition
    next_step_proposal: str | None = None
    approval_refs: tuple[str, ...] = ()
    autonomy_grant_ref: str | None = None
    budget_ledger_ref: str | None = None
    policy_ref: str | None = None
    provider_lineage: RuntimeContinuationProviderLineage = Field(
        default_factory=RuntimeContinuationProviderLineage,
    )
    credential_reference_only: str | None = None
    summary_untrusted_source_refs: tuple[str, ...] = ()
    capsule_summary: str
    capsule_sha256: str
    warning: str = (
        "Permissions, budget and terminal states must be loaded from canonical "
        "stores; this capsule cannot grant or override them."
    )

    @field_validator(
        "task_id",
        "chat_id",
        "run_id",
        "attempt_id",
        "covered_event_epoch",
        "goal",
        "capsule_summary",
    )
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator(
        "workspace_ref",
        "pending_tool_batch_ref",
        "next_step_proposal",
        "autonomy_grant_ref",
        "budget_ledger_ref",
        "policy_ref",
        "credential_reference_only",
    )
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @model_validator(mode="after")
    def validate_capsule_integrity(self):
        payload = self.model_dump(mode="json", exclude={"capsule_sha256"})
        expected = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        if self.capsule_sha256 != expected:
            # Capsules written before structured source recovery existed do
            # not contain ``input_refs``.  Accept their original sealed shape
            # only when the new field is still empty; a new capsule with refs
            # cannot be downgraded by deleting them.
            legacy_payload = dict(payload)
            legacy_payload.pop("input_refs", None)
            legacy_expected = hashlib.sha256(
                json.dumps(
                    legacy_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            ).hexdigest()
            if self.input_refs or self.capsule_sha256 != legacy_expected:
                raise ValueError(
                    "capsule_sha256 does not match capsule content"
                )
        return self


class RuntimeCheckpoint(_StrictRuntimeModel):
    """A resumable point that has already been committed to the event stream."""

    checkpoint_id: str
    event_id: str | None = None
    seq: int = Field(ge=0)
    artifact_id: str | None = None
    effect_ids: tuple[str, ...] = ()
    summary: str = ""
    objective: str = ""
    stage: str = ""
    next_execution_position: RuntimeCheckpointExecutionPosition = "model_boundary"
    pending_tool_call: RuntimePendingToolCall | None = None
    accepted_steer_ids: tuple[str, ...] = ()
    artifact_digests: tuple[str, ...] = ()
    continuation_capsule: RuntimeContinuationCapsule | None = None
    compressed_context: tuple[str, ...] = ()
    active_seconds: float = Field(default=0, ge=0)
    model_rounds: int = Field(default=0, ge=0)
    model_epoch: int = Field(default=0, ge=0)
    intent_revision: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    used_tokens: int = Field(default=0, ge=0)
    no_progress_rounds: int = Field(default=0, ge=0)
    failure_fingerprints: tuple[tuple[str, int], ...] = ()
    inbox_after_seq: int = Field(default=0, ge=0)
    segment_index: int = Field(default=0, ge=0)
    segment_tool_calls: int = Field(default=0, ge=0)
    segment_tokens: int = Field(default=0, ge=0)
    segment_active_seconds: float = Field(default=0, ge=0)
    convergence_state: RuntimeConvergenceState = Field(
        default_factory=RuntimeConvergenceState,
    )

    @field_validator("checkpoint_id")
    @classmethod
    def validate_checkpoint_id(cls, value: str) -> str:
        return _non_empty_text(value, "checkpoint_id")

    @field_validator("event_id", "artifact_id")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("effect_ids")
    @classmethod
    def normalize_effect_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for effect_id in value:
            text = _non_empty_text(effect_id, "effect_ids")
            if text not in normalized:
                normalized.append(text)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_pending_execution_position(self):
        if self.next_execution_position == "before_tool":
            if self.pending_tool_call is None:
                raise ValueError("before_tool checkpoint requires pending_tool_call")
        elif self.pending_tool_call is not None:
            raise ValueError(
                "pending_tool_call is only valid at the before_tool position"
            )
        return self


class RuntimeStepRef(_StrictRuntimeModel):
    """Current Step pointer embedded in the Run/Attempt snapshot."""

    step_id: str
    index: int = Field(ge=0)
    kind: RuntimeStepKind
    title: str = ""

    @field_validator("step_id")
    @classmethod
    def validate_step_id(cls, value: str) -> str:
        return _non_empty_text(value, "step_id")


class RuntimeRunReason(_StrictRuntimeModel):
    """Structured error or stop reason for the control plane."""

    code: str
    message: str = ""
    category: RuntimeRunReasonCategory = "unknown"
    retryable: bool = False

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return _non_empty_text(value, "code")


class RuntimeProgressCostSummary(_StrictRuntimeModel):
    """Exact local ledger facts; unknown provider charges remain explicit."""

    currency: Literal["CNY"] = "CNY"
    limit_micro_cny: int | None = Field(default=None, ge=0)
    reserved_micro_cny: int = Field(default=0, ge=0)
    committed_micro_cny: int = Field(default=0, ge=0)
    settled_count: int = Field(default=0, ge=0)
    pending_count: int = Field(default=0, ge=0)
    ambiguous_count: int = Field(default=0, ge=0)
    overrun_count: int = Field(default=0, ge=0)


class RuntimeProgressEffectSummary(_StrictRuntimeModel):
    """Latest status per effect, never inferred from the final response."""

    status_counts: dict[str, int] = Field(default_factory=dict)
    unresolved_effect_ids: tuple[str, ...] = ()
    ambiguous_effect_ids: tuple[str, ...] = ()


class RuntimeProgressReport(_StrictRuntimeModel):
    """Durable factual report for deadline, terminal, or manual snapshots."""

    schema_version: Literal[1] = 1
    report_id: str
    kind: RuntimeProgressReportKind
    generated_at: datetime
    run_id: str
    attempt_id: str
    task_revision: int | None = Field(default=None, ge=0)
    contract_revision: int = Field(ge=0)
    status: RuntimeRunStatus
    terminal: bool
    objective: str = ""
    completed_milestones: tuple[RuntimeContinuationMilestone, ...] = ()
    acceptance_results: tuple[dict[str, object], ...] = ()
    open_acceptance_items: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    user_decisions: tuple[str, ...] = ()
    effect_summary: RuntimeProgressEffectSummary = Field(
        default_factory=RuntimeProgressEffectSummary,
    )
    cost_summary: RuntimeProgressCostSummary = Field(
        default_factory=RuntimeProgressCostSummary,
    )
    active_seconds: float | None = Field(default=None, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    wait_seconds: float | None = Field(default=None, ge=0)
    unknown_facts: tuple[str, ...] = ()
    grant_id: str | None = None
    grant_revision: int | None = Field(default=None, ge=1)
    grant_expires_at: datetime | None = None
    grant_revoked: bool | None = None
    wake_key: str | None = None
    wait_reason: RuntimeWaitReason | None = None
    narrative_status: RuntimeProgressNarrativeStatus = "not_requested"
    narrative: str | None = None
    narrative_error: str | None = None

    @field_validator("report_id", "run_id", "attempt_id")
    @classmethod
    def validate_identity(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator(
        "generated_at",
        "grant_expires_at",
    )
    @classmethod
    def normalize_report_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)

    @field_validator(
        "grant_id",
        "wake_key",
        "narrative",
        "narrative_error",
    )
    @classmethod
    def normalize_report_optional_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @model_validator(mode="after")
    def validate_narrative_state(self):
        is_terminal = self.status in {
            "succeeded",
            "failed",
            "cancelled",
            "stalled",
        }
        if self.terminal != is_terminal:
            raise ValueError("report terminal flag must match the run status")
        if self.kind == "terminal" and not is_terminal:
            raise ValueError("terminal report requires a terminal run status")
        if self.kind == "deadline_snapshot" and (
            self.status != "paused" or self.wait_reason != "deadline"
        ):
            raise ValueError(
                "deadline snapshot requires a deadline-paused run status"
            )
        if self.narrative_status == "generated" and self.narrative is None:
            raise ValueError("generated narrative requires narrative text")
        if self.narrative_status == "failed" and self.narrative_error is None:
            raise ValueError("failed narrative requires narrative_error")
        if self.narrative_status != "generated" and self.narrative is not None:
            raise ValueError("only a generated narrative may contain narrative text")
        if self.narrative_status != "failed" and self.narrative_error is not None:
            raise ValueError("only a failed narrative may contain narrative_error")
        return self


class RuntimeRunContract(_StrictRuntimeModel):
    """Authoritative control-plane snapshot for one active Run attempt."""

    schema_version: Literal[1] = RUNTIME_RUN_CONTRACT_SCHEMA_VERSION
    run_id: str
    attempt_id: str
    source: RuntimeRunSource
    owner: RuntimeRunOwner
    status: RuntimeRunStatus = "queued"
    revision: int = Field(default=0, ge=0)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    settled_at: datetime | None = None
    budget: RuntimeBudget = Field(default_factory=RuntimeBudget)
    task_contract: RuntimeTaskContract | None = None
    wake_intent: RuntimeWakeIntent | None = None
    wait_reason: RuntimeWaitReason | None = None
    next_eligible_at: datetime | None = None
    delivery_status: RuntimeDeliveryStatus = "pending"
    completion_evidence: RuntimeCompletionEvidence | None = None
    progress_report: RuntimeProgressReport | None = None
    checkpoint: RuntimeCheckpoint | None = None
    current_step: RuntimeStepRef | None = None
    error: RuntimeRunReason | None = None
    stop_reason: RuntimeRunReason | None = None
    contract_hash: str = ""
    policy_snapshot_hash: str = ""
    release_edition: RuntimeReleaseEdition

    @field_validator("run_id", "attempt_id")
    @classmethod
    def validate_identity(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("lease_owner")
    @classmethod
    def normalize_lease_owner(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator(
        "lease_expires_at",
        "created_at",
        "started_at",
        "heartbeat_at",
        "settled_at",
        "next_eligible_at",
    )
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def validate_wait_state(self):
        if self.next_eligible_at is not None and self.wait_reason is None:
            raise ValueError("next_eligible_at requires wait_reason")
        return self


class RuntimeAttemptContract(RuntimeRunContract):
    """Attempt-specific snapshot used for retry, resume, and takeover."""

    attempt_reason: RuntimeAttemptReason = "start"
    previous_attempt_id: str | None = None

    @field_validator("previous_attempt_id")
    @classmethod
    def normalize_previous_attempt_id(cls, value: str | None) -> str | None:
        return _optional_text(value)


class RuntimeStepContract(_StrictRuntimeModel):
    """Versioned Step snapshot for a recoverable unit of work."""

    schema_version: Literal[1] = RUNTIME_RUN_CONTRACT_SCHEMA_VERSION
    run_id: str
    attempt_id: str
    step_id: str
    source: RuntimeRunSource
    owner: RuntimeRunOwner
    index: int = Field(ge=0)
    kind: RuntimeStepKind
    title: str = ""
    status: RuntimeStepStatus = "queued"
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=_utc_now)
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    settled_at: datetime | None = None
    checkpoint: RuntimeCheckpoint | None = None
    error: RuntimeRunReason | None = None
    release_edition: RuntimeReleaseEdition

    @field_validator("run_id", "attempt_id", "step_id")
    @classmethod
    def validate_identity(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("created_at", "started_at", "heartbeat_at", "settled_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)


class RuntimeEffectRecord(_StrictRuntimeModel):
    """Versioned external effect intent/result record."""

    schema_version: Literal[1] = RUNTIME_RUN_CONTRACT_SCHEMA_VERSION
    run_id: str
    attempt_id: str
    step_id: str
    effect_id: str
    source: RuntimeRunSource
    owner: RuntimeRunOwner
    status: RuntimeEffectStatus = "intent"
    revision: int = Field(default=0, ge=0)
    tool_name: str
    args_hash: str
    policy_snapshot_hash: str
    idempotency_key: str
    approval_id: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    started_at: datetime | None = None
    settled_at: datetime | None = None
    result_hash: str | None = None
    ambiguity_reason: str | None = None
    release_edition: RuntimeReleaseEdition

    @field_validator(
        "run_id",
        "attempt_id",
        "step_id",
        "effect_id",
        "tool_name",
        "args_hash",
        "policy_snapshot_hash",
        "idempotency_key",
    )
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("approval_id", "result_hash", "ambiguity_reason")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("created_at", "started_at", "settled_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)


def _owner_from_legacy(raw: Mapping[str, object]) -> RuntimeRunOwner:
    owner_payload = _mapping_from(raw.get("owner"))
    if owner_payload is not None:
        return RuntimeRunOwner.model_validate(owner_payload)
    return RuntimeRunOwner(
        user_id=str(raw.get("user_id") or "default"),
        agent_id=_text_from(raw.get("agent_id"), "agent_id"),
        session_id=_optional_text(raw.get("session_id")),
        workspace_id=_optional_text(raw.get("workspace_id") or raw.get("workspace")),
        channel=str(raw.get("channel") or "console"),
    )


def _reason_from_legacy(value: object, *, code: str) -> RuntimeRunReason | None:
    if value is None:
        return None
    mapping = _mapping_from(value)
    if mapping is not None:
        return RuntimeRunReason.model_validate(mapping)
    message = str(value).strip()
    if not message:
        return None
    return RuntimeRunReason(code=code, message=message, category="runtime")


def _migrate_legacy_runtime_run_contract(
    raw: Mapping[str, object],
) -> RuntimeRunContract:
    run_id = _text_from(
        raw.get("run_id") or raw.get("id") or raw.get("task_run_id"),
        "run_id",
    )
    attempt_id = str(raw.get("attempt_id") or f"{run_id}:attempt-1")
    started_at = _datetime_from(raw.get("started_at"))
    created_at = _datetime_from(raw.get("created_at")) or started_at or _utc_now()
    settled_at = _datetime_from(raw.get("settled_at") or raw.get("ended_at"))
    heartbeat_at = _datetime_from(
        raw.get("heartbeat_at") or raw.get("last_heartbeat_at")
    )
    source = _run_source_from(raw.get("source"))
    status = _run_status_from(raw.get("status") or "queued")

    return RuntimeRunContract(
        schema_version=RUNTIME_RUN_CONTRACT_SCHEMA_VERSION,
        run_id=run_id,
        attempt_id=attempt_id,
        source=source,
        owner=_owner_from_legacy(raw),
        status=status,
        revision=int(raw.get("revision") or 0),
        lease_owner=_optional_text(raw.get("lease_owner")),
        lease_expires_at=_datetime_from(raw.get("lease_expires_at")),
        created_at=created_at,
        started_at=started_at,
        heartbeat_at=heartbeat_at,
        settled_at=settled_at,
        budget=RuntimeBudget.model_validate(raw.get("budget") or {}),
        task_contract=RuntimeTaskContract.model_validate(raw["task_contract"])
        if _mapping_from(raw.get("task_contract")) is not None
        else None,
        wait_reason=raw.get("wait_reason"),
        next_eligible_at=_datetime_from(raw.get("next_eligible_at")),
        delivery_status=str(raw.get("delivery_status") or "pending"),
        completion_evidence=RuntimeCompletionEvidence.model_validate(
            raw["completion_evidence"]
        )
        if _mapping_from(raw.get("completion_evidence")) is not None
        else None,
        checkpoint=RuntimeCheckpoint.model_validate(raw["checkpoint"])
        if _mapping_from(raw.get("checkpoint")) is not None
        else None,
        current_step=RuntimeStepRef.model_validate(raw["current_step"])
        if _mapping_from(raw.get("current_step")) is not None
        else None,
        error=_reason_from_legacy(
            raw.get("error") or raw.get("failure_reason"),
            code="legacy_failure",
        ),
        stop_reason=_reason_from_legacy(raw.get("stop_reason"), code="legacy_stop"),
        contract_hash=str(raw.get("contract_hash") or ""),
        policy_snapshot_hash=str(raw.get("policy_snapshot_hash") or ""),
        release_edition=_release_edition_from(raw.get("release_edition")),
    )


def migrate_runtime_run_contract(
    raw: Mapping[str, object],
) -> RuntimeRunContract:
    """Return the current RuntimeRunContract or fail with an explicit reason."""

    version = raw.get("schema_version", 0)
    try:
        version_int = int(version)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeRunContractMigrationError(
            f"invalid runtime run contract schema_version: {version!r}"
        ) from exc

    if version_int == RUNTIME_RUN_CONTRACT_SCHEMA_VERSION:
        return RuntimeRunContract.model_validate(raw)
    if version_int == 0:
        return _migrate_legacy_runtime_run_contract(raw)
    raise RuntimeRunContractMigrationError(
        f"unsupported runtime run contract schema_version: {version_int}"
    )
