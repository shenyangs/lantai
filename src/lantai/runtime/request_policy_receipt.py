# -*- coding: utf-8 -*-
"""Content-free evidence for Native Runtime's assigned base policy payload.

The receipt is captured from the exact built-in payload already assigned by
the Native constructor.  It never serializes a model, calls a custom hook, or
retains policy values.  Unsupported values make the diagnostic unavailable
without changing runtime behavior.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass
from dataclasses import field
from typing import Any, Final
from uuid import uuid4

from .input import RuntimeInput, RuntimeRequestContext
from .tools import ToolCall, ToolPolicyContext, ToolPolicyOutcome


REQUEST_POLICY_CONSTRUCTION_RECEIPT_SCHEMA_V1: Final = (
    "com.lantai.runtime.request-policy-construction-receipt.v1"
)
REQUEST_POLICY_EFFECTIVE_RECEIPT_SCHEMA_V1: Final = (
    "com.lantai.runtime.request-policy-effective-receipt.v1"
)
REQUEST_POLICY_DISPATCH_RECEIPT_SCHEMA_V1: Final = (
    "com.lantai.runtime.request-policy-dispatch-receipt.v1"
)
REQUEST_POLICY_CONSUMPTION_RECEIPT_SCHEMA_V1: Final = (
    "com.lantai.runtime.request-policy-consumption-receipt.v1"
)
REQUEST_POLICY_OBSERVATION_TOKEN_SCHEMA_V1: Final = (
    "com.lantai.runtime.request-policy-observation-token.v1"
)
REQUEST_POLICY_OBSERVATION_INVOCATION_SCHEMA_V1: Final = (
    "com.lantai.runtime.request-policy-observation-invocation.v1"
)

_REQUEST_CONTEXT_FIELDS: Final = (
    "agent_id",
    "session_id",
    "user_id",
    "channel",
    "chat_id",
    "run_id",
    "root_session_id",
    "workspace_dir",
    "metadata",
)
_TOOL_POLICY_CONTEXT_FIELDS: Final = (
    "agent_id",
    "session_id",
    "root_session_id",
    "chat_id",
    "turn_id",
    "workspace_dir",
    "workspace_grant_id",
    "workspace_access_mode",
    "workspace_trust_state",
    "permission_profile",
    "permission_mode",
    "approval_level",
    "distribution",
    "request_context",
    "metadata",
)
_DEADLINE_KEYS: Final = frozenset(
    {
        "native_turn_deadline_monotonic",
        "turn_deadline_monotonic",
        "deadline_monotonic",
        "native_turn_timeout_seconds",
        "turn_timeout_seconds",
    }
)
_TERMINAL_EVENT_TYPES: Final = frozenset(
    {"turn.completed", "turn.failed", "turn.interrupted", "turn.stalled"}
)
_OBSERVATION_STATUSES: Final = frozenset(
    {"pending", "claimed", "formed", "exhausted", "taken"}
)
_LIVE_OVERRIDE_MODES: Final = frozenset(
    {
        "not_available",
        "not_reached",
        "missing_binding",
        "read_failed",
        "unchanged",
        "applied",
    }
)
_POLICY_CONSUMER_PHASES: Final = frozenset({"initial", "post_override"})
_POLICY_CONSUMER_STAGES: Final = frozenset(
    {
        "artifact_delivery",
        "builtin_policy",
        "collaboration_read_only",
        "delegation_tool_scope",
        "full_access",
        "official_chrome",
        "official_guanlan",
        "remembered_approval",
        "runtime_owned_artifact",
        "runtime_phase",
        "tool_guard",
        "workspace_access",
    }
)
_POLICY_CONSUMER_STAGE_ORDER: Final = {
    "initial": {
        "runtime_phase": 0,
        "official_chrome": 1,
        "workspace_access": 2,
        "official_guanlan": 3,
        "collaboration_read_only": 4,
        "delegation_tool_scope": 5,
    },
    "post_override": {
        "runtime_owned_artifact": 0,
        "artifact_delivery": 1,
        "builtin_policy": 2,
        "full_access": 3,
        "remembered_approval": 4,
        "tool_guard": 5,
    },
}
_DEFAULT_POLICY_MIDDLEWARE_NAME: Final = "RuntimeToolPolicyMiddleware"
_LOCK_TYPE: Final = type(threading.Lock())


def _canonical_json_value(value: Any) -> Any:
    """Encode exact inert JSON built-ins without extensible conversions."""

    value_type = type(value)
    if value is None:
        return {"$null": True}
    if value_type is bool:
        return {"$bool": value}
    if value_type is int:
        return {"$int": value}
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError("Request policy receipt numbers must be finite")
        return {"$float": repr(value)}
    if value_type is str:
        return {"$string": value}
    if value_type is list:
        return {"$array": [_canonical_json_value(item) for item in value]}
    if value_type is dict:
        rows: list[list[Any]] = []
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(
                    "Request policy receipt object keys require exact strings"
                )
            rows.append([key, _canonical_json_value(item)])
        return {"$object": rows}
    raise TypeError(
        "Request policy receipt values require exact inert JSON built-ins"
    )


def request_policy_payload_sha256(payload: dict[str, Any]) -> str:
    """Hash one exact JSON-compatible policy payload without retaining it."""

    if type(payload) is not dict:
        raise TypeError(
            "Request policy receipt requires an exact built-in dict"
        )
    encoded = json.dumps(
        _canonical_json_value(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def request_policy_payload_is_deadline_free(payload: dict[str, Any]) -> bool:
    """Return false for any deadline key; reject extensible containers."""

    if type(payload) is not dict:
        raise TypeError("Request policy deadline scan requires an exact dict")

    def contains_deadline(value: Any) -> bool:
        value_type = type(value)
        if value is None or value_type in {str, bool, int}:
            return False
        if value_type is float:
            if not math.isfinite(value):
                raise ValueError(
                    "Request policy deadline scan requires finite numbers"
                )
            return False
        if value_type is list:
            return any(contains_deadline(item) for item in value)
        if value_type is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError(
                        "Request policy deadline keys require strings"
                    )
                if key in _DEADLINE_KEYS or contains_deadline(item):
                    return True
            return False
        raise TypeError(
            "Request policy deadline scan requires inert JSON data"
        )

    return not contains_deadline(payload)


def project_runtime_request_context(
    request: RuntimeRequestContext,
) -> dict[str, Any]:
    """Project fixed exact request fields without model serialization."""

    if type(request) is not RuntimeRequestContext:
        raise TypeError(
            "Policy observation requires exact RuntimeRequestContext"
        )
    if tuple(RuntimeRequestContext.model_fields) != _REQUEST_CONTEXT_FIELDS:
        raise RuntimeError("RuntimeRequestContext observation schema drifted")
    return {
        name: object.__getattribute__(request, name)
        for name in _REQUEST_CONTEXT_FIELDS
    }


def project_tool_policy_context(context: ToolPolicyContext) -> dict[str, Any]:
    """Project fixed exact policy fields without model serialization."""

    if type(context) is not ToolPolicyContext:
        raise TypeError("Policy observation requires exact ToolPolicyContext")
    if tuple(ToolPolicyContext.model_fields) != _TOOL_POLICY_CONTEXT_FIELDS:
        raise RuntimeError("ToolPolicyContext observation schema drifted")
    return {
        name: object.__getattribute__(context, name)
        for name in _TOOL_POLICY_CONTEXT_FIELDS
    }


def project_request_policy_input(
    runtime_input: RuntimeInput,
    assigned_base_policy: dict[str, Any],
) -> dict[str, Any]:
    """Project the five input-boundary policy fields from actual inputs."""

    if type(runtime_input) is not RuntimeInput:
        raise TypeError("Policy observation requires exact RuntimeInput")
    if type(assigned_base_policy) is not dict:
        raise TypeError("Policy observation requires exact assigned policy")
    model_slot = object.__getattribute__(runtime_input, "model_slot")
    tool_policy_context = object.__getattribute__(
        runtime_input,
        "tool_policy_context",
    )
    runtime_metadata = object.__getattribute__(runtime_input, "metadata")
    if model_slot is not None and type(model_slot) is not str:
        raise TypeError("Policy observation model slot is invalid")
    if (
        type(tool_policy_context) is not dict
        or type(runtime_metadata) is not dict
    ):
        raise TypeError(
            "Policy observation input mappings must be exact dicts"
        )
    return {
        "runtime_request": project_runtime_request_context(
            runtime_input.request
        ),
        "model_slot": model_slot,
        "tool_policy_context": tool_policy_context,
        "runtime_metadata": runtime_metadata,
        "native_tool_policy": assigned_base_policy,
    }


def _valid_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


@dataclass(frozen=True, slots=True, repr=False)
class RequestPolicyConstructionReceipt:
    """Immutable hash-only receipt for the constructor-assigned payload."""

    schema_version: str
    assigned_payload_sha256: str
    top_level_field_count: int

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version
            != REQUEST_POLICY_CONSTRUCTION_RECEIPT_SCHEMA_V1
        ):
            raise ValueError("Unsupported request policy receipt schema")
        if not _valid_sha256(self.assigned_payload_sha256):
            raise ValueError("Invalid request policy receipt digest")
        if type(self.top_level_field_count) is not int or (
            self.top_level_field_count < 0
        ):
            raise TypeError("Invalid request policy receipt field count")

    def __repr__(self) -> str:
        schema_version = (
            self.schema_version
            if type(self.schema_version) is str
            and self.schema_version
            == REQUEST_POLICY_CONSTRUCTION_RECEIPT_SCHEMA_V1
            else "invalid"
        )
        assigned_payload_sha256 = (
            self.assigned_payload_sha256
            if _valid_sha256(self.assigned_payload_sha256)
            else None
        )
        top_level_field_count: int | str = (
            self.top_level_field_count
            if type(self.top_level_field_count) is int
            and self.top_level_field_count >= 0
            else "invalid"
        )
        return (
            "RequestPolicyConstructionReceipt("
            f"schema_version={schema_version!r}, "
            f"assigned_payload_sha256={assigned_payload_sha256!r}, "
            f"top_level_field_count={top_level_field_count!r}, "
            "private_values=False)"
        )


def capture_request_policy_construction_receipt(
    assigned_payload: dict[str, Any],
) -> RequestPolicyConstructionReceipt:
    """Capture the payload that the constructor has already assigned."""

    if type(assigned_payload) is not dict:
        raise TypeError(
            "Request policy receipt requires an exact built-in dict"
        )
    return RequestPolicyConstructionReceipt(
        schema_version=REQUEST_POLICY_CONSTRUCTION_RECEIPT_SCHEMA_V1,
        assigned_payload_sha256=request_policy_payload_sha256(
            assigned_payload
        ),
        top_level_field_count=len(assigned_payload),
    )


class _EffectivePolicyObservationState:
    __slots__ = (
        "__weakref__",
        "dispatch_invalid",
        "dispatch_taken",
        "dispatch_unavailable",
        "consumption_invalid",
        "consumption_taken",
        "consumption_unavailable",
        "consumptions",
        "evaluations",
        "invocation_sha256",
        "lock",
        "receipt",
        "status",
        "terminal_type",
    )

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.dispatch_invalid = False
        self.dispatch_taken = False
        self.dispatch_unavailable = False
        self.consumption_invalid = False
        self.consumption_taken = False
        self.consumption_unavailable = False
        self.consumptions: list[_PolicyConsumptionState] = []
        self.evaluations: list[_PolicyEvaluationState] = []
        self.invocation_sha256: str | None = None
        self.receipt: RequestPolicyEffectiveTurnReceipt | None = None
        self.status = "pending"
        self.terminal_type: str | None = None


class _PolicyEvaluationState:
    __slots__ = (
        "completed",
        "entry_context_sha256",
        "evaluation_index",
        "middleware_rows",
        "tool_call_sha256",
    )

    def __init__(
        self,
        *,
        evaluation_index: int,
        tool_call_sha256: str,
        entry_context_sha256: str,
    ) -> None:
        self.completed = False
        self.entry_context_sha256 = entry_context_sha256
        self.evaluation_index = evaluation_index
        self.middleware_rows: list[tuple[int, str, str]] = []
        self.tool_call_sha256 = tool_call_sha256


class _PolicyConsumptionState:
    __slots__ = (
        "completed",
        "consumer_rows",
        "delivered_context_sha256",
        "evaluation_index",
        "middleware_index",
        "outcome_action",
        "override_mode",
        "post_override_context_sha256",
    )

    def __init__(
        self,
        *,
        evaluation_index: int,
        middleware_index: int,
        delivered_context_sha256: str,
    ) -> None:
        self.completed = False
        self.consumer_rows: list[tuple[str, str, str]] = []
        self.delivered_context_sha256 = delivered_context_sha256
        self.evaluation_index = evaluation_index
        self.middleware_index = middleware_index
        self.outcome_action: str | None = None
        self.override_mode: str | None = None
        self.post_override_context_sha256: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class EffectivePolicyObservationToken:
    """Opaque one-shot binding passed explicitly through RuntimeRunner."""

    schema_version: str
    observer_sha256: str
    nonce_sha256: str
    run_id_sha256: str
    expected_turn_id_sha256: str
    policy_input_sha256: str
    _observer_nonce: str = field(repr=False, compare=False)
    _nonce: str = field(repr=False, compare=False)
    _state: _EffectivePolicyObservationState = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version
            != REQUEST_POLICY_OBSERVATION_TOKEN_SCHEMA_V1
        ):
            raise ValueError("Unsupported policy observation token schema")
        for value in (
            self.observer_sha256,
            self.nonce_sha256,
            self.run_id_sha256,
            self.expected_turn_id_sha256,
            self.policy_input_sha256,
        ):
            if not _valid_sha256(value):
                raise ValueError("Invalid policy observation token digest")
        if (
            type(self._observer_nonce) is not str
            or type(self._nonce) is not str
        ):
            raise TypeError("Policy observation token nonces require strings")
        if type(self._state) is not _EffectivePolicyObservationState:
            raise TypeError("Policy observation token state is invalid")

    def __repr__(self) -> str:
        digests = (
            self.observer_sha256,
            self.nonce_sha256,
            self.run_id_sha256,
            self.expected_turn_id_sha256,
            self.policy_input_sha256,
        )
        valid = bool(
            type(self.schema_version) is str
            and self.schema_version
            == REQUEST_POLICY_OBSERVATION_TOKEN_SCHEMA_V1
            and all(_valid_sha256(value) for value in digests)
        )
        state = (
            self._state
            if type(self._state) is _EffectivePolicyObservationState
            else None
        )
        status = (
            state.status
            if state is not None
            and type(state.status) is str
            and state.status in _OBSERVATION_STATUSES
            else "invalid"
        )
        return (
            "EffectivePolicyObservationToken("
            f"valid={valid!r}, status={status!r}, private_values=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EffectivePolicyObservationInvocation:
    """Unique one-run authority claimed from an observation token."""

    schema_version: str
    invocation_sha256: str
    observer_sha256: str
    nonce_sha256: str
    _invocation_nonce: str = field(repr=False, compare=False)
    _token: EffectivePolicyObservationToken = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version
            != REQUEST_POLICY_OBSERVATION_INVOCATION_SCHEMA_V1
        ):
            raise ValueError(
                "Unsupported policy observation invocation schema"
            )
        if not all(
            _valid_sha256(value)
            for value in (
                self.invocation_sha256,
                self.observer_sha256,
                self.nonce_sha256,
            )
        ):
            raise ValueError("Invalid policy observation invocation digest")
        if (
            type(self._invocation_nonce) is not str
            or type(self._token) is not EffectivePolicyObservationToken
        ):
            raise TypeError(
                "Policy observation invocation internals are invalid"
            )

    def __repr__(self) -> str:
        valid = bool(
            type(self.schema_version) is str
            and self.schema_version
            == REQUEST_POLICY_OBSERVATION_INVOCATION_SCHEMA_V1
            and all(
                _valid_sha256(value)
                for value in (
                    self.invocation_sha256,
                    self.observer_sha256,
                    self.nonce_sha256,
                )
            )
        )
        return (
            "EffectivePolicyObservationInvocation("
            f"valid={valid!r}, private_values=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class RequestPolicyEffectiveTurnReceipt:
    """Hash-only proof of one deadline-free effective policy formation."""

    schema_version: str
    observer_sha256: str
    nonce_sha256: str
    invocation_sha256: str
    run_id_sha256: str
    actual_turn_id_sha256: str
    policy_input_sha256: str
    effective_payload_sha256: str
    top_level_field_count: int

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version
            != REQUEST_POLICY_EFFECTIVE_RECEIPT_SCHEMA_V1
        ):
            raise ValueError("Unsupported effective policy receipt schema")
        for value in (
            self.observer_sha256,
            self.nonce_sha256,
            self.invocation_sha256,
            self.run_id_sha256,
            self.actual_turn_id_sha256,
            self.policy_input_sha256,
            self.effective_payload_sha256,
        ):
            if not _valid_sha256(value):
                raise ValueError("Invalid effective policy receipt digest")
        if type(self.top_level_field_count) is not int or (
            self.top_level_field_count != len(_TOOL_POLICY_CONTEXT_FIELDS)
        ):
            raise ValueError("Invalid effective policy receipt field count")

    def __repr__(self) -> str:
        digests = (
            self.observer_sha256,
            self.nonce_sha256,
            self.invocation_sha256,
            self.run_id_sha256,
            self.actual_turn_id_sha256,
            self.policy_input_sha256,
            self.effective_payload_sha256,
        )
        field_count: int | str = (
            self.top_level_field_count
            if type(self.top_level_field_count) is int
            and self.top_level_field_count == len(_TOOL_POLICY_CONTEXT_FIELDS)
            else "invalid"
        )
        return (
            "RequestPolicyEffectiveTurnReceipt("
            f"valid={all(_valid_sha256(value) for value in digests)!r}, "
            f"top_level_field_count={field_count!r}, "
            "private_values=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PolicyEvaluationObservation:
    """Opaque handle for one policy evaluator entry."""

    invocation_sha256: str
    evaluation_index: int
    _invocation: EffectivePolicyObservationInvocation = field(
        repr=False,
        compare=False,
    )
    _evaluation: _PolicyEvaluationState = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _valid_sha256(self.invocation_sha256):
            raise ValueError("Invalid policy evaluation observation digest")
        if type(self.evaluation_index) is not int or self.evaluation_index < 0:
            raise ValueError("Invalid policy evaluation observation index")
        if type(self._invocation) is not EffectivePolicyObservationInvocation:
            raise TypeError("Invalid policy evaluation invocation")
        if type(self._evaluation) is not _PolicyEvaluationState:
            raise TypeError("Invalid policy evaluation state")

    def __repr__(self) -> str:
        evaluation_index: int | str = (
            self.evaluation_index
            if type(self.evaluation_index) is int
            and self.evaluation_index >= 0
            else "invalid"
        )
        valid = bool(
            _valid_sha256(self.invocation_sha256)
            and type(evaluation_index) is int
        )
        return (
            "PolicyEvaluationObservation("
            f"valid={valid!r}, evaluation_index={evaluation_index!r}, "
            "private_values=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class RequestPolicyDispatchReceipt:
    """Hash-only evidence for evaluator and middleware context dispatch."""

    schema_version: str
    observer_sha256: str
    nonce_sha256: str
    invocation_sha256: str
    effective_payload_sha256: str
    evaluations_sha256: str
    evaluation_count: int
    middleware_dispatch_count: int
    all_contexts_matched: bool

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != REQUEST_POLICY_DISPATCH_RECEIPT_SCHEMA_V1
        ):
            raise ValueError("Unsupported policy dispatch receipt schema")
        for value in (
            self.observer_sha256,
            self.nonce_sha256,
            self.invocation_sha256,
            self.effective_payload_sha256,
            self.evaluations_sha256,
        ):
            if not _valid_sha256(value):
                raise ValueError("Invalid policy dispatch receipt digest")
        if (
            type(self.evaluation_count) is not int
            or self.evaluation_count < 1
            or type(self.middleware_dispatch_count) is not int
            or self.middleware_dispatch_count < 0
            or type(self.all_contexts_matched) is not bool
        ):
            raise ValueError("Invalid policy dispatch receipt counts")

    def __repr__(self) -> str:
        evaluation_count: int | str = (
            self.evaluation_count
            if type(self.evaluation_count) is int
            and self.evaluation_count >= 1
            else "invalid"
        )
        middleware_dispatch_count: int | str = (
            self.middleware_dispatch_count
            if type(self.middleware_dispatch_count) is int
            and self.middleware_dispatch_count >= 0
            else "invalid"
        )
        all_contexts_matched: bool | str = (
            self.all_contexts_matched
            if type(self.all_contexts_matched) is bool
            else "invalid"
        )
        return (
            "RequestPolicyDispatchReceipt("
            f"evaluation_count={evaluation_count!r}, "
            f"middleware_dispatch_count={middleware_dispatch_count!r}, "
            f"all_contexts_matched={all_contexts_matched!r}, "
            "private_values=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PolicyConsumptionObservation:
    """Opaque handle for one default middleware consumption path."""

    invocation_sha256: str
    evaluation_index: int
    middleware_index: int
    _evaluation_observation: PolicyEvaluationObservation = field(
        repr=False,
        compare=False,
    )
    _consumption: _PolicyConsumptionState = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _valid_sha256(self.invocation_sha256):
            raise ValueError("Invalid policy consumption observation digest")
        if (
            type(self.evaluation_index) is not int
            or self.evaluation_index < 0
            or type(self.middleware_index) is not int
            or self.middleware_index < 0
            or type(self._evaluation_observation)
            is not PolicyEvaluationObservation
            or type(self._consumption) is not _PolicyConsumptionState
        ):
            raise TypeError("Invalid policy consumption observation")

    def __repr__(self) -> str:
        valid = bool(
            _valid_sha256(self.invocation_sha256)
            and type(self.evaluation_index) is int
            and self.evaluation_index >= 0
            and type(self.middleware_index) is int
            and self.middleware_index >= 0
        )
        return (
            "PolicyConsumptionObservation("
            f"valid={valid!r}, private_values=False)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class RequestPolicyConsumptionReceipt:
    """Hash-only evidence for live override and final policy consumers."""

    schema_version: str
    observer_sha256: str
    nonce_sha256: str
    invocation_sha256: str
    dispatch_evaluations_sha256: str
    consumptions_sha256: str
    default_middleware_dispatch_count: int
    consumption_count: int
    live_permission_evaluation_count: int
    live_override_applied_count: int
    guard_consumption_count: int
    all_context_chains_valid: bool

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version
            != REQUEST_POLICY_CONSUMPTION_RECEIPT_SCHEMA_V1
        ):
            raise ValueError("Unsupported policy consumption receipt schema")
        for value in (
            self.observer_sha256,
            self.nonce_sha256,
            self.invocation_sha256,
            self.dispatch_evaluations_sha256,
            self.consumptions_sha256,
        ):
            if not _valid_sha256(value):
                raise ValueError("Invalid policy consumption receipt digest")
        counts = (
            self.default_middleware_dispatch_count,
            self.consumption_count,
            self.live_permission_evaluation_count,
            self.live_override_applied_count,
            self.guard_consumption_count,
        )
        if (
            any(type(value) is not int or value < 0 for value in counts)
            or self.default_middleware_dispatch_count < 1
            or self.consumption_count < 1
            or self.default_middleware_dispatch_count != self.consumption_count
            or self.live_permission_evaluation_count > self.consumption_count
            or self.live_override_applied_count
            > self.live_permission_evaluation_count
            or self.guard_consumption_count > self.consumption_count
            or type(self.all_context_chains_valid) is not bool
        ):
            raise ValueError("Invalid policy consumption receipt counts")

    def __repr__(self) -> str:
        consumption_count: int | str = (
            self.consumption_count
            if type(self.consumption_count) is int
            and self.consumption_count >= 1
            else "invalid"
        )
        live_permission_evaluation_count: int | str = (
            self.live_permission_evaluation_count
            if type(self.live_permission_evaluation_count) is int
            and self.live_permission_evaluation_count >= 0
            else "invalid"
        )
        live_override_applied_count: int | str = (
            self.live_override_applied_count
            if type(self.live_override_applied_count) is int
            and self.live_override_applied_count >= 0
            else "invalid"
        )
        guard_consumption_count: int | str = (
            self.guard_consumption_count
            if type(self.guard_consumption_count) is int
            and self.guard_consumption_count >= 0
            else "invalid"
        )
        all_context_chains_valid: bool | str = (
            self.all_context_chains_valid
            if type(self.all_context_chains_valid) is bool
            else "invalid"
        )
        return (
            "RequestPolicyConsumptionReceipt("
            f"consumption_count={consumption_count!r}, "
            "live_permission_evaluation_count="
            f"{live_permission_evaluation_count!r}, "
            f"live_override_applied_count={live_override_applied_count!r}, "
            f"guard_consumption_count={guard_consumption_count!r}, "
            f"all_context_chains_valid={all_context_chains_valid!r}, "
            "private_values=False)"
        )


def is_valid_request_policy_construction_receipt(
    receipt: object,
) -> bool:
    return bool(
        type(receipt) is RequestPolicyConstructionReceipt
        and type(receipt.schema_version) is str
        and receipt.schema_version
        == REQUEST_POLICY_CONSTRUCTION_RECEIPT_SCHEMA_V1
        and _valid_sha256(receipt.assigned_payload_sha256)
        and type(receipt.top_level_field_count) is int
        and receipt.top_level_field_count >= 0
    )


def is_valid_request_policy_effective_receipt(receipt: object) -> bool:
    if type(receipt) is not RequestPolicyEffectiveTurnReceipt:
        return False
    if (
        type(receipt.schema_version) is not str
        or receipt.schema_version != REQUEST_POLICY_EFFECTIVE_RECEIPT_SCHEMA_V1
        or type(receipt.top_level_field_count) is not int
        or receipt.top_level_field_count != len(_TOOL_POLICY_CONTEXT_FIELDS)
    ):
        return False
    return all(
        _valid_sha256(value)
        for value in (
            receipt.observer_sha256,
            receipt.nonce_sha256,
            receipt.invocation_sha256,
            receipt.run_id_sha256,
            receipt.actual_turn_id_sha256,
            receipt.policy_input_sha256,
            receipt.effective_payload_sha256,
        )
    )


def is_valid_request_policy_dispatch_receipt(receipt: object) -> bool:
    return bool(
        type(receipt) is RequestPolicyDispatchReceipt
        and type(receipt.schema_version) is str
        and receipt.schema_version == REQUEST_POLICY_DISPATCH_RECEIPT_SCHEMA_V1
        and all(
            _valid_sha256(value)
            for value in (
                receipt.observer_sha256,
                receipt.nonce_sha256,
                receipt.invocation_sha256,
                receipt.effective_payload_sha256,
                receipt.evaluations_sha256,
            )
        )
        and type(receipt.evaluation_count) is int
        and receipt.evaluation_count >= 1
        and type(receipt.middleware_dispatch_count) is int
        and receipt.middleware_dispatch_count >= 0
        and type(receipt.all_contexts_matched) is bool
    )


def is_valid_request_policy_consumption_receipt(receipt: object) -> bool:
    if type(receipt) is not RequestPolicyConsumptionReceipt:
        return False
    counts = (
        receipt.default_middleware_dispatch_count,
        receipt.consumption_count,
        receipt.live_permission_evaluation_count,
        receipt.live_override_applied_count,
        receipt.guard_consumption_count,
    )
    return bool(
        type(receipt.schema_version) is str
        and receipt.schema_version
        == REQUEST_POLICY_CONSUMPTION_RECEIPT_SCHEMA_V1
        and all(
            _valid_sha256(value)
            for value in (
                receipt.observer_sha256,
                receipt.nonce_sha256,
                receipt.invocation_sha256,
                receipt.dispatch_evaluations_sha256,
                receipt.consumptions_sha256,
            )
        )
        and all(type(value) is int and value >= 0 for value in counts)
        and receipt.default_middleware_dispatch_count >= 1
        and receipt.consumption_count >= 1
        and receipt.default_middleware_dispatch_count
        == receipt.consumption_count
        and receipt.live_permission_evaluation_count
        <= receipt.consumption_count
        and receipt.live_override_applied_count
        <= receipt.live_permission_evaluation_count
        and receipt.guard_consumption_count <= receipt.consumption_count
        and type(receipt.all_context_chains_valid) is bool
    )


def _binding_sha256(name: str, value: str) -> str:
    if type(name) is not str or type(value) is not str:
        raise TypeError("Policy observation bindings require strings")
    return request_policy_payload_sha256({name: value})


def _evaluation_state_is_safe(evaluation: object) -> bool:
    if type(evaluation) is not _PolicyEvaluationState:
        return False
    if (
        type(evaluation.completed) is not bool
        or type(evaluation.evaluation_index) is not int
        or evaluation.evaluation_index < 0
        or not _valid_sha256(evaluation.tool_call_sha256)
        or not _valid_sha256(evaluation.entry_context_sha256)
        or type(evaluation.middleware_rows) is not list
    ):
        return False
    return all(
        type(row) is tuple
        and len(row) == 3
        and type(row[0]) is int
        and row[0] >= 0
        and _valid_sha256(row[1])
        and _valid_sha256(row[2])
        for row in evaluation.middleware_rows
    )


def _consumption_state_is_safe(consumption: object) -> bool:
    if type(consumption) is not _PolicyConsumptionState:
        return False
    if (
        type(consumption.completed) is not bool
        or type(consumption.evaluation_index) is not int
        or consumption.evaluation_index < 0
        or type(consumption.middleware_index) is not int
        or consumption.middleware_index < 0
        or not _valid_sha256(consumption.delivered_context_sha256)
        or type(consumption.consumer_rows) is not list
        or (
            consumption.override_mode is not None
            and (
                type(consumption.override_mode) is not str
                or consumption.override_mode not in _LIVE_OVERRIDE_MODES
            )
        )
        or (
            consumption.post_override_context_sha256 is not None
            and not _valid_sha256(consumption.post_override_context_sha256)
        )
        or (
            consumption.outcome_action is not None
            and type(consumption.outcome_action) is not str
        )
    ):
        return False
    return all(
        type(row) is tuple
        and len(row) == 3
        and type(row[0]) is str
        and row[0] in _POLICY_CONSUMER_STAGES
        and type(row[1]) is str
        and row[1] in _POLICY_CONSUMER_PHASES
        and _valid_sha256(row[2])
        for row in consumption.consumer_rows
    )


def _state_is_safe(state: object) -> bool:
    if type(state) is not _EffectivePolicyObservationState:
        return False
    if (
        type(state.lock) is not _LOCK_TYPE
        or type(state.dispatch_invalid) is not bool
        or type(state.dispatch_taken) is not bool
        or type(state.dispatch_unavailable) is not bool
        or type(state.consumption_invalid) is not bool
        or type(state.consumption_taken) is not bool
        or type(state.consumption_unavailable) is not bool
        or type(state.consumptions) is not list
        or not all(
            _consumption_state_is_safe(item) for item in state.consumptions
        )
        or type(state.evaluations) is not list
        or not all(
            _evaluation_state_is_safe(item) for item in state.evaluations
        )
        or type(state.status) is not str
        or state.status not in _OBSERVATION_STATUSES
        or (
            state.invocation_sha256 is not None
            and not _valid_sha256(state.invocation_sha256)
        )
        or (
            state.terminal_type is not None
            and (
                type(state.terminal_type) is not str
                or state.terminal_type not in _TERMINAL_EVENT_TYPES
            )
        )
        or (
            state.receipt is not None
            and not is_valid_request_policy_effective_receipt(state.receipt)
        )
    ):
        return False
    return True


def _token_structure_is_safe(token: object) -> bool:
    if type(token) is not EffectivePolicyObservationToken:
        return False
    if (
        type(token.schema_version) is not str
        or token.schema_version != REQUEST_POLICY_OBSERVATION_TOKEN_SCHEMA_V1
        or type(token._observer_nonce) is not str
        or type(token._nonce) is not str
        or not _state_is_safe(token._state)
    ):
        return False
    return all(
        _valid_sha256(value)
        for value in (
            token.observer_sha256,
            token.nonce_sha256,
            token.run_id_sha256,
            token.expected_turn_id_sha256,
            token.policy_input_sha256,
        )
    )


def _invocation_structure_is_safe(invocation: object) -> bool:
    if type(invocation) is not EffectivePolicyObservationInvocation:
        return False
    if (
        type(invocation.schema_version) is not str
        or invocation.schema_version
        != REQUEST_POLICY_OBSERVATION_INVOCATION_SCHEMA_V1
        or type(invocation._invocation_nonce) is not str
        or not _token_structure_is_safe(invocation._token)
        or not all(
            _valid_sha256(value)
            for value in (
                invocation.invocation_sha256,
                invocation.observer_sha256,
                invocation.nonce_sha256,
            )
        )
    ):
        return False
    return bool(
        invocation.observer_sha256 == invocation._token.observer_sha256
        and invocation.nonce_sha256 == invocation._token.nonce_sha256
        and invocation.invocation_sha256
        == _binding_sha256("invocation_nonce", invocation._invocation_nonce)
    )


def claim_effective_policy_observation(
    token: object,
) -> EffectivePolicyObservationInvocation | None:
    """Claim one token for exactly one explicit RuntimeRunner invocation."""

    try:
        if not _token_structure_is_safe(token):
            return None
        assert type(token) is EffectivePolicyObservationToken
        state = token._state
        invocation_nonce = uuid4().hex
        invocation_sha256 = _binding_sha256(
            "invocation_nonce",
            invocation_nonce,
        )
        with state.lock:
            if not _state_is_safe(state) or state.status != "pending":
                return None
            state.status = "claimed"
            state.invocation_sha256 = invocation_sha256
        return EffectivePolicyObservationInvocation(
            schema_version=REQUEST_POLICY_OBSERVATION_INVOCATION_SCHEMA_V1,
            invocation_sha256=invocation_sha256,
            observer_sha256=token.observer_sha256,
            nonce_sha256=token.nonce_sha256,
            _invocation_nonce=invocation_nonce,
            _token=token,
        )
    except Exception:
        return None


def mark_effective_policy_observation_exhausted(
    invocation: EffectivePolicyObservationInvocation,
    *,
    terminal_type: str,
    terminal_turn_id: str,
) -> None:
    """Mark only a normally exhausted stream with an exact terminal event."""

    if not _invocation_structure_is_safe(invocation):
        return
    if (
        type(terminal_type) is not str
        or terminal_type not in _TERMINAL_EVENT_TYPES
    ):
        return
    if type(terminal_turn_id) is not str or not terminal_turn_id:
        return
    state = invocation._token._state
    with state.lock:
        if not _state_is_safe(state):
            return
        receipt = state.receipt
        if (
            state.status != "formed"
            or state.invocation_sha256 != invocation.invocation_sha256
            or not is_valid_request_policy_effective_receipt(receipt)
        ):
            return
        assert type(receipt) is RequestPolicyEffectiveTurnReceipt
        if receipt.actual_turn_id_sha256 != _binding_sha256(
            "turn_id",
            terminal_turn_id,
        ):
            return
        state.status = "exhausted"
        state.terminal_type = terminal_type


class RequestPolicyReceiptObserver:
    """Turn-local construction and effective-policy diagnostic owner."""

    def __init__(
        self,
        assigned_base_policy: object,
        *,
        default_turn_timeout_seconds: float | None,
    ) -> None:
        self._assigned_base_policy: dict[str, Any] | None = None
        self._default_turn_timeout_seconds = default_turn_timeout_seconds
        self._observer_nonce: str | None = None
        self._observer_sha256: str | None = None
        self._construction_receipt: RequestPolicyConstructionReceipt | None = (
            None
        )
        try:
            if type(assigned_base_policy) is not dict:
                return
            observer_nonce = uuid4().hex
            observer_sha256 = _binding_sha256(
                "observer_nonce",
                observer_nonce,
            )
            self._assigned_base_policy = assigned_base_policy
            self._observer_nonce = observer_nonce
            self._observer_sha256 = observer_sha256
            self._construction_receipt = (
                capture_request_policy_construction_receipt(
                    assigned_base_policy
                )
            )
        except Exception:
            self._assigned_base_policy = None
            self._observer_nonce = None
            self._observer_sha256 = None
            self._construction_receipt = None

    def construction_receipt(self) -> RequestPolicyConstructionReceipt | None:
        return self._construction_receipt

    def begin(
        self,
        runtime_input: RuntimeInput,
    ) -> EffectivePolicyObservationToken | None:
        """Create an unregistered explicit token for one deadline-free turn."""

        try:
            if type(runtime_input) is not RuntimeInput:
                return None
            if (
                self._default_turn_timeout_seconds is not None
                or type(self._assigned_base_policy) is not dict
                or type(self._observer_nonce) is not str
                or not _valid_sha256(self._observer_sha256)
            ):
                return None
            assigned_base_policy = self._assigned_base_policy
            observer_nonce = self._observer_nonce
            observer_sha256 = self._observer_sha256
            assert type(assigned_base_policy) is dict
            assert type(observer_nonce) is str
            assert type(observer_sha256) is str
            request = object.__getattribute__(runtime_input, "request")
            if type(request) is not RuntimeRequestContext:
                return None
            run_id = object.__getattribute__(request, "run_id")
            if type(run_id) is not str or not run_id:
                return None
            policy_input = project_request_policy_input(
                runtime_input,
                assigned_base_policy,
            )
            if not request_policy_payload_is_deadline_free(policy_input):
                return None
            policy_input_sha256 = request_policy_payload_sha256(policy_input)
            nonce = uuid4().hex
            return EffectivePolicyObservationToken(
                schema_version=REQUEST_POLICY_OBSERVATION_TOKEN_SCHEMA_V1,
                observer_sha256=observer_sha256,
                nonce_sha256=_binding_sha256("nonce", nonce),
                run_id_sha256=_binding_sha256("run_id", run_id),
                expected_turn_id_sha256=_binding_sha256("turn_id", run_id),
                policy_input_sha256=policy_input_sha256,
                _observer_nonce=observer_nonce,
                _nonce=nonce,
                _state=_EffectivePolicyObservationState(),
            )
        except Exception:
            return None

    def capture_effective(
        self,
        invocation: EffectivePolicyObservationInvocation | None,
        *,
        runtime_input: RuntimeInput,
        actual_turn_id: str,
        effective_context: ToolPolicyContext,
        deadline_monotonic: float | None,
    ) -> None:
        """Bind the exact effective context already formed by Native."""

        try:
            if (
                type(runtime_input) is not RuntimeInput
                or not self._owns(invocation)
                or deadline_monotonic is not None
            ):
                return
            assert invocation is not None
            token = invocation._token
            assigned_base_policy = self._assigned_base_policy
            if type(assigned_base_policy) is not dict:
                return
            request = object.__getattribute__(runtime_input, "request")
            if type(request) is not RuntimeRequestContext:
                return
            run_id = object.__getattribute__(request, "run_id")
            if (
                type(run_id) is not str
                or not run_id
                or type(actual_turn_id) is not str
            ):
                return
            policy_input = project_request_policy_input(
                runtime_input,
                assigned_base_policy,
            )
            effective_payload = project_tool_policy_context(effective_context)
            if not request_policy_payload_is_deadline_free(
                policy_input
            ) or not (
                request_policy_payload_is_deadline_free(effective_payload)
            ):
                return
            policy_input_sha256 = request_policy_payload_sha256(policy_input)
            if (
                token.run_id_sha256 != _binding_sha256("run_id", run_id)
                or token.expected_turn_id_sha256
                != _binding_sha256("turn_id", actual_turn_id)
                or token.policy_input_sha256 != policy_input_sha256
            ):
                return
            receipt = RequestPolicyEffectiveTurnReceipt(
                schema_version=REQUEST_POLICY_EFFECTIVE_RECEIPT_SCHEMA_V1,
                observer_sha256=token.observer_sha256,
                nonce_sha256=token.nonce_sha256,
                invocation_sha256=invocation.invocation_sha256,
                run_id_sha256=token.run_id_sha256,
                actual_turn_id_sha256=token.expected_turn_id_sha256,
                policy_input_sha256=policy_input_sha256,
                effective_payload_sha256=request_policy_payload_sha256(
                    effective_payload
                ),
                top_level_field_count=len(effective_payload),
            )
            state = token._state
            if type(state) is not _EffectivePolicyObservationState:
                return
            with state.lock:
                if (
                    not _state_is_safe(state)
                    or state.status != "claimed"
                    or state.invocation_sha256 != invocation.invocation_sha256
                ):
                    return
                state.receipt = receipt
                state.status = "formed"
        except Exception:
            return

    def start_policy_evaluation(
        self,
        invocation: EffectivePolicyObservationInvocation | None,
        *,
        tool_call: ToolCall,
        policy_context: ToolPolicyContext,
    ) -> PolicyEvaluationObservation | None:
        """Record one evaluator entry from the effective turn context."""

        try:
            if (
                not self._owns(invocation)
                or type(tool_call) is not ToolCall
                or type(tool_call.id) is not str
                or type(tool_call.name) is not str
            ):
                return None
            assert invocation is not None
            entry_payload = project_tool_policy_context(policy_context)
            if not request_policy_payload_is_deadline_free(entry_payload):
                self._mark_dispatch_unavailable(invocation)
                return None
            entry_sha256 = request_policy_payload_sha256(entry_payload)
            tool_call_sha256 = request_policy_payload_sha256(
                {"tool_call_id": tool_call.id, "tool_name": tool_call.name}
            )
            state = invocation._token._state
            with state.lock:
                if not _state_is_safe(state) or state.status != "formed":
                    return None
                effective = state.receipt
                if not is_valid_request_policy_effective_receipt(effective):
                    return None
                assert type(effective) is RequestPolicyEffectiveTurnReceipt
                evaluation = _PolicyEvaluationState(
                    evaluation_index=len(state.evaluations),
                    tool_call_sha256=tool_call_sha256,
                    entry_context_sha256=entry_sha256,
                )
                if entry_sha256 != effective.effective_payload_sha256:
                    state.dispatch_invalid = True
                observation = PolicyEvaluationObservation(
                    invocation_sha256=invocation.invocation_sha256,
                    evaluation_index=evaluation.evaluation_index,
                    _invocation=invocation,
                    _evaluation=evaluation,
                )
                state.evaluations.append(evaluation)
                return observation
        except Exception:
            self._mark_dispatch_unavailable(invocation)
            return None

    def record_policy_middleware_dispatch(
        self,
        observation: PolicyEvaluationObservation | None,
        *,
        middleware_index: int,
        middleware_name: str,
        delivered_context: ToolPolicyContext,
    ) -> None:
        """Record the exact copy passed to one ordered middleware."""

        try:
            if (
                not self._owns_evaluation(observation)
                or type(middleware_index) is not int
                or middleware_index < 0
                or type(middleware_name) is not str
            ):
                return
            assert observation is not None
            delivered_payload = project_tool_policy_context(delivered_context)
            if not request_policy_payload_is_deadline_free(delivered_payload):
                self._mark_dispatch_unavailable(observation._invocation)
                return
            delivered_sha256 = request_policy_payload_sha256(delivered_payload)
            middleware_sha256 = request_policy_payload_sha256(
                {"middleware": middleware_name}
            )
            state = observation._invocation._token._state
            evaluation = observation._evaluation
            with state.lock:
                if (
                    not _state_is_safe(state)
                    or not _evaluation_state_is_safe(evaluation)
                    or evaluation.completed
                    or evaluation not in state.evaluations
                    or middleware_index != len(evaluation.middleware_rows)
                ):
                    return
                if delivered_sha256 != evaluation.entry_context_sha256:
                    state.dispatch_invalid = True
                evaluation.middleware_rows.append(
                    (middleware_index, middleware_sha256, delivered_sha256)
                )
        except Exception:
            if type(observation) is PolicyEvaluationObservation:
                self._mark_dispatch_unavailable(observation._invocation)

    def start_policy_consumption(
        self,
        observation: PolicyEvaluationObservation | None,
        *,
        middleware_index: int,
        delivered_context: ToolPolicyContext,
    ) -> PolicyConsumptionObservation | None:
        """Bind one exact default middleware to its dispatch row."""

        try:
            if (
                not self._owns_evaluation(observation)
                or type(middleware_index) is not int
                or middleware_index < 0
            ):
                return None
            assert observation is not None
            delivered_payload = project_tool_policy_context(delivered_context)
            if not request_policy_payload_is_deadline_free(delivered_payload):
                self._mark_consumption_unavailable(observation._invocation)
                return None
            delivered_sha256 = request_policy_payload_sha256(delivered_payload)
            state = observation._invocation._token._state
            evaluation = observation._evaluation
            with state.lock:
                if (
                    not _state_is_safe(state)
                    or not _evaluation_state_is_safe(evaluation)
                    or middleware_index >= len(evaluation.middleware_rows)
                    or any(
                        item.evaluation_index == observation.evaluation_index
                        and item.middleware_index == middleware_index
                        for item in state.consumptions
                    )
                ):
                    return None
                dispatch_row = evaluation.middleware_rows[middleware_index]
                default_middleware_sha256 = request_policy_payload_sha256(
                    {"middleware": _DEFAULT_POLICY_MIDDLEWARE_NAME}
                )
                consumption = _PolicyConsumptionState(
                    evaluation_index=observation.evaluation_index,
                    middleware_index=middleware_index,
                    delivered_context_sha256=delivered_sha256,
                )
                if (
                    dispatch_row[0] != middleware_index
                    or dispatch_row[1] != default_middleware_sha256
                    or dispatch_row[2] != delivered_sha256
                ):
                    state.consumption_invalid = True
                handle = PolicyConsumptionObservation(
                    invocation_sha256=observation.invocation_sha256,
                    evaluation_index=observation.evaluation_index,
                    middleware_index=middleware_index,
                    _evaluation_observation=observation,
                    _consumption=consumption,
                )
                state.consumptions.append(consumption)
                return handle
        except Exception:
            if type(observation) is PolicyEvaluationObservation:
                self._mark_consumption_unavailable(observation._invocation)
            return None

    def record_policy_context_consumption(
        self,
        observation: PolicyConsumptionObservation | None,
        *,
        stage: str,
        phase: str,
        context: ToolPolicyContext,
    ) -> None:
        """Record one fixed policy consumer and the context it received."""

        try:
            if (
                not self._owns_consumption(observation)
                or type(stage) is not str
                or stage not in _POLICY_CONSUMER_STAGES
                or type(phase) is not str
                or phase not in _POLICY_CONSUMER_PHASES
            ):
                return
            assert observation is not None
            payload = project_tool_policy_context(context)
            if not request_policy_payload_is_deadline_free(payload):
                self._mark_consumption_unavailable(
                    observation._evaluation_observation._invocation
                )
                return
            context_sha256 = request_policy_payload_sha256(payload)
            invocation = observation._evaluation_observation._invocation
            state = invocation._token._state
            consumption = observation._consumption
            with state.lock:
                if (
                    not _state_is_safe(state)
                    or not _consumption_state_is_safe(consumption)
                    or consumption.completed
                    or consumption not in state.consumptions
                ):
                    return
                expected_sha256 = (
                    consumption.delivered_context_sha256
                    if phase == "initial"
                    else consumption.post_override_context_sha256
                )
                if (
                    expected_sha256 is None
                    or context_sha256 != expected_sha256
                ):
                    state.consumption_invalid = True
                if any(row[0] == stage for row in consumption.consumer_rows):
                    state.consumption_invalid = True
                previous = (
                    consumption.consumer_rows[-1]
                    if consumption.consumer_rows
                    else None
                )
                if previous is not None:
                    previous_stage, previous_phase, _previous_sha256 = previous
                    if (
                        phase == "initial"
                        and previous_phase == "post_override"
                    ):
                        state.consumption_invalid = True
                    elif phase == previous_phase and (
                        _POLICY_CONSUMER_STAGE_ORDER[phase][stage]
                        <= _POLICY_CONSUMER_STAGE_ORDER[previous_phase][
                            previous_stage
                        ]
                    ):
                        state.consumption_invalid = True
                consumption.consumer_rows.append(
                    (stage, phase, context_sha256)
                )
        except Exception:
            if type(observation) is PolicyConsumptionObservation:
                self._mark_consumption_unavailable(
                    observation._evaluation_observation._invocation
                )

    def record_live_permission_result(
        self,
        observation: PolicyConsumptionObservation | None,
        *,
        mode: str,
        context: ToolPolicyContext,
    ) -> None:
        """Record the authorized live-permission transform boundary."""

        try:
            if (
                not self._owns_consumption(observation)
                or type(mode) is not str
                or mode not in _LIVE_OVERRIDE_MODES
            ):
                return
            assert observation is not None
            payload = project_tool_policy_context(context)
            if not request_policy_payload_is_deadline_free(payload):
                self._mark_consumption_unavailable(
                    observation._evaluation_observation._invocation
                )
                return
            context_sha256 = request_policy_payload_sha256(payload)
            invocation = observation._evaluation_observation._invocation
            state = invocation._token._state
            consumption = observation._consumption
            with state.lock:
                if (
                    not _state_is_safe(state)
                    or not _consumption_state_is_safe(consumption)
                    or consumption.completed
                    or consumption.override_mode is not None
                ):
                    return
                consumption.override_mode = mode
                consumption.post_override_context_sha256 = context_sha256
                if (
                    mode != "applied"
                    and context_sha256 != consumption.delivered_context_sha256
                ):
                    state.consumption_invalid = True
        except Exception:
            if type(observation) is PolicyConsumptionObservation:
                self._mark_consumption_unavailable(
                    observation._evaluation_observation._invocation
                )

    def complete_policy_consumption(
        self,
        observation: PolicyConsumptionObservation | None,
        *,
        outcome: ToolPolicyOutcome,
    ) -> None:
        """Close one successful default middleware return path."""

        try:
            if (
                not self._owns_consumption(observation)
                or type(outcome) is not ToolPolicyOutcome
                or type(outcome.action) is not str
            ):
                return
            assert observation is not None
            invocation = observation._evaluation_observation._invocation
            state = invocation._token._state
            consumption = observation._consumption
            with state.lock:
                if (
                    not _state_is_safe(state)
                    or not _consumption_state_is_safe(consumption)
                    or consumption.completed
                    or consumption.override_mode is None
                ):
                    return
                consumption.outcome_action = outcome.action
                consumption.completed = True
        except Exception:
            if type(observation) is PolicyConsumptionObservation:
                self._mark_consumption_unavailable(
                    observation._evaluation_observation._invocation
                )

    def complete_policy_evaluation(
        self,
        observation: PolicyEvaluationObservation | None,
    ) -> None:
        """Close one evaluator record after its policy outcome is formed."""

        try:
            if not self._owns_evaluation(observation):
                return
            assert observation is not None
            state = observation._invocation._token._state
            evaluation = observation._evaluation
            with state.lock:
                if (
                    not _state_is_safe(state)
                    or not _evaluation_state_is_safe(evaluation)
                    or evaluation.completed
                    or evaluation not in state.evaluations
                ):
                    return
                evaluation.completed = True
        except Exception:
            if type(observation) is PolicyEvaluationObservation:
                self._mark_dispatch_unavailable(observation._invocation)

    def take(
        self,
        invocation: EffectivePolicyObservationInvocation | None,
        *,
        terminal_type: str,
        terminal_turn_id: str,
    ) -> RequestPolicyEffectiveTurnReceipt | None:
        """Take one receipt only after normal stream exhaustion."""

        try:
            if not self._owns(invocation):
                return None
            assert invocation is not None
            token = invocation._token
            state = token._state
            with state.lock:
                if not _state_is_safe(state):
                    return None
                receipt = state.receipt
                if (
                    state.status != "exhausted"
                    or state.invocation_sha256 != invocation.invocation_sha256
                    or state.terminal_type != terminal_type
                    or not is_valid_request_policy_effective_receipt(receipt)
                ):
                    return None
                assert type(receipt) is RequestPolicyEffectiveTurnReceipt
                if receipt.actual_turn_id_sha256 != _binding_sha256(
                    "turn_id", terminal_turn_id
                ):
                    return None
                state.status = "taken"
                return receipt
        except Exception:
            return None

    def take_dispatch(
        self,
        invocation: EffectivePolicyObservationInvocation | None,
        *,
        terminal_type: str,
        terminal_turn_id: str,
    ) -> RequestPolicyDispatchReceipt | None:
        """Take complete per-tool dispatch evidence once."""

        try:
            if not self._owns(invocation):
                return None
            assert invocation is not None
            state = invocation._token._state
            with state.lock:
                if (
                    not _state_is_safe(state)
                    or state.status not in {"exhausted", "taken"}
                    or state.terminal_type != terminal_type
                    or state.dispatch_taken
                    or state.dispatch_unavailable
                    or not state.evaluations
                    or not all(item.completed for item in state.evaluations)
                ):
                    return None
                effective = state.receipt
                if not is_valid_request_policy_effective_receipt(effective):
                    return None
                assert type(effective) is RequestPolicyEffectiveTurnReceipt
                if effective.actual_turn_id_sha256 != _binding_sha256(
                    "turn_id", terminal_turn_id
                ):
                    return None
                evaluation_rows = [
                    {
                        "evaluation_index": item.evaluation_index,
                        "tool_call_sha256": item.tool_call_sha256,
                        "entry_context_sha256": item.entry_context_sha256,
                        "middleware_rows": [
                            list(row) for row in item.middleware_rows
                        ],
                    }
                    for item in state.evaluations
                ]
                receipt = RequestPolicyDispatchReceipt(
                    schema_version=REQUEST_POLICY_DISPATCH_RECEIPT_SCHEMA_V1,
                    observer_sha256=effective.observer_sha256,
                    nonce_sha256=effective.nonce_sha256,
                    invocation_sha256=invocation.invocation_sha256,
                    effective_payload_sha256=(
                        effective.effective_payload_sha256
                    ),
                    evaluations_sha256=request_policy_payload_sha256(
                        {"evaluations": evaluation_rows}
                    ),
                    evaluation_count=len(evaluation_rows),
                    middleware_dispatch_count=sum(
                        len(item.middleware_rows) for item in state.evaluations
                    ),
                    all_contexts_matched=not state.dispatch_invalid,
                )
                state.dispatch_taken = True
                return receipt
        except Exception:
            return None

    def take_consumption(
        self,
        invocation: EffectivePolicyObservationInvocation | None,
        *,
        terminal_type: str,
        terminal_turn_id: str,
    ) -> RequestPolicyConsumptionReceipt | None:
        """Take complete default middleware consumption evidence once."""

        try:
            if not self._owns(invocation):
                return None
            assert invocation is not None
            state = invocation._token._state
            with state.lock:
                if (
                    not _state_is_safe(state)
                    or state.status not in {"exhausted", "taken"}
                    or state.terminal_type != terminal_type
                    or state.consumption_taken
                    or state.consumption_unavailable
                    or not state.consumptions
                    or not all(item.completed for item in state.consumptions)
                ):
                    return None
                effective = state.receipt
                if not is_valid_request_policy_effective_receipt(effective):
                    return None
                assert type(effective) is RequestPolicyEffectiveTurnReceipt
                if effective.actual_turn_id_sha256 != _binding_sha256(
                    "turn_id", terminal_turn_id
                ):
                    return None
                evaluation_rows = [
                    {
                        "evaluation_index": item.evaluation_index,
                        "tool_call_sha256": item.tool_call_sha256,
                        "entry_context_sha256": item.entry_context_sha256,
                        "middleware_rows": [
                            list(row) for row in item.middleware_rows
                        ],
                    }
                    for item in state.evaluations
                ]
                consumption_rows = [
                    {
                        "evaluation_index": item.evaluation_index,
                        "middleware_index": item.middleware_index,
                        "delivered_context_sha256": (
                            item.delivered_context_sha256
                        ),
                        "override_mode": item.override_mode,
                        "post_override_context_sha256": (
                            item.post_override_context_sha256
                        ),
                        "consumer_rows": [
                            list(row) for row in item.consumer_rows
                        ],
                        "outcome_action": item.outcome_action,
                    }
                    for item in state.consumptions
                ]
                guard_count = sum(
                    1
                    for item in state.consumptions
                    for stage, _phase, _sha256 in item.consumer_rows
                    if stage == "tool_guard"
                )
                default_middleware_sha256 = request_policy_payload_sha256(
                    {"middleware": _DEFAULT_POLICY_MIDDLEWARE_NAME}
                )
                default_middleware_dispatch_count = sum(
                    row[1] == default_middleware_sha256
                    for item in state.evaluations
                    for row in item.middleware_rows
                )
                receipt = RequestPolicyConsumptionReceipt(
                    schema_version=(
                        REQUEST_POLICY_CONSUMPTION_RECEIPT_SCHEMA_V1
                    ),
                    observer_sha256=effective.observer_sha256,
                    nonce_sha256=effective.nonce_sha256,
                    invocation_sha256=effective.invocation_sha256,
                    dispatch_evaluations_sha256=request_policy_payload_sha256(
                        {"evaluations": evaluation_rows}
                    ),
                    consumptions_sha256=request_policy_payload_sha256(
                        {"consumptions": consumption_rows}
                    ),
                    default_middleware_dispatch_count=(
                        default_middleware_dispatch_count
                    ),
                    consumption_count=len(consumption_rows),
                    live_permission_evaluation_count=sum(
                        item.override_mode != "not_reached"
                        for item in state.consumptions
                    ),
                    live_override_applied_count=sum(
                        item.override_mode == "applied"
                        for item in state.consumptions
                    ),
                    guard_consumption_count=guard_count,
                    all_context_chains_valid=not state.consumption_invalid,
                )
                state.consumption_taken = True
                return receipt
        except Exception:
            return None

    def _owns_consumption(self, observation: object) -> bool:
        if type(observation) is not PolicyConsumptionObservation:
            return False
        if (
            type(observation.evaluation_index) is not int
            or type(observation.middleware_index) is not int
            or not _valid_sha256(observation.invocation_sha256)
            or type(observation._evaluation_observation)
            is not PolicyEvaluationObservation
            or not _consumption_state_is_safe(observation._consumption)
            or not self._owns_evaluation(observation._evaluation_observation)
        ):
            return False
        return bool(
            observation.invocation_sha256
            == observation._evaluation_observation.invocation_sha256
            and observation.evaluation_index
            == observation._consumption.evaluation_index
            and observation.middleware_index
            == observation._consumption.middleware_index
        )

    def _owns_evaluation(self, observation: object) -> bool:
        if type(observation) is not PolicyEvaluationObservation:
            return False
        if (
            type(observation.evaluation_index) is not int
            or observation.evaluation_index < 0
            or not _valid_sha256(observation.invocation_sha256)
            or type(observation._invocation)
            is not EffectivePolicyObservationInvocation
            or not _evaluation_state_is_safe(observation._evaluation)
        ):
            return False
        return bool(
            observation.evaluation_index
            == observation._evaluation.evaluation_index
            and observation.invocation_sha256
            == observation._invocation.invocation_sha256
            and self._owns(observation._invocation)
        )

    def _mark_dispatch_unavailable(
        self,
        invocation: EffectivePolicyObservationInvocation | None,
    ) -> None:
        try:
            if not self._owns(invocation):
                return
            assert invocation is not None
            state = invocation._token._state
            if type(state.lock) is not _LOCK_TYPE:
                return
            with state.lock:
                if _state_is_safe(state):
                    state.dispatch_unavailable = True
        except Exception:
            return

    def _mark_consumption_unavailable(
        self,
        invocation: EffectivePolicyObservationInvocation | None,
    ) -> None:
        try:
            if not self._owns(invocation):
                return
            assert invocation is not None
            state = invocation._token._state
            if type(state.lock) is not _LOCK_TYPE:
                return
            with state.lock:
                if _state_is_safe(state):
                    state.consumption_unavailable = True
        except Exception:
            return

    def _owns(
        self,
        invocation: EffectivePolicyObservationInvocation | None,
    ) -> bool:
        if not _invocation_structure_is_safe(invocation):
            return False
        assert invocation is not None
        token = invocation._token
        if type(self._observer_nonce) is not str or not _valid_sha256(
            self._observer_sha256
        ):
            return False
        return bool(
            token._observer_nonce == self._observer_nonce
            and token.observer_sha256 == self._observer_sha256
            and token.nonce_sha256 == _binding_sha256("nonce", token._nonce)
        )


__all__ = [
    "EffectivePolicyObservationInvocation",
    "EffectivePolicyObservationToken",
    "PolicyConsumptionObservation",
    "PolicyEvaluationObservation",
    "REQUEST_POLICY_CONSUMPTION_RECEIPT_SCHEMA_V1",
    "REQUEST_POLICY_CONSTRUCTION_RECEIPT_SCHEMA_V1",
    "REQUEST_POLICY_DISPATCH_RECEIPT_SCHEMA_V1",
    "REQUEST_POLICY_EFFECTIVE_RECEIPT_SCHEMA_V1",
    "REQUEST_POLICY_OBSERVATION_TOKEN_SCHEMA_V1",
    "RequestPolicyConstructionReceipt",
    "RequestPolicyConsumptionReceipt",
    "RequestPolicyDispatchReceipt",
    "RequestPolicyEffectiveTurnReceipt",
    "RequestPolicyReceiptObserver",
    "capture_request_policy_construction_receipt",
    "claim_effective_policy_observation",
    "is_valid_request_policy_construction_receipt",
    "is_valid_request_policy_consumption_receipt",
    "is_valid_request_policy_dispatch_receipt",
    "is_valid_request_policy_effective_receipt",
    "mark_effective_policy_observation_exhausted",
    "project_request_policy_input",
    "project_runtime_request_context",
    "project_tool_policy_context",
    "request_policy_payload_is_deadline_free",
    "request_policy_payload_sha256",
]
