# -*- coding: utf-8 -*-
"""Native Lantai runtime adapter for ordinary chat turns."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse
from uuid import uuid4

from jsonschema.validators import validator_for
from pydantic import ValidationError

from ...agents.memory_usage_trace import record_delivered_persona_memory
from ...agents.persona.perceptible_presence import (
    PersonaTurnPlan,
    evaluate_persona_delivery,
)
from ...agents.persona.post_answer_reflection import (
    PostAnswerReflectionRequest,
)
from ...app.experience.presentation import tool_presentation_for_tool_name
from ...providers.retry_chat_model import _is_retryable
from ...security.tool_guard.approval import ApprovalDecision
from ...utils.async_iterator import DelegatedAsyncIterator
from ...utils.async_settlement import await_settled
from ...security.tool_guard.models import (
    GuardFinding,
    GuardSeverity,
    GuardThreatCategory,
    ToolGuardResult,
)
from ..assistant_artifact_projection import (
    AssistantArtifactProjectionAccumulator,
)
from ..analysis_context_projection import (
    analysis_scope_from_runtime_context,
    current_analysis_context_message,
    is_builtin_analysis_continuity_tool,
)
from ..artifact_effects import (
    RuntimeArtifactEffectJournal,
    artifact_effect_public_payload,
    project_committed_primary_delivery,
    recover_artifact_delivery,
    register_artifact_candidate,
)
from ..events import RuntimeEvent, RuntimePresentation, RuntimeUsage
from ..effects import (
    RuntimeApprovalBinding,
    RuntimeEffectJournal,
    RuntimeEffectJournalError,
    RuntimeEffectRecoveryCapability,
)
from ..coherence import parse_or_build_tool_envelope
from ..cost_accounting import RuntimeBudgetAdmissionError
from ..convergence import (
    ConvergenceController,
    ConvergenceDecision,
    build_convergence_review_prompt,
    convergence_mode,
    parse_convergence_review,
    policy_for_work_method,
)
from ..continuation import (
    build_continuation_capsule,
    continuation_input_refs,
    continuation_source_messages,
    merge_verified_milestones,
    model_context_profile_for_client,
    rollover_model_context,
    snapshot_continuation_artifacts,
    verified_milestones_from_tool_result,
)
from ..capability_catalog import (
    PROGRESSIVE_TOOL_GROUPS,
    progressive_tool_group_for,
)
from ..errors import runtime_exception_message as _exception_message
from ..file_delivery import (
    FILE_DELIVERY_TOOL_NAME,
    expected_file_type_label,
    file_path_matches_extensions,
    has_matching_file_delivery_block,
    resolve_workspace_deliverable_path,
    select_archive_member_delivery_paths,
    select_generated_delivery_paths,
    snapshot_generated_files,
)
from ..input import RuntimeInput, RuntimeRequestContext
from ..inbox_admission import (
    RuntimeInboxAdmission,
    RuntimeInboxClaim,
    RuntimeInboxConflict,
)
from ..hosted_collaboration import close_public_room_snapshot
from ..messages import ContentBlock, RuntimeMessage
from ..native_tool_effects import NativeToolEffectMixin
from ..native_model_projection import NativeModelProjectionMixin
from ..mcp_tools import mcp_structured_result_metadata, project_mcp_tool_spec
from ..policy_chain import evaluate_policy_chain
from ..model_messages import model_messages_from_runtime_messages
from ..model_round import (
    ModelRoundAccumulator,
    ModelRoundProtocolError as _ModelProtocolError,
)
from ..next_model_round import (
    capture_model_round_lineage,
    prepare_next_model_round,
)
from ..progressive_tools import transition_progressive_tool_groups
from ..private_editor_completion import PrivateEditorCompletionFence
from ..public_reasoning import (
    PublicReasoningProjector,
    has_degenerate_placeholder_flood,
    prompt_leak_guard_for_runtime_input,
    without_runtime_public_progress_claims,
)
from ..request_policy_receipt import (
    EffectivePolicyObservationInvocation,
    RequestPolicyConstructionReceipt,
    RequestPolicyReceiptObserver,
)
from ..recovery_policy import (
    ModelRoundRecoveryState,
    decide_model_round_recovery,
)
from ..runtime_contract_messages import (
    model_output_recovery_message,
    model_stream_idle_recovery_message,
    primary_turn_ownership_message,
)
from ..session_turn_lifecycle import SessionTurnLifecycle
from ..models import (
    LantaiModelClient,
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
)
from ..tools import (
    LantaiToolRegistry,
    McpClientSpec,
    ToolCall,
    ToolPolicyContext,
    ToolPolicyOutcome,
    ToolResult,
    ToolSpec,
)
from ..turn_control import RuntimeInputConflictError, RuntimeTurnControlStore
from ..tool_transaction import (
    ToolArgumentRepairBudget,
    ToolTransaction,
    ToolTransactionRuntimePortMixin,
    close_owned_async_iterator,
    execute_owned_sync,
    validate_tool_call_batch,
)
from ..tool_public_progress import (
    model_tool_public_progress,
    tool_specs_with_public_progress_contract,
    without_public_progress_argument,
    without_public_progress_block_data,
)
from ..work_method_resolution import (
    collaboration_is_required,
    durable_artifact_required_from_metadata,
)
from ..runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeCheckpoint,
    RuntimeCheckpointExecutionPosition,
    RuntimeContinuationCapsule,
    RuntimeContinuationMilestone,
    RuntimeEffectRecord,
    RuntimePendingToolCall,
    RuntimeRunOwner,
    RuntimeRunReducerAction,
    RuntimeRunStatus,
    RuntimeRunTransitionError,
    RuntimeTaskContract,
    RuntimeWaitReason,
    reduce_runtime_run,
    revise_chat_task_contract,
    task_contract_effective_objective,
)
from ..store import (
    RuntimeRunStore,
    RuntimeRunStoreConflictError,
    RuntimeRunStoreError,
)
from ..task_revision import persist_runtime_task_command

from ..native_turn_support import (
    logger,
    ModelClientFactory,
    _resumed_pending_tool_model_events,
    _model_message_transaction_tail,
    DEFAULT_NATIVE_TURN_TIMEOUT_SECONDS,
    DEFAULT_NATIVE_MODEL_FIRST_EVENT_TIMEOUT_SECONDS,
    DEFAULT_NATIVE_MODEL_IDLE_TIMEOUT_SECONDS,
    DEFAULT_NATIVE_MODEL_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_NATIVE_MODEL_STREAM_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_NATIVE_TOOL_TIMEOUT_SECONDS,
    DEFAULT_NATIVE_TOOL_HEARTBEAT_INTERVAL_SECONDS,
    _CONVERGENCE_REVIEW_TIMEOUT_SECONDS,
    _MAIN_RESPONSE_MAX_TOKENS,
    _COLLABORATION_STAFFING_MAX_TOKENS,
    _COLLABORATION_STAFFING_SYSTEM_TEXT,
    _COLLABORATION_MEMBER_SYSTEM_TEXT,
    _DEEP_RESEARCH_SYSTEM_TEXT,
    _MIN_RESPONSE_MAX_TOKENS,
    _MAIN_OUTPUT_RECOVERY_LIMIT,
    _MAIN_STREAM_IDLE_RECOVERY_LIMIT,
    _QUQU_LINE_RE,
    _FENCED_CODE_RE,
    _PROGRESSIVE_TOOL_NAME,
    _PROGRESSIVE_TOOL_GROUPS,
    _DECLARE_DELIVERABLE_CONTRACT_TOOL,
    _RESOLVE_PENDING_DELIVERABLE_TOOL,
    _SUBAGENT_SUBMIT_TOOL,
    _SUBAGENT_CHECK_TOOL,
    _SUBAGENT_FOREGROUND_TOOL,
    _SUBAGENT_BATCH_TOOL,
    _SAFE_IDENTITY_TOOLS,
    _SUBAGENT_TASK_ID_RE,
    _SUBAGENT_STATUS_RE,
    _TERMINAL_SUBAGENT_STATUSES,
    _TURN_DEADLINE_KEYS,
    _TURN_TIMEOUT_KEYS,
    _MODEL_IDLE_TIMEOUT_KEYS,
    _MODEL_FIRST_EVENT_TIMEOUT_KEYS,
    _MODEL_REQUEST_TIMEOUT_KEYS,
    _TOOL_TIMEOUT_KEYS,
    _APPROVAL_TIMEOUT_KEYS,
    _TurnDeadlineExceeded,
    _ModelStreamDeadlineExceeded,
    _ModelStreamIdleInterruption,
    _ModelStreamControlInterruption,
    _ProviderModelStreamTimeout,
    _project_model_stream_boundaries,
    _ToolSteered,
    _ToolArgumentRepairExhausted,
    _IncompleteModelOutput,
    _MissingVisibleFinalAnswer,
    _DegenerateModelOutput,
    _TaskBudgetExceeded,
    _RepeatedToolFailure,
    _RequiredCapabilityFailed,
    _RuntimeDurabilityBlocked,
    _progressive_tool_group,
    _progressive_group_description,
    _progressive_loader_spec,
    _tools_for_turn_intelligence,
    _model_classified_durable_artifact_turn,
    _explicit_model_multimodal_support,
    _is_visual_creation_request,
    _pptx_skill_output_candidate,
    _file_fingerprint,
    _tool_declared_delivery_candidates,
    _model_declared_generated_paths,
    _coerce_non_negative_float,
    _first_float,
    _timeout_from_metadata,
    _runtime_metadata,
    _main_response_max_tokens,
    _internal_execution_messages,
    _collaboration_staffing_messages,
    _collaboration_member_messages,
    _deep_research_messages,
    _runtime_event_has_visible_completed_message,
    _event_with_persona_delivery,
    _event_with_outcome_status,
    _synthesis_boundary_is_incomplete,
    _latest_user_text,
    _prose_requests_hosted_collaboration,
    _prose_requests_roundtable,
    _required_collaboration_contract_message,
    _primary_turn_ownership_message,
    _pending_deliverable_context_message,
    _model_output_recovery_message,
    _model_stream_idle_recovery_message,
    _extract_ququ_line,
    _ququ_line_fingerprint,
    _ququ_fingerprints_overlap,
    _ququ_repeats_history,
    _subagent_task_id,
    _subagent_status,
    _runtime_state_workspace_dir,
    _stable_sha256,
    _runtime_release_edition,
    _runtime_owner,
    _touch_runtime_run_heartbeat,
    _runtime_run_heartbeat_loop,
    _start_runtime_run_heartbeat,
    _cancel_runtime_run_heartbeat,
    _approval_binding_for_tool_call,
    _DeadlineBudget,
    _runtime_usage,
    _without_empty_values,
    _turn_started_payload,
    _tool_argument_snapshot,
    _safe_collaboration_arguments,
    _terminal_collaboration_snapshot,
    _collaboration_failure_metadata,
    _tool_call_public_payload,
    _public_tool_result_metadata,
    _public_guard_payload,
    _json_safe,
    _block_to_tool_output,
    _normalise_tool_result,
    _tool_result_text,
    _tool_result_output,
    _tool_result_payload,
    _tool_failure_fingerprint,
    _tool_result_error_code,
    _tool_call_fingerprint,
    _runtime_message_from_model_response,
    _model_response_has_output,
    _model_response_has_visible_output,
    _validate_tool_call_arguments,
    _normalize_tool_call_argument_aliases,
    _message_from_delta,
    _tool_preamble_public_progress_event,
    _presentation,
    _agent_presentation_context,
    _tool_payload_with_agent_presentation_context,
    _synthetic_guard_result,
)


def _is_retryable_model_stream_transport_error(exc: Exception) -> bool:
    """Return whether a provider stream failed through a transient transport.

    Provider SDKs frequently wrap the underlying httpx exception.  Walk only
    the explicit exception chain and reuse the same bounded retry taxonomy as
    the model transport wrapper; arbitrary model/protocol failures must still
    fail the Run instead of being silently replayed.
    """

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, Exception) and _is_retryable(current):
            return True
        for nested in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


class _ModelRequestAdmissionDenied(RuntimeError):
    """Stop a runtime quietly when a dynamic model boundary closes."""


class _ContinuationSourceUnavailable(RuntimeError):
    """Stop rather than silently losing or replaying a structured input."""

    def __init__(self, conflict: RuntimeInboxConflict) -> None:
        super().__init__(conflict.reason)
        self.conflict = conflict


_MODEL_REQUEST_TIMEOUT_UNSET = object()
_MODEL_LIVENESS_POLICY_VERSION = "request-liveness-2026-09-21"


class NativeLantaiRuntime(NativeToolEffectMixin, NativeModelProjectionMixin, ToolTransactionRuntimePortMixin):
    """Minimal ReAct loop implemented on Lantai-owned interfaces."""

    def __init__(
        self,
        *,
        model_client_factory: ModelClientFactory,
        tool_registry: LantaiToolRegistry,
        tool_policy_context: ToolPolicyContext | None = None,
        approval_service: Any | None = None,
        max_iters: int | None = None,
        turn_timeout_seconds: float | None = None,
        model_first_event_timeout_seconds: float
        | None = (DEFAULT_NATIVE_MODEL_FIRST_EVENT_TIMEOUT_SECONDS),
        model_idle_timeout_seconds: float
        | None = DEFAULT_NATIVE_MODEL_IDLE_TIMEOUT_SECONDS,
        model_request_timeout_seconds: Any = _MODEL_REQUEST_TIMEOUT_UNSET,
        model_stream_close_timeout_seconds: float
        | None = (DEFAULT_NATIVE_MODEL_STREAM_CLOSE_TIMEOUT_SECONDS),
        tool_timeout_seconds: float
        | None = DEFAULT_NATIVE_TOOL_TIMEOUT_SECONDS,
        effect_journal: RuntimeEffectJournal | None = None,
        artifact_effect_journal: RuntimeArtifactEffectJournal | None = None,
        progressive_tools: bool = False,
        protected_write_admission: Callable[[str], str | None] | None = None,
        model_request_admission: Callable[[], str | None] | None = None,
    ) -> None:
        self._model_client_factory = model_client_factory
        self._registry = tool_registry
        self._base_policy_context = (
            tool_policy_context.model_dump(mode="python")
            if tool_policy_context is not None
            else ToolPolicyContext().model_dump(mode="python")
        )
        self._approval_service = approval_service
        self._max_iters = None if max_iters is None else max(1, max_iters)
        self._turn_timeout_seconds = _coerce_non_negative_float(
            turn_timeout_seconds,
        )
        self.request_policy_observer = RequestPolicyReceiptObserver(
            self._base_policy_context,
            default_turn_timeout_seconds=self._turn_timeout_seconds,
        )
        self._model_first_event_timeout_seconds = _coerce_non_negative_float(
            model_first_event_timeout_seconds,
        )
        self._model_idle_timeout_seconds = _coerce_non_negative_float(
            model_idle_timeout_seconds,
        )
        if model_request_timeout_seconds is _MODEL_REQUEST_TIMEOUT_UNSET:
            self._model_request_timeout_seconds = (
                DEFAULT_NATIVE_MODEL_REQUEST_TIMEOUT_SECONDS
            )
            self._model_request_timeout_origin = "version_default"
        elif model_request_timeout_seconds is None:
            self._model_request_timeout_seconds = None
            self._model_request_timeout_origin = "explicit_none"
        else:
            if isinstance(model_request_timeout_seconds, bool):
                parsed_request_timeout = None
            else:
                try:
                    parsed_request_timeout = float(
                        model_request_timeout_seconds
                    )
                except (TypeError, ValueError):
                    parsed_request_timeout = None
            if (
                parsed_request_timeout is None
                or not math.isfinite(parsed_request_timeout)
                or parsed_request_timeout <= 0
            ):
                raise ValueError(
                    "model_request_timeout_seconds must be a positive finite "
                    "number or None"
                )
            self._model_request_timeout_seconds = parsed_request_timeout
            self._model_request_timeout_origin = "explicit_positive"
        self._model_stream_close_timeout_seconds = (
            _coerce_non_negative_float(model_stream_close_timeout_seconds)
            or DEFAULT_NATIVE_MODEL_STREAM_CLOSE_TIMEOUT_SECONDS
        )
        self._tool_timeout_seconds = _coerce_non_negative_float(
            tool_timeout_seconds,
        )
        self._progressive_tools = bool(progressive_tools)
        self._equipped_tool_groups: set[str] = set()
        self._effect_journal = effect_journal
        self._artifact_effect_journal = artifact_effect_journal
        self._protected_write_admission = protected_write_admission
        self._model_request_admission = model_request_admission
        self._post_answer_reflection_request: (
            PostAnswerReflectionRequest | None
        ) = None

    @property
    def progressive_tools_enabled(self) -> bool:
        return self._progressive_tools

    @property
    def equipped_tool_groups(self) -> set[str]:
        return self._equipped_tool_groups

    def model_request_policy_receipt(self) -> dict[str, Any]:
        """Return the trusted effective model-stream liveness policy."""

        return {
            "policy_version": _MODEL_LIVENESS_POLICY_VERSION,
            "model_request_total": {
                "timeout_seconds": self._model_request_timeout_seconds,
                "origin": self._model_request_timeout_origin,
            },
            "first_event_timeout_seconds": (
                self._model_first_event_timeout_seconds
            ),
            "idle_timeout_seconds": self._model_idle_timeout_seconds,
            "stream_close_timeout_seconds": (
                self._model_stream_close_timeout_seconds
            ),
            "safe_current_round_recovery": {
                "max_attempts": _MAIN_STREAM_IDLE_RECOVERY_LIMIT,
                "reasons": [
                    "first_event_timeout",
                    "stream_idle_timeout",
                ],
                "recovered_dispatch_max_retries": 0,
                "partial_output_commit": False,
            },
        }

    @equipped_tool_groups.setter
    def equipped_tool_groups(self, value: set[str]) -> None:
        self._equipped_tool_groups = value

    @property
    def tool_registry(self) -> LantaiToolRegistry:
        return self._registry

    @property
    def approval_service(self) -> Any | None:
        return self._approval_service

    @property
    def artifact_effect_journal(
        self,
    ) -> RuntimeArtifactEffectJournal | None:
        return self._artifact_effect_journal

    @property
    def tool_timeout_seconds(self) -> float | None:
        return self._tool_timeout_seconds

    @property
    def runtime_heartbeat_interval_seconds(self) -> float:
        return DEFAULT_NATIVE_TOOL_HEARTBEAT_INTERVAL_SECONDS

    def protected_write_denial_reason(self, capability: str) -> str | None:
        """Check the cached release entitlement before an external write."""

        if self._protected_write_admission is None:
            return None
        return self._protected_write_admission(capability)

    def _require_model_request_admission(self) -> None:
        if self._model_request_admission is None:
            return
        denial_reason = self._model_request_admission()
        if denial_reason is not None:
            raise _ModelRequestAdmissionDenied(denial_reason)

    def request_policy_construction_receipt(
        self,
    ) -> RequestPolicyConstructionReceipt | None:
        """Return immutable construction-time evidence, when safely available."""

        return self.request_policy_observer.construction_receipt()

    def take_post_answer_reflection_request(
        self,
    ) -> PostAnswerReflectionRequest | None:
        """Transfer the one-shot sidecar request to the application runner."""

        request = self._post_answer_reflection_request
        self._post_answer_reflection_request = None
        return request

    async def run_turn(
        self,
        runtime_input: RuntimeInput,
        *,
        request_policy_observation_invocation: (
            EffectivePolicyObservationInvocation | None
        ) = None,
    ) -> AsyncIterator[RuntimeEvent]:
        # Progressive capabilities are scoped to one user turn. The model may
        # accumulate groups within this run, but a later user message starts
        # from the compact loader again.
        self._equipped_tool_groups.clear()
        self._post_answer_reflection_request = None
        request = runtime_input.request
        turn_id = request.run_id or str(uuid4())
        turn_lifecycle = SessionTurnLifecycle(
            session_id=request.session_id,
            turn_id=turn_id,
        )
        budget = _DeadlineBudget.from_runtime_input(
            runtime_input,
            default_timeout_seconds=self._turn_timeout_seconds,
        )
        runtime_metadata = _runtime_metadata(runtime_input)
        persona_turn_plan = PersonaTurnPlan.from_dict(
            runtime_metadata.get("persona_turn_plan"),
        )
        persona_delivery_required = bool(
            persona_turn_plan is not None and persona_turn_plan.require_visible
        )
        runtime_budget = runtime_metadata.get("runtime_budget")
        budget_values = (
            runtime_budget if isinstance(runtime_budget, dict) else {}
        )

        def budget_int(name: str) -> int | None:
            value = budget_values.get(name)
            if value is None or isinstance(value, bool):
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        # Resource ceilings persisted by older releases are compatibility
        # data, not executable policy. Healthy work must not be truncated by
        # aggregate rounds, tool calls, or tokens.
        max_model_rounds = None
        max_tool_calls = None
        max_tokens = None
        max_no_progress_rounds = budget_int("max_no_progress_rounds")
        max_same_failure_count = budget_int("max_same_failure_count") or 3
        checkpoint_metadata = runtime_metadata.get("runtime_checkpoint")
        checkpoint_values = (
            checkpoint_metadata
            if isinstance(checkpoint_metadata, dict)
            else {}
        )
        model_round_count = int(checkpoint_values.get("model_rounds") or 0)
        model_epoch = int(checkpoint_values.get("model_epoch") or 0)
        intent_revision = int(checkpoint_values.get("intent_revision") or 0)
        tool_call_count = int(checkpoint_values.get("tool_calls") or 0)
        used_tokens = int(checkpoint_values.get("used_tokens") or 0)
        no_progress_rounds = int(
            checkpoint_values.get("no_progress_rounds") or 0
        )
        failure_counts: dict[str, int] = {
            str(key): max(0, int(value))
            for key, value in checkpoint_values.get("failure_fingerprints")
            or ()
        }
        state_workspace_dir = str(
            runtime_metadata.get("state_workspace_dir")
            or request.workspace_dir
            or ""
        ).strip()
        expected_attempt_id = str(
            runtime_metadata.get("attempt_id") or ""
        ).strip()
        expected_lease_owner = str(
            runtime_metadata.get("runtime_lease_owner") or ""
        ).strip()
        canonical_run_available = False
        canonical_run_load_error: BaseException | None = None
        if state_workspace_dir and turn_id:
            try:
                candidate_run = await RuntimeRunStore(
                    state_workspace_dir
                ).load_contract(turn_id)
            except (RuntimeRunStoreError, OSError, ValueError) as exc:
                candidate_run = None
                canonical_run_load_error = exc
            if candidate_run is not None and (
                not expected_attempt_id
                or candidate_run.attempt_id == expected_attempt_id
            ):
                canonical_run_available = True
                runtime_metadata["attempt_id"] = candidate_run.attempt_id
                runtime_metadata["runtime_revision"] = candidate_run.revision
                runtime_metadata[
                    "runtime_lease_owner"
                ] = candidate_run.lease_owner
                if candidate_run.task_contract is not None:
                    runtime_metadata[
                        "task_contract"
                    ] = candidate_run.task_contract.model_dump(mode="json")
        turn_control_store = (
            RuntimeTurnControlStore(state_workspace_dir)
            if state_workspace_dir and turn_id
            else None
        )
        inbox_admission = (
            RuntimeInboxAdmission(
                store=turn_control_store,
                run_id=turn_id,
                runtime_metadata=runtime_metadata,
            )
            if turn_control_store is not None
            else None
        )
        inbox_after_seq = int(checkpoint_values.get("inbox_after_seq") or 0)
        accepted_steer_ids: set[str] = {
            str(value)
            for value in checkpoint_values.get("accepted_steer_ids") or ()
            if str(value).strip()
        }
        previous_active_seconds = float(
            checkpoint_values.get("active_seconds") or 0.0
        )
        run_started_monotonic = asyncio.get_running_loop().time()
        checkpoint_last_monotonic = run_started_monotonic
        checkpoint_last_tool_count = tool_call_count
        checkpoint_durability_confirmed = False
        task_contract_metadata = runtime_metadata.get("task_contract")
        task_contract: RuntimeTaskContract | None = None
        if isinstance(task_contract_metadata, dict):
            try:
                task_contract = RuntimeTaskContract.model_validate(
                    task_contract_metadata
                )
            except ValidationError:
                # Older intake metadata used the same key for lightweight
                # artifact labels.  Those labels are compatibility hints,
                # not a durable TaskContract and must not acquire authority
                # merely because steer support now understands the full
                # contract shape.
                task_contract = None
        collaboration_required = collaboration_is_required(runtime_input)
        prose_collaboration_requested = (
            not collaboration_required
            and _prose_requests_hosted_collaboration(runtime_input)
        )
        prose_roundtable_requested = _prose_requests_roundtable(runtime_input)
        current_client_turn_id = str(
            runtime_metadata.get("client_turn_id")
            or (
                task_contract_metadata.get("client_turn_id")
                if isinstance(task_contract_metadata, dict)
                else ""
            )
            or ""
        ).strip()
        checkpoint_policy = (
            task_contract_metadata.get("checkpoint_policy")
            if isinstance(task_contract_metadata, dict)
            else None
        )
        checkpoint_every_tools = 6
        checkpoint_every_seconds = 300.0
        if isinstance(checkpoint_policy, dict):
            try:
                checkpoint_every_tools = max(
                    1,
                    int(checkpoint_policy.get("every_tool_calls") or 6),
                )
                checkpoint_every_seconds = max(
                    1.0,
                    float(
                        checkpoint_policy.get("every_active_seconds") or 300
                    ),
                )
            except (TypeError, ValueError):
                checkpoint_every_tools = 6
                checkpoint_every_seconds = 300.0
        segment_index = int(checkpoint_values.get("segment_index") or 0)
        segment_tool_calls = int(
            checkpoint_values.get("segment_tool_calls") or 0
        )
        segment_tokens = int(checkpoint_values.get("segment_tokens") or 0)
        previous_segment_active_seconds = float(
            checkpoint_values.get("segment_active_seconds") or 0.0
        )
        segment_started_monotonic = run_started_monotonic
        model_messages: list[ModelMessage] = []
        observed_generated_paths: dict[str, tuple[int, int] | None] = {}
        continuation_artifact_digest_cache: dict[
            tuple[str, int, int], str
        ] = {}
        continuation_effect_refs: tuple[str, ...] = ()
        continuation_approval_refs: tuple[str, ...] = ()
        continuation_pending_operation_refs: tuple[str, ...] = ()
        continuation_blocked_reasons: tuple[str, ...] = ()
        continuation_provider_id = ""
        continuation_model_id = ""
        continuation_route_metadata: dict[str, Any] = {}
        raw_continuation_capsule = checkpoint_values.get(
            "continuation_capsule"
        )
        try:
            continuation_capsule = (
                RuntimeContinuationCapsule.model_validate(
                    raw_continuation_capsule
                )
                if isinstance(raw_continuation_capsule, dict)
                else None
            )
        except ValidationError:
            continuation_capsule = None
        continuation_generation = (
            continuation_capsule.context_generation
            if continuation_capsule is not None
            else 0
        )
        continuation_milestones: tuple[RuntimeContinuationMilestone, ...] = (
            continuation_capsule.completed_milestones
            if continuation_capsule is not None
            else ()
        )
        task_objective = ""
        task_acceptance: tuple[str, ...] = ()
        task_work_method = "standard"
        if task_contract is not None:
            task_objective = task_contract_effective_objective(task_contract)
            task_acceptance = task_contract.acceptance
            task_work_method = task_contract.work_method_id
        collaboration_member_execution = bool(
            str(runtime_metadata.get("collaboration_member_id") or "").strip()
            and str(runtime_metadata.get("presentation_context") or "").strip()
            == "subagent_internal"
        )
        convergence = ConvergenceController(
            objective=task_objective or _latest_user_text(runtime_input),
            acceptance=task_acceptance,
            policy=policy_for_work_method(
                task_work_method,
                max_no_progress_rounds=max_no_progress_rounds,
                collaboration_member=bool(
                    runtime_metadata.get("collaboration_member_id")
                ),
                overrides=(
                    runtime_metadata.get("convergence_policy")
                    if isinstance(
                        runtime_metadata.get("convergence_policy"),
                        dict,
                    )
                    else None
                ),
            ),
            mode=convergence_mode(runtime_metadata.get("convergence_mode")),
            snapshot=(
                checkpoint_values.get("convergence_state")
                if isinstance(checkpoint_values.get("convergence_state"), dict)
                else None
            ),
        )

        def refresh_continuation_capsule(
            *,
            generation: int,
            next_execution_position: RuntimeCheckpointExecutionPosition,
            pending_tool_call: ToolCall | None = None,
        ) -> RuntimeContinuationCapsule:
            artifacts = snapshot_continuation_artifacts(
                observed_generated_paths,
                workspace_dir=state_workspace_dir or request.workspace_dir,
                digest_cache=continuation_artifact_digest_cache,
            )
            retained_input_refs = {
                (
                    item.inbox_seq,
                    item.command_id,
                    item.client_turn_id,
                    item.payload_digest,
                ): item
                for item in (
                    continuation_capsule.input_refs
                    if continuation_capsule is not None
                    else ()
                )
            }
            for item in continuation_input_refs(model_messages):
                retained_input_refs[
                    (
                        item.inbox_seq,
                        item.command_id,
                        item.client_turn_id,
                        item.payload_digest,
                    )
                ] = item
            return build_continuation_capsule(
                task_contract=task_contract,
                task_id=turn_id,
                chat_id=request.session_id,
                run_id=turn_id,
                attempt_id=(
                    expected_attempt_id
                    or str(runtime_metadata.get("attempt_id") or "")
                    or f"{turn_id}:native-attempt"
                ),
                context_generation=generation,
                covered_event_seq=seq,
                workspace_dir=state_workspace_dir or request.workspace_dir,
                completed_milestones=continuation_milestones,
                source_refs=tuple(sorted(convergence.seen_sources)),
                input_refs=tuple(retained_input_refs.values()),
                artifacts=artifacts,
                effect_result_refs=continuation_effect_refs,
                approval_refs=continuation_approval_refs,
                pending_operation_refs=continuation_pending_operation_refs,
                blocked_reasons=continuation_blocked_reasons,
                next_execution_position=next_execution_position,
                pending_tool_batch_ref=(
                    f"tool-call:{pending_tool_call.id}"
                    if pending_tool_call is not None
                    else None
                ),
                provider_id=continuation_provider_id,
                model_id=continuation_model_id,
                route_metadata=continuation_route_metadata,
            )

        async def persist_checkpoint(
            stage: str,
            *,
            force: bool = False,
            status: RuntimeRunStatus | None = None,
            wait_reason: RuntimeWaitReason | None = None,
            next_execution_position: RuntimeCheckpointExecutionPosition = (
                "model_boundary"
            ),
            pending_tool_call: ToolCall | None = None,
        ) -> None:
            nonlocal canonical_run_available, canonical_run_load_error
            nonlocal checkpoint_durability_confirmed
            nonlocal checkpoint_last_monotonic, checkpoint_last_tool_count
            nonlocal continuation_effect_refs, continuation_approval_refs
            nonlocal continuation_pending_operation_refs
            nonlocal continuation_blocked_reasons
            nonlocal continuation_capsule
            if not state_workspace_dir or not turn_id:
                self._confirm_checkpoint_tool_receipts(turn_id, stage)
                return
            now_monotonic = asyncio.get_running_loop().time()
            due = (
                tool_call_count - checkpoint_last_tool_count
                >= checkpoint_every_tools
                or now_monotonic - checkpoint_last_monotonic
                >= checkpoint_every_seconds
                or (
                    (
                        canonical_run_available
                        or canonical_run_load_error is not None
                    )
                    and not checkpoint_durability_confirmed
                )
            )
            if not force and not due:
                return
            store = RuntimeRunStore(state_workspace_dir)
            persistence_required = bool(
                canonical_run_available or canonical_run_load_error is not None
            )
            max_conflict_attempts = 3
            for attempt in range(1, max_conflict_attempts + 1):
                try:
                    contract = await store.load_contract(turn_id)
                    canonical_run_load_error = None
                    if contract is None:
                        if not persistence_required:
                            self._confirm_checkpoint_tool_receipts(turn_id, stage)
                            return
                        raise _RuntimeDurabilityBlocked(
                            stage,
                            kind="canonical_run_missing",
                            cause=RuntimeRunStoreError(
                                "Canonical RuntimeRun contract is missing."
                            ),
                            attempts=attempt,
                        )
                    canonical_run_available = True
                    if (
                        expected_attempt_id
                        and contract.attempt_id != expected_attempt_id
                    ):
                        raise _RuntimeDurabilityBlocked(
                            stage,
                            kind="ownership_lost",
                            cause=RuntimeRunStoreConflictError(
                                "Canonical RuntimeRun attempt changed."
                            ),
                            attempts=attempt,
                        )
                    if (
                        expected_lease_owner
                        and contract.lease_owner != expected_lease_owner
                    ):
                        raise _RuntimeDurabilityBlocked(
                            stage,
                            kind="ownership_lost",
                            cause=RuntimeRunStoreConflictError(
                                "Canonical RuntimeRun lease owner changed."
                            ),
                            attempts=attempt,
                        )
                    if contract.status in TERMINAL_RUNTIME_RUN_STATUSES:
                        raise _RuntimeDurabilityBlocked(
                            stage,
                            kind="ownership_lost",
                            cause=RuntimeRunStoreConflictError(
                                "Canonical RuntimeRun is already terminal."
                            ),
                            attempts=attempt,
                        )
                    latest_effects: dict[str, RuntimeEffectRecord] = {}
                    if self._effect_journal is not None:
                        for effect in self._effect_journal.list_effects(
                            contract.run_id,
                            contract.attempt_id,
                        ):
                            current = latest_effects.get(effect.effect_id)
                            if (
                                current is None
                                or effect.revision > current.revision
                            ):
                                latest_effects[effect.effect_id] = effect
                    settled_effect_ids = tuple(
                        sorted(
                            effect_id
                            for effect_id, effect in latest_effects.items()
                            if effect.status
                            in {
                                "blocked",
                                "committed",
                                "failed",
                                "probed_committed",
                                "safe_to_retry",
                                "compensated",
                                "human_resolved",
                                "cancelled",
                            }
                        )
                    )
                    continuation_effect_refs = tuple(
                        sorted(
                            (
                                f"effect:{effect.effect_id}:revision:{effect.revision}:"
                                f"status:{effect.status}:result:{effect.result_hash}"
                                if effect.result_hash
                                else (
                                    f"effect:{effect.effect_id}:revision:{effect.revision}:"
                                    f"status:{effect.status}"
                                )
                            )
                            for effect in latest_effects.values()
                            if effect.status
                            in {
                                "blocked",
                                "committed",
                                "failed",
                                "probed_committed",
                                "safe_to_retry",
                                "compensated",
                                "human_resolved",
                                "cancelled",
                            }
                        )
                    )
                    continuation_approval_refs = tuple(
                        sorted(
                            f"approval:{effect.approval_id}:effect:{effect.effect_id}:"
                            f"status:{effect.status}"
                            for effect in latest_effects.values()
                            if effect.approval_id
                        )
                    )
                    continuation_pending_operation_refs = tuple(
                        sorted(
                            f"effect:{effect.effect_id}:revision:{effect.revision}:"
                            f"status:{effect.status}"
                            for effect in latest_effects.values()
                            if effect.status
                            in {
                                "intent",
                                "waiting_approval",
                                "approved",
                                "running",
                                "ambiguous",
                            }
                        )
                    )
                    continuation_blocked_reasons = tuple(
                        sorted(
                            f"effect:{effect.effect_id}:status:{effect.status}"
                            for effect in latest_effects.values()
                            if effect.status in {"blocked", "ambiguous"}
                        )
                    )
                    capsule = refresh_continuation_capsule(
                        generation=continuation_generation,
                        next_execution_position=next_execution_position,
                        pending_tool_call=pending_tool_call,
                    )
                    continuation_capsule = capsule
                    objective = ""
                    if task_contract is not None:
                        objective = task_contract_effective_objective(
                            task_contract
                        )
                    checkpoint = RuntimeCheckpoint(
                        checkpoint_id=f"checkpoint:{turn_id}:{uuid4()}",
                        seq=seq,
                        effect_ids=settled_effect_ids,
                        summary=(
                            f"stage={stage}; model_rounds={model_round_count}; "
                            f"tool_calls={tool_call_count}; inbox_seq={inbox_after_seq}"
                        ),
                        objective=objective,
                        stage=stage,
                        next_execution_position=next_execution_position,
                        pending_tool_call=(
                            RuntimePendingToolCall(
                                call_id=pending_tool_call.id,
                                tool_name=pending_tool_call.name,
                                arguments=dict(pending_tool_call.arguments),
                                metadata=dict(pending_tool_call.metadata),
                            )
                            if pending_tool_call is not None
                            else None
                        ),
                        accepted_steer_ids=tuple(sorted(accepted_steer_ids)),
                        artifact_digests=tuple(
                            artifact.content_sha256
                            for artifact in capsule.artifacts
                            if artifact.content_sha256 is not None
                        ),
                        continuation_capsule=capsule,
                        compressed_context=tuple(
                            json.dumps(
                                message.model_dump(mode="json"),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        for message in self._checkpoint_tool_result_context(turn_id, model_messages)
                        ),
                        active_seconds=max(
                            0.0,
                            previous_active_seconds
                            + now_monotonic
                            - run_started_monotonic,
                        ),
                        model_rounds=model_round_count,
                        model_epoch=model_epoch,
                        intent_revision=intent_revision,
                        tool_calls=tool_call_count,
                        used_tokens=used_tokens,
                        no_progress_rounds=no_progress_rounds,
                        failure_fingerprints=tuple(
                            sorted(failure_counts.items())
                        ),
                        inbox_after_seq=inbox_after_seq,
                        segment_index=segment_index,
                        segment_tool_calls=segment_tool_calls,
                        segment_tokens=segment_tokens,
                        segment_active_seconds=max(
                            0.0,
                            previous_segment_active_seconds
                            + now_monotonic
                            - segment_started_monotonic,
                        ),
                        convergence_state=convergence.snapshot(),
                    )
                    updated = reduce_runtime_run(
                        contract,
                        RuntimeRunReducerAction(
                            type="heartbeat",
                            expected_attempt_id=contract.attempt_id,
                            expected_revision=contract.revision,
                            lease_owner=contract.lease_owner,
                            status=status,
                            wait_reason=wait_reason,
                            checkpoint=checkpoint,
                        ),
                    ).run
                    await store.save_contract(
                        updated,
                        expected_revision=contract.revision,
                        expected_attempt_id=contract.attempt_id,
                        lease_owner=contract.lease_owner,
                    )
                    self._confirm_checkpoint_tool_receipts(turn_id, stage, checkpoint)
                except _RuntimeDurabilityBlocked:
                    raise
                except RuntimeRunStoreConflictError as exc:
                    if attempt < max_conflict_attempts:
                        continue
                    raise _RuntimeDurabilityBlocked(
                        stage,
                        kind="conflict_exhausted",
                        cause=exc,
                        attempts=attempt,
                    ) from exc
                except (
                    RuntimeRunStoreError,
                    RuntimeEffectJournalError,
                    RuntimeRunTransitionError,
                    OSError,
                    ValueError,
                ) as exc:
                    raise _RuntimeDurabilityBlocked(
                        stage,
                        kind="persistence_error",
                        cause=exc,
                        attempts=attempt,
                    ) from exc
                checkpoint_durability_confirmed = True
                checkpoint_last_monotonic = now_monotonic
                checkpoint_last_tool_count = tool_call_count
                return

        policy_context = self._policy_context_for_turn(
            runtime_input,
            turn_id=turn_id,
            budget=budget,
        )
        self.request_policy_observer.capture_effective(
            request_policy_observation_invocation,
            runtime_input=runtime_input,
            actual_turn_id=turn_id,
            effective_context=policy_context,
            deadline_monotonic=budget.deadline_monotonic,
        )
        seq = 1
        # Preserve completed delivery if a later provider continuation fails.
        file_delivery_completed = False
        # Delivery activates only after a committed primary artifact effect.
        file_delivery_requested = False
        # A steer invalidates automatic completion/delivery decisions made for
        # an older objective. Explicit later tool calls remain policy-checked
        # and may establish a new primary artifact.
        delivery_automation_suspended = bool(
            task_contract is not None and task_contract.revision > 0
        )
        raw_pending_deliverable_context = runtime_metadata.get(
            "pending_deliverable_context"
        )
        pending_source_run_id = (
            str(raw_pending_deliverable_context.get("previous_run_id") or "").strip()
            if isinstance(raw_pending_deliverable_context, dict)
            else ""
        )
        try:
            pending_source_contract_revision = max(
                0,
                int(
                    raw_pending_deliverable_context.get(
                        "source_contract_revision"
                    )
                    or 0
                ),
            ) if isinstance(raw_pending_deliverable_context, dict) else 0
            pending_source_task_revision = max(
                0,
                int(
                    raw_pending_deliverable_context.get("source_task_revision")
                    or 0
                ),
            ) if isinstance(raw_pending_deliverable_context, dict) else 0
        except (TypeError, ValueError):
            pending_source_contract_revision = 0
            pending_source_task_revision = 0
        pending_job_id = (
            str(raw_pending_deliverable_context.get("pending_job_id") or "").strip()
            or None
            if isinstance(raw_pending_deliverable_context, dict)
            else None
        )
        pending_inherited_contract = (
            raw_pending_deliverable_context.get("deliverable_contract")
            if isinstance(raw_pending_deliverable_context, dict)
            else None
        )
        pending_choice_required = bool(
            pending_source_run_id
            and isinstance(pending_inherited_contract, dict)
        )
        persisted_pending_resolution = (
            task_contract_metadata.get("pending_deliverable_resolution")
            if isinstance(task_contract_metadata, dict)
            else None
        )
        pending_choice_resolved = bool(
            pending_choice_required
            and isinstance(persisted_pending_resolution, dict)
            and str(
                persisted_pending_resolution.get("source_run_id") or ""
            ).strip()
            == pending_source_run_id
            and int(
                persisted_pending_resolution.get("source_contract_revision")
                or 0
            )
            == pending_source_contract_revision
            and int(
                persisted_pending_resolution.get("source_task_revision") or 0
            )
            == pending_source_task_revision
            and (
                str(
                    persisted_pending_resolution.get("pending_job_id") or ""
                ).strip()
                or None
            )
            == pending_job_id
            and persisted_pending_resolution.get("decision")
            in {"continue", "replace", "cancel"}
        )
        raw_deliverable_contract = (
            task_contract_metadata.get("deliverable_contract")
            if isinstance(task_contract_metadata, dict)
            else None
        )
        bound_extensions = (
            raw_deliverable_contract.get("accepted_extensions")
            if isinstance(raw_deliverable_contract, dict)
            else ()
        )
        expected_file_extensions = frozenset(
            extension
            for extension in (
                str(item or "").strip().lower()
                for item in (
                    bound_extensions
                    if isinstance(bound_extensions, (list, tuple))
                    else ()
                )
            )
            if extension
        )
        deliverable_contract_declared = bool(expected_file_extensions)
        declared_delivery_paths: list[str] = []
        delivery_idempotency_keys: dict[str, str] = {}
        ambiguous_delivery_artifact_ids: tuple[str, ...] = ()
        workspace_path = Path(request.workspace_dir).expanduser()
        artifact_attempt_id = str(
            runtime_metadata.get("attempt_id")
            or f"{request.run_id or turn_id}:native-attempt"
        )
        if self._artifact_effect_journal is not None:
            recovery = recover_artifact_delivery(
                self._artifact_effect_journal,
                effect_journal=self._effect_journal,
                run_id=turn_id,
                attempt_id=artifact_attempt_id,
                workspace_dir=workspace_path,
            )
            declared_delivery_paths.extend(recovery.pending_paths)
            delivery_idempotency_keys.update(recovery.idempotency_keys)
            file_delivery_requested = bool(recovery.pending_paths)
            file_delivery_completed = recovery.completed
            ambiguous_delivery_artifact_ids = recovery.ambiguous_artifact_ids
            if recovery.pending_paths:
                expected_file_extensions = frozenset(
                    suffix
                    for suffix in (
                        Path(path).suffix.lower()
                        for path in recovery.pending_paths
                    )
                    if suffix
                )
        recover_generated_delivery = bool(
            deliverable_contract_declared
            or _model_classified_durable_artifact_turn(runtime_metadata)
        )
        generated_file_baseline = (
            snapshot_generated_files(str(workspace_path))
            if recover_generated_delivery
            else {}
        )
        prompt_leak_guard = prompt_leak_guard_for_runtime_input(runtime_input)
        public_reasoning = PublicReasoningProjector(prompt_leak_guard)
        collaboration_preparing_emitted = False

        yield turn_lifecycle.start(
            seq=seq,
            summary=runtime_input.messages[-1].text_content()
            if runtime_input.messages
            else "",
            payload=_turn_started_payload(
                request,
                turn_id=turn_id,
                task_contract=(
                    task_contract_metadata
                    if isinstance(task_contract_metadata, dict)
                    else None
                ),
                turn_intelligence=(
                    runtime_metadata.get("turn_intelligence")
                    if isinstance(
                        runtime_metadata.get("turn_intelligence"),
                        dict,
                    )
                    else None
                ),
            ),
        )
        if ambiguous_delivery_artifact_ids:
            seq += 1
            yield RuntimeEvent(
                id=str(uuid4()),
                seq=seq,
                type="runtime.warning",
                status="failed",
                source="system",
                session_id=request.session_id,
                turn_id=turn_id,
                summary="附件发送状态需要确认，已停止自动重试。",
                payload={
                    "kind": "artifact_delivery_ambiguous",
                    "details": {
                        "artifact_ids": list(ambiguous_delivery_artifact_ids),
                    },
                },
            )
        if collaboration_required:
            seq += 1
            yield RuntimeEvent(
                id=str(uuid4()),
                seq=seq,
                type="runtime.warning",
                status="running",
                source="system",
                session_id=request.session_id,
                turn_id=turn_id,
                summary="沈行知正在组建协作室。",
                payload={
                    "kind": "collaboration_preparing",
                    "trace_title": "正在组建协作室",
                    "trace_detail": "沈行知正在选择成员与分工",
                },
            )
            collaboration_preparing_emitted = True

        try:
            model_client = self._model_client_factory(request.agent_id)
            continuation_provider_id = str(
                getattr(model_client, "provider_id", "") or ""
            )
            continuation_model_id = str(
                getattr(model_client, "model_name", "") or ""
            )
            raw_route_metadata = getattr(model_client, "routing_snapshot", {})
            continuation_route_metadata = (
                dict(raw_route_metadata)
                if isinstance(raw_route_metadata, dict)
                else {}
            )
            continuation_context_profile = model_context_profile_for_client(
                model_client
            )
            if _explicit_model_multimodal_support(
                model_client
            ) is False and _is_visual_creation_request(
                runtime_input,
                expected_file_extensions,
            ):
                seq += 1
                yield RuntimeEvent(
                    id=str(uuid4()),
                    seq=seq,
                    type="runtime.warning",
                    status="info",
                    source="system",
                    session_id=request.session_id,
                    turn_id=turn_id,
                    summary=(
                        "当前模型不支持多模态；建议切换支持多模态的" "模型以获得更好的版式与视觉质检，本次仍会继续执行。"
                    ),
                    payload={
                        "kind": "visual_task_text_only_model",
                        "details": {
                            "model": str(
                                getattr(model_client, "model_name", "") or "",
                            ),
                            "recommendation": "switch_to_multimodal_model",
                            "execution_continues": True,
                        },
                    },
                )
            model_messages = model_messages_from_runtime_messages(
                runtime_input.messages
            )
            ownership_insert_at = next(
                (
                    index
                    for index, message in enumerate(model_messages)
                    if message.role != "system"
                ),
                len(model_messages),
            )
            model_messages.insert(
                ownership_insert_at,
                _primary_turn_ownership_message(),
            )
            current_analysis_message = current_analysis_context_message(
                state_root=state_workspace_dir,
                scope=analysis_scope_from_runtime_context(
                    request_session_id=request.session_id,
                    runtime_metadata=runtime_metadata,
                ),
            )
            if current_analysis_message is not None:
                model_messages.insert(
                    ownership_insert_at + 1,
                    current_analysis_message,
                )
            pending_deliverable_message = _pending_deliverable_context_message(
                runtime_metadata.get("pending_deliverable_context")
            )
            if pending_deliverable_message is not None:
                model_messages.insert(
                    ownership_insert_at
                    + (2 if current_analysis_message is not None else 1),
                    pending_deliverable_message,
                )
            model_messages = turn_lifecycle.restore_model_messages(
                model_messages,
                attempt_reason=str(
                    runtime_metadata.get("runtime_attempt_reason") or "",
                ),
                checkpoint_metadata=(
                    checkpoint_metadata
                    if isinstance(checkpoint_metadata, dict)
                    else None
                ),
            )
            if continuation_capsule is not None and continuation_capsule.input_refs:
                first_ref = continuation_capsule.input_refs[0]
                if inbox_admission is None:
                    raise _ContinuationSourceUnavailable(
                        RuntimeInboxConflict(
                            item_seq=first_ref.inbox_seq,
                            client_turn_id=first_ref.client_turn_id,
                            reason=(
                                "continuation source cannot be revalidated "
                                "without the canonical Run Inbox"
                            ),
                            kind="run_inbox_continuation_source_unavailable",
                        )
                    )
                recovered_sources, source_conflict = (
                    inbox_admission.recover_continuation_sources(
                        continuation_capsule.input_refs
                    )
                )
                if source_conflict is not None:
                    raise _ContinuationSourceUnavailable(source_conflict)
                existing_refs = {
                    (
                        item.inbox_seq,
                        item.command_id,
                        item.client_turn_id,
                        item.payload_digest,
                    )
                    for item in continuation_input_refs(model_messages)
                }
                for source_message in recovered_sources:
                    source_ref = continuation_input_refs((source_message,))[0]
                    identity = (
                        source_ref.inbox_seq,
                        source_ref.command_id,
                        source_ref.client_turn_id,
                        source_ref.payload_digest,
                    )
                    if identity not in existing_refs:
                        model_messages.append(source_message)
                        existing_refs.add(identity)
            health_capsule = runtime_metadata.get("harness_health_capsule")
            harness_stop_external_effects = bool(
                isinstance(health_capsule, dict)
                and health_capsule.get("stop_external_effects")
            )
            if isinstance(health_capsule, dict):
                finding_codes = ", ".join(
                    str(code)
                    for code in health_capsule.get("codes") or ()
                    if str(code).strip()
                )
                model_messages.append(
                    ModelMessage.text(
                        "system",
                        (
                            "<lantai_runtime_health>"
                            f"level={health_capsule.get('level')}; "
                            f"codes={finding_codes or 'unknown'}. "
                            f"{health_capsule.get('guidance') or ''}"
                            " This is a control fact, not user content."
                            "</lantai_runtime_health>"
                        ),
                        metadata={
                            "runtime_health_capsule": True,
                            "instruction_trust": "trusted_system",
                            "instruction_origin": "runtime.health_capsule",
                        },
                    ),
                )
            if collaboration_required:
                model_messages.append(
                    _required_collaboration_contract_message(retry=False),
                )
            all_tools = await self._collect_tool_specs()
            tools = _tools_for_turn_intelligence(
                all_tools,
                runtime_metadata,
            )
            resumed_pending_tool_call: ToolCall | None = None
            if (
                str(runtime_metadata.get("runtime_attempt_reason") or "")
                == "resume"
                and checkpoint_values.get("next_execution_position")
                == "before_tool"
            ):
                raw_pending_tool_call = checkpoint_values.get(
                    "pending_tool_call"
                )
                try:
                    pending = RuntimePendingToolCall.model_validate(
                        raw_pending_tool_call
                    )
                except (TypeError, ValidationError) as exc:
                    raise RuntimeError(
                        "Resume checkpoint is missing a valid pending tool call."
                    ) from exc
                resumed_pending_tool_call = ToolCall(
                    id=pending.call_id,
                    name=pending.tool_name,
                    arguments=dict(pending.arguments),
                    metadata=dict(pending.metadata),
                )
                if resumed_pending_tool_call.name not in {
                    spec.name for spec in tools
                }:
                    tools.extend(
                        spec
                        for spec in all_tools
                        if spec.enabled
                        and spec.name == resumed_pending_tool_call.name
                    )
            if collaboration_required:
                # Staffing is forced through available_tools below, but the
                # parent still owns the Run after the room closes. Keep its
                # complete eligible tool universe so it can verify, create and
                # deliver from the collaboration evidence. Only make sure the
                # collaboration entry point survived intent filtering.
                batch_tools = [
                    spec
                    for spec in all_tools
                    if spec.name == _SUBAGENT_BATCH_TOOL and spec.enabled
                ]
                existing_names = {spec.name for spec in tools}
                tools.extend(
                    spec
                    for spec in batch_tools
                    if spec.name not in existing_names
                )
            if collaboration_required and self._progressive_tools:
                # A user-selected work method is an execution contract. Make
                # its authoritative batch tool available immediately so the
                # model cannot silently downgrade the room into role-play.
                self._equipped_tool_groups.add("collaboration")
            tool_specs_by_name = {spec.name: spec for spec in tools}
            private_editor_completion = PrivateEditorCompletionFence.from_specs(tools)
            assistant_artifact_projection = (
                AssistantArtifactProjectionAccumulator()
            )
            lineage_tools = (
                tool_specs_with_public_progress_contract(tools)
                if request.agent_id == "default"
                else tools
            )
            model_round_lineage = capture_model_round_lineage(
                runtime_input,
                allowed_tools=lineage_tools,
                model_slot=(
                    f"{model_client.provider_id}/{model_client.model_name}"
                ),
                tool_policy_context=policy_context.model_dump(mode="json"),
                turn_id=turn_id,
                model_route=getattr(model_client, "routing_snapshot", {}),
            )
            completed_transport_calls: dict[str, tuple[str, ToolResult]] = {}
            quarantined_calls: dict[str, str] = {}
            successful_call_counts: dict[str, int] = {}
            successful_tool_counts: dict[str, int] = {}
            analysis_presentation_attempted = False
            final_synthesis_only = harness_stop_external_effects
            final_synthesis_reason = (
                "runtime_health_l3" if harness_stop_external_effects else ""
            )
            outstanding_subagent_tasks: set[str] = set()
            deferred_required_capability_message_events: list[
                RuntimeEvent
            ] = []
            required_collaboration_attempts = 0
            auto_delivery_attempted_keys: set[str] = set()
            file_ready_for_delivery = bool(declared_delivery_paths)
            file_delivery_retries = 0
            durable_artifact_completion_retries = 0
            visual_candidate_paths: set[str] = set()
            tool_argument_repair_budget = ToolArgumentRepairBudget()
            model_output_recovery_attempts = 0
            model_stream_idle_recovery_attempts = 0
            model_stream_transport_recovery_attempts = 0
            for warning in self._mcp_discovery_warnings():
                seq += 1
                yield RuntimeEvent(
                    id=str(uuid4()),
                    seq=seq,
                    type="runtime.warning",
                    status="failed",
                    source="system",
                    session_id=request.session_id,
                    turn_id=turn_id,
                    summary=warning["summary"],
                    payload=warning,
                )

            iteration = 0
            while self._max_iters is None or iteration < self._max_iters:
                iteration += 1
                budget.remaining()
                convergence.start_round()
                resuming_pending_tool = resumed_pending_tool_call is not None
                # Checkpoints are durability snapshots only. Older versions
                # used token/tool/time thresholds to cut a healthy sustained
                # task down to the last 12 messages; that was an invisible,
                # lossy interruption. Context shaping now belongs solely to the
                # model-aware context guard and never changes task completion.
                if not resuming_pending_tool:
                    await persist_checkpoint("model_boundary")
                    model_round_count += 1
                    if (
                        max_model_rounds is not None
                        and model_round_count > max_model_rounds
                    ):
                        raise _TaskBudgetExceeded(
                            "max_model_rounds",
                            max_model_rounds,
                        )
                round_had_success = False
                model_round = ModelRoundAccumulator()
                deferred_persona_completion_events: list[RuntimeEvent] = []
                deferred_internal_execution_message_events: list[
                    RuntimeEvent
                ] = []
                input_claims_to_settle: list[RuntimeInboxClaim] = []
                if inbox_admission is not None and not resuming_pending_tool:
                    # Consume at most one steer per model boundary.  Appending
                    # several consecutive user messages to one model request
                    # makes most providers answer only the newest message and
                    # silently strands the earlier drafts.  The Run Inbox is a
                    # durable FIFO: one item gets one model turn before the
                    # next item is admitted.
                    admission_outcome = inbox_admission.claim_next(
                        after_seq=inbox_after_seq,
                    )
                    inbox_after_seq = admission_outcome.next_after_seq
                    if admission_outcome.conflict is not None:
                        conflict = admission_outcome.conflict
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="failed",
                            source="system",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary=(
                                "Run Inbox input was rejected before model "
                                "binding."
                            ),
                            payload={
                                "kind": conflict.kind,
                                "client_turn_id": conflict.client_turn_id,
                                "inbox_seq": conflict.item_seq,
                                "reason": conflict.reason,
                            },
                        )
                    claim = admission_outcome.claim
                    if claim is not None:
                        claim_text = claim.message.text_content()
                        # RuntimeTaskCommand remains the append-only textual
                        # objective history.  A structured-only steer (for
                        # example one image with no caption) is already
                        # durable in the Run Inbox and is bound to the next
                        # ModelRequest below; forcing it through the legacy
                        # non-empty text contract would either reject valid
                        # input or require inventing user text.
                        if task_contract is not None and claim_text:
                            if canonical_run_available:
                                revised_run = (
                                    await persist_runtime_task_command(
                                        workspace_dir=state_workspace_dir,
                                        run_id=turn_id,
                                        attempt_id=str(
                                            runtime_metadata.get("attempt_id")
                                            or ""
                                        ),
                                        client_turn_id=claim.client_turn_id,
                                        command=claim_text,
                                    )
                                )
                                task_contract = revised_run.task_contract
                                runtime_metadata[
                                    "attempt_id"
                                ] = revised_run.attempt_id
                                runtime_metadata[
                                    "runtime_revision"
                                ] = revised_run.revision
                                runtime_metadata[
                                    "runtime_lease_owner"
                                ] = revised_run.lease_owner
                            else:
                                task_contract = revise_chat_task_contract(
                                    task_contract,
                                    client_turn_id=claim.client_turn_id,
                                    command=claim_text,
                                )
                            if task_contract is not None:
                                task_contract_metadata = (
                                    task_contract.model_dump(mode="json")
                                )
                                runtime_metadata[
                                    "task_contract"
                                ] = task_contract_metadata
                                task_objective = (
                                    task_contract_effective_objective(
                                        task_contract
                                    )
                                )
                                task_acceptance = task_contract.acceptance
                                convergence.revise_intent(
                                    objective=task_objective,
                                    acceptance=task_acceptance,
                                )
                        delivery_automation_suspended = True
                        # Turn intelligence describes the message that opened
                        # the Run. Once a newer durable command is accepted it
                        # cannot remain an authority for hiding capabilities.
                        # Re-expose the registry universe; normal tool policy,
                        # approval, and progressive loading still apply.
                        tools = list(all_tools)
                        tool_specs_by_name = {
                            spec.name: spec for spec in tools
                        }
                        lineage_tools = (
                            tool_specs_with_public_progress_contract(tools)
                            if request.agent_id == "default"
                            else tools
                        )
                        lineage_input = runtime_input.model_copy(
                            update={"metadata": runtime_metadata}
                        )
                        model_round_lineage = capture_model_round_lineage(
                            lineage_input,
                            allowed_tools=lineage_tools,
                            model_slot=(
                                f"{model_client.provider_id}/"
                                f"{model_client.model_name}"
                            ),
                            tool_policy_context=policy_context.model_dump(
                                mode="json"
                            ),
                            turn_id=turn_id,
                            model_route=getattr(
                                model_client,
                                "routing_snapshot",
                                {},
                            ),
                        )
                        input_claims_to_settle.append(claim)
                        accepted_steer_ids.add(claim.client_turn_id)
                        intent_revision += 1
                        current_client_turn_id = claim.client_turn_id
                        model_messages.append(claim.message)
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="info",
                            source="user",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary="Run Inbox steer consumed at model boundary.",
                            payload={
                                "kind": "run_inbox_consumed",
                                "client_turn_id": claim.client_turn_id,
                                "inbox_seq": claim.item_seq,
                                "steer_text": claim.message.text_content(),
                            },
                        )
                force_file_delivery = (
                    file_delivery_requested
                    and file_ready_for_delivery
                    and not file_delivery_completed
                    and not delivery_automation_suspended
                )
                force_primary_finalization = (
                    self._artifact_effect_journal is not None
                    and file_ready_for_delivery
                    and not file_delivery_requested
                    and not file_delivery_completed
                    and not delivery_automation_suspended
                )
                if (
                    not force_file_delivery
                    and max_model_rounds is not None
                    and model_round_count >= max_model_rounds
                    and not final_synthesis_only
                ):
                    final_synthesis_only = True
                    final_synthesis_reason = "model_round_budget"
                    model_messages.append(
                        self._terminal_synthesis_message(
                            reason=final_synthesis_reason,
                        )
                    )
                if harness_stop_external_effects:
                    available_tools = []
                    tool_choice = "none"
                elif pending_choice_required and not pending_choice_resolved:
                    available_tools = [
                        spec
                        for spec in tools
                        if spec.name == _RESOLVE_PENDING_DELIVERABLE_TOOL
                        and bool(spec.metadata.get("runtime_owned"))
                        and bool(
                            spec.metadata.get("pending_deliverable_choice")
                        )
                    ]
                    if not available_tools:
                        raise RuntimeError(
                            "Pending deliverable context requires a semantic "
                            "choice resolver, but that tool is unavailable."
                        )
                    tool_choice = "required"
                elif final_synthesis_only:
                    # Convergence and budget decisions are terminal for tool
                    # execution. A stale, unverified artifact candidate must
                    # never reopen required finalization or delivery.
                    available_tools = []
                    tool_choice = "auto"
                elif force_file_delivery:
                    available_tools = [
                        spec
                        for spec in tools
                        if spec.name == FILE_DELIVERY_TOOL_NAME
                    ]
                    if not available_tools:
                        raise RuntimeError(
                            "File delivery was requested, but "
                            "send_file_to_user is unavailable.",
                        )
                    tool_choice = "required"
                elif force_primary_finalization:
                    available_tools = [
                        spec
                        for spec in tools
                        if spec.name == "finalize_deliverable"
                    ]
                    if not available_tools:
                        raise RuntimeError(
                            "Durable artifact recovery requires "
                            "finalize_deliverable, but that tool is unavailable."
                        )
                    tool_choice = "required"
                elif (
                    collaboration_required
                    and successful_tool_counts.get(_SUBAGENT_BATCH_TOOL, 0)
                    == 0
                ):
                    available_tools = [
                        spec
                        for spec in tools
                        if spec.name == _SUBAGENT_BATCH_TOOL
                    ]
                    if not available_tools:
                        raise RuntimeError(
                            "Collaboration work method requires "
                            "collaborate_with_agents, but that tool is unavailable."
                        )
                    tool_choice = "required"
                else:
                    analysis_presentation_continuation_locked = bool(
                        current_analysis_message is not None
                        and analysis_presentation_attempted
                    )
                    available_tools = [
                        spec
                        for spec in tools
                        if (
                            not (
                                spec.name == _SUBAGENT_BATCH_TOOL
                                and successful_tool_counts.get(
                                    _SUBAGENT_BATCH_TOOL,
                                    0,
                                )
                                > 0
                            )
                            and (
                                (
                                    analysis_presentation_continuation_locked
                                    and is_builtin_analysis_continuity_tool(
                                        spec
                                    )
                                )
                                or (
                                    not analysis_presentation_continuation_locked
                                    and (
                                        not self._progressive_tools
                                        or spec.name
                                        in {
                                            _PROGRESSIVE_TOOL_NAME,
                                            _SUBAGENT_BATCH_TOOL,
                                        }
                                        or (
                                            spec.name
                                            in private_editor_completion.trusted_tool_names
                                        )
                                        or (
                                            spec.name
                                            == _DECLARE_DELIVERABLE_CONTRACT_TOOL
                                            and bool(
                                                spec.metadata.get(
                                                    "runtime_owned"
                                                )
                                            )
                                            and bool(
                                                spec.metadata.get(
                                                    "deliverable_contract"
                                                )
                                            )
                                        )
                                        or _progressive_tool_group(spec)
                                        in self._equipped_tool_groups
                                        or (
                                            current_analysis_message is not None
                                            and is_builtin_analysis_continuity_tool(
                                                spec
                                            )
                                        )
                                        )
                                    )
                                or (
                                    spec.name
                                    == _RESOLVE_PENDING_DELIVERABLE_TOOL
                                    and bool(
                                        spec.metadata.get("runtime_owned")
                                    )
                                    and bool(
                                        spec.metadata.get(
                                            "pending_deliverable_choice"
                                        )
                                    )
                                )
                            )
                        )
                    ]
                    tool_choice = "auto"
                # Terminal synthesis never advertises tools, including when an
                # earlier round observed an unverified artifact candidate.
                synthesis_round_active = bool(
                    final_synthesis_only
                    and not available_tools
                    and not resuming_pending_tool
                )
                collaboration_staffing_round = bool(
                    collaboration_required
                    and successful_tool_counts.get(_SUBAGENT_BATCH_TOOL, 0)
                    == 0
                    and not final_synthesis_only
                )
                deep_research_execution_round = bool(
                    task_work_method == "deep_research"
                    and not final_synthesis_only
                )
                internal_worker_round = bool(
                    collaboration_member_execution
                    or deep_research_execution_round
                )
                provider_tools = (
                    tool_specs_with_public_progress_contract(available_tools)
                    if request.agent_id == "default"
                    else available_tools
                )
                model_request_metadata = self._model_request_metadata(
                    request=runtime_input.request,
                    turn_id=turn_id,
                    budget=budget,
                    runtime_metadata=runtime_metadata,
                    request_ordinal=model_round_count,
                )
                if not resuming_pending_tool:
                    model_epoch += 1
                model_request_metadata["model_epoch"] = model_epoch
                model_request_metadata["intent_revision"] = intent_revision
                model_request_metadata[
                    "runtime_model_stream_recovery_attempt"
                ] = model_stream_idle_recovery_attempts
                if collaboration_staffing_round:
                    model_request_metadata[
                        "runtime_phase"
                    ] = "collaboration_staffing"
                elif collaboration_member_execution:
                    model_request_metadata[
                        "runtime_phase"
                    ] = "collaboration_member_execution"
                elif deep_research_execution_round:
                    model_request_metadata[
                        "runtime_phase"
                    ] = "deep_research_execution"
                response_max_tokens = _main_response_max_tokens(
                    runtime_metadata,
                )
                if collaboration_staffing_round:
                    response_max_tokens = min(
                        response_max_tokens,
                        _COLLABORATION_STAFFING_MAX_TOKENS,
                    )
                if not resuming_pending_tool:
                    rollover_capsule = refresh_continuation_capsule(
                        generation=continuation_generation + 1,
                        next_execution_position="model_boundary",
                    )
                    rollover = rollover_model_context(
                        model_messages,
                        capsule=rollover_capsule,
                        profile=continuation_context_profile,
                        tools=provider_tools,
                        tool_choice=tool_choice,
                        generation={"max_tokens": response_max_tokens},
                    )
                    if rollover.rolled_over:
                        continuation_generation = (
                            rollover_capsule.context_generation
                        )
                        continuation_capsule = rollover_capsule
                        model_messages = list(rollover.messages)
                        await persist_checkpoint(
                            "context_rollover",
                            force=True,
                            next_execution_position="model_boundary",
                        )
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="info",
                            source="system",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary="长任务已在安全边界续接到新的模型上下文。",
                            payload={
                                "kind": "context_rollover",
                                "details": {
                                    "context_generation": continuation_generation,
                                    "capsule_sha256": (
                                        rollover_capsule.capsule_sha256
                                    ),
                                    "before_upper_bound": (
                                        rollover.before_upper_bound
                                    ),
                                    "after_upper_bound": (
                                        rollover.after_upper_bound
                                    ),
                                    "target_input_tokens": (
                                        rollover.target_input_tokens
                                    ),
                                    "profile_status": rollover.profile_status,
                                    "used_unknown_fallback": (
                                        rollover.used_unknown_fallback
                                    ),
                                },
                            },
                        )
                retained_sources = continuation_source_messages(model_messages)
                if retained_sources:
                    if inbox_admission is None:
                        first_ref = continuation_input_refs(retained_sources)[0]
                        raise _ContinuationSourceUnavailable(
                            RuntimeInboxConflict(
                                item_seq=first_ref.inbox_seq,
                                client_turn_id=first_ref.client_turn_id,
                                reason=(
                                    "continuation source cannot be revalidated "
                                    "without the canonical Run Inbox"
                                ),
                                kind=(
                                    "run_inbox_continuation_source_unavailable"
                                ),
                            )
                        )
                    source_conflict = (
                        inbox_admission.validate_continuation_sources(
                            retained_sources
                        )
                    )
                    if source_conflict is not None:
                        raise _ContinuationSourceUnavailable(source_conflict)
                prepared_model_round = prepare_next_model_round(
                    lineage=model_round_lineage,
                    messages=(
                        _collaboration_staffing_messages(model_messages)
                        if collaboration_staffing_round
                        else (
                            _collaboration_member_messages(model_messages)
                            if collaboration_member_execution
                            else (
                                _deep_research_messages(model_messages)
                                if deep_research_execution_round
                                else model_messages
                            )
                        )
                    ),
                    tools=provider_tools,
                    tool_choice=tool_choice,
                    model_slot=(
                        f"{model_client.provider_id}/{model_client.model_name}"
                    ),
                    tool_policy_context=policy_context.model_dump(mode="json"),
                    generation={
                        "max_tokens": response_max_tokens,
                    },
                    metadata=model_request_metadata,
                )

                if input_claims_to_settle:
                    model_request_id = str(
                        prepared_model_round.request.metadata.get(
                            "model_request_id"
                        )
                        or ""
                    ).strip()
                    if not model_request_id:
                        raise RuntimeInputConflictError(
                            "model request identity is required before input binding"
                        )
                    for claim in input_claims_to_settle:
                        inbox_admission.bind_to_model_request(
                            claim,
                            model_request_id=model_request_id,
                            model_epoch=model_epoch,
                            intent_revision=intent_revision,
                        )

                idle_interruption: _ModelStreamIdleInterruption | None = None
                control_interruption: (
                    _ModelStreamControlInterruption | None
                ) = None
                model_event_stream = (
                    _resumed_pending_tool_model_events(
                        resumed_pending_tool_call
                    )
                    if resuming_pending_tool
                    and resumed_pending_tool_call is not None
                    else _project_model_stream_boundaries(
                        self._stream_model_events(
                            model_client,
                            prepared_model_round.request,
                            runtime_input=runtime_input,
                            budget=budget,
                        ),
                        runtime_input=runtime_input,
                        close_stream=self._close_model_stream,
                        inbox_admission=inbox_admission,
                        inbox_after_seq=inbox_after_seq,
                    )
                )
                stream_transport_error: Exception | None = None
                try:
                    async with DelegatedAsyncIterator(
                        model_event_stream
                    ) as _owned_events_1:
                        async for model_event in _owned_events_1:
                            if isinstance(model_event, _ModelStreamIdleInterruption):
                                idle_interruption = model_event
                                break
                            if isinstance(
                                model_event, _ModelStreamControlInterruption
                            ):
                                control_interruption = model_event
                                break
                            model_round.accept(model_event)
                            if resuming_pending_tool:
                                continue
                            if input_claims_to_settle:
                                for claim in input_claims_to_settle:
                                    try:
                                        inbox_admission.settle_consumed(claim)
                                    except RuntimeInputConflictError:
                                        logger.warning(
                                            "Run Inbox claim could not be settled",
                                            extra={
                                                "run_id": turn_id,
                                                "item_seq": claim.item_seq,
                                            },
                                            exc_info=True,
                                        )
                                input_claims_to_settle.clear()
                            async for event in self._runtime_events_from_model_event(
                                model_event,
                                runtime_input=runtime_input,
                                turn_id=turn_id,
                                client_turn_id=current_client_turn_id,
                                seq_start=seq,
                            ):
                                event = public_reasoning.project(event)
                                if event is None:
                                    continue
                                seq = event.seq
                                if deep_research_execution_round and event.type in {
                                    "message.started",
                                    "message.delta",
                                    "message.completed",
                                }:
                                    # Research rounds produce an internal evidence
                                    # handoff. Only the primary persona synthesis is a
                                    # user-visible assistant message.
                                    deferred_internal_execution_message_events.append(
                                        event
                                    )
                                elif (
                                    (
                                        collaboration_required
                                        and successful_tool_counts.get(
                                            _SUBAGENT_BATCH_TOOL,
                                            0,
                                        )
                                        == 0
                                    )
                                    # A standard-mode turn can still explicitly
                                    # ask for a real hosted room in prose. Some
                                    # providers emit a short "I will convene it"
                                    # message before their batch tool call. Keep
                                    # that provisional chatter off the public
                                    # stream until the response boundary tells us
                                    # whether a room was actually opened.
                                    or prose_collaboration_requested
                                    or force_file_delivery
                                    or private_editor_completion.should_buffer
                                ) and event.type in {
                                    "message.started",
                                    "message.delta",
                                    "message.completed",
                                }:
                                    # Until the mandatory room exists, assistant text is
                                    # provisional control chatter.  Buffer every stream
                                    # surface, not only message.completed, so rejected
                                    # rounds cannot flash half an answer into the UI.
                                    deferred_required_capability_message_events.append(
                                        event
                                    )
                                elif _runtime_event_has_visible_completed_message(
                                    event
                                ):
                                    deferred_persona_completion_events.append(event)
                                else:
                                    try:
                                        yield event
                                    except asyncio.CancelledError as _stream_cancel:
                                        _owned_events_1.request_cancel(_stream_cancel)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not _is_retryable_model_stream_transport_error(exc):
                        raise
                    stream_transport_error = exc

                round_result = model_round.finish(
                    (
                        "cancelled"
                        if control_interruption is not None
                        else (
                            "timeout"
                            if idle_interruption is not None
                            else (
                                "provider_error"
                                if stream_transport_error is not None
                                else "completed"
                            )
                        )
                    ),
                )
                if control_interruption is not None:
                    wakeup = control_interruption.wakeup
                    for claim in input_claims_to_settle:
                        try:
                            inbox_admission.settle_superseded_before_surface(
                                claim,
                                superseding_command_id=wakeup.command_id,
                            )
                        except RuntimeInputConflictError:
                            logger.warning(
                                "Run Inbox claim could not be superseded",
                                extra={
                                    "run_id": turn_id,
                                    "item_seq": claim.item_seq,
                                    "command_id": wakeup.command_id,
                                },
                                exc_info=True,
                            )
                    input_claims_to_settle.clear()
                    deferred_persona_completion_events.clear()
                    deferred_required_capability_message_events.clear()
                    deferred_internal_execution_message_events.clear()
                    seq += 1
                    yield RuntimeEvent(
                        id=str(uuid4()),
                        seq=seq,
                        type="runtime.warning",
                        status="info",
                        source="system",
                        session_id=request.session_id,
                        turn_id=turn_id,
                        summary="已在安全边界让出旧模型请求，正在应用新方向。",
                        payload={
                            "kind": "model_stream_yielded_for_redirect",
                            "client_turn_id": wakeup.client_turn_id,
                            "inbox_seq": wakeup.item_seq,
                            "details": {
                                "command_id": wakeup.command_id,
                                "visible_message_ids": sorted(
                                    round_result.stream_message_ids,
                                ),
                                "unexecuted_tool_call_ids": [
                                    call.id for call in round_result.tool_calls
                                ],
                            },
                        },
                    )
                    await persist_checkpoint(
                        "model_stream_yielded_for_redirect",
                        force=True,
                        next_execution_position="model_boundary",
                    )
                    continue
                if stream_transport_error is not None:
                    if (
                        not round_result.saw_event
                        or model_stream_transport_recovery_attempts
                        >= _MAIN_STREAM_IDLE_RECOVERY_LIMIT
                    ):
                        raise stream_transport_error
                    model_stream_transport_recovery_attempts += 1
                    deferred_persona_completion_events.clear()
                    deferred_required_capability_message_events.clear()
                    deferred_internal_execution_message_events.clear()
                    model_messages.append(
                        _model_stream_idle_recovery_message(
                            attempt=model_stream_transport_recovery_attempts,
                        )
                    )
                    seq += 1
                    yield RuntimeEvent(
                        id=str(uuid4()),
                        seq=seq,
                        type="runtime.warning",
                        status="info",
                        source="system",
                        session_id=request.session_id,
                        turn_id=turn_id,
                        summary="模型连接意外中断，正在从已有进度继续。",
                        payload={
                            "kind": "model_output_recovery",
                            **(
                                {"client_turn_id": current_client_turn_id}
                                if current_client_turn_id
                                else {}
                            ),
                            "details": {
                                "reason": "provider_connection_interrupted",
                                "attempt": model_stream_transport_recovery_attempts,
                                "max_attempts": _MAIN_STREAM_IDLE_RECOVERY_LIMIT,
                                "discard_message_ids": sorted(
                                    round_result.stream_message_ids,
                                ),
                                "unexecuted_tool_call_ids": [
                                    call.id for call in round_result.tool_calls
                                ],
                            },
                        },
                    )
                    await persist_checkpoint(
                        "model_stream_transport_recovery",
                        force=True,
                        next_execution_position="model_boundary",
                    )
                    continue
                if (
                    idle_interruption is not None
                    and round_result.response is None
                ):
                    if (
                        model_stream_idle_recovery_attempts
                        >= _MAIN_STREAM_IDLE_RECOVERY_LIMIT
                    ):
                        raise idle_interruption.error
                    model_stream_idle_recovery_attempts += 1
                    recovery_reason = (
                        "first_event_timeout"
                        if idle_interruption.error.phase == "first_event"
                        else "stream_idle_timeout"
                    )
                    deferred_persona_completion_events.clear()
                    deferred_required_capability_message_events.clear()
                    deferred_internal_execution_message_events.clear()
                    model_messages.append(
                        _model_stream_idle_recovery_message(
                            attempt=model_stream_idle_recovery_attempts,
                        )
                    )
                    seq += 1
                    yield RuntimeEvent(
                        id=str(uuid4()),
                        seq=seq,
                        type="runtime.warning",
                        status="info",
                        source="system",
                        session_id=request.session_id,
                        turn_id=turn_id,
                        summary=(
                            "暂未收到上游响应，正在检查连接并从已有进度继续。"
                            if recovery_reason == "first_event_timeout"
                            else "模型响应意外中断，正在从已有进度继续。"
                        ),
                        payload={
                            "kind": "model_output_recovery",
                            **(
                                {"client_turn_id": current_client_turn_id}
                                if current_client_turn_id
                                else {}
                            ),
                            "details": {
                                "reason": recovery_reason,
                                "timeout_seconds": (
                                    idle_interruption.error.timeout_seconds
                                ),
                                "attempt": model_stream_idle_recovery_attempts,
                                "max_attempts": _MAIN_STREAM_IDLE_RECOVERY_LIMIT,
                                "discard_message_ids": sorted(
                                    round_result.stream_message_ids,
                                ),
                            },
                        },
                    )
                    await persist_checkpoint(
                        "model_stream_idle_recovery",
                        force=True,
                    )
                    continue
                tool_calls = list(round_result.tool_calls)
                round_stream_message_ids = set(
                    round_result.stream_message_ids,
                )
                response = round_result.response
                if request.agent_id == "default":
                    tool_calls = [
                        without_public_progress_argument(tool_call)
                        for tool_call in tool_calls
                    ]
                round_recovery = decide_model_round_recovery(
                    round_result,
                    ModelRoundRecoveryState(
                        output_recovery_attempts=(
                            model_output_recovery_attempts
                        ),
                        output_recovery_limit=_MAIN_OUTPUT_RECOVERY_LIMIT,
                        accept_truncated_best_effort=bool(
                            runtime_metadata.get(
                                "native_accept_truncated_best_effort",
                            ),
                        ),
                        degenerate_output=bool(
                            response is not None
                            and not tool_calls
                            and has_degenerate_placeholder_flood(
                                response.message.text_content(),
                            )
                        ),
                    ),
                )
                usage = round_result.usage
                if usage is not None:
                    response_tokens = int(
                        usage.total_tokens
                        or usage.input_tokens + usage.output_tokens
                    )
                    used_tokens += response_tokens
                    segment_tokens += response_tokens
                    if (
                        max_tokens is not None
                        and used_tokens > max_tokens
                        and tool_calls
                        and not final_synthesis_only
                    ):
                        final_synthesis_only = True
                        final_synthesis_reason = "token_budget"
                        model_messages.append(
                            self._terminal_synthesis_message(
                                reason=final_synthesis_reason,
                            )
                        )
                if round_recovery.action == "fail_provider":
                    raise RuntimeError(
                        round_result.model_error or "model error"
                    )

                if round_recovery.action == "fail_missing_response":
                    raise RuntimeError(
                        "Native runtime model stream ended without a final response."
                    )

                if tool_calls:
                    deferred_required_capability_message_events.clear()
                    if synthesis_round_active:
                        # Some OpenAI-compatible providers still emit a tool
                        # call even when the request advertises no tools.  A
                        # protocol mismatch at the very last step must not
                        # erase useful work from the preceding rounds.  Keep a
                        # visible answer when one exists and ignore the late
                        # call instead of failing the whole turn.
                        if (
                            response is None
                            or not _model_response_has_visible_output(response)
                        ):
                            raise _ModelProtocolError(
                                "Model requested a tool during the final synthesis round."
                            )
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="failed",
                            source="system",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary="已忽略最终总结阶段的额外工具调用。",
                            payload={
                                "kind": "final_synthesis_tool_call_ignored",
                                "tool_names": [
                                    call.name for call in tool_calls
                                ],
                            },
                        )
                        tool_calls = []
                    specs_by_name = {spec.name: spec for spec in tools}
                    collaboration_spec = specs_by_name.get(
                        _SUBAGENT_BATCH_TOOL
                    )
                    collaboration_properties = (
                        collaboration_spec.input_schema.get("properties", {})
                        if collaboration_spec is not None
                        and isinstance(collaboration_spec.input_schema, dict)
                        else {}
                    )
                    if (
                        prose_roundtable_requested
                        and "protocol" in collaboration_properties
                    ):
                        tool_calls = [
                            ToolCall(
                                id=tool_call.id,
                                name=tool_call.name,
                                arguments={
                                    **dict(tool_call.arguments),
                                    "protocol": "roundtable",
                                },
                                metadata=dict(tool_call.metadata),
                            )
                            if tool_call.name == _SUBAGENT_BATCH_TOOL
                            else tool_call
                            for tool_call in tool_calls
                        ]
                    validation = validate_tool_call_batch(
                        tool_calls,
                        finish_reason=round_result.finish_reason,
                        normalize=lambda call: _normalize_tool_call_argument_aliases(
                            call,
                            specs_by_name=specs_by_name,
                        ),
                        validate=lambda call: _validate_tool_call_arguments(
                            call,
                            specs_by_name=specs_by_name,
                        ),
                    )
                    tool_calls = list(validation.tool_calls)
                    invalid_tool_calls = dict(validation.errors_by_call_id)
                    if invalid_tool_calls:
                        # Preserve a complete assistant/tool transaction for
                        # provider APIs, but never execute a malformed batch.
                        # Feeding the schema failure back as tool evidence lets
                        # the model repair its own call without exposing an
                        # internal protocol exception to the user.
                        tool_argument_repair_budget.record_failed_batch()
                        if response is None:
                            response = ModelResponse(
                                message=ModelMessage(role="assistant"),
                            )
                        model_messages.append(
                            self._assistant_model_message(
                                response, tool_calls
                            ),
                        )
                        for tool_call in tool_calls:
                            detail = invalid_tool_calls.get(tool_call.id)
                            if detail is None:
                                detail = (
                                    "This call was not executed because another "
                                    "call in the same batch had invalid arguments; "
                                    "send the complete batch again."
                                )
                            model_messages.append(
                                self._tool_result_model_message(
                                    ToolResult(
                                        call_id=tool_call.id,
                                        name=tool_call.name,
                                        ok=False,
                                        content=[
                                            {
                                                "type": "text",
                                                "text": detail,
                                            }
                                        ],
                                        error=detail,
                                        metadata={
                                            "protocol_recovery": True,
                                            "executed": False,
                                        },
                                    )
                                )
                            )
                        deferred_required_capability_message_events.clear()
                        for (
                            deferred_event
                        ) in deferred_persona_completion_events:
                            yield deferred_event
                        deferred_persona_completion_events.clear()
                        if tool_argument_repair_budget.exhausted:
                            raise _ToolArgumentRepairExhausted(
                                tool_argument_repair_budget.attempts,
                            )
                        continue
                if response is not None and not tool_calls:
                    if round_recovery.action == "recover_output":
                        deferred_persona_completion_events.clear()
                        deferred_required_capability_message_events.clear()
                        model_output_recovery_attempts = (
                            round_recovery.next_output_recovery_attempts
                        )
                        model_messages.append(
                            self._assistant_model_message(response, []),
                        )
                        model_messages.append(
                            _model_output_recovery_message(
                                attempt=model_output_recovery_attempts,
                            ),
                        )
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="info",
                            source="system",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary=("模型输出较长，" "正在从已有进度继续。"),
                            payload={
                                "kind": "model_output_recovery",
                                **(
                                    {
                                        "client_turn_id": (
                                            current_client_turn_id
                                        )
                                    }
                                    if current_client_turn_id
                                    else {}
                                ),
                                "details": {
                                    "finish_reason": (
                                        round_result.finish_reason
                                    ),
                                    "attempt": model_output_recovery_attempts,
                                    "max_attempts": (
                                        _MAIN_OUTPUT_RECOVERY_LIMIT
                                    ),
                                    "discard_message_ids": sorted(
                                        round_stream_message_ids
                                    ),
                                },
                            },
                        )
                        continue
                    if round_recovery.action == "fail_incomplete_output":
                        deferred_persona_completion_events.clear()
                        deferred_required_capability_message_events.clear()
                        raise _IncompleteModelOutput(
                            round_result.finish_reason,
                            recovery_attempts=model_output_recovery_attempts,
                        )
                    if round_recovery.action == "fail_invalid_output":
                        deferred_persona_completion_events.clear()
                        deferred_required_capability_message_events.clear()
                        raise _DegenerateModelOutput(
                            round_recovery.reason_code,
                        )
                if response is not None and not resuming_pending_tool:
                    model_messages.append(
                        self._assistant_model_message(response, tool_calls),
                    )

                batch_tool_called = any(
                    tool_call.name == _SUBAGENT_BATCH_TOOL
                    for tool_call in tool_calls
                )
                if tool_calls and deferred_required_capability_message_events:
                    if not batch_tool_called and not private_editor_completion.should_buffer:
                        # This was an ordinary tool round after a prose hint,
                        # not the requested hosted room. Preserve normal
                        # streaming behavior instead of dropping its answer.
                        for (
                            deferred_event
                        ) in deferred_required_capability_message_events:
                            yield deferred_event
                    deferred_required_capability_message_events.clear()

                if tool_calls and deferred_persona_completion_events:
                    for deferred_event in deferred_persona_completion_events:
                        yield deferred_event
                    deferred_persona_completion_events.clear()

                if tool_calls:
                    deferred_internal_execution_message_events.clear()

                if not tool_calls:
                    grounding, editor_events, editor_completions = (
                        private_editor_completion.resolve_no_tool_boundary(
                            response_exists=response is not None,
                            model_messages=model_messages,
                            buffered_events=(
                                deferred_required_capability_message_events
                            ),
                            is_visible_completion=(
                                _runtime_event_has_visible_completed_message
                            ),
                        )
                    )
                    if grounding is not None:
                        deferred_persona_completion_events.clear()
                        final_synthesis_only = True
                        final_synthesis_reason = "private_editor_fact_grounding"
                        model_messages.append(grounding)
                        continue
                    for editor_event in editor_events:
                        yield editor_event
                    deferred_persona_completion_events.extend(editor_completions)
                    if force_file_delivery:
                        deferred_required_capability_message_events.clear()
                    elif (
                        prose_collaboration_requested
                        and deferred_required_capability_message_events
                    ):
                        # The provider declined to invoke the room. This is
                        # not an execution-contract failure in standard mode,
                        # so publish the buffered ordinary answer rather than
                        # making it disappear.
                        for (
                            deferred_event
                        ) in deferred_required_capability_message_events:
                            yield deferred_event
                        deferred_required_capability_message_events.clear()
                    if inbox_admission is not None:
                        if inbox_admission.has_pending(
                            after_seq=inbox_after_seq,
                        ):
                            # Only detect work here.  Admission happens at the
                            # next model-boundary block above.  Consuming here
                            # and then again at the next boundary would merge
                            # two queued user turns into one provider request.
                            for (
                                deferred_event
                            ) in deferred_persona_completion_events:
                                yield deferred_event
                            deferred_persona_completion_events.clear()
                            continue
                    if (
                        collaboration_required
                        and successful_tool_counts.get(_SUBAGENT_BATCH_TOOL, 0)
                        == 0
                    ):
                        deferred_required_capability_message_events.clear()
                        required_collaboration_attempts += 1
                        if required_collaboration_attempts >= 2:
                            raise _RequiredCapabilityFailed(
                                _SUBAGENT_BATCH_TOOL,
                                required_collaboration_attempts,
                                error_code="capability_not_invoked",
                            )
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="info",
                            source="system",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary="协作工作法已拦截未开房的直接结案。",
                            payload={
                                "kind": "collaboration_contract_enforced",
                                "error_code": "collaborate_with_agents_required",
                            },
                        )
                        model_messages.append(
                            _required_collaboration_contract_message(
                                retry=True
                            ),
                        )
                        continue
                    if deep_research_execution_round:
                        # The preceding assistant message is a private research
                        # handoff in canonical history. Restore the full primary
                        # prompt for one tool-free, user-facing synthesis round.
                        deferred_internal_execution_message_events.clear()
                        final_synthesis_only = True
                        final_synthesis_reason = "deep_research_completed"
                        model_messages.append(
                            self._deep_research_final_synthesis_message(),
                        )
                        continue
                    if (
                        response is not None
                        and self._artifact_effect_journal is None
                        and not final_synthesis_only
                        and not delivery_automation_suspended
                        and not file_delivery_requested
                        and not file_delivery_completed
                        and observed_generated_paths
                    ):
                        model_declared_paths = _model_declared_generated_paths(
                            response.message.text_content(),
                            workspace_dir=str(workspace_path),
                            generated_paths=observed_generated_paths,
                        )
                        if model_declared_paths:
                            declared_delivery_paths.extend(
                                model_declared_paths
                            )
                            expected_file_extensions = frozenset(
                                suffix
                                for suffix in (
                                    Path(path).suffix.lower()
                                    for path in model_declared_paths
                                )
                                if suffix
                            )
                            file_delivery_requested = True
                            file_ready_for_delivery = True
                            recover_generated_delivery = True
                            seq += 1
                            yield RuntimeEvent(
                                id=str(uuid4()),
                                seq=seq,
                                type="runtime.warning",
                                status="info",
                                source="system",
                                session_id=request.session_id,
                                turn_id=turn_id,
                                summary="已把本轮明确交付的文件附加到对话。",
                                payload={
                                    "kind": "model_declared_file_delivery_recovered",
                                },
                            )
                    if (
                        recover_generated_delivery
                        and not final_synthesis_only
                        and not delivery_automation_suspended
                        and not file_delivery_requested
                        and not file_delivery_completed
                    ):
                        recovered_paths = select_generated_delivery_paths(
                            str(workspace_path),
                            generated_file_baseline,
                            expected_file_extensions,
                        )
                        if recovered_paths:
                            recovered_paths = recovered_paths[:1]
                            for recovered_path in recovered_paths:
                                if (
                                    recovered_path
                                    not in declared_delivery_paths
                                ):
                                    declared_delivery_paths.append(
                                        recovered_path
                                    )
                            expected_file_extensions = frozenset(
                                suffix
                                for suffix in (
                                    Path(path).suffix.lower()
                                    for path in recovered_paths
                                )
                                if suffix
                            )
                            recovered_visual_candidate = any(
                                path in visual_candidate_paths
                                for path in recovered_paths
                            )
                            file_ready_for_delivery = (
                                not recovered_visual_candidate
                            )
                            if self._artifact_effect_journal is not None:
                                durable_artifact_completion_retries += 1
                                if (
                                    recovered_visual_candidate
                                    and durable_artifact_completion_retries > 2
                                ):
                                    recover_generated_delivery = False
                                    final_synthesis_reason = (
                                        "artifact_candidate_unverified"
                                    )
                                    seq += 1
                                    yield RuntimeEvent(
                                        id=str(uuid4()),
                                        seq=seq,
                                        type="runtime.warning",
                                        status="failed",
                                        source="system",
                                        session_id=request.session_id,
                                        turn_id=turn_id,
                                        summary=("文件候选已保留，但尚未通过视觉验收。"),
                                        payload={
                                            "kind": "artifact_candidate_unverified",
                                            "details": {
                                                "delivery_status": "candidate",
                                                "candidate_paths": list(
                                                    recovered_paths
                                                ),
                                            },
                                        },
                                    )
                                elif durable_artifact_completion_retries > 2:
                                    raise _RequiredCapabilityFailed(
                                        "finalize_deliverable",
                                        durable_artifact_completion_retries,
                                        error_code="capability_not_invoked",
                                    )
                                else:
                                    deferred_persona_completion_events.clear()
                                    instruction = (
                                        "已检测到本轮真实主产物候选："
                                        f"{recovered_paths[0]}。请继续使用可用工具"
                                        "完成渲染、检查和必要修复；只有实际验收通过后"
                                        "才能调用 finalize_deliverable，且必须绑定"
                                        "本版文件的验证证据。"
                                        if recovered_visual_candidate
                                        else (
                                            "已检测到本轮真实主产物候选："
                                            f"{recovered_paths[0]}。现在只能调用 "
                                            "finalize_deliverable 确认该文件；不要改选辅助"
                                            "说明、脚本或其他文件，也不要先输出完成答复。"
                                        )
                                    )
                                    model_messages.append(
                                        ModelMessage.text(
                                            "system",
                                            instruction,
                                            metadata={
                                                "instruction_trust": "trusted_system",
                                                "instruction_origin": (
                                                    "runtime.artifact_visual_candidate_recovery"
                                                    if recovered_visual_candidate
                                                    else "runtime.artifact_finalization_recovery"
                                                ),
                                            },
                                        )
                                    )
                                    continue
                            if self._artifact_effect_journal is None:
                                file_delivery_requested = True
                                seq += 1
                                yield RuntimeEvent(
                                    id=str(uuid4()),
                                    seq=seq,
                                    type="runtime.warning",
                                    status="info",
                                    source="system",
                                    session_id=request.session_id,
                                    turn_id=turn_id,
                                    summary="已恢复本轮生成文件的附件交付。",
                                    payload={
                                        "kind": "generated_file_delivery_recovered",
                                    },
                                )
                    if (
                        file_delivery_requested
                        and not final_synthesis_only
                        and not delivery_automation_suspended
                        and not file_delivery_completed
                        and any(
                            spec.name == FILE_DELIVERY_TOOL_NAME
                            for spec in tools
                        )
                    ):
                        if self._artifact_effect_journal is not None:
                            current_delivery = recover_artifact_delivery(
                                self._artifact_effect_journal,
                                effect_journal=self._effect_journal,
                                run_id=turn_id,
                                attempt_id=artifact_attempt_id,
                                workspace_dir=workspace_path,
                            )
                            current_paths = set(current_delivery.pending_paths)
                            stale_delivery_paths = [
                                path
                                for path in declared_delivery_paths
                                if delivery_idempotency_keys.get(path)
                                and (
                                    path not in current_paths
                                    or current_delivery.idempotency_keys.get(
                                        path
                                    )
                                    != delivery_idempotency_keys.get(path)
                                )
                            ]
                            if stale_delivery_paths:
                                file_delivery_requested = False
                                file_ready_for_delivery = False
                                file_delivery_completed = False
                                visual_candidate_paths.update(
                                    stale_delivery_paths
                                )
                                deferred_persona_completion_events.clear()
                                seq += 1
                                yield RuntimeEvent(
                                    id=str(uuid4()),
                                    seq=seq,
                                    type="runtime.warning",
                                    status="failed",
                                    source="system",
                                    session_id=request.session_id,
                                    turn_id=turn_id,
                                    summary=("文件在交付前发生变化，旧验证已失效。"),
                                    payload={
                                        "kind": "artifact_verification_stale",
                                        "error_code": (
                                            "artifact_revision_changed"
                                        ),
                                        "details": {
                                            "delivery_status": "candidate",
                                            "candidate_paths": (
                                                stale_delivery_paths
                                            ),
                                            "invalid_artifact_ids": list(
                                                current_delivery.invalid_artifact_ids
                                            ),
                                        },
                                    },
                                )
                                model_messages.append(
                                    ModelMessage.text(
                                        "system",
                                        "主产物在验证后、发送前发生了变化。旧验证回执"
                                        "不能证明新字节；请重新检查当前版本，必要时修复，"
                                        "再调用 finalize_deliverable 绑定新版本。",
                                        metadata={
                                            "instruction_trust": "trusted_system",
                                            "instruction_origin": (
                                                "runtime.artifact_revision_fence"
                                            ),
                                        },
                                    )
                                )
                                continue
                        generated_paths = [
                            path
                            for path in declared_delivery_paths
                            if delivery_idempotency_keys.get(
                                path, f"legacy-file-delivery:{path}"
                            )
                            not in auto_delivery_attempted_keys
                        ]
                        delivery_batch_complete = bool(generated_paths)
                        for generated_path in generated_paths:
                            tool_call_count += 1
                            segment_tool_calls += 1
                            if (
                                max_tool_calls is not None
                                and tool_call_count > max_tool_calls
                            ):
                                raise _TaskBudgetExceeded(
                                    "max_tool_calls",
                                    max_tool_calls,
                                )
                            delivery_key = delivery_idempotency_keys.get(
                                generated_path,
                                f"legacy-file-delivery:{generated_path}",
                            )
                            auto_delivery_attempted_keys.add(delivery_key)
                            auto_call = ToolCall(
                                id=(
                                    "auto-file-delivery-"
                                    + hashlib.sha256(
                                        delivery_key.encode("utf-8")
                                    ).hexdigest()[:24]
                                ),
                                name=FILE_DELIVERY_TOOL_NAME,
                                arguments={"file_path": generated_path},
                                metadata={
                                    "auto_delivery": True,
                                    "idempotency_key": delivery_key,
                                },
                            )
                            auto_result: ToolResult | None = None
                            await persist_checkpoint(
                                "before_external_effect",
                                force=True,
                                next_execution_position="before_tool",
                                pending_tool_call=auto_call,
                            )
                            auto_transaction = self._run_tool_call(
                                auto_call,
                                runtime_input=runtime_input,
                                turn_id=turn_id,
                                client_turn_id=current_client_turn_id,
                                seq_start=seq,
                                policy_context=policy_context,
                                policy_observation_invocation=(
                                    request_policy_observation_invocation
                                ),
                                budget=budget,
                                expected_file_extensions=(
                                    expected_file_extensions
                                ),
                            )
                            try:
                                async for event, result in self._durable_tool_completion_events(
                                    auto_transaction, persist_checkpoint=persist_checkpoint,
                                    runtime_input=runtime_input, turn_id=turn_id,
                                    client_turn_id=current_client_turn_id,
                                    spec=tool_specs_by_name.get(auto_call.name),
                                ):
                                    seq = event.seq
                                    auto_result = result
                                    yield event
                            finally:
                                await close_owned_async_iterator(
                                    auto_transaction,
                                    task_name=(
                                        f"native-auto-tool-close-{auto_call.id}"
                                    ),
                                )
                            if auto_result is None:
                                delivery_batch_complete = False
                                continue
                            await persist_checkpoint(
                                "tool_boundary",
                                force=True,
                                next_execution_position="after_tool",
                            )
                            if not auto_result.ok:
                                delivery_batch_complete = False
                                fingerprint = _tool_failure_fingerprint(
                                    auto_call,
                                    auto_result,
                                )
                                failure_counts[fingerprint] = (
                                    failure_counts.get(fingerprint, 0) + 1
                                )
                                if (
                                    failure_counts[fingerprint]
                                    >= max_same_failure_count
                                ):
                                    quarantined_calls[
                                        _tool_call_fingerprint(auto_call)
                                    ] = (
                                        "identical call quarantined after repeated "
                                        "failure; change arguments or strategy"
                                    )
                                    file_delivery_requested = False
                                    seq += 1
                                    yield RuntimeEvent(
                                        id=str(uuid4()),
                                        seq=seq,
                                        type="runtime.warning",
                                        status="failed",
                                        source="system",
                                        session_id=request.session_id,
                                        turn_id=turn_id,
                                        summary=(
                                            "文件已保留，但附件发送连续失败；" "已停止自动重试。"
                                        ),
                                        payload={
                                            "kind": "file_delivery_incomplete",
                                            "details": {
                                                "path": generated_path,
                                                "attempts": failure_counts[
                                                    fingerprint
                                                ],
                                            },
                                        },
                                    )
                                    break
                            if auto_result.ok:
                                round_had_success = True
                                file_ready_for_delivery = True
                            if (
                                auto_result.ok
                                and has_matching_file_delivery_block(
                                    auto_result.content,
                                    {Path(generated_path).suffix.lower()},
                                )
                            ):
                                continue
                            delivery_batch_complete = False
                        if generated_paths:
                            file_delivery_completed = delivery_batch_complete
                    if (
                        file_delivery_requested
                        and not final_synthesis_only
                        and not delivery_automation_suspended
                        and not file_delivery_completed
                    ):
                        file_delivery_retries += 1
                        if file_delivery_retries > 1:
                            seq += 1
                            yield RuntimeEvent(
                                id=str(uuid4()),
                                seq=seq,
                                type="runtime.warning",
                                status="failed",
                                source="system",
                                session_id=request.session_id,
                                turn_id=turn_id,
                                summary="文件未能附加到对话，已停止自动重试。",
                                payload={
                                    "kind": "file_delivery_incomplete",
                                    "details": {
                                        "paths": list(declared_delivery_paths),
                                    },
                                },
                            )
                            raise _RequiredCapabilityFailed(
                                FILE_DELIVERY_TOOL_NAME,
                                file_delivery_retries,
                                error_code="delivery_not_attached",
                            )
                        else:
                            model_messages.append(
                                self._file_delivery_recovery_message(
                                    file_ready=file_ready_for_delivery,
                                    expected_extensions=expected_file_extensions,
                                ),
                            )
                            for (
                                deferred_event
                            ) in deferred_persona_completion_events:
                                yield deferred_event
                            deferred_persona_completion_events.clear()
                            continue
                    if (
                        recover_generated_delivery
                        and not final_synthesis_only
                        and not delivery_automation_suspended
                        and not file_delivery_requested
                        and not file_delivery_completed
                        and not visual_candidate_paths
                    ):
                        # A durable-artifact turn is not complete merely
                        # because the model emitted explanatory prose.  In
                        # particular, a missing interpreter alias (for
                        # example `python` when `python3` is available) is a
                        # recoverable execution detail, not a user blocker.
                        # Discard the premature completion text and give the
                        # same turn one focused recovery round.
                        durable_artifact_completion_retries += 1
                        deferred_persona_completion_events.clear()
                        if durable_artifact_completion_retries > 1:
                            seq += 1
                            yield RuntimeEvent(
                                id=str(uuid4()),
                                seq=seq,
                                type="runtime.warning",
                                status="failed",
                                source="system",
                                session_id=request.session_id,
                                turn_id=turn_id,
                                summary="未生成可交付文件，已停止自动恢复。",
                                payload={
                                    "kind": "durable_artifact_completion_failed",
                                    "details": {
                                        "attempts": durable_artifact_completion_retries,
                                        "expected_extensions": sorted(
                                            expected_file_extensions
                                        ),
                                    },
                                },
                            )
                            raise _RequiredCapabilityFailed(
                                "durable_artifact_delivery",
                                durable_artifact_completion_retries,
                                error_code="artifact_not_created",
                            )
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="info",
                            source="system",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary="尚未生成可交付文件，已自动继续恢复。",
                            payload={
                                "kind": "durable_artifact_completion_recovery",
                                "details": {
                                    "attempt": durable_artifact_completion_retries,
                                    "expected_extensions": sorted(
                                        expected_file_extensions
                                    ),
                                },
                            },
                        )
                        model_messages.append(
                            self._durable_artifact_completion_recovery_message(
                                expected_extensions=expected_file_extensions,
                            )
                        )
                        continue
                    if response is not None:
                        if not _model_response_has_visible_output(response):
                            if not final_synthesis_only:
                                final_synthesis_only = True
                                final_synthesis_reason = (
                                    "missing_model_output"
                                    if not _model_response_has_output(response)
                                    else "missing_visible_answer"
                                )
                                model_messages.append(
                                    self._terminal_synthesis_message(
                                        reason=final_synthesis_reason,
                                    )
                                )
                                continue
                            raise _MissingVisibleFinalAnswer()
                    persona_report = None
                    need_persona = False
                    need_ququ = False
                    if response is not None:
                        response_text = response.message.text_content()
                        if (
                            not internal_worker_round
                            and persona_turn_plan is not None
                            and persona_turn_plan.require_visible
                        ):
                            persona_report = evaluate_persona_delivery(
                                persona_turn_plan,
                                response_text,
                            )
                            need_persona = not persona_report.delivered
                        need_ququ = (
                            (not internal_worker_round)
                            and bool(
                                runtime_metadata.get("ququ_hard_required")
                                or (
                                    persona_turn_plan is not None
                                    and persona_turn_plan.ququ_bypass_worthy
                                )
                            )
                            and not _extract_ququ_line(response_text)
                        )
                        if need_persona or need_ququ:
                            self._post_answer_reflection_request = (
                                PostAnswerReflectionRequest(
                                    agent_id=request.agent_id,
                                    workspace_dir=(
                                        _runtime_state_workspace_dir(
                                            runtime_input
                                        )
                                        or request.workspace_dir
                                        or ""
                                    ),
                                    chat_id=str(request.chat_id or ""),
                                    session_id=request.session_id,
                                    run_id=turn_id,
                                    client_turn_id=(
                                        current_client_turn_id or turn_id
                                    ),
                                    answer_message_id=str(
                                        response.message.id or turn_id
                                    ),
                                    user_message=_latest_user_text(
                                        runtime_input
                                    ),
                                    final_answer=response_text,
                                    plan=(
                                        persona_turn_plan.to_dict()
                                        if persona_turn_plan is not None
                                        else {}
                                    ),
                                    subject_context=(
                                        dict(persona_turn_plan.subject_context)
                                        if persona_turn_plan is not None
                                        else {}
                                    ),
                                    need_persona=need_persona,
                                    need_ququ=need_ququ,
                                    assistant_history=[
                                        message.text_content()
                                        for message in model_messages
                                        if message.role == "assistant"
                                    ],
                                )
                            )
                    if persona_report is not None:
                        persona_delivery = persona_report.to_dict(
                            attempts=1 if need_persona else 0,
                            runtime="native",
                            fallback_guard=False,
                        )
                        response.message.metadata = {
                            **dict(response.message.metadata or {}),
                            "persona_delivery": persona_delivery,
                        }
                        trace_workspace = str(
                            runtime_metadata.get("state_workspace_dir")
                            or request.workspace_dir
                            or ""
                        ).strip()
                        if trace_workspace:
                            try:
                                record_delivered_persona_memory(
                                    trace_workspace,
                                    query=_latest_user_text(runtime_input),
                                    plan=persona_turn_plan,
                                    report=persona_report,
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to merge delivered persona memory "
                                    "into the answer recall trace",
                                    exc_info=True,
                                )
                        for (
                            deferred_event
                        ) in deferred_persona_completion_events:
                            rendered_event = _event_with_persona_delivery(
                                deferred_event,
                                persona_delivery,
                            )
                            if _synthesis_boundary_is_incomplete(
                                final_synthesis_reason
                            ):
                                rendered_event = _event_with_outcome_status(
                                    rendered_event,
                                    outcome_status="incomplete",
                                    boundary_reason=final_synthesis_reason,
                                )
                            yield assistant_artifact_projection.project(
                                rendered_event,
                            )
                        deferred_persona_completion_events.clear()
                    elif deferred_persona_completion_events:
                        for (
                            deferred_event
                        ) in deferred_persona_completion_events:
                            if _synthesis_boundary_is_incomplete(
                                final_synthesis_reason
                            ):
                                deferred_event = _event_with_outcome_status(
                                    deferred_event,
                                    outcome_status="incomplete",
                                    boundary_reason=final_synthesis_reason,
                                )
                            yield assistant_artifact_projection.project(
                                deferred_event,
                            )
                        deferred_persona_completion_events.clear()
                    if assistant_artifact_projection.has_pending():
                        seq += 1
                        pending_artifact_event = (
                            assistant_artifact_projection.pending_completion(
                                seq=seq,
                                session_id=request.session_id,
                                turn_id=turn_id,
                            )
                        )
                        if pending_artifact_event is not None:
                            yield pending_artifact_event
                    if (
                        inbox_admission is not None
                        and not inbox_admission.close_if_drained(
                            after_seq=inbox_after_seq,
                        )
                    ):
                        # Admission and closing share the Run Inbox lock. If a
                        # current-Run input won that race, continue the sole
                        # owner loop and consume it instead of persisting a
                        # terminal Run with an admitted orphan.
                        continue
                    await persist_checkpoint(
                        "completed",
                        force=True,
                        next_execution_position="terminal",
                    )
                    yield turn_lifecycle.complete(
                        seq=seq + 1,
                        payload={
                            "durable_artifact_required": (
                                recover_generated_delivery
                            ),
                            "outcome_status": (
                                "incomplete"
                                if _synthesis_boundary_is_incomplete(
                                    final_synthesis_reason
                                )
                                else "delivered"
                            ),
                            "reason": final_synthesis_reason or None,
                        },
                    )
                    return

                for tool_call in tool_calls:
                    if (
                        current_analysis_message is not None
                        and tool_call.name == "create_analysis_presentation"
                    ):
                        # Once the model has selected the sealed-analysis
                        # presentation path, keep retries on its bounded status
                        # and analysis tools even when the first call fails.
                        # An internal renderer failure must not implicitly
                        # broaden the turn into loader, shell, or Python access.
                        analysis_presentation_attempted = True
                    terminal_required_failure: _RequiredCapabilityFailed | None = (
                        None
                    )
                    required_capability_retry_count = 0
                    call_fingerprint = _tool_call_fingerprint(tool_call)
                    transport_call_id = str(tool_call.id or "").strip()
                    prior_transport_call = completed_transport_calls.get(
                        transport_call_id
                    )
                    if prior_transport_call is not None:
                        prior_fingerprint, prior_result = prior_transport_call
                        if prior_fingerprint != call_fingerprint:
                            raise _ModelProtocolError(
                                "provider reused tool call id "
                                f"{transport_call_id} for a different operation"
                            )
                        model_messages.append(
                            self._tool_result_model_message(prior_result)
                        )
                        continue
                    if call_fingerprint in quarantined_calls:
                        model_messages.append(
                            self._skipped_tool_result(
                                tool_call,
                                reason=quarantined_calls[call_fingerprint],
                            )
                        )
                        continue
                    if (
                        tool_call.name == _SUBAGENT_BATCH_TOOL
                        and successful_tool_counts.get(
                            _SUBAGENT_BATCH_TOOL,
                            0,
                        )
                        > 0
                    ):
                        # A hosted collaboration room is the single evidence
                        # handoff for this Run.  After that handoff succeeds,
                        # the parent keeps working with its other capabilities;
                        # a stale or adversarial provider response must not
                        # open a second room with a different call id.
                        model_messages.append(
                            self._skipped_tool_result(
                                tool_call,
                                reason=(
                                    "hosted collaboration already completed; "
                                    "continue as the parent Agent"
                                ),
                            )
                        )
                        continue
                    is_resumed_pending_call = bool(
                        resumed_pending_tool_call is not None
                        and tool_call.id == resumed_pending_tool_call.id
                        and tool_call.name == resumed_pending_tool_call.name
                    )
                    if not is_resumed_pending_call:
                        tool_call_count += 1
                        segment_tool_calls += 1
                    if (
                        max_tool_calls is not None
                        and tool_call_count > max_tool_calls
                    ):
                        if not final_synthesis_only:
                            final_synthesis_only = True
                            final_synthesis_reason = "tool_call_budget"
                            model_messages.append(
                                self._terminal_synthesis_message(
                                    reason=final_synthesis_reason,
                                )
                            )
                        model_messages.append(
                            self._skipped_tool_result(
                                tool_call,
                                reason="tool call budget reached",
                            )
                        )
                        continue
                    spec = tool_specs_by_name.get(tool_call.name)
                    if (
                        tool_call.name == FILE_DELIVERY_TOOL_NAME
                        and self._artifact_effect_journal is not None
                    ):
                        requested_delivery_path = (
                            resolve_workspace_deliverable_path(
                                str(workspace_path),
                                tool_call.arguments.get("file_path"),
                                require_file=False,
                            )
                        )
                        expected_delivery_key = delivery_idempotency_keys.get(
                            requested_delivery_path or ""
                        )
                        current_delivery = recover_artifact_delivery(
                            self._artifact_effect_journal,
                            effect_journal=self._effect_journal,
                            run_id=turn_id,
                            attempt_id=artifact_attempt_id,
                            workspace_dir=workspace_path,
                        )
                        current_delivery_key = (
                            current_delivery.idempotency_keys.get(
                                requested_delivery_path or ""
                            )
                        )
                        if current_delivery_key and not expected_delivery_key:
                            delivery_idempotency_keys[
                                requested_delivery_path or ""
                            ] = current_delivery_key
                            expected_delivery_key = current_delivery_key
                        if (
                            deliverable_contract_declared
                            and requested_delivery_path
                            and not expected_delivery_key
                        ):
                            file_delivery_requested = False
                            file_ready_for_delivery = False
                            file_delivery_completed = False
                            visual_candidate_paths.add(requested_delivery_path)
                            deferred_persona_completion_events.clear()
                            unverified_result = ToolResult(
                                call_id=tool_call.id,
                                name=tool_call.name,
                                ok=False,
                                error=(
                                    "Artifact has not been verified; finalize "
                                    "this revision before delivery."
                                ),
                                metadata={
                                    "error_code": "artifact_not_verified",
                                    "deterministic_failure": True,
                                    "delivery_status": "candidate",
                                    "candidate_preserved": True,
                                    "candidate_path": requested_delivery_path,
                                },
                            )
                            if transport_call_id:
                                completed_transport_calls[
                                    transport_call_id
                                ] = (
                                    call_fingerprint,
                                    unverified_result.model_copy(deep=True),
                                )
                            seq += 1
                            yield self._tool_completed_event(
                                unverified_result,
                                runtime_input=runtime_input,
                                turn_id=turn_id,
                                client_turn_id=current_client_turn_id,
                                seq=seq,
                            )
                            seq += 1
                            yield RuntimeEvent(
                                id=str(uuid4()),
                                seq=seq,
                                type="runtime.warning",
                                status="failed",
                                source="system",
                                session_id=request.session_id,
                                turn_id=turn_id,
                                summary="已阻止发送尚未验收的文件候选。",
                                payload={
                                    "kind": "artifact_delivery_unverified",
                                    "error_code": "artifact_not_verified",
                                    "details": {
                                        "delivery_status": "candidate",
                                        "candidate_path": (
                                            requested_delivery_path
                                        ),
                                    },
                                },
                            )
                            model_messages.append(
                                self._tool_result_model_message(
                                    unverified_result
                                )
                            )
                            model_messages.append(
                                ModelMessage.text(
                                    "system",
                                    "发送已被阻止：主产物仍是未验收候选。"
                                    "请先实际检查当前版本，并调用 "
                                    "finalize_deliverable 生成与当前字节绑定的"
                                    "验证回执后再交付。",
                                    metadata={
                                        "instruction_trust": "trusted_system",
                                        "instruction_origin": (
                                            "runtime.artifact_verification_fence"
                                        ),
                                    },
                                )
                            )
                            continue
                        if expected_delivery_key:
                            if current_delivery_key != expected_delivery_key:
                                stale_path = requested_delivery_path or str(
                                    tool_call.arguments.get("file_path") or ""
                                )
                                file_delivery_requested = False
                                file_ready_for_delivery = False
                                file_delivery_completed = False
                                if stale_path:
                                    visual_candidate_paths.add(stale_path)
                                stale_result = ToolResult(
                                    call_id=tool_call.id,
                                    name=tool_call.name,
                                    ok=False,
                                    error=(
                                        "Artifact bytes changed after verification; "
                                        "re-verify this revision before delivery."
                                    ),
                                    metadata={
                                        "error_code": (
                                            "artifact_revision_changed"
                                        ),
                                        "deterministic_failure": True,
                                        "delivery_status": "candidate",
                                        "candidate_preserved": bool(
                                            stale_path
                                        ),
                                        "candidate_path": stale_path,
                                        "invalid_artifact_ids": list(
                                            current_delivery.invalid_artifact_ids
                                        ),
                                    },
                                )
                                if transport_call_id:
                                    completed_transport_calls[
                                        transport_call_id
                                    ] = (
                                        call_fingerprint,
                                        stale_result.model_copy(deep=True),
                                    )
                                seq += 1
                                yield self._tool_completed_event(
                                    stale_result,
                                    runtime_input=runtime_input,
                                    turn_id=turn_id,
                                    client_turn_id=current_client_turn_id,
                                    seq=seq,
                                )
                                seq += 1
                                yield RuntimeEvent(
                                    id=str(uuid4()),
                                    seq=seq,
                                    type="runtime.warning",
                                    status="failed",
                                    source="system",
                                    session_id=request.session_id,
                                    turn_id=turn_id,
                                    summary=("已阻止用旧验证发送已变化的文件。"),
                                    payload={
                                        "kind": "artifact_verification_stale",
                                        "error_code": (
                                            "artifact_revision_changed"
                                        ),
                                        "details": {
                                            "delivery_status": "candidate",
                                            "candidate_path": stale_path,
                                        },
                                    },
                                )
                                model_messages.append(
                                    self._tool_result_model_message(
                                        stale_result
                                    )
                                )
                                model_messages.append(
                                    ModelMessage.text(
                                        "system",
                                        "发送已被阻止：主产物字节在验证后发生变化。"
                                        "请重新检查当前版本，并再次调用 "
                                        "finalize_deliverable 生成与当前字节绑定的"
                                        "验证回执后再交付。",
                                        metadata={
                                            "instruction_trust": "trusted_system",
                                            "instruction_origin": (
                                                "runtime.artifact_revision_fence"
                                            ),
                                        },
                                    )
                                )
                                continue
                    delivery_candidates = _tool_declared_delivery_candidates(
                        tool_call,
                        spec=spec,
                        workspace_dir=str(workspace_path),
                    )
                    delivery_fingerprints_before = {
                        path: _file_fingerprint(path)
                        for path in delivery_candidates
                    }
                    await persist_checkpoint(
                        (
                            "before_external_effect"
                            if spec is None or not spec.read_only
                            else "before_tool"
                        ),
                        force=True,
                        next_execution_position="before_tool",
                        pending_tool_call=tool_call,
                    )
                    should_emit_collaboration_preparing = bool(
                        tool_call.name == _SUBAGENT_BATCH_TOOL
                        and not collaboration_preparing_emitted
                    )
                    tool_transaction = self._run_tool_call(
                        tool_call,
                        runtime_input=runtime_input,
                        turn_id=turn_id,
                        client_turn_id=current_client_turn_id,
                        seq_start=seq,
                        policy_context=policy_context,
                        policy_observation_invocation=(
                            request_policy_observation_invocation
                        ),
                        budget=budget,
                        expected_file_extensions=expected_file_extensions,
                        inbox_after_seq=inbox_after_seq,
                        collaboration_preparing_already_emitted=(
                            collaboration_preparing_emitted
                        ),
                    )
                    try:
                        async for event, result in self._durable_tool_completion_events(
                            tool_transaction, persist_checkpoint=persist_checkpoint,
                            runtime_input=runtime_input, turn_id=turn_id,
                            client_turn_id=current_client_turn_id, spec=spec,
                        ):
                            seq = event.seq
                            yield event
                    finally:
                        await close_owned_async_iterator(
                            tool_transaction,
                            task_name=f"native-tool-close-{tool_call.id}",
                        )
                    if transport_call_id:
                        completed_transport_calls[transport_call_id] = (
                            call_fingerprint,
                            result.model_copy(deep=True),
                        )
                    private_editor_completion.observe(tool_call.name, result)
                    if should_emit_collaboration_preparing:
                        collaboration_preparing_emitted = True
                    quarantined_failure = False
                    if not result.ok:
                        unknown_external_effect = bool(
                            isinstance(result.metadata, dict)
                            and result.metadata.get("side_effect_outcome")
                            in {"unknown", "committed"}
                            and result.metadata.get("automatic_retry_allowed")
                            is False
                        )
                        if unknown_external_effect:
                            quarantined_calls[call_fingerprint] = (
                                "ambiguous external effect must be reconciled "
                                "before retry"
                            )
                            quarantined_failure = True
                        fingerprint = _tool_failure_fingerprint(
                            tool_call, result
                        )
                        failure_counts[fingerprint] = (
                            failure_counts.get(fingerprint, 0) + 1
                        )
                        if (
                            collaboration_required
                            and tool_call.name == _SUBAGENT_BATCH_TOOL
                            and successful_tool_counts.get(
                                _SUBAGENT_BATCH_TOOL,
                                0,
                            )
                            == 0
                        ):
                            required_collaboration_attempts += 1
                            required_capability_retry_count = (
                                required_collaboration_attempts
                            )
                            capability_error_code = _tool_result_error_code(
                                result
                            )
                            terminal_attempt = (
                                capability_error_code
                                in {
                                    "approval_denied",
                                    "policy_denied",
                                    "approval_ui_unavailable",
                                }
                                or required_collaboration_attempts >= 2
                            )
                            if terminal_attempt:
                                terminal_required_failure = (
                                    _RequiredCapabilityFailed(
                                        _SUBAGENT_BATCH_TOOL,
                                        required_collaboration_attempts,
                                        error_code=capability_error_code,
                                    )
                                )
                        deterministic_failure = bool(
                            isinstance(result.metadata, dict)
                            and result.metadata.get("deterministic_failure")
                            is True
                        )
                        if not unknown_external_effect and (
                            deterministic_failure
                            or failure_counts[fingerprint]
                            >= max_same_failure_count
                        ):
                            # Circuit-break the failing capability for this turn,
                            # including mutating tools.  Killing the entire turn
                            # here discarded useful partial work and produced the
                            # high-frequency "tool repeated/failed" dead ends seen
                            # by users.  The model may now use another capability
                            # or give an honest partial answer without more tools.
                            quarantined_calls[call_fingerprint] = (
                                "identical call quarantined after repeated failure; "
                                "change arguments or strategy"
                            )
                            quarantined_failure = True
                    if result.ok:
                        for (
                            path,
                            before,
                        ) in delivery_fingerprints_before.items():
                            after = _file_fingerprint(path)
                            if after is not None and after != before:
                                observed_generated_paths[path] = after
                    progress_policy = (
                        spec.metadata.get("progress_policy")
                        if spec is not None
                        else None
                    )
                    explicit_progress = (
                        result.metadata.get("progress_made")
                        if isinstance(result.metadata, dict)
                        else None
                    )
                    prior_successes = successful_call_counts.get(
                        call_fingerprint,
                        0,
                    )
                    prior_tool_successes = successful_tool_counts.get(
                        tool_call.name,
                        0,
                    )
                    convergence_result = result
                    if (
                        explicit_progress is None
                        and progress_policy == "first_tool_success"
                    ):
                        convergence_result = result.model_copy(
                            update={
                                "metadata": {
                                    **dict(result.metadata or {}),
                                    "progress_made": bool(
                                        result.ok and prior_tool_successes == 0
                                    ),
                                },
                            },
                        )
                    if result.ok:
                        continuation_milestones = merge_verified_milestones(
                            continuation_milestones,
                            verified_milestones_from_tool_result(
                                result,
                                acceptance=task_acceptance,
                            ),
                        )
                    convergence.observe(tool_call, convergence_result)
                    if result.ok:
                        successful_call_counts[call_fingerprint] = (
                            prior_successes + 1
                        )
                        successful_tool_counts[tool_call.name] = (
                            prior_tool_successes + 1
                        )
                        # A successful tool call and useful task progress are
                        # different facts. State loaders report whether their
                        # state actually changed; discovery/control-plane tools
                        # can opt into a once-per-tool policy so changing an
                        # argument cannot keep an empty loop alive forever.
                        if explicit_progress is True:
                            round_had_success = True
                        elif explicit_progress is False:
                            pass
                        elif progress_policy == "first_tool_success":
                            if prior_tool_successes == 0:
                                round_had_success = True
                        elif prior_successes == 0:
                            # Re-running the exact successful action may be a
                            # useful verification once, but it must not reset
                            # the no-progress budget forever.
                            round_had_success = True
                    if result.ok and tool_call.name == _SUBAGENT_SUBMIT_TOOL:
                        task_id = _subagent_task_id(result, tool_call)
                        if not task_id:
                            raise _ModelProtocolError(
                                "Sub-agent task submission succeeded without "
                                "a trackable task id."
                            )
                        outstanding_subagent_tasks.add(task_id)
                    elif result.ok and tool_call.name == _SUBAGENT_CHECK_TOOL:
                        task_id = _subagent_task_id(result, tool_call)
                        if (
                            task_id
                            and _subagent_status(result)
                            in _TERMINAL_SUBAGENT_STATUSES
                        ):
                            outstanding_subagent_tasks.discard(task_id)
                    if result.ok and tool_call.name == "finalize_deliverable":
                        delivery_automation_suspended = False
                        primary_delivery = project_committed_primary_delivery(
                            result.metadata,
                            workspace_dir=workspace_path,
                            legacy_primary_path=(
                                tool_call.arguments.get("primary_path")
                                if self._artifact_effect_journal is None
                                else None
                            ),
                        )
                        if self._artifact_effect_journal is not None:
                            primary_delivery = recover_artifact_delivery(
                                self._artifact_effect_journal,
                                effect_journal=self._effect_journal,
                                run_id=turn_id,
                                attempt_id=artifact_attempt_id,
                                workspace_dir=workspace_path,
                            )
                            declared_delivery_paths = list(
                                primary_delivery.pending_paths
                            )
                            # A prior successful delivery covers only its bytes.
                            file_delivery_completed = (
                                primary_delivery.completed
                            )
                        for resolved in primary_delivery.pending_paths:
                            if resolved not in declared_delivery_paths:
                                declared_delivery_paths.append(resolved)
                            delivery_key = (
                                primary_delivery.idempotency_keys.get(
                                    resolved,
                                    "",
                                )
                            )
                            if delivery_key:
                                delivery_idempotency_keys[
                                    resolved
                                ] = delivery_key
                            suffix = Path(resolved).suffix.lower()
                            expected_file_extensions = (
                                frozenset({suffix}) if suffix else frozenset()
                            )
                            file_delivery_requested = True
                            file_ready_for_delivery = True
                    if (
                        result.ok
                        and tool_call.name
                        == _RESOLVE_PENDING_DELIVERABLE_TOOL
                    ):
                        resolution_source = str(
                            tool_call.arguments.get("source_run_id") or ""
                        ).strip()
                        resolution_decision = str(
                            tool_call.arguments.get("decision") or ""
                        ).strip()
                        if (
                            resolution_source == pending_source_run_id
                            and resolution_decision
                            in {"continue", "replace", "cancel"}
                        ):
                            pending_choice_resolved = True
                            if resolution_decision == "continue":
                                raw_extensions = (
                                    pending_inherited_contract.get(
                                        "accepted_extensions"
                                    )
                                    if isinstance(
                                        pending_inherited_contract, dict
                                    )
                                    else ()
                                )
                                inherited_extensions = frozenset(
                                    extension
                                    for extension in (
                                        str(item or "").strip().lower()
                                        for item in (
                                            raw_extensions
                                            if isinstance(
                                                raw_extensions, (list, tuple)
                                            )
                                            else ()
                                        )
                                    )
                                    if extension
                                )
                                if inherited_extensions:
                                    expected_file_extensions = (
                                        inherited_extensions
                                    )
                                    deliverable_contract_declared = True
                                    recover_generated_delivery = True
                                    generated_file_baseline = (
                                        snapshot_generated_files(
                                            str(workspace_path)
                                        )
                                    )
                    if (
                        result.ok
                        and tool_call.name
                        == _DECLARE_DELIVERABLE_CONTRACT_TOOL
                    ):
                        raw_extensions = tool_call.arguments.get(
                            "accepted_extensions"
                        )
                        if isinstance(raw_extensions, (list, tuple)):
                            normalized_extensions = frozenset(
                                extension
                                for extension in (
                                    (
                                        text
                                        if text.startswith(".")
                                        else f".{text}"
                                    )
                                    for text in (
                                        str(item or "").strip().lower()
                                        for item in raw_extensions
                                    )
                                    if text
                                )
                                if extension
                            )
                            if normalized_extensions:
                                expected_file_extensions = (
                                    normalized_extensions
                                )
                                deliverable_contract_declared = True
                                recover_generated_delivery = True
                                generated_file_baseline = (
                                    snapshot_generated_files(
                                        str(workspace_path)
                                    )
                                )
                    pptx_skill_candidate = _pptx_skill_output_candidate(
                        tool_call,
                        result,
                        workspace_dir=str(workspace_path),
                    )
                    if pptx_skill_candidate is not None:
                        expected_file_extensions = frozenset({".pptx"})
                        recover_generated_delivery = True
                        # A generator result proves that bytes exist. It does
                        # not prove those bytes passed the bound visual checks.
                        # Keep all repair and inspection tools available until
                        # finalize_deliverable promotes this exact revision.
                        file_ready_for_delivery = False
                        visual_candidate_paths.add(pptx_skill_candidate)
                        observed_generated_paths[
                            pptx_skill_candidate
                        ] = _file_fingerprint(pptx_skill_candidate)
                        candidate_artifact_payloads: list[
                            dict[str, object]
                        ] = []
                        if (
                            self._artifact_effect_journal is not None
                            and self._effect_journal is not None
                        ):
                            candidate_run_id = str(
                                request.run_id or turn_id or request.session_id
                            ).strip()
                            candidate_attempt_id = str(
                                tool_call.metadata.get("attempt_id")
                                or runtime_metadata.get("attempt_id")
                                or f"{candidate_run_id}:native-attempt"
                            ).strip()
                            candidate_effect_id = str(
                                tool_call.metadata.get("effect_id")
                                or runtime_metadata.get("effect_id")
                                or f"effect:{tool_call.id}"
                            ).strip()
                            source_effect = self._effect_journal.latest_effect(
                                candidate_run_id,
                                candidate_attempt_id,
                                candidate_effect_id,
                            )
                            if source_effect is None:
                                raise RuntimeError(
                                    "PPT candidate has no settled source effect."
                                )
                            candidate_records = register_artifact_candidate(
                                self._artifact_effect_journal,
                                source_effect=source_effect,
                                tool_call=tool_call,
                                candidate_path=pptx_skill_candidate,
                                result=result,
                                workspace_dir=workspace_path,
                            )
                            candidate_artifact_payloads = [
                                artifact_effect_public_payload(record)
                                for record in candidate_records
                            ]
                            result = result.model_copy(
                                update={
                                    "metadata": {
                                        **dict(result.metadata or {}),
                                        "artifact_candidates": (
                                            candidate_artifact_payloads
                                        ),
                                        "delivery_status": "candidate",
                                    }
                                }
                            )
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="info",
                            source="system",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary="PPT 文件候选已保留，等待视觉验收。",
                            payload={
                                "kind": "artifact_candidate_registered",
                                "details": {
                                    "delivery_status": "candidate",
                                    "candidate_path": pptx_skill_candidate,
                                    "artifact_effects": (
                                        candidate_artifact_payloads
                                    ),
                                },
                            },
                        )
                    if (
                        result.ok
                        and pptx_skill_candidate is None
                        and has_matching_file_delivery_block(
                            result.content,
                            expected_file_extensions,
                        )
                    ):
                        file_delivery_completed = True
                    if result.ok and tool_call.name == FILE_DELIVERY_TOOL_NAME:
                        archive_member_paths = (
                            select_archive_member_delivery_paths(
                                str(workspace_path),
                                tool_call.arguments.get("file_path"),
                            )
                        )
                        if archive_member_paths:
                            for member_path in archive_member_paths:
                                if member_path not in declared_delivery_paths:
                                    declared_delivery_paths.append(member_path)
                            expected_file_extensions = frozenset(
                                Path(path).suffix.lower()
                                for path in archive_member_paths
                            )
                            file_delivery_requested = True
                            file_ready_for_delivery = True
                            # The ZIP is already the batch download.  Complete
                            # this turn only after every represented member has
                            # also been exposed as its own named attachment.
                            file_delivery_completed = False
                    assistant_artifact_projection.observe(spec, result)
                    model_messages.append(
                        self._tool_result_model_message(result)
                    )
                    if pptx_skill_candidate is not None:
                        model_messages.append(
                            ModelMessage.text(
                                "system",
                                "PPT 生成器已产生真实主产物候选："
                                f"{pptx_skill_candidate}。请继续使用可用工具完成"
                                "渲染、视觉检查和必要修复；候选文件不等于验收"
                                "通过。只有实际验收通过后，才能用 "
                                "finalize_deliverable 绑定这一版文件及其验证证据。",
                                metadata={
                                    "instruction_trust": "trusted_system",
                                    "instruction_origin": (
                                        "runtime.pptx_skill_artifact_recovery"
                                    ),
                                },
                            )
                        )
                    if (
                        result.ok
                        and tool_call.name == _SUBAGENT_BATCH_TOOL
                        and not final_synthesis_only
                    ):
                        # A hosted room produces consultation evidence; only
                        # the parent Agent owns acceptance and user-visible
                        # delivery. Convergence policy limits repeated rooms by
                        # actual evidence and progress instead of disabling the
                        # collaboration capability after its first success.
                        model_messages.append(
                            self._collaboration_handoff_message(),
                        )
                    if terminal_required_failure is not None:
                        deferred_required_capability_message_events.clear()
                        raise terminal_required_failure
                    if required_capability_retry_count:
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="running",
                            source="system",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary="协作室组建遇到问题，正在修正配置。",
                            payload={
                                "kind": "required_capability_retrying",
                                "error_code": _tool_result_error_code(result),
                                "trace_title": "正在修正协作配置",
                                "trace_detail": "沈行知正在重新选择成员与分工",
                                "details": {
                                    "capability": _SUBAGENT_BATCH_TOOL,
                                    "attempt": required_capability_retry_count,
                                    "maximum_attempts": 2,
                                },
                            },
                        )
                    if quarantined_failure:
                        if tool_call.name == FILE_DELIVERY_TOOL_NAME:
                            file_delivery_requested = False
                        seq += 1
                        yield RuntimeEvent(
                            id=str(uuid4()),
                            seq=seq,
                            type="runtime.warning",
                            status="failed",
                            source="system",
                            session_id=request.session_id,
                            turn_id=turn_id,
                            summary=(
                                f"已暂停本轮中反复失败的调用 "
                                f"{tool_call.name}，避免以同样参数无效重试。"
                            ),
                            payload={
                                "kind": "tool_quarantined",
                                "error": {
                                    "tool_name": tool_call.name,
                                    "attempts": failure_counts[fingerprint],
                                    "call_fingerprint": call_fingerprint,
                                },
                            },
                        )
                        model_messages.append(
                            self._tool_quarantine_recovery_message(
                                tool_call.name
                            )
                        )
                    await persist_checkpoint(
                        "tool_boundary",
                        force=True,
                        next_execution_position="after_tool",
                    )
                    if is_resumed_pending_call:
                        resumed_pending_tool_call = None

                legacy_no_progress_rounds = no_progress_rounds
                convergence_decision = convergence.finish_round(
                    model_round_count=model_round_count,
                    max_model_rounds=max_model_rounds,
                    used_tokens=used_tokens,
                    max_tokens=max_tokens,
                )
                no_progress_rounds = convergence.consecutive_no_progress_rounds
                if convergence.mode in {"off", "shadow"}:
                    if (
                        convergence.mode == "shadow"
                        and convergence_decision.should_intervene
                    ):
                        seq += 1
                        yield self._convergence_runtime_event(
                            seq=seq,
                            session_id=request.session_id,
                            turn_id=turn_id,
                            decision=convergence_decision,
                            shadow=True,
                        )
                    no_progress_rounds = legacy_no_progress_rounds
                    if round_had_success:
                        no_progress_rounds = 0
                    else:
                        no_progress_rounds += 1
                        if (
                            max_no_progress_rounds is not None
                            and no_progress_rounds >= max_no_progress_rounds
                        ):
                            final_synthesis_only = True
                            final_synthesis_reason = "no_progress"
                            model_messages.append(
                                self._terminal_synthesis_message(
                                    reason=final_synthesis_reason,
                                )
                            )
                            no_progress_rounds = 0
                    continue

                if not convergence_decision.should_intervene:
                    continue

                if convergence_decision.action == "review":
                    if (
                        max_model_rounds is not None
                        and model_round_count
                        >= max_model_rounds
                        - convergence.policy.reserve_synthesis_rounds
                    ):
                        convergence_decision = convergence.record_review(
                            "synthesize",
                            reason="review_skipped_to_reserve_synthesis_round",
                        )
                    else:
                        model_round_count += 1
                        try:
                            (
                                review_action,
                                review_reason,
                                review_usage,
                            ) = await self._run_convergence_review(
                                model_client=model_client,
                                runtime_input=runtime_input,
                                turn_id=turn_id,
                                budget=budget,
                                prompt=build_convergence_review_prompt(
                                    objective=convergence.objective,
                                    acceptance=convergence.acceptance,
                                    trigger=convergence_decision,
                                ),
                                request_ordinal=model_round_count,
                            )
                            if review_usage is not None:
                                review_tokens = int(
                                    review_usage.total_tokens
                                    or review_usage.input_tokens
                                    + review_usage.output_tokens
                                )
                                used_tokens += review_tokens
                                segment_tokens += review_tokens
                        except _ModelRequestAdmissionDenied:
                            raise
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # pylint: disable=broad-except
                            review_action = "synthesize"
                            review_reason = "convergence_review_unavailable"
                        convergence_decision = convergence.record_review(
                            review_action,
                            reason=review_reason,
                        )
                        if convergence_decision.action == "continue":
                            model_messages.append(
                                ModelMessage.text(
                                    "system",
                                    (
                                        "一次性收敛检查允许再执行一轮。"
                                        "只能选择一个能明确改变未完成验收项的步骤；"
                                        "如果该步骤没有产生新证据或状态变化，"
                                        "下一轮必须直接总结，不再更换近义参数重试。"
                                    ),
                                    metadata={
                                        "instruction_trust": "trusted_system",
                                        "instruction_origin": (
                                            "runtime.convergence_continue"
                                        ),
                                    },
                                )
                            )
                            continue

                if convergence_decision.action in {"synthesize", "checkpoint"}:
                    seq += 1
                    yield self._convergence_runtime_event(
                        seq=seq,
                        session_id=request.session_id,
                        turn_id=turn_id,
                        decision=convergence_decision,
                        shadow=False,
                    )
                    final_synthesis_only = True
                    final_synthesis_reason = convergence_decision.reason
                    model_messages.append(
                        self._terminal_synthesis_message(
                            reason=final_synthesis_reason,
                            checkpoint=(
                                convergence_decision.action == "checkpoint"
                            ),
                        )
                    )
                    await persist_checkpoint(
                        "convergence_boundary", force=True
                    )

            # This path is reachable only in explicit finite diagnostic/test
            # harnesses. Product turns pass max_iters=None and converge through
            # progress/failure semantics instead of a fixed iteration quota.
            raise _TaskBudgetExceeded("max_model_rounds", self._max_iters)
        except _ModelRequestAdmissionDenied as exc:
            logger.info("Blocked runtime model request: %s", exc)
            return
        except asyncio.CancelledError:
            try:
                await persist_checkpoint("interrupted", force=True)
            except _RuntimeDurabilityBlocked:
                logger.error(
                    "Runtime cancellation could not persist its interrupted "
                    "checkpoint",
                    exc_info=True,
                )
            yield turn_lifecycle.interrupt(
                seq=seq + 1,
            )
            raise
        except Exception as exc:
            if not isinstance(exc, _RuntimeDurabilityBlocked):
                try:
                    if isinstance(exc, RuntimeBudgetAdmissionError):
                        await persist_checkpoint(
                            "awaiting_budget",
                            force=True,
                            status="paused",
                            wait_reason="budget",
                        )
                    else:
                        await persist_checkpoint("failed", force=True)
                except _RuntimeDurabilityBlocked as durability_exc:
                    logger.error(
                        "Runtime failure could not persist its failed checkpoint",
                        exc_info=True,
                    )
                    exc = durability_exc
            summary = _exception_message(exc, fallback="Native runtime")
            error_code = None
            outcome_status = "incomplete"
            if isinstance(exc, _RuntimeDurabilityBlocked):
                error_code = "runtime_durability_blocked"
                outcome_status = "blocked"
                summary = "运行状态无法安全持久化，本轮已在继续执行外部写入前停止。"
            elif isinstance(exc, RuntimeBudgetAdmissionError):
                error_code = exc.code
                outcome_status = "blocked"
                summary = "费用达到你设定的范围，已保留当前成果，" "等待你明确提高预算或调整允许的模型。"
            elif isinstance(exc, _TaskBudgetExceeded):
                error_code = "task_budget_exceeded"
                outcome_status = "blocked"
            elif isinstance(exc, _RepeatedToolFailure):
                error_code = "repeated_tool_failure"
                outcome_status = "blocked"
                summary = f"同一工具连续失败 {exc.count} 次，本轮已停止，" "避免无效重试。"
            elif isinstance(exc, _RequiredCapabilityFailed):
                error_code = "required_capability_failed"
                outcome_status = "blocked"
                if exc.capability == _SUBAGENT_BATCH_TOOL:
                    summary = "协作室未能安全建立，已停止自动重试，" "没有把未完成的协作冒充为答案。"
                elif exc.capability == FILE_DELIVERY_TOOL_NAME:
                    summary = "文件已保留，但未能安全附加到对话；" "已停止自动重试，本轮不会冒充为交付成功。"
                else:
                    summary = "必需能力执行失败，已停止自动重试。"
            elif isinstance(exc, _ModelStreamDeadlineExceeded):
                error_code = (
                    "model_request_timeout"
                    if exc.phase == "request"
                    else (
                        "model_first_event_timeout"
                        if exc.phase == "first_event"
                        else "model_stream_idle_timeout"
                    )
                )
            elif _is_retryable_model_stream_transport_error(exc):
                error_code = "provider_connection_interrupted"
                summary = "模型连接反复中断，已保留当前进度并停止自动续接。"
            elif isinstance(exc, _MissingVisibleFinalAnswer):
                error_code = "missing_visible_answer"
            elif isinstance(exc, _IncompleteModelOutput):
                error_code = "model_output_truncated"
                summary = "模型输出在正文完成前被截断，本轮不能冒充已交付。"
            elif isinstance(exc, _DegenerateModelOutput):
                error_code = "model_output_invalid"
                outcome_status = "blocked"
                summary = "模型返回了大量未解析占位符，系统已拦截，" "本轮未作为有效交付保存。"
            elif isinstance(exc, _ToolArgumentRepairExhausted):
                error_code = "tool_argument_repair_exhausted"
                outcome_status = "blocked"
                summary = "模型连续未能给出完整操作信息，本轮已停止自动重试。"
            elif isinstance(exc, _ContinuationSourceUnavailable):
                error_code = "continuation_source_unavailable"
                outcome_status = "blocked"
                summary = (
                    "早期图片或文件已无法按原授权与原字节安全复用，"
                    "本轮已停止；请重新授权或补充该材料。"
                )
            if file_delivery_completed and not isinstance(
                exc,
                _RuntimeDurabilityBlocked,
            ):
                if assistant_artifact_projection.has_pending():
                    seq += 1
                    pending_artifact_event = (
                        assistant_artifact_projection.pending_completion(
                            seq=seq,
                            session_id=request.session_id,
                            turn_id=turn_id,
                        )
                    )
                    if pending_artifact_event is not None:
                        yield pending_artifact_event
                seq += 1
                for terminal_event in turn_lifecycle.partial_delivery_failure(
                    seq=seq,
                    failure_summary=summary,
                    error_type=type(exc).__name__,
                ):
                    yield terminal_event
                return
            failure_payload: dict[str, Any] = {
                "error_type": type(exc).__name__,
                "error_code": error_code,
                "outcome_status": outcome_status,
            }
            if isinstance(exc, _ModelStreamDeadlineExceeded):
                failure_payload["error"] = {
                    "timeout_phase": exc.phase,
                    "timeout_seconds": exc.timeout_seconds,
                }
            elif isinstance(exc, _IncompleteModelOutput):
                failure_payload["error"] = {
                    "finish_reason": exc.finish_reason,
                    "recovery_attempts": exc.recovery_attempts,
                }
            elif isinstance(exc, _DegenerateModelOutput):
                failure_payload["error"] = {"reason": exc.reason}
            elif isinstance(exc, _RequiredCapabilityFailed):
                failure_payload["error"] = {
                    "capability": exc.capability,
                    "attempts": exc.count,
                    "cause_code": exc.error_code,
                }
            elif isinstance(exc, _RuntimeDurabilityBlocked):
                failure_payload["error"] = {
                    "stage": exc.stage,
                    "kind": exc.kind,
                    "cause_type": exc.cause_type,
                    "attempts": exc.attempts,
                }
            elif isinstance(exc, _ToolArgumentRepairExhausted):
                failure_payload["error"] = {"attempts": exc.attempts}
            elif isinstance(exc, RuntimeBudgetAdmissionError):
                failure_payload["error"] = {
                    "run_id": exc.run_id,
                    "reservation_id": exc.reservation_id,
                }
            elif isinstance(exc, _ContinuationSourceUnavailable):
                failure_payload["error"] = {
                    "kind": exc.conflict.kind,
                    "inbox_seq": exc.conflict.item_seq,
                    "client_turn_id": exc.conflict.client_turn_id,
                    "reason": exc.conflict.reason,
                }
            yield turn_lifecycle.fail(
                seq=seq + 1,
                summary=summary,
                payload=failure_payload,
            )
            raise

    @staticmethod
    def _tool_quarantine_recovery_message(tool_name: str) -> ModelMessage:
        return ModelMessage.text(
            "system",
            (
                f"工具 {tool_name} 的这一组参数已连续返回同一错误，"
                "本轮已暂停这个具体调用以避免重复消耗。"
                "请根据当前信息自行判断：可以更换参数或其他能力，"
                "也可以直接回答并如实说明未完成部分。"
                "不要重复原调用，不要虚构成功。"
            ),
            metadata={
                "instruction_trust": "trusted_system",
                "instruction_origin": "runtime.tool_quarantine_recovery",
            },
        )

    @staticmethod
    def _skipped_tool_result(
        tool_call: ToolCall, *, reason: str
    ) -> ModelMessage:
        message = NativeLantaiRuntime._tool_result_model_message(
            ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                ok=False,
                content=[
                    {
                        "type": "text",
                        "text": f"Tool call was not executed: {reason}.",
                    }
                ],
                error=reason,
                metadata={"skipped": True, "reason": reason},
            )
        )
        message.metadata = {
            **message.metadata,
            "skipped": True,
            "reason": reason,
        }
        return message

    @staticmethod
    def _convergence_runtime_event(
        *,
        seq: int,
        session_id: str | None,
        turn_id: str,
        decision: ConvergenceDecision,
        shadow: bool,
    ) -> RuntimeEvent:
        checkpoint = decision.action == "checkpoint"
        return RuntimeEvent(
            id=str(uuid4()),
            seq=seq,
            type="runtime.warning",
            status="info",
            source="system",
            session_id=session_id,
            turn_id=turn_id,
            summary=(
                "检测到可能的执行循环（仅记录，未干预）。"
                if shadow
                else (
                    "已保存当前成果，等待你决定是否继续。"
                    if checkpoint
                    else "资料或执行路径已趋于饱和，正在整理现有成果。"
                )
            ),
            payload={
                "kind": (
                    "convergence_shadow"
                    if shadow
                    else (
                        "convergence_checkpoint"
                        if checkpoint
                        else "convergence_synthesis"
                    )
                ),
                "trace_title": (
                    "收敛检查" if shadow else ("已保存进度" if checkpoint else "正在收尾")
                ),
                "trace_detail": decision.summary,
                "reason": decision.reason,
                "decision": decision.action,
                "details": decision.evidence,
            },
        )

    async def _run_convergence_review(
        self,
        *,
        model_client: LantaiModelClient,
        runtime_input: RuntimeInput,
        turn_id: str,
        budget: _DeadlineBudget,
        prompt: str,
        request_ordinal: int,
    ) -> tuple[str, str, ModelUsage | None]:
        """Run at most one tool-free semantic review for a suspicious trajectory."""

        metadata = self._model_request_metadata(
            request=runtime_input.request,
            turn_id=turn_id,
            budget=budget,
            runtime_metadata=_runtime_metadata(runtime_input),
            request_ordinal=request_ordinal,
        )
        metadata["convergence_review"] = True
        self._require_model_request_admission()
        result = await self._await_with_turn_budget(
            model_client.complete(
                ModelRequest(
                    messages=[
                        ModelMessage.text(
                            "system",
                            prompt,
                            metadata={
                                "instruction_trust": "trusted_system",
                                "instruction_origin": (
                                    "runtime.convergence_review"
                                ),
                            },
                        )
                    ],
                    tools=[],
                    tool_choice="none",
                    stream=False,
                    generation={"max_tokens": 512},
                    metadata=metadata,
                ),
            ),
            budget=budget,
            timeout_seconds=_CONVERGENCE_REVIEW_TIMEOUT_SECONDS,
        )
        action, reason = parse_convergence_review(
            result.message.text_content(),
        )
        return action, reason, result.usage

    @staticmethod
    def _terminal_synthesis_message(
        *,
        reason: str,
        checkpoint: bool = False,
    ) -> ModelMessage:
        safe_reason = {
            "token_budget": "已到达本轮上下文预算",
            "tool_call_budget": "已到达本轮工具调用预算",
            "missing_model_output": "模型未返回可用内容",
            "missing_visible_answer": "模型未返回可见答案",
        }.get(reason, "连续步骤未产生新的可验证进展")
        return ModelMessage.text(
            "system",
            (
                f"本轮因{safe_reason}，已停止继续调用工具。"
                "请现在直接给用户可见的最终答复：说清已完成的部分、"
                "未完成的部分与真实原因。普通文字回答就是有效的终止方式；"
                + (
                    "这次是可恢复检查点：请明确告诉用户当前成果已经保留，" "以及用户可以回复继续、缩小范围或调整目标。"
                    if checkpoint
                    else ""
                )
                + "不要向用户暴露任何内部停止机制的名称或术语。"
                "除非确实需要新的授权、材料或选择，不要要求用户仅回复‘继续’"
                "来恢复执行。不要声称会在后台继续，不要虚构交付。"
            ),
            metadata={
                "instruction_trust": "trusted_system",
                "instruction_origin": "runtime.terminal_synthesis",
                "runtime_checkpoint": checkpoint,
            },
        )

    @staticmethod
    def _collaboration_handoff_message() -> ModelMessage:
        return ModelMessage.text(
            "system",
            (
                "协作成员的咨询已经收口，这些结果是新的内部证据。"
                "你仍是本 Run 的唯一执行所有者和最终交付者："
                "继续核验成员结果，处理分歧、无效结果与证据缺口，"
                "并用现有能力完成用户要求的创建、读取、验证和交付。"
                "不要把成员完成误写成已经向用户交付，也不要再次开房。"
            ),
            metadata={
                "instruction_trust": "trusted_system",
                "instruction_origin": "runtime.collaboration_handoff",
                "collaboration_handoff": True,
            },
        )

    @staticmethod
    def _deep_research_final_synthesis_message() -> ModelMessage:
        return ModelMessage.text(
            "system",
            (
                "前面的深度研究轮次是内部证据交接。下一轮不再提供任何工具；"
                "你是唯一最终交付者。请基于完整研究记录核验来源，处理矛盾、"
                "不确定性与证据缺口，再以当前主 Agent 的完整人格和"
                "连续上下文直接回答用户。不要提及内部草稿或执行边界。"
            ),
            metadata={
                "instruction_trust": "trusted_system",
                "instruction_origin": "runtime.deep_research_final_synthesis",
                "deep_research_final_synthesis": True,
            },
        )

    @staticmethod
    def _completed_response_event(
        response: ModelResponse,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        seq: int,
        client_turn_id: str = "",
    ) -> RuntimeEvent:
        message = _runtime_message_from_model_response(
            response,
            strip_tool_public_progress=(
                runtime_input.request.agent_id == "default"
            ),
        )
        message.metadata = {
            **dict(message.metadata or {}),
            **_agent_presentation_context(runtime_input),
            **({"client_turn_id": client_turn_id} if client_turn_id else {}),
        }
        return RuntimeEvent(
            id=str(uuid4()),
            seq=seq,
            type="message.completed",
            status="completed",
            source="assistant",
            session_id=runtime_input.request.session_id,
            turn_id=turn_id,
            item_id=response.message.id or message.id,
            summary=message.text_content(),
            payload={"message": message.model_dump(mode="json")},
            usage=_runtime_usage(response.usage),
        )

    @staticmethod
    def _file_delivery_recovery_message(
        *,
        file_ready: bool,
        expected_extensions: frozenset[str] = frozenset(),
    ) -> ModelMessage:
        next_action = (
            f"{expected_file_type_label(expected_extensions)} 文件已经成功写入；"
            "现在只能调用 send_file_to_user，"
            "把该文件作为可点击附件发送到当前对话。"
            if file_ready
            else (
                f"请先创建并验证用户要求的 "
                f"{expected_file_type_label(expected_extensions)} 文件，"
                "再调用 send_file_to_user，"
                "把真实文件作为可点击附件发送到当前对话。"
            )
        )
        return ModelMessage.text(
            "system",
            (
                "用户要求的是文件交付，不是内部路径。"
                f"{next_action}如果附件发送失败，如实说明错误，"
                "不要用内部路径伪装成已交付。"
            ),
            metadata={
                "instruction_trust": "trusted_system",
                "instruction_origin": "runtime.file_delivery_recovery",
            },
        )

    @staticmethod
    def _durable_artifact_completion_recovery_message(
        *,
        expected_extensions: frozenset[str] = frozenset(),
    ) -> ModelMessage:
        return ModelMessage.text(
            "system",
            (
                "本轮要求交付真实的"
                f"{expected_file_type_label(expected_extensions)} 文件，"
                "在文件成功创建、验证并通过 send_file_to_user 作为附件发送前，"
                "任务尚未完成。不要仅说明进度、内部路径或请求用户再说一次继续。"
                "缺少 `python` 命令别名属于可恢复的运行时差异：优先改用 `python3`，"
                "其次使用工作区配置的捆绑运行时或已安装的等价工具，并在同一轮继续。"
                "只有穷尽安全的本地替代方案后，才可以报告真正的终端阻塞。"
            ),
            metadata={
                "instruction_trust": "trusted_system",
                "instruction_origin": (
                    "runtime.durable_artifact_completion_recovery"
                ),
            },
        )

    async def _stream_model_events(
        self,
        model_client: LantaiModelClient,
        request: ModelRequest,
        *,
        runtime_input: RuntimeInput,
        budget: _DeadlineBudget,
    ) -> AsyncIterator[ModelStreamEvent]:
        metadata = _runtime_metadata(runtime_input)
        idle_timeout = self._model_idle_timeout_seconds
        first_event_timeout = self._model_first_event_timeout_seconds
        # Model-stream deadlines are trusted construction-time policy.
        # Persisted profile/runtime metadata from older releases is data only:
        # it must not silently restore the former 300-second wall, shorten a
        # phase deadline, or let model/tool output establish system policy.
        request_timeout = self._model_request_timeout_seconds
        ignored_timeout_keys = tuple(
            key
            for key in (
                *_MODEL_FIRST_EVENT_TIMEOUT_KEYS,
                *_MODEL_IDLE_TIMEOUT_KEYS,
                *_MODEL_REQUEST_TIMEOUT_KEYS,
            )
            if key in metadata
        )
        if ignored_timeout_keys:
            logger.info(
                "Ignored legacy runtime model-stream timeout metadata",
                extra={
                    "session_id": runtime_input.request.session_id,
                    "run_id": runtime_input.request.run_id,
                    "model_request_id": request.metadata.get(
                        "model_request_id"
                    ),
                    "ignored_timeout_keys": ignored_timeout_keys,
                    "effective_model_first_event_timeout_seconds": (
                        first_event_timeout
                    ),
                    "effective_model_idle_timeout_seconds": idle_timeout,
                    "effective_model_request_timeout_seconds": (
                        request_timeout
                    ),
                },
            )
        policy_receipt = self.model_request_policy_receipt()
        transport_receipt = dict(
            getattr(model_client, "model_transport_policy_receipt", {}) or {}
        )
        if transport_receipt:
            policy_receipt["provider_adapter"] = transport_receipt
        policy_receipt["config_hash_sha256"] = hashlib.sha256(
            json.dumps(
                policy_receipt,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request = request.model_copy(
            update={
                "metadata": {
                    **request.metadata,
                    "model_liveness_policy": policy_receipt,
                }
            }
        )
        self._require_model_request_admission()
        stream = model_client.stream(request)
        iterator = stream.__aiter__()
        received_provider_event = False
        stream_started = asyncio.get_running_loop().time()
        while True:
            try:
                next_event = iterator.__anext__()
                stream_elapsed = (
                    asyncio.get_running_loop().time() - stream_started
                )
                request_remaining = (
                    max(
                        0.0,
                        request_timeout - stream_elapsed,
                    )
                    if request_timeout is not None
                    else None
                )
                phase_timeout = (
                    idle_timeout
                    if received_provider_event
                    else (
                        max(0.0, first_event_timeout - stream_elapsed)
                        if first_event_timeout is not None
                        else None
                    )
                )
                timeout = budget.timeout(
                    phase_timeout,
                    request_remaining,
                )
                if timeout is None:
                    event = await next_event
                else:

                    async def await_provider_event() -> ModelStreamEvent:
                        try:
                            return await next_event
                        except asyncio.TimeoutError as exc:
                            raise _ProviderModelStreamTimeout(exc) from exc

                    event = await asyncio.wait_for(
                        await_provider_event(),
                        timeout=timeout,
                    )
            except StopAsyncIteration:
                return
            except _ProviderModelStreamTimeout as exc:
                await self._close_model_stream(stream)
                raise exc.cause
            except asyncio.TimeoutError:
                await self._close_model_stream(stream)
                if budget.expired():
                    raise _TurnDeadlineExceeded() from None
                if (
                    request_timeout is not None
                    and asyncio.get_running_loop().time() - stream_started
                    >= request_timeout
                ):
                    raise _ModelStreamDeadlineExceeded(
                        "request",
                        request_timeout,
                    ) from None
                raise _ModelStreamDeadlineExceeded(
                    "idle" if received_provider_event else "first_event",
                    (
                        idle_timeout
                        if received_provider_event
                        else first_event_timeout
                    )
                    or timeout,
                ) from None
            except asyncio.CancelledError:
                await self._close_model_stream(stream)
                raise
            is_provider_event = event.kind != "message_start"
            if (
                is_provider_event
                and not received_provider_event
                and event.kind == "provider_activity"
            ):
                event = event.model_copy(
                    update={
                        "metadata": {
                            **event.metadata,
                            "first_provider_activity": True,
                        }
                    }
                )
            if is_provider_event and not received_provider_event:
                logger.info(
                    "Native model produced first provider event",
                    extra={
                        "session_id": runtime_input.request.session_id,
                        "run_id": runtime_input.request.run_id,
                        "model_request_id": request.metadata.get(
                            "model_request_id"
                        ),
                        "provider_first_event_ms": round(
                            max(
                                0.0,
                                (
                                    asyncio.get_running_loop().time()
                                    - stream_started
                                )
                                * 1000,
                            ),
                            2,
                        ),
                        "foreground_latency_ms": metadata.get(
                            "foreground_latency_ms"
                        ),
                    },
                )
            if is_provider_event:
                received_provider_event = True
            try:
                yield event
            except (asyncio.CancelledError, GeneratorExit):
                # A downstream transport can stop while this iterator is
                # suspended at yield, outside the provider-await handler.
                await self._close_model_stream(stream)
                raise

    @staticmethod
    def _consume_model_stream_close_task(task: asyncio.Future[Any]) -> None:
        if task.cancelled():
            return
        with suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _close_model_stream(self, stream: Any) -> None:
        close = getattr(stream, "aclose", None)
        if not callable(close):
            return
        close_task: asyncio.Future[Any] | None = None
        try:
            close_task = asyncio.ensure_future(close())
            done, _pending = await asyncio.wait(
                {close_task},
                timeout=self._model_stream_close_timeout_seconds,
            )
            if close_task not in done:
                close_task.cancel()
                close_task.add_done_callback(
                    self._consume_model_stream_close_task
                )
                logger.warning(
                    "Native model stream close timed out; local iterator "
                    "retired but remote cancellation was not confirmed",
                    extra={
                        "timeout_seconds": (
                            self._model_stream_close_timeout_seconds
                        ),
                    },
                )
                return
            await close_task
        except asyncio.CancelledError:
            if close_task is not None and not close_task.done():
                close_task.cancel()
                close_task.add_done_callback(
                    self._consume_model_stream_close_task
                )
            raise
        except Exception:
            logger.warning(
                "Native model stream close failed; local iterator retired "
                "but remote cancellation was not confirmed",
                exc_info=True,
            )

    async def _await_with_turn_budget(
        self,
        awaitable: Awaitable[Any],
        *,
        budget: _DeadlineBudget,
        timeout_seconds: float | None = None,
    ) -> Any:
        try:
            try:
                timeout = budget.timeout(timeout_seconds)
            except BaseException:
                # Admission failed before the awaitable was scheduled. Close
                # coroutine objects here so a rejected external invocation
                # cannot leak an un-awaited wrapper or run later at GC time.
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                raise
            if timeout is None:
                return await awaitable
            operation = asyncio.ensure_future(awaitable)
            try:
                return await asyncio.wait_for(operation, timeout=timeout)
            except asyncio.CancelledError:
                # A repeated stop can interrupt wait_for while it joins the
                # cancelled operation. Retain the real invocation through its
                # cleanup before releasing the attempt or advancing the model.
                try:
                    await await_settled(operation)
                except (asyncio.CancelledError, Exception):
                    pass
                raise
        except asyncio.TimeoutError:
            if budget.expired():
                raise _TurnDeadlineExceeded() from None
            raise

    async def _run_tool_call(
        self,
        tool_call: ToolCall,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        client_turn_id: str = "",
        seq_start: int,
        policy_context: ToolPolicyContext,
        policy_observation_invocation: (
            EffectivePolicyObservationInvocation | None
        ) = None,
        budget: _DeadlineBudget,
        expected_file_extensions: frozenset[str] = frozenset(),
        inbox_after_seq: int = 0,
        collaboration_preparing_already_emitted: bool = False,
    ) -> AsyncIterator[tuple[RuntimeEvent, ToolResult]]:
        transaction = ToolTransaction(self)
        transaction_stream = transaction.run(
            tool_call,
            runtime_input=runtime_input,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            seq_start=seq_start,
            policy_context=policy_context,
            policy_observation_invocation=policy_observation_invocation,
            budget=budget,
            expected_file_extensions=expected_file_extensions,
            inbox_after_seq=inbox_after_seq,
            collaboration_preparing_already_emitted=(
                collaboration_preparing_already_emitted
            ),
        )
        try:
            async for outcome in transaction_stream:
                yield outcome.event, outcome.result
        finally:
            await close_owned_async_iterator(transaction_stream)

    async def _evaluate_policy(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        *,
        policy_context: ToolPolicyContext,
        budget: _DeadlineBudget,
        policy_observation_invocation: (
            EffectivePolicyObservationInvocation | None
        ) = None,
    ) -> ToolPolicyOutcome:
        return await evaluate_policy_chain(
            registry=self._registry,
            await_with_budget=self._await_with_turn_budget,
            deadline_exception_type=_TurnDeadlineExceeded,
            tool_call=tool_call,
            spec=spec,
            policy_context=policy_context,
            budget=budget,
            policy_observer=self.request_policy_observer,
            policy_observation_invocation=policy_observation_invocation,
        )

    def _record_tool_effect_intent(
        self, *args, **kwargs,
    ):
        return super()._record_tool_effect_intent(*args, **kwargs)

    async def _request_approval(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        outcome: ToolPolicyOutcome,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        seq_start: int,
        policy_context: ToolPolicyContext,
        budget: _DeadlineBudget,
    ) -> dict[str, Any]:
        request = runtime_input.request
        guard_result = _synthetic_guard_result(
            tool_call=tool_call, outcome=outcome
        )
        approval_binding = _approval_binding_for_tool_call(
            tool_call=tool_call,
            spec=spec,
            outcome=outcome,
            runtime_input=runtime_input,
            turn_id=turn_id,
        )
        approval_binding_payload = approval_binding.model_dump(mode="json")
        pending = None
        approval_timeout = _timeout_from_metadata(
            _runtime_metadata(runtime_input),
            _APPROVAL_TIMEOUT_KEYS,
            None,
        )
        if self._approval_service is not None:
            pending = await self._await_with_turn_budget(
                self._approval_service.create_pending(
                    session_id=request.session_id,
                    root_session_id=request.root_session_id
                    or request.session_id,
                    user_id=request.user_id,
                    channel=request.channel,
                    agent_id=request.agent_id,
                    tool_name=tool_call.name,
                    result=guard_result,
                    timeout_seconds=approval_timeout,
                    extra={
                        "tool_call": {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "input": dict(tool_call.arguments),
                        },
                        "policy_decision": outcome.policy_payload,
                        "approval_binding": approval_binding_payload,
                        "approval_options": [
                            "allow_once",
                            "allow_session",
                            "allow_workspace",
                            "deny",
                            "view_rules",
                        ],
                        "rememberable": bool(
                            isinstance(outcome.policy_payload, dict)
                            and outcome.policy_payload.get("rememberable")
                        ),
                        "source_run_id": turn_id,
                        "source_agent_name": request.agent_id,
                        "workspace_dir": policy_context.workspace_dir,
                        "workspace_grant_id": policy_context.workspace_grant_id,
                        "workspace_access_mode": (
                            policy_context.workspace_access_mode
                        ),
                        "permission_snapshot_at": (
                            policy_context.request_context.get(
                                "permission_snapshot_at",
                            )
                        ),
                    },
                ),
                budget=budget,
            )

        request_id = getattr(pending, "request_id", None) or str(uuid4())
        auto_approved = bool(
            pending is not None
            and getattr(pending, "status", "")
            == ApprovalDecision.APPROVED.value
            and pending.future.done()
        )
        budget_pause_started_monotonic = (
            asyncio.get_running_loop().time()
            if pending is not None and not auto_approved
            else None
        )
        payload = {
            "request_id": request_id,
            "tool_name": tool_call.name,
            "id": tool_call.id,
            **_tool_argument_snapshot(dict(tool_call.arguments)),
            "reason": outcome.reason,
            "policy_decision": outcome.policy_payload,
            "approval_binding": approval_binding_payload,
            "guard": _public_guard_payload(guard_result),
        }
        requested_event = RuntimeEvent(
            id=str(uuid4()),
            seq=seq_start + 1,
            type="approval.requested",
            status="running",
            source="system",
            session_id=request.session_id,
            turn_id=turn_id,
            item_id=request_id,
            summary=outcome.approval_copy
            or f"Approve tool call: {tool_call.name}",
            payload=payload,
            presentation=_presentation(
                tool_call.name,
                payload=payload,
                summary=outcome.reason,
                is_approval=True,
            ),
        )
        return {
            "pending": pending,
            "request_id": request_id,
            "payload": payload,
            "requested_event": requested_event,
            "auto_approved": auto_approved,
            "budget_pause_started_monotonic": budget_pause_started_monotonic,
        }

    async def _wait_for_approval_decision(
        self,
        approval: dict[str, Any],
        *,
        runtime_input: RuntimeInput,
        budget: _DeadlineBudget,
    ) -> ApprovalDecision:
        pending = approval.get("pending")
        request_id = str(approval.get("request_id") or "")
        if pending is None:
            return ApprovalDecision.DENIED

        approval_timeout = _timeout_from_metadata(
            _runtime_metadata(runtime_input),
            _APPROVAL_TIMEOUT_KEYS,
            getattr(
                pending,
                "timeout_seconds",
                None,
            ),
        )
        wait_started = approval.get("budget_pause_started_monotonic")
        if not isinstance(wait_started, (int, float)):
            wait_started = asyncio.get_running_loop().time()
        try:
            if approval_timeout is None:
                return await asyncio.shield(pending.future)
            return await asyncio.wait_for(
                asyncio.shield(pending.future),
                timeout=approval_timeout,
            )
        except asyncio.TimeoutError:
            if self._approval_service is not None:
                await self._approval_service.resolve_request(
                    request_id,
                    ApprovalDecision.TIMEOUT,
                )
            return ApprovalDecision.TIMEOUT
        except asyncio.CancelledError:
            if self._approval_service is not None:
                await self._approval_service.resolve_request(
                    request_id,
                    ApprovalDecision.DENIED,
                )
            raise
        finally:
            budget.pause_for(asyncio.get_running_loop().time() - wait_started)

    def _approval_resolved_event(
        self,
        tool_call: ToolCall,
        approval: dict[str, Any],
        decision: ApprovalDecision,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        seq: int,
    ) -> RuntimeEvent:
        payload = dict(approval.get("payload") or {})
        resolved_payload = dict(payload)
        resolved_payload["decision"] = decision.value
        return RuntimeEvent(
            id=str(uuid4()),
            seq=seq,
            type="approval.resolved",
            status=(
                "completed"
                if decision == ApprovalDecision.APPROVED
                else "interrupted"
            ),
            source="system",
            session_id=runtime_input.request.session_id,
            turn_id=turn_id,
            item_id=str(approval.get("request_id") or ""),
            summary=f"approval {decision.value}",
            payload=resolved_payload,
            presentation=_presentation(
                tool_call.name,
                payload=resolved_payload,
                summary=f"approval {decision.value}",
                is_approval=True,
            ),
        )

    async def _execute_callable(
        self,
        executor: Callable[..., Any],
        arguments: dict[str, Any],
    ) -> Any:
        if inspect.iscoroutinefunction(executor) or inspect.isasyncgenfunction(
            executor,
        ):
            result = executor(**arguments)
        else:
            result = await execute_owned_sync(executor, arguments)
        if isinstance(result, Awaitable):
            result = await result
        if hasattr(result, "__aiter__"):
            collected = []
            async for item in result:
                collected.append(_json_safe(item))
            return collected
        return result

    async def _execute_mcp_tool(
        self,
        spec: McpClientSpec,
        tool_call: ToolCall,
    ) -> Any:
        timeout = spec.execution_timeout
        call = spec.client.call_tool(tool_call.name, dict(tool_call.arguments))
        if timeout:
            return await asyncio.wait_for(call, timeout=timeout)
        return await call

    async def _collect_tool_specs(self) -> list[ToolSpec]:
        blocked_tool_names = self._registry.blocked_tool_names()
        specs = [
            spec
            for spec, _executor in self._registry.tool_items()
            if spec.enabled and spec.name not in blocked_tool_names
        ]
        registered_names = {spec.name for spec in specs}
        for spec in self._registry.mcp_client_specs():
            for tool_spec, _client_spec in await self._mcp_tools_for_client(
                spec
            ):
                if (
                    not tool_spec.enabled
                    or tool_spec.name in blocked_tool_names
                    or tool_spec.name in registered_names
                ):
                    continue
                registered_names.add(tool_spec.name)
                specs.append(tool_spec)
        if self._progressive_tools:
            return [_progressive_loader_spec(specs), *specs]
        return specs

    async def _find_mcp_tool(
        self,
        tool_name: str,
    ) -> tuple[ToolSpec, McpClientSpec] | None:
        if tool_name in self._registry.blocked_tool_names():
            return None
        for spec in self._registry.mcp_client_specs():
            for item in await self._mcp_tools_for_client(spec):
                if item[0].name == tool_name:
                    return item
        return None

    async def _mcp_tools_for_client(
        self,
        spec: McpClientSpec,
    ) -> list[tuple[ToolSpec, McpClientSpec]]:
        cached = spec.metadata.get("_native_tool_specs")
        if isinstance(cached, list):
            return [(ToolSpec.model_validate(item), spec) for item in cached]

        try:
            raw_tools = await spec.client.list_tools()
        except Exception as exc:  # pylint: disable=broad-except
            spec.metadata["_native_tool_discovery_error"] = _exception_message(
                exc,
                fallback=f"MCP tool discovery for {spec.name}",
            )
            spec.metadata["_native_tool_discovery_error_type"] = type(
                exc
            ).__name__
            return []

        spec.metadata.pop("_native_tool_discovery_error", None)
        spec.metadata.pop("_native_tool_discovery_error_type", None)
        tools: list[tuple[ToolSpec, McpClientSpec]] = []
        for raw in raw_tools or []:
            try:
                tool_spec = project_mcp_tool_spec(raw, spec)
            except ValueError:
                continue
            tools.append((tool_spec, spec))
        spec.metadata["_native_tool_specs"] = [
            tool_spec.model_dump(mode="json") for tool_spec, _client in tools
        ]
        return tools

    def _mcp_discovery_warnings(self) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        for spec in self._registry.mcp_client_specs():
            cached = spec.metadata.get("_native_tool_specs")
            if isinstance(cached, list):
                for raw_tool in cached:
                    metadata = (
                        raw_tool.get("metadata", {})
                        if isinstance(raw_tool, dict)
                        else {}
                    )
                    reason = (
                        metadata.get("unavailable_reason")
                        if isinstance(metadata, dict)
                        else None
                    )
                    if reason:
                        name = str(raw_tool.get("name") or "MCP tool")
                        warnings.append(
                            {
                                "kind": "mcp_tool_unavailable",
                                "client": spec.name,
                                "error": str(reason),
                                "error_type": "UnsupportedCapability",
                                "summary": f"{name} is unavailable: {reason}",
                            },
                        )
            error = spec.metadata.get("_native_tool_discovery_error")
            if not error:
                continue
            error_text = str(error).strip() or (
                f"MCP tool discovery failed for {spec.name}"
            )
            warnings.append(
                {
                    "kind": "mcp_tool_discovery_failed",
                    "client": spec.name,
                    "error": error_text,
                    "error_type": str(
                        spec.metadata.get("_native_tool_discovery_error_type")
                        or "Error",
                    ),
                    "summary": error_text,
                }
            )
        return warnings

    @staticmethod
    def _assistant_model_message(
        response: ModelResponse,
        tool_calls: list[ToolCall],
    ) -> ModelMessage:
        content = [
            block
            for block in response.message.content
            if block.type != "tool_call"
        ]
        for tool_call in tool_calls:
            content.append(
                ModelContentBlock(
                    type="tool_call",
                    data={
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": dict(tool_call.arguments),
                        **(
                            {
                                "extra_content": dict(
                                    tool_call.metadata["extra_content"],
                                ),
                            }
                            if isinstance(
                                tool_call.metadata.get("extra_content"),
                                dict,
                            )
                            else {}
                        ),
                    },
                ),
            )
        return ModelMessage(
            id=response.message.id,
            role="assistant",
            content=content,
            name=response.message.name,
            metadata=dict(response.message.metadata),
        )


    def _tool_completed_event(
        self,
        result: ToolResult,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        client_turn_id: str = "",
        seq: int,
    ) -> RuntimeEvent:
        payload = _tool_payload_with_agent_presentation_context(
            _tool_result_payload(result),
            runtime_input,
        )
        if (
            str(
                _runtime_metadata(runtime_input).get("harness_mode")
                or "shadow"
            )
            .strip()
            .lower()
            == "off"
        ):
            payload.pop("canonical_tool", None)
        if client_turn_id:
            payload = {**payload, "client_turn_id": client_turn_id}
        return RuntimeEvent(
            id=str(uuid4()),
            seq=seq,
            type="tool.call.completed",
            status="completed" if result.ok else "failed",
            source="tool",
            session_id=runtime_input.request.session_id,
            turn_id=turn_id,
            item_id=result.call_id,
            summary=_tool_result_text(result)[:240],
            payload=payload,
            presentation=_presentation(result.name, payload=payload),
        )

__all__ = ["NativeLantaiRuntime"]
