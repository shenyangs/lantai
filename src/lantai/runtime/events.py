"""Runtime event contracts owned by Lantai."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from .effects import RuntimeApprovalBinding

RUNTIME_EVENT_SCHEMA_VERSION = 1

RuntimeEventType = Literal[
    "turn.started",
    "turn.continue_requested",
    "turn.control_input.admitted",
    "message.started",
    "message.delta",
    "message.completed",
    "tool.call.started",
    "tool.call.delta",
    "tool.call.completed",
    "tool.started",
    "tool.completed",
    "approval.requested",
    "approval.resolved",
    "memory.updated",
    "plan.updated",
    "model.routing_decision",
    "runtime.budget_changed",
    "runtime.notification",
    "runtime.warning",
    "runtime.error",
    "turn.completed",
    "turn.interrupted",
    "turn.failed",
    "turn.stalled",
    "item.completed",
]
RuntimeEventStatus = Literal[
    "queued",
    "running",
    "completed",
    "finished",
    "interrupted",
    "failed",
    "stalled",
    "info",
]
RuntimeEventSource = Literal["user", "assistant", "tool", "system"]
RuntimePresentationSurface = Literal[
    "message",
    "tool",
    "subagent",
    "approval",
    "service",
    "debug",
]
RuntimePresentationVisibility = Literal[
    "visible",
    "hidden",
    "quiet",
    "prominent",
]
RuntimeRiskLevel = Literal["low", "medium", "high"]
RuntimeContinueMode = Literal["reconnect", "retry", "resume", "takeover"]
RuntimeBudgetChangeActor = Literal["user", "system", "policy", "runtime"]
RuntimeNotificationKind = Literal[
    "waiting_approval",
    "ambiguous_effect",
    "budget_exhausted",
    "final_complete",
]
RuntimeNotificationPriority = Literal["low", "normal", "high"]


class RuntimeEventPayloadValidationError(ValueError):
    """Raised when a registered RuntimeEvent payload fails validation."""

    def __init__(self, event_type: str, error: ValidationError) -> None:
        self.event_type = event_type
        self.error = error
        super().__init__(f"Invalid RuntimeEvent payload for {event_type}: {error}")


class RuntimeEventPayload(BaseModel):
    """Common bounded fields allowed in RuntimeEvent payload models."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    type: str | None = None
    legacy_kind: str | None = None
    legacy_runtime_type: str | None = None
    summary: str | None = None
    label: str | None = None
    detail: str | None = None
    phase: str | None = None
    trace_title: str | None = None
    trace_detail: str | None = None
    duration_ms: int | float | None = None
    engine: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    root_session_id: str | None = None
    chat_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None
    client_turn_id: str | None = None
    operation: str | None = None
    inbox_seq: int | None = None
    turn_id: str | None = None
    scope: str | None = None
    message_count: int | None = None
    updated_at: str | None = None
    elapsed_seconds: int | float | None = None
    threshold_seconds: int | float | None = None
    last_activity_at: str | None = None
    workspace_dir: str | None = None
    topic_workspace_dir: str | None = None
    manual_workspace_dir: str | None = None
    effective_workspace_dir: str | None = None
    binding_mode: str | None = None
    text_preview: str | None = None
    status: str | None = None
    final_task_status: str | None = None
    outcome_status: str | None = None
    delivery_status: str | None = None
    pending_turn_cleared: bool | None = None
    active_run_event_count: int | None = None
    active_run: bool | None = None
    reason: str | None = None
    checkpoint_id: str | None = None
    source_checkpoint_id: str | None = None
    source_seq: int | None = None
    target_seq: int | None = None
    replay_from_seq: int | None = None
    replay_to_seq: int | None = None
    command: str | None = None
    cwd: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    output_summary: str | None = None
    result_preview: str | None = None
    preview_url: str | None = None
    preview_harness: JsonValue | None = None
    browser_engine: str | None = None
    screenshot_path: str | None = None
    console_errors: JsonValue | None = None
    page_errors: JsonValue | None = None
    network_errors: JsonValue | None = None
    files_changed: JsonValue | None = None
    sub_run: JsonValue | None = None
    parent_run_id: str | None = None
    agent_name: str | None = None
    agent_role: str | None = None
    delegation_reason: str | None = None
    permission_profile: str | None = None
    requested_profile: str | None = None
    effective_profile: str | None = None
    requested_permission_mode: str | None = None
    effective_permission_mode: str | None = None
    inherited_context_summary: str | None = None
    current_tool: str | None = None
    result_summary: str | None = None
    failure_reason: str | None = None
    source_run_id: str | None = None
    source_agent_name: str | None = None
    policy_decision: JsonValue | None = None
    routing_decision: JsonValue | None = None
    guard: JsonValue | None = None
    approval_request_id: str | None = None
    remembered_approval: JsonValue | None = None
    distribution: str | None = None
    task_id: str | None = None
    invocation_id: str | None = None
    job_id: str | None = None
    job_name: str | None = None
    task_type: str | None = None
    channel: str | None = None
    target: str | None = None
    query_path: str | None = None
    final_status: str | None = None
    result_reason: str | None = None
    model: str | None = None
    prompt_preview: str | None = None
    quantity: int | None = None
    size: int | str | None = None
    quality: str | None = None
    asset_count: int | None = None
    remaining_points: int | float | None = None
    error_text: str | None = None
    change_type: str | None = None
    skill_name: str | None = None
    source: str | None = None
    details: JsonValue | None = None
    usage: JsonValue | None = None
    presentation: JsonValue | None = None
    failure_diagnosis: JsonValue | None = None
    redaction_version: int | None = None
    data_classification: str | None = None
    redactions: JsonValue | None = None
    truncations: JsonValue | None = None


class RuntimeTurnEventPayload(RuntimeEventPayload):
    """Payload for turn lifecycle and terminal events."""

    request: JsonValue | None = None
    task_contract: JsonValue | None = None
    turn_intelligence: JsonValue | None = None
    durable_artifact_required: bool | None = None
    error_type: str | None = None
    error_code: str | None = None
    error: JsonValue | None = None


class RuntimeMessageEventPayload(RuntimeEventPayload):
    """Payload for message stream events."""

    message_id: str | None = None
    answer_message_id: str | None = None
    source_message_id: str | None = None
    message: JsonValue | None = None
    delta: str | None = None
    text: str | None = None
    content_type: str | None = None
    stream_message_type: str | None = None
    message_type: str | None = None
    public_progress: JsonValue | None = None


class RuntimeTurnContinueRequestedPayload(RuntimeEventPayload):
    """Payload for an explicit continue/retry/resume/takeover request."""

    request_id: str
    mode: RuntimeContinueMode
    run_id: str | None = None
    requested_run_id: str | None = None
    requested_chat_id: str | None = None
    requested_session_id: str | None = None
    attempt_id: str | None = None
    requested_by: str | None = None
    reason: str = ""
    revision: int | None = Field(default=None, ge=0)
    checkpoint_id: str | None = None
    after_seq: int | None = Field(default=None, ge=0)
    latest_seq: int | None = Field(default=None, ge=0)
    stage_label: str | None = None


class RuntimeControlInputEventPayload(RuntimeEventPayload):
    """Payload for one durable control-input lifecycle observation."""

    command_id: str
    client_turn_id: str
    intent: str
    admission_status: str
    execution_status: str
    server_seq: int = Field(ge=1)
    next_waiting_reason: str | None = None
    replaces_command_id: str | None = None
    superseded_by_command_id: str | None = None
    model_request_id: str | None = None
    model_epoch: int | None = Field(default=None, ge=0)
    intent_revision: int | None = Field(default=None, ge=0)


class RuntimeToolEventPayload(RuntimeEventPayload):
    """Payload for tool call intent, execution, and result events."""

    name: str | None = None
    tool_name: str | None = None
    effect_id: str | None = None
    call_id: str | None = None
    tool_call_id: str | None = None
    tool_input: JsonValue | None = None
    arguments: JsonValue | None = None
    input: JsonValue | None = None
    argument_keys: list[str] | None = None
    arguments_redacted: bool | None = None
    output: JsonValue | None = None
    ok: bool | None = None
    error: JsonValue | None = None
    error_type: str | None = None
    error_code: str | None = None
    metadata: JsonValue | None = None
    collaboration: JsonValue | None = None
    artifact_effects: JsonValue | None = None
    canonical_tool: JsonValue | None = None


class RuntimeApprovalEventPayload(RuntimeToolEventPayload):
    """Payload for approval request and resolution events."""

    request_id: str | None = None
    approval_request_id: str | None = None
    decision: str | None = None
    severity: str | None = None
    findings_count: int | None = None
    approval_binding: RuntimeApprovalBinding | None = None
    rememberable: bool | None = None
    approval_options: list[str] | None = None
    permission_scope: str | None = None
    decision_scope: str | None = None


class RuntimeRoutingDecisionEventPayload(RuntimeEventPayload):
    """Payload for model/provider routing decisions."""

    route: str | None = None
    provider: str | None = None
    model: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    reasons: list[str] | None = None
    mode: str | None = None
    complexity: str | None = None
    previous_route_inherited: bool | None = None
    inherited_from_run_id: str | None = None
    estimated_context_tokens: int | float | None = None
    estimated_cost_points: int | float | None = None
    fallback_reason: str | None = None


class RuntimeBudgetChangedEventPayload(RuntimeEventPayload):
    """Payload for a structured Loop budget mutation event."""

    schema_version: Literal[1]
    contract_id: str
    previous_contract_hash: str
    next_contract_hash: str
    previous_budget_hash: str
    next_budget_hash: str
    changed_by: RuntimeBudgetChangeActor
    reason: str
    event_hash: str


class RuntimeNotificationEventPayload(RuntimeEventPayload):
    """Payload for user/operator notifications derived from Runtime state."""

    schema_version: Literal[1]
    notification_id: str
    notification_kind: RuntimeNotificationKind
    priority: RuntimeNotificationPriority = "normal"
    title: str
    message: str
    dedupe_key: str
    run_id: str
    attempt_id: str | None = None
    contract_id: str | None = None
    contract_hash: str | None = None
    source_status: str | None = None
    trigger_reason: str = ""
    export_recommended: bool = False
    action_url: str | None = None


class RuntimeWarningEventPayload(RuntimeEventPayload):
    """Payload for runtime warnings and non-terminal diagnostics."""

    kind: str | None = None
    client: str | None = None
    primary: str | None = None
    fallback: str | None = None
    error_type: str | None = None
    error_code: str | None = None
    error: JsonValue | None = None
    decision: str | None = None
    has_raw_payload: bool | None = None
    steer_text: str | None = None
    tool_names: list[str] | None = None


class RuntimeErrorEventPayload(RuntimeWarningEventPayload):
    """Payload for non-terminal runtime error diagnostics."""


class RuntimeItemCompletedEventPayload(RuntimeEventPayload):
    """Payload for completed artifacts, checkpoints, tasks, and projections."""

    title: str | None = None
    artifact_id: str | None = None
    checkpoint_id: str | None = None
    path: str | None = None
    url: str | None = None
    mime_type: str | None = None
    size: int | None = None
    size_bytes: int | None = None
    artifact_size_bytes: int | None = None
    text: str | None = None
    tool_name: str | None = None


RuntimeEventPayloadModel = type[RuntimeEventPayload]

RUNTIME_EVENT_PAYLOAD_MODELS: dict[str, RuntimeEventPayloadModel] = {
    "turn.started": RuntimeTurnEventPayload,
    "turn.continue_requested": RuntimeTurnContinueRequestedPayload,
    "turn.control_input.admitted": RuntimeControlInputEventPayload,
    "message.started": RuntimeMessageEventPayload,
    "message.delta": RuntimeMessageEventPayload,
    "message.completed": RuntimeMessageEventPayload,
    "tool.call.started": RuntimeToolEventPayload,
    "tool.call.delta": RuntimeToolEventPayload,
    "tool.call.completed": RuntimeToolEventPayload,
    "tool.started": RuntimeToolEventPayload,
    "tool.completed": RuntimeToolEventPayload,
    "approval.requested": RuntimeApprovalEventPayload,
    "approval.resolved": RuntimeApprovalEventPayload,
    "memory.updated": RuntimeItemCompletedEventPayload,
    "plan.updated": RuntimeItemCompletedEventPayload,
    "model.routing_decision": RuntimeRoutingDecisionEventPayload,
    "runtime.budget_changed": RuntimeBudgetChangedEventPayload,
    "runtime.notification": RuntimeNotificationEventPayload,
    "runtime.warning": RuntimeWarningEventPayload,
    "runtime.error": RuntimeErrorEventPayload,
    "turn.completed": RuntimeTurnEventPayload,
    "turn.interrupted": RuntimeTurnEventPayload,
    "turn.failed": RuntimeTurnEventPayload,
    "turn.stalled": RuntimeTurnEventPayload,
    "item.completed": RuntimeItemCompletedEventPayload,
}


class RuntimeUsage(BaseModel):
    """Normalized token and entitlement usage for one event or turn."""

    input_tokens: int | float = 0
    output_tokens: int | float = 0
    cache_tokens: int | float | None = None
    context_tokens: int | float = 0
    cost_points: int | float = 0


class RuntimePresentation(BaseModel):
    """Renderer hint carried with tool-like runtime events."""

    visibility: RuntimePresentationVisibility = "visible"
    surface: RuntimePresentationSurface = "message"
    risk_level: RuntimeRiskLevel = "low"
    display_name: str = ""
    summary: str = ""


class RuntimeEvent(BaseModel):
    """Stable event row emitted by Lantai Runtime adapters."""

    schema_version: Literal[1] = RUNTIME_EVENT_SCHEMA_VERSION
    id: str
    seq: int = 0
    type: RuntimeEventType
    status: RuntimeEventStatus | None = None
    source: RuntimeEventSource = "system"
    session_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    usage: RuntimeUsage | None = None
    presentation: RuntimePresentation | None = None
    created_at: str | None = None

    def model_post_init(self, __context: Any, /) -> None:
        validate_runtime_event_payload(self.type, self.payload)


def runtime_event_payload_model_for_type(
    event_type: str,
) -> RuntimeEventPayloadModel | None:
    """Return the strict payload model registered for a RuntimeEvent type."""

    return RUNTIME_EVENT_PAYLOAD_MODELS.get(event_type)


def validate_runtime_event_payload(
    event_type: str,
    payload: Mapping[str, Any] | None,
) -> RuntimeEventPayload | None:
    """Validate registered payloads while legacy unregistered types pass through."""

    payload_model = runtime_event_payload_model_for_type(event_type)
    if payload_model is None:
        return None
    try:
        return payload_model.model_validate(payload or {})
    except ValidationError as exc:
        raise RuntimeEventPayloadValidationError(event_type, exc) from exc


class RuntimeEventContractViolation(BaseModel):
    """One RuntimeEvent sequence invariant violation."""

    code: str
    message: str
    event_id: str = ""
    seq: int | None = None
    item_id: str = ""
    turn_id: str = ""


_TERMINAL_TYPES = {
    "turn.completed",
    "turn.failed",
    "turn.interrupted",
    "turn.stalled",
}
_SIDE_EFFECT_OR_PUBLIC_TYPES = {
    "turn.control_input.admitted",
    "message.started",
    "message.delta",
    "message.completed",
    "tool.call.started",
    "tool.call.delta",
    "tool.call.completed",
    "tool.started",
    "tool.completed",
    "approval.requested",
    "approval.resolved",
    "memory.updated",
    "plan.updated",
    "runtime.budget_changed",
}
_TOOL_STARTED_TYPES = {"tool.call.started", "tool.started"}
_TOOL_COMPLETED_TYPES = {"tool.call.completed", "tool.completed"}


def _event_item_key(event: RuntimeEvent) -> str:
    payload = event.payload or {}
    for value in (
        event.item_id,
        payload.get("id"),
        payload.get("call_id"),
        payload.get("tool_call_id"),
    ):
        if value:
            return str(value)
    return ""


def _approval_request_key(event: RuntimeEvent) -> str:
    payload = event.payload or {}
    for value in (
        payload.get("request_id"),
        payload.get("approval_request_id"),
        event.item_id,
    ):
        if value:
            return str(value)
    return ""


class RuntimeEventSequenceValidator:
    """Validate one turn's RuntimeEvent stream before it becomes a contract."""

    def __init__(self, *, allow_legacy_tool_completed: bool = False) -> None:
        self.allow_legacy_tool_completed = allow_legacy_tool_completed

    def validate(
        self,
        events: Iterable[RuntimeEvent],
    ) -> list[RuntimeEventContractViolation]:
        violations: list[RuntimeEventContractViolation] = []
        seen_ids: set[str] = set()
        seen_turn_ids: set[str] = set()
        open_tools: dict[str, RuntimeEvent] = {}
        open_approvals: dict[str, RuntimeEvent] = {}
        last_seq: int | None = None
        terminal_event: RuntimeEvent | None = None

        for event in events:
            if event.id in seen_ids:
                violations.append(
                    self._violation(
                        "duplicate_event_id",
                        f"RuntimeEvent id is not unique: {event.id}",
                        event,
                    )
                )
            seen_ids.add(event.id)

            if last_seq is not None and event.seq <= last_seq:
                violations.append(
                    self._violation(
                        "non_increasing_seq",
                        "RuntimeEvent seq must be strictly increasing.",
                        event,
                    )
                )
            last_seq = event.seq

            if event.turn_id:
                seen_turn_ids.add(event.turn_id)
                if len(seen_turn_ids) > 1:
                    violations.append(
                        self._violation(
                            "mixed_turn_id",
                            "RuntimeEvent sequence must not mix turn_id values.",
                            event,
                        )
                    )

            if terminal_event and event.type in _SIDE_EFFECT_OR_PUBLIC_TYPES:
                violations.append(
                    self._violation(
                        "side_effect_after_terminal",
                        "RuntimeEvent sequence emitted work after a terminal event.",
                        event,
                    )
                )

            if event.type in _TERMINAL_TYPES:
                if terminal_event is not None:
                    violations.append(
                        self._violation(
                            "multiple_terminal_events",
                            "RuntimeEvent sequence must contain at most one terminal event.",
                            event,
                        )
                    )
                terminal_event = event

            if event.type in _TOOL_STARTED_TYPES:
                key = _event_item_key(event)
                arguments = event.payload.get("arguments", event.payload.get("input"))
                if arguments is not None and not isinstance(arguments, dict):
                    violations.append(
                        self._violation(
                            "tool_arguments_not_object",
                            "Runtime tool call arguments must be an object.",
                            event,
                        )
                    )
                if not key:
                    violations.append(
                        self._violation(
                            "tool_call_missing_id",
                            "Runtime tool call start is missing an item id.",
                            event,
                        )
                    )
                elif key in open_tools:
                    violations.append(
                        self._violation(
                            "duplicate_open_tool_call",
                            f"Runtime tool call already started: {key}",
                            event,
                        )
                    )
                else:
                    open_tools[key] = event

            if event.type in _TOOL_COMPLETED_TYPES:
                key = _event_item_key(event)
                if (
                    event.type == "tool.completed"
                    and not self.allow_legacy_tool_completed
                ):
                    violations.append(
                        self._violation(
                            "legacy_tool_completed_type",
                            "RuntimeEvent should use tool.call.completed as the canonical tool completion type.",
                            event,
                        )
                    )
                if not key:
                    violations.append(
                        self._violation(
                            "tool_completion_missing_id",
                            "Runtime tool completion is missing an item id.",
                            event,
                        )
                    )
                elif key not in open_tools:
                    violations.append(
                        self._violation(
                            "tool_completed_without_start",
                            f"Runtime tool completion has no matching start: {key}",
                            event,
                        )
                    )
                else:
                    open_tools.pop(key, None)

            if event.type == "approval.requested":
                key = _approval_request_key(event)
                if not key:
                    violations.append(
                        self._violation(
                            "approval_request_missing_id",
                            "Runtime approval request is missing a request id.",
                            event,
                        )
                    )
                else:
                    open_approvals[key] = event

            if event.type == "approval.resolved":
                key = _approval_request_key(event)
                if not key:
                    violations.append(
                        self._violation(
                            "approval_resolution_missing_id",
                            "Runtime approval resolution is missing a request id.",
                            event,
                        )
                    )
                elif key not in open_approvals:
                    violations.append(
                        self._violation(
                            "approval_resolved_without_request",
                            f"Runtime approval resolution has no matching request: {key}",
                            event,
                        )
                    )
                else:
                    open_approvals.pop(key, None)

        for key, event in open_tools.items():
            violations.append(
                self._violation(
                    "tool_started_without_completion",
                    f"Runtime tool call did not complete: {key}",
                    event,
                )
            )
        for key, event in open_approvals.items():
            violations.append(
                self._violation(
                    "approval_requested_without_resolution",
                    f"Runtime approval request did not resolve: {key}",
                    event,
                )
            )
        return violations

    @staticmethod
    def _violation(
        code: str,
        message: str,
        event: RuntimeEvent,
    ) -> RuntimeEventContractViolation:
        return RuntimeEventContractViolation(
            code=code,
            message=message,
            event_id=event.id,
            seq=event.seq,
            item_id=event.item_id or _event_item_key(event),
            turn_id=event.turn_id or "",
        )


def validate_runtime_event_sequence(
    events: Iterable[RuntimeEvent],
    *,
    allow_legacy_tool_completed: bool = False,
) -> list[RuntimeEventContractViolation]:
    """Return RuntimeEvent contract violations for one logical turn."""

    return RuntimeEventSequenceValidator(
        allow_legacy_tool_completed=allow_legacy_tool_completed,
    ).validate(events)
