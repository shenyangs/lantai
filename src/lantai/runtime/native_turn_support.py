# -*- coding: utf-8 -*-
"""Deterministic budget, lifecycle, and projection support for native turns.

This module owns no run loop and executes no tools.  It keeps the pure and
state-local helpers shared by the authoritative native runtime and its narrow
tool-transaction port out of the adapter dependency boundary.
"""

from __future__ import annotations

import asyncio
import contextvars
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

from pydantic import ValidationError

from ..agents.memory_usage_trace import record_delivered_persona_memory
from ..agents.persona.perceptible_presence import (
    PersonaTurnPlan,
    evaluate_persona_delivery,
)
from ..agents.persona.post_answer_reflection import (
    PostAnswerReflectionRequest,
)
from ..app.experience.presentation import tool_presentation_for_tool_name
from ..security.tool_guard.approval import ApprovalDecision
from ..security.tool_guard.models import (
    GuardFinding,
    GuardSeverity,
    GuardThreatCategory,
    ToolGuardResult,
)
from .events import RuntimeEvent, RuntimePresentation, RuntimeUsage
from .artifact_effects import (
    RuntimeArtifactEffectJournal,
    artifact_effect_public_payload,
    project_committed_primary_delivery,
    recover_artifact_delivery,
    register_artifact_candidate,
)
from .effects import (
    RuntimeApprovalBinding,
    RuntimeEffectJournal,
    RuntimeEffectRecoveryCapability,
    RuntimeEffectReducerAction,
    assess_runtime_effect_ambiguity,
    reduce_runtime_effect,
)
from .coherence import parse_or_build_tool_envelope
from .convergence import (
    ConvergenceController,
    ConvergenceDecision,
    build_convergence_review_prompt,
    convergence_mode,
    parse_convergence_review,
    policy_for_work_method,
)
from .capability_catalog import (
    PROGRESSIVE_TOOL_GROUPS,
    progressive_tool_group_for,
)
from .errors import runtime_exception_message as _exception_message
from .file_delivery import (
    FILE_DELIVERY_TOOL_NAME,
    expected_file_type_label,
    file_path_matches_extensions,
    has_matching_file_delivery_block,
    resolve_workspace_deliverable_path,
    select_archive_member_delivery_paths,
    select_generated_delivery_paths,
    snapshot_generated_files,
)
from .input import RuntimeInput, RuntimeRequestContext
from .inbox_admission import (
    RuntimeInboxAdmission,
    RuntimeInboxClaim,
    RuntimeInboxWakeup,
)
from .hosted_collaboration import close_public_room_snapshot
from .messages import ContentBlock, RuntimeMessage
from .mac_use_effects import interpret_mac_use_effect
from .mcp_tools import mcp_structured_result_metadata, project_mcp_tool_spec
from .policy_chain import evaluate_policy_chain
from .model_messages import model_messages_from_runtime_messages
from .model_round import (
    ModelRoundAccumulator,
    ModelRoundProtocolError as _ModelProtocolError,
)
from .next_model_round import (
    capture_model_round_lineage,
    prepare_next_model_round,
)
from .progressive_tools import transition_progressive_tool_groups
from .public_reasoning import (
    PublicReasoningProjector,
    has_degenerate_placeholder_flood,
    prompt_leak_guard_for_runtime_input,
    without_runtime_public_progress_claims,
)
from .request_policy_receipt import (
    EffectivePolicyObservationInvocation,
    RequestPolicyConstructionReceipt,
    RequestPolicyReceiptObserver,
)
from .recovery_policy import (
    ModelRoundRecoveryState,
    decide_model_round_recovery,
)
from .runtime_contract_messages import (
    model_output_recovery_message,
    model_stream_idle_recovery_message,
    pending_deliverable_context_message,
    primary_turn_ownership_message,
)
from .session_turn_lifecycle import SessionTurnLifecycle
from .models import (
    LantaiModelClient,
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
)
from .tool_argument_validation import tool_argument_schema_feedback
from .tools import (
    LantaiToolRegistry,
    McpClientSpec,
    ToolCall,
    ToolPolicyContext,
    ToolPolicyOutcome,
    ToolResult,
    ToolSpec,
)
from .turn_control import RuntimeInputConflictError, RuntimeTurnControlStore
from .tool_public_progress import (
    model_tool_public_progress,
    tool_specs_with_public_progress_contract,
    without_public_progress_argument,
    without_public_progress_block_data,
)
from .work_method_resolution import (
    collaboration_is_required,
    durable_artifact_required_from_metadata,
)
from .runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeCheckpoint,
    RuntimeCheckpointExecutionPosition,
    RuntimeEffectRecord,
    RuntimePendingToolCall,
    RuntimeRunOwner,
    RuntimeRunReducerAction,
    RuntimeRunTransitionError,
    RuntimeTaskContract,
    reduce_runtime_run,
    revise_chat_task_contract,
    task_contract_effective_objective,
)
from .store import (
    RuntimeRunStore,
    RuntimeRunStoreConflictError,
    RuntimeRunStoreError,
)
from .task_revision import persist_runtime_task_command

logger = logging.getLogger(__name__)

ModelClientFactory = Callable[[str | None], LantaiModelClient]


async def _resumed_pending_tool_model_events(
    tool_call: ToolCall,
) -> AsyncIterator[ModelStreamEvent]:
    """Re-enter a durable pending tool boundary without asking the model again."""

    yield ModelStreamEvent(
        kind="tool_call_completed",
        tool_call=tool_call,
    )
    yield ModelStreamEvent(
        kind="message_completed",
        response=ModelResponse(message=ModelMessage(role="assistant")),
    )


def _model_message_transaction_tail(
    messages: list[ModelMessage],
    limit: int,
) -> list[ModelMessage]:
    """Return a bounded tail without splitting assistant/tool transactions."""

    if limit <= 0 or len(messages) <= limit:
        return list(messages)
    start = len(messages) - limit
    if messages[start].role != "tool":
        return list(messages[start:])
    while start > 0 and messages[start].role == "tool":
        start -= 1
    if messages[start].role != "assistant" or not any(
        block.type == "tool_call" for block in messages[start].content
    ):
        start += 1
    return list(messages[start:])


DEFAULT_NATIVE_TURN_TIMEOUT_SECONDS = 60 * 60
DEFAULT_NATIVE_MODEL_FIRST_EVENT_TIMEOUT_SECONDS = 5 * 60
DEFAULT_NATIVE_MODEL_IDLE_TIMEOUT_SECONDS = 120.0
# A healthy provider stream has no arbitrary total lifetime by default.  An
# embedding may still opt into a positive construction-time deadline for a
# diagnostic or explicitly bounded operation.
DEFAULT_NATIVE_MODEL_REQUEST_TIMEOUT_SECONDS: float | None = None
DEFAULT_NATIVE_MODEL_STREAM_CLOSE_TIMEOUT_SECONDS = 2.0
DEFAULT_NATIVE_TOOL_TIMEOUT_SECONDS = 10 * 60
DEFAULT_NATIVE_TOOL_HEARTBEAT_INTERVAL_SECONDS = 10.0
_CONVERGENCE_REVIEW_TIMEOUT_SECONDS = 45.0
_MAIN_RESPONSE_MAX_TOKENS = 65_536
_COLLABORATION_STAFFING_MAX_TOKENS = 4_096
_COLLABORATION_STAFFING_SYSTEM_TEXT = (
    '<collaboration-staffing visibility="internal">'
    "This request only staffs the hosted collaboration room. Read the user "
    "request, conversation, trusted attachment manifest, and collaboration "
    "contract. Express the concrete issue with the existing goal, "
    "why_parallel, tasks[].label, acceptance_criteria, and protocol fields; "
    "do not make another model or tool call merely to restate the issue. "
    "Choose roundtable for judgment, strategy, trade-offs, or multiple "
    "plausible interpretations whose final answer benefits from mutual "
    "review. Choose parallel_consultation for independent research, section "
    "drafting, material collection, or artifact production. Create 2-4 "
    "complementary, self-contained assignments that preserve the requested "
    "outcome, source scope, constraints, and output format, then call "
    "collaborate_with_agents exactly once. Use distinct "
    "configured agent IDs or display names when the conversation supplies "
    "them; otherwise use distinct specialist role names and let the host "
    "resolve them. Do not answer the user or perform final synthesis."
    "</collaboration-staffing>"
)
_COLLABORATION_MEMBER_SYSTEM_TEXT = (
    '<internal-worker mode="collaboration-member" visibility="internal">'
    "Work only on the assigned subtask. Use the available evidence and tools "
    "to return a concise, verifiable handoff to the host agent. Preserve facts, "
    "sources, uncertainty, and requested constraints. Do not imitate an agent "
    "persona, address the end user, or write the final user-facing answer."
    "</internal-worker>"
)
_DEEP_RESEARCH_SYSTEM_TEXT = (
    '<internal-worker mode="deep-research" visibility="internal">'
    "Investigate the user's objective thoroughly with the available tools and "
    "context. Build a source-aware evidence handoff: findings, citations or "
    "artifact references, uncertainty, conflicts, and remaining gaps. Do not "
    "imitate an agent persona or write the final user-facing answer; the primary "
    "agent will synthesize the delivery in a separate final round."
    "</internal-worker>"
)
_MIN_RESPONSE_MAX_TOKENS = 256
_MAIN_OUTPUT_RECOVERY_LIMIT = 1
_MAIN_STREAM_IDLE_RECOVERY_LIMIT = 1
_QUQU_LINE_RE = re.compile(
    r"^\s*~>\[(lead|echo)\]\s*(\S.*)$",
    re.MULTILINE,
)
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)

_PROGRESSIVE_TOOL_NAME = "equip_lantai_tools"
_PROGRESSIVE_TOOL_GROUPS = PROGRESSIVE_TOOL_GROUPS

_DECLARE_DELIVERABLE_CONTRACT_TOOL = "declare_deliverable_contract"
_RESOLVE_PENDING_DELIVERABLE_TOOL = "resolve_pending_deliverable_choice"
_SUBAGENT_SUBMIT_TOOL = "submit_to_agent"
_SUBAGENT_CHECK_TOOL = "check_agent_task"
_SUBAGENT_FOREGROUND_TOOL = "chat_with_agent"
_SUBAGENT_BATCH_TOOL = "collaborate_with_agents"
_SAFE_IDENTITY_TOOLS = frozenset(
    {
        _SUBAGENT_FOREGROUND_TOOL,
        _SUBAGENT_SUBMIT_TOOL,
        _SUBAGENT_BATCH_TOOL,
    }
)
_SUBAGENT_TASK_ID_RE = re.compile(
    r"^\[TASK_ID:\s*(?P<task_id>[^\]]+)\]\s*$",
    flags=re.MULTILINE,
)
_SUBAGENT_STATUS_RE = re.compile(
    r"^\[STATUS:\s*(?P<status>[^\]]+)\]\s*$",
    flags=re.MULTILINE,
)
_TERMINAL_SUBAGENT_STATUSES = frozenset(
    {"finished", "completed", "failed", "cancelled", "canceled", "interrupted"}
)
_TURN_DEADLINE_KEYS = (
    "native_turn_deadline_monotonic",
    "turn_deadline_monotonic",
    "deadline_monotonic",
)
_TURN_TIMEOUT_KEYS = (
    "native_turn_timeout_seconds",
    "turn_timeout_seconds",
)
_MODEL_IDLE_TIMEOUT_KEYS = (
    "native_model_idle_timeout_seconds",
    "model_idle_timeout_seconds",
)
_MODEL_FIRST_EVENT_TIMEOUT_KEYS = (
    "native_model_first_event_timeout_seconds",
    "model_first_event_timeout_seconds",
)
_MODEL_REQUEST_TIMEOUT_KEYS = (
    "native_model_request_timeout_seconds",
    "model_request_timeout_seconds",
)
_TOOL_TIMEOUT_KEYS = (
    "native_tool_timeout_seconds",
    "tool_timeout_seconds",
)
_APPROVAL_TIMEOUT_KEYS = (
    "native_approval_timeout_seconds",
    "approval_timeout_seconds",
)


class _TurnDeadlineExceeded(asyncio.TimeoutError):
    """Raised when the per-turn runtime deadline is exhausted."""


class _ModelStreamDeadlineExceeded(asyncio.TimeoutError):
    """Raised when the provider stream misses a phase-specific deadline."""

    def __init__(self, phase: str, timeout_seconds: float) -> None:
        self.phase = phase
        self.timeout_seconds = timeout_seconds
        formatted_timeout = f"{timeout_seconds:g}"
        if phase == "request":
            message = (
                f"单次模型请求已运行 {formatted_timeout} 秒，系统已停止继续等待。"
                "已生成内容仍保留，可重试或调整后继续。"
            )
        elif phase == "first_event":
            message = (
                f"模型服务在 {formatted_timeout} 秒内没有返回首个响应。"
                "本轮已停止等待，请重试；若持续发生，"
                "请切换模型或检查模型服务状态。"
            )
        else:
            message = (
                f"模型响应已中断超过 {formatted_timeout} 秒。"
                "本轮已停止等待，请重试。"
            )
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _ModelStreamIdleInterruption:
    """Internal boundary for a recoverable silent provider stream."""

    error: _ModelStreamDeadlineExceeded


@dataclass(frozen=True, slots=True)
class _ModelStreamControlInterruption:
    """Internal boundary value after safely yielding for a redirect."""

    wakeup: RuntimeInboxWakeup


class _ProviderModelStreamTimeout(RuntimeError):
    """Carries a timeout raised by the provider, not by our watchdog."""

    def __init__(self, cause: asyncio.TimeoutError) -> None:
        self.cause = cause
        super().__init__(str(cause))


async def _project_model_stream_boundaries(
    stream: AsyncIterator[ModelStreamEvent],
    *,
    runtime_input: RuntimeInput,
    close_stream: Callable[[Any], Awaitable[None]],
    inbox_admission: RuntimeInboxAdmission | None = None,
    inbox_after_seq: int = 0,
) -> AsyncIterator[
    ModelStreamEvent
    | _ModelStreamIdleInterruption
    | _ModelStreamControlInterruption
]:
    """Project model idle and durable redirect boundaries."""

    if inbox_admission is None:
        try:
            try:
                async for event in stream:
                    yield event
            except _ModelStreamDeadlineExceeded as exc:
                if exc.phase not in {"first_event", "idle"}:
                    raise
                yield _ModelStreamIdleInterruption(error=exc)
        finally:
            await close_stream(stream)
        return

    iterator = stream.__aiter__()
    next_event_task: asyncio.Task[ModelStreamEvent] | None = None
    control_poll_available = inbox_admission is not None
    try:
        while True:
            if control_poll_available and inbox_admission is not None:
                try:
                    wakeup = inbox_admission.peek_immediate_redirect(
                        after_seq=inbox_after_seq,
                    )
                except (OSError, ValueError):
                    control_poll_available = False
                    logger.warning(
                        "Run Inbox redirect polling is unavailable; "
                        "the active model request will continue",
                        extra={"run_id": runtime_input.request.run_id},
                        exc_info=True,
                    )
                else:
                    if wakeup is not None:
                        yield _ModelStreamControlInterruption(wakeup=wakeup)
                        return
            next_event_task = asyncio.create_task(iterator.__anext__())
            while True:
                done, _ = await asyncio.wait(
                    {next_event_task},
                    timeout=(0.05 if control_poll_available else None),
                )
                if done:
                    break
                if inbox_admission is None:
                    continue
                try:
                    wakeup = inbox_admission.peek_immediate_redirect(
                        after_seq=inbox_after_seq,
                    )
                except (OSError, ValueError):
                    control_poll_available = False
                    logger.warning(
                        "Run Inbox redirect polling is unavailable; "
                        "the active model request will continue",
                        extra={"run_id": runtime_input.request.run_id},
                        exc_info=True,
                    )
                    continue
                if wakeup is None:
                    continue
                next_event_task.cancel()
                await asyncio.gather(next_event_task, return_exceptions=True)
                next_event_task = None
                yield _ModelStreamControlInterruption(wakeup=wakeup)
                return
            try:
                yield next_event_task.result()
            except StopAsyncIteration:
                return
            finally:
                next_event_task = None
    except _ModelStreamDeadlineExceeded as exc:
        if exc.phase not in {"first_event", "idle"}:
            raise
        yield _ModelStreamIdleInterruption(error=exc)
    finally:
        if next_event_task is not None and not next_event_task.done():
            next_event_task.cancel()
            await asyncio.gather(next_event_task, return_exceptions=True)
        await close_stream(stream)


class _ToolSteered(RuntimeError):
    """Raised when fresh user input supersedes a waitable collaboration call."""


class _ToolArgumentRepairExhausted(_ModelProtocolError):
    """Raised after bounded recovery from malformed model tool arguments."""

    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(
            "Model repeatedly returned incomplete tool arguments after "
            f"{attempts} repair attempts."
        )


class _IncompleteModelOutput(RuntimeError):
    """Raised when a provider explicitly cuts off the visible answer."""

    def __init__(self, finish_reason: str, *, recovery_attempts: int = 0) -> None:
        self.finish_reason = finish_reason
        self.recovery_attempts = max(0, recovery_attempts)
        super().__init__(
            f"Model output ended before completion (finish_reason={finish_reason})."
        )


class _MissingVisibleFinalAnswer(RuntimeError):
    """The bounded tool-free synthesis still returned reasoning only."""

    def __init__(self) -> None:
        super().__init__(
            "模型未能给出最终回复，已停止自动等待；已生成内容仍保留。"
        )


class _DegenerateModelOutput(RuntimeError):
    """Raised when a provider returns unresolved placeholder scaffolding."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Degenerate model output: {reason}.")


class _TaskBudgetExceeded(RuntimeError):
    """Raised when a persisted TaskContract budget is exhausted."""

    def __init__(self, dimension: str, limit: int | float) -> None:
        self.dimension = dimension
        self.limit = limit
        super().__init__(f"Task budget exceeded: {dimension}={limit}.")


class _RepeatedToolFailure(RuntimeError):
    """Raised when one deterministic tool failure reaches its retry limit."""

    def __init__(self, fingerprint: str, count: int) -> None:
        self.fingerprint = fingerprint
        self.count = count
        super().__init__(
            "Repeated deterministic tool failure stopped after "
            f"{count} attempts ({fingerprint})."
        )


class _RequiredCapabilityFailed(RuntimeError):
    """Raised when a mandatory capability cannot be established safely."""

    def __init__(
        self,
        capability: str,
        count: int,
        *,
        error_code: str,
    ) -> None:
        self.capability = capability
        self.count = count
        self.error_code = error_code
        super().__init__(
            f"required capability {capability} failed after {count} attempts "
            f"({error_code})."
        )


class _RuntimeDurabilityBlocked(RuntimeError):
    """Raised when a canonical Run cannot cross a durable safety boundary."""

    def __init__(
        self,
        stage: str,
        *,
        kind: str,
        cause: BaseException,
        attempts: int,
    ) -> None:
        self.stage = stage
        self.kind = kind
        self.cause_type = type(cause).__name__
        self.attempts = max(1, attempts)
        super().__init__(
            f"Runtime durability blocked at {stage}: {kind} "
            f"({self.cause_type}, attempts={self.attempts})."
        )


def _progressive_tool_group(spec: ToolSpec) -> str:
    """Map a native tool spec to its on-demand capability group."""

    return progressive_tool_group_for(
        origin=spec.origin,
        name=spec.name,
        metadata=spec.metadata,
    )


def _progressive_group_description(
    group_name: str,
    specs: list[ToolSpec],
) -> str:
    """Describe a group with the concrete tools available in this runtime."""

    base_description = _PROGRESSIVE_TOOL_GROUPS[group_name][0]
    tool_names = sorted(
        {
            spec.name
            for spec in specs
            if spec.enabled
            and spec.name != _PROGRESSIVE_TOOL_NAME
            and _progressive_tool_group(spec) == group_name
        },
    )
    if not tool_names:
        return f"{base_description} No callable tools are currently registered."
    return (
        f"{base_description} Currently available tools: "
        + ", ".join(f"`{name}`" for name in tool_names)
        + "."
    )


def _progressive_loader_spec(specs: list[ToolSpec]) -> ToolSpec:
    """Return the authoritative compact capability index for this runtime."""

    return ToolSpec(
        name=_PROGRESSIVE_TOOL_NAME,
        description=(
            "Authoritative capability index and loader. Each group field "
            "lists the tools currently callable in this runtime. Equip the "
            "smallest relevant groups before an external action. Multiple "
            "calls accumulate for the current user turn; call with no true "
            "fields to clear all equipped groups. Do not "
            "claim a capability is unavailable merely because its schema is "
            "not exposed yet; equip its listed group first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                group_name: {
                    "type": "boolean",
                    "default": False,
                    "description": _progressive_group_description(
                        group_name,
                        specs,
                    ),
                }
                for group_name, (_description, _tool_names) in (
                    _PROGRESSIVE_TOOL_GROUPS.items()
                )
            },
            "additionalProperties": False,
        },
        origin="builtin",
        read_only=True,
        concurrency_safe=True,
        metadata={
            "runtime_owned": True,
            "progressive_tool_loader": True,
            "progress_policy": "state_change",
        },
    )


def _tools_for_turn_intelligence(
    specs: list[ToolSpec],
    runtime_metadata: dict[str, Any],
) -> list[ToolSpec]:
    """Apply only a model-authored side-effect ceiling to tool exposure."""

    raw = runtime_metadata.get("turn_intelligence")
    if not isinstance(raw, dict):
        return specs
    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    enforce_model_ceiling = bool(
        raw.get("status") == "classified"
        and confidence >= 0.65
        and raw.get("interaction_mode") in {"conversation", "answer"}
    )
    if not enforce_model_ceiling:
        return specs

    # Hosted collaboration is a read-only execution method, not an external
    # side effect.  Keep its single semantic entry point visible for answer
    # turns so the primary model can honor an explicit prose request for a
    # real room even when the optional auxiliary classifier missed that
    # request.  Hiding it here made the model's only remaining option a fake
    # one-model "roundtable" rendered as prose.
    collaboration_tools = [
        spec
        for spec in specs
        if spec.name == _SUBAGENT_BATCH_TOOL and spec.enabled
    ]
    pending_choice_tools = [
        spec
        for spec in specs
        if (
            spec.name == _RESOLVE_PENDING_DELIVERABLE_TOOL
            and spec.enabled
            and isinstance(
                runtime_metadata.get("pending_deliverable_context"), dict
            )
        )
    ]
    needs_read_tools = bool(
        raw.get("workspace_intent") == "inspect"
        or raw.get("external_action_intent") == "read"
    )
    if not needs_read_tools:
        return [
            *(
                collaboration_tools
                if raw.get("interaction_mode") == "answer"
                else []
            ),
            *pending_choice_tools,
        ]

    progressive = any(spec.name == _PROGRESSIVE_TOOL_NAME for spec in specs)
    filtered = [
        spec
        for spec in specs
        if spec.name != _PROGRESSIVE_TOOL_NAME and bool(spec.read_only)
    ]
    if progressive:
        return [
            _progressive_loader_spec(filtered),
            *filtered,
            *pending_choice_tools,
        ]
    return [*filtered, *pending_choice_tools]


def _model_classified_durable_artifact_turn(
    runtime_metadata: dict[str, Any],
) -> bool:
    """Return whether the model-owned intent contract expects a real file."""

    return durable_artifact_required_from_metadata(runtime_metadata)


def _explicit_model_multimodal_support(model_client: object) -> bool | None:
    """Return only an explicitly declared multimodal capability."""

    endpoint = getattr(model_client, "endpoint", None)
    for owner in (endpoint, model_client):
        value = getattr(owner, "supports_multimodal", None)
        if isinstance(value, bool):
            return value
    return None


def _is_visual_creation_request(
    runtime_input: RuntimeInput,
    expected_file_extensions: Iterable[str],
) -> bool:
    """Detect presentation and other explicitly visual creation requests."""

    visual_extensions = {
        ".gif",
        ".jpeg",
        ".jpg",
        ".key",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".svg",
        ".webp",
    }
    if any(
        str(extension or "").strip().lower() in visual_extensions
        for extension in expected_file_extensions
    ):
        return True
    latest_user_text = next(
        (
            message.text_content()
            for message in reversed(runtime_input.messages)
            if message.role == "user"
        ),
        "",
    )
    visual_noun = re.search(
        r"(?i)(?:\bpptx?\b|\bpower\s*point\b|\bpresentation\b|"
        r"\bslide\s*deck\b|\binfographic\b|\bposter\b|"
        r"\bimage\b|幻灯片|演示文稿|演示稿|海报|信息图|"
        r"配图|视觉稿|UI\s*设计|页面设计)",
        latest_user_text,
    )
    creation_intent = re.search(
        r"(?i)(?:\bcreate\b|\bgenerate\b|\bmake\b|\bbuild\b|"
        r"生成|制作|做成|设计|创建|出一份)",
        latest_user_text,
    )
    return bool(visual_noun and creation_intent)


def _pptx_skill_output_candidate(
    tool_call: ToolCall,
    result: ToolResult,
    *,
    workspace_dir: str,
) -> str | None:
    """Project a successful stable PPT generator file into a primary candidate."""

    if not result.ok or tool_call.name != "run_skill_script":
        return None
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    if str(metadata.get("skill_name") or "").strip().lower() != "pptx":
        return None
    script_path = Path(str(metadata.get("script_path") or ""))
    if script_path.name != "generate.py":
        return None
    for block in result.content:
        if str(block.get("type") or "") != "file":
            continue
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "url":
            continue
        parsed = urlparse(str(source.get("url") or ""))
        if parsed.scheme != "file":
            continue
        resolved = resolve_workspace_deliverable_path(
            workspace_dir,
            unquote(parsed.path),
        )
        if resolved and Path(resolved).suffix.lower() == ".pptx":
            return resolved
    return None


def _file_fingerprint(path: str | None) -> tuple[int, int] | None:
    if not path:
        return None
    try:
        stat_result = Path(path).stat()
    except OSError:
        return None
    return int(stat_result.st_size), int(stat_result.st_mtime_ns)


def _tool_declared_delivery_candidates(
    tool_call: ToolCall,
    *,
    spec: ToolSpec | None,
    workspace_dir: str,
) -> tuple[str, ...]:
    """Return safe output paths explicitly named by a mutating tool call."""

    if (
        spec is None
        or spec.read_only
        or spec.output_semantics.artifact_role != "candidate"
    ):
        return ()
    paths: list[str] = []
    for key in spec.output_semantics.argument_path_fields:
        resolved = resolve_workspace_deliverable_path(
            workspace_dir,
            tool_call.arguments.get(key),
            require_file=False,
        )
        if resolved and resolved not in paths:
            paths.append(resolved)
    return tuple(paths)


def _model_declared_generated_paths(
    response_text: str,
    *,
    workspace_dir: str,
    generated_paths: Iterable[str],
) -> tuple[str, ...]:
    """Select generated files the model explicitly names as its delivery."""

    text = str(response_text or "")
    root = Path(workspace_dir).expanduser().resolve(strict=False)
    declared: list[str] = []
    for raw_path in generated_paths:
        resolved = resolve_workspace_deliverable_path(
            str(root),
            raw_path,
            require_file=True,
        )
        if not resolved:
            continue
        path = Path(resolved)
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            continue
        filename_pattern = re.compile(
            rf"(?<![\w.-]){re.escape(path.name)}(?![\w.-])",
            flags=re.UNICODE,
        )
        if resolved in text or relative in text or filename_pattern.search(text):
            declared.append(resolved)
    return tuple(declared)


def _coerce_non_negative_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return max(0.0, parsed)


def _first_float(metadata: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in metadata:
            continue
        parsed = _coerce_non_negative_float(metadata.get(key))
        if parsed is not None:
            return parsed
    return None


def _timeout_from_metadata(
    metadata: dict[str, Any],
    keys: tuple[str, ...],
    default: float | None,
) -> float | None:
    parsed = _first_float(metadata, keys)
    if parsed is not None:
        return parsed
    return default


def _runtime_metadata(runtime_input: RuntimeInput) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    metadata.update(dict(runtime_input.request.metadata or {}))
    metadata.update(dict(runtime_input.metadata or {}))
    return metadata


def _main_response_max_tokens(runtime_metadata: dict[str, Any]) -> int:
    """Resolve a caller-scoped response cap without changing normal turns."""

    raw = runtime_metadata.get("native_response_max_tokens")
    if raw is None or isinstance(raw, bool):
        return _MAIN_RESPONSE_MAX_TOKENS
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return _MAIN_RESPONSE_MAX_TOKENS
    return max(
        _MIN_RESPONSE_MAX_TOKENS,
        min(_MAIN_RESPONSE_MAX_TOKENS, parsed),
    )


def _internal_execution_messages(
    messages: list[ModelMessage],
    *,
    system_text: str,
    instruction_origin: str,
    marker: str,
) -> list[ModelMessage]:
    """Project canonical context into a persona-free internal worker request.

    Runtime-owned operational contracts remain available, while compiled agent
    prompts and legacy/recall system messages stay behind the delivery boundary.
    The caller retains the canonical list for the primary agent's final round.
    """

    projected = [
        ModelMessage.text(
            "system",
            system_text,
            metadata={
                "instruction_trust": "trusted_system",
                "instruction_origin": instruction_origin,
                marker: True,
            },
        )
    ]
    for message in messages:
        if message.role == "system":
            origin = str(message.metadata.get("instruction_origin") or "")
            if not origin.startswith("runtime."):
                continue
            if origin in {
                "runtime.primary_turn_ownership",
                "runtime.collaboration_final_synthesis",
                "runtime.deep_research_final_synthesis",
            }:
                continue
        projected.append(message)
    return projected


def _collaboration_staffing_messages(
    messages: list[ModelMessage],
) -> list[ModelMessage]:
    """Use a compact, persona-free context for collaboration staffing."""

    return _internal_execution_messages(
        messages,
        system_text=_COLLABORATION_STAFFING_SYSTEM_TEXT,
        instruction_origin="runtime.collaboration_staffing",
        marker="collaboration_staffing",
    )


def _collaboration_member_messages(
    messages: list[ModelMessage],
) -> list[ModelMessage]:
    """Use a neutral execution context for a hosted collaboration member."""

    return _internal_execution_messages(
        messages,
        system_text=_COLLABORATION_MEMBER_SYSTEM_TEXT,
        instruction_origin="runtime.collaboration_member_execution",
        marker="collaboration_member_execution",
    )


def _deep_research_messages(
    messages: list[ModelMessage],
) -> list[ModelMessage]:
    """Use a neutral execution context for deep-research worker rounds."""

    return _internal_execution_messages(
        messages,
        system_text=_DEEP_RESEARCH_SYSTEM_TEXT,
        instruction_origin="runtime.deep_research_execution",
        marker="deep_research_execution",
    )


def _runtime_event_has_visible_completed_message(event: RuntimeEvent) -> bool:
    if event.type != "message.completed":
        return False
    raw_message = event.payload.get("message")
    if not isinstance(raw_message, dict):
        return False
    try:
        message = RuntimeMessage.model_validate(raw_message)
    except Exception:
        return bool(event.summary.strip())
    return any(
        block.type not in {"thinking", "reasoning", "tool_call"}
        and bool((block.text or "").strip() or block.data)
        for block in message.content
    )


def _event_with_persona_delivery(
    event: RuntimeEvent,
    report: dict[str, Any],
) -> RuntimeEvent:
    raw_message = event.payload.get("message")
    if not isinstance(raw_message, dict):
        return event
    message_payload = dict(raw_message)
    metadata = dict(message_payload.get("metadata") or {})
    metadata["persona_delivery"] = report
    message_payload["metadata"] = metadata
    payload = dict(event.payload)
    payload["message"] = message_payload
    return event.model_copy(update={"payload": payload})


def _event_with_outcome_status(
    event: RuntimeEvent,
    *,
    outcome_status: str,
    boundary_reason: str,
) -> RuntimeEvent:
    """Attach semantic task outcome to the durable assistant message."""

    raw_message = event.payload.get("message")
    if not isinstance(raw_message, dict):
        return event
    message_payload = dict(raw_message)
    metadata = dict(message_payload.get("metadata") or {})
    metadata.update(
        {
            "outcome_status": outcome_status,
            "execution_boundary_reason": boundary_reason,
        }
    )
    message_payload["metadata"] = metadata
    payload = dict(event.payload)
    payload["message"] = message_payload
    return event.model_copy(update={"payload": payload})


def _synthesis_boundary_is_incomplete(reason: str) -> bool:
    """Return whether a forced synthesis is a best-effort partial result."""

    normalized = str(reason or "").strip()
    return bool(normalized) and normalized not in {
        "collaboration_completed",
        "deep_research_completed",
        "missing_model_output",
        "missing_visible_answer",
        "private_editor_fact_grounding",
    }


def _latest_user_text(runtime_input: RuntimeInput) -> str:
    for message in reversed(runtime_input.messages):
        if message.role == "user":
            return message.text_content()
    return ""


def _prose_requests_hosted_collaboration(runtime_input: RuntimeInput) -> bool:
    """Recognize an explicit request for a real hosted room, not role-play.

    This controls presentation buffering only. Work-method selection remains
    authoritative in ``work_method_resolution``; a standard turn that does
    not call the batch tool still receives its ordinary answer.
    """

    text = _latest_user_text(runtime_input).casefold()
    if not text:
        return False
    compact = re.sub(r"\s+", "", text)
    return any(
        marker in compact
        for marker in (
            "协作室",
            "协作工具",
            "协作会议",
            "会议厅",
            "会议室",
            "collaborate_with_agents",
            "collaborationroom",
            "collaborationtool",
        )
    )


def _prose_requests_roundtable(runtime_input: RuntimeInput) -> bool:
    """Recognize an explicit request for the bounded roundtable protocol."""

    compact = re.sub(r"\s+", "", _latest_user_text(runtime_input).casefold())
    return bool(compact) and any(
        marker in compact
        for marker in (
            "圆桌",
            "会议厅",
            "会议室",
            "会议形式",
            "roundtable",
        )
    )


def _required_collaboration_contract_message(*, retry: bool) -> ModelMessage:
    retry_clause = (
        "上一轮未调用工具不代表任务已经回答；原用户请求仍是当前任务。"
        if retry
        else "这是首轮即生效的执行契约。"
    )
    return ModelMessage.text(
        "system",
        (
            '<collaboration-contract enforcement="required">'
            "用户选择了协作工作法。该选择覆盖通用的“协作可选”说明，"
            "也覆盖辅助轮次智力中的 standard 工作法标签。"
            f"{retry_clause}"
            "不得直接回答，不得给出多个模拟方案，也不得用多次 "
            "chat_with_agent 冒充协作室。必须先调用当前唯一可用的 "
            "collaborate_with_agents 工具；待成员进入终态后，才能统一交付。"
            "</collaboration-contract>"
        ),
        metadata={
            "instruction_trust": "trusted_system",
            "instruction_origin": "runtime.collaboration_contract",
            "collaboration_contract_enforced": True,
            "collaboration_contract_retry": retry,
        },
    )


def _primary_turn_ownership_message() -> ModelMessage:
    """Give the visible primary turn sole ownership of intent and completion."""

    return primary_turn_ownership_message()


def _pending_deliverable_context_message(
    context: object,
) -> ModelMessage | None:
    """Build the trusted projection for one unresolved prior deliverable."""

    if not isinstance(context, dict):
        return None
    return pending_deliverable_context_message(context)


def _model_output_recovery_message(*, attempt: int) -> ModelMessage:
    """Resume one cut-off provider response without accepting its fragment."""

    return model_output_recovery_message(attempt=attempt)


def _model_stream_idle_recovery_message(*, attempt: int) -> ModelMessage:
    """Resume one interrupted provider stream without accepting its fragment."""

    return model_stream_idle_recovery_message(attempt=attempt)


def _extract_ququ_line(value: str) -> str:
    """Return the first protocol-valid Ququ line outside fenced code."""

    match = _QUQU_LINE_RE.search(_FENCED_CODE_RE.sub("", str(value or "")))
    if match is None:
        return ""
    return f"~>[{match.group(1)}] {match.group(2).strip()}"


def _ququ_line_fingerprint(value: str) -> str:
    """Normalize one Ququ line for exact semantic-repeat checks."""

    exact = _extract_ququ_line(value)
    body = exact.split("]", 1)[1] if exact else str(value or "")
    return re.sub(r"[\W_]+", "", body, flags=re.UNICODE).casefold()


def _ququ_fingerprints_overlap(left: str, right: str) -> bool:
    """Return whether two substantial lines recite one another.

    The post-answer model sometimes wraps the complete main answer in a
    longer aside instead of returning it verbatim.  Exact equality misses
    that failure and renders the answer twice under two legitimate message
    ids.  Containment is safe only for substantial fingerprints: short shared
    phrases such as greetings or connective words are not message identity.
    """

    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 8:
        return False
    if left in right or right in left:
        return True

    # A Ququ can still repeat the answer by quoting one long, distinctive
    # fragment from it (for example a sentinel, code symbol, title, or a full
    # clause) and surrounding that quote with new introspection.  That is not
    # a second answer semantically, but it is duplicated visible body content
    # and therefore violates the channel boundary.  Twelve normalized
    # characters is deliberately substantial: it catches verbatim excerpts
    # without treating short shared connective phrases as repetition.
    shorter, longer = sorted((left, right), key=len)
    shared_span = 12
    if len(shorter) < shared_span:
        return False
    return any(
        shorter[index : index + shared_span] in longer
        for index in range(len(shorter) - shared_span + 1)
    )


def _ququ_repeats_history(
    ququ_line: str,
    model_messages: list[ModelMessage],
) -> bool:
    """Reject a generated aside already delivered in assistant history."""

    candidate = _ququ_line_fingerprint(ququ_line)
    if not candidate:
        return False
    for message in model_messages:
        if message.role != "assistant":
            continue
        visible_text = _FENCED_CODE_RE.sub("", message.text_content())
        for match in _QUQU_LINE_RE.finditer(visible_text):
            historical = f"~>[{match.group(1)}] {match.group(2).strip()}"
            if _ququ_fingerprints_overlap(
                _ququ_line_fingerprint(historical),
                candidate,
            ):
                return True
        # The repair model is explicitly asked not to recite the completed
        # answer. Reject an exact recitation even though it has a fresh Ququ
        # marker and message id; otherwise the UI and persisted transcript
        # contain the same sentence twice.
        main_answer = _QUQU_LINE_RE.sub("", visible_text).strip()
        if main_answer and _ququ_fingerprints_overlap(
            _ququ_line_fingerprint(main_answer),
            candidate,
        ):
            return True
    return False


def _subagent_task_id(result: ToolResult, tool_call: ToolCall) -> str:
    text = _tool_result_text(result)
    match = _SUBAGENT_TASK_ID_RE.search(text)
    if match:
        return match.group("task_id").strip()
    return str(tool_call.arguments.get("task_id") or "").strip()


def _subagent_status(result: ToolResult) -> str:
    match = _SUBAGENT_STATUS_RE.search(_tool_result_text(result))
    return match.group("status").strip().lower() if match else ""


def _runtime_state_workspace_dir(runtime_input: RuntimeInput) -> str:
    """Return the Lantai-owned state root, never the external project root."""

    metadata = _runtime_metadata(runtime_input)
    state_workspace_dir = str(metadata.get("state_workspace_dir") or "").strip()
    if state_workspace_dir:
        return state_workspace_dir

    legacy_request_context = metadata.get("legacy_request_context")
    if isinstance(legacy_request_context, dict):
        state_workspace_dir = str(
            legacy_request_context.get("state_workspace_dir") or "",
        ).strip()
        if state_workspace_dir:
            return state_workspace_dir

    return str(runtime_input.request.workspace_dir or "").strip()


def _stable_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _runtime_release_edition(runtime_input: RuntimeInput) -> str:
    metadata = _runtime_metadata(runtime_input)
    for key in ("release_edition", "distribution"):
        text = str(metadata.get(key) or "").strip().lower()
        if text in {"mas", "app_store", "app-store", "testflight"}:
            return "mas"
    return "official"


def _runtime_owner(runtime_input: RuntimeInput) -> RuntimeRunOwner:
    request = runtime_input.request
    workspace_id = str(request.workspace_dir or "").strip() or None
    return RuntimeRunOwner(
        user_id=request.user_id or "default",
        agent_id=request.agent_id or "default",
        session_id=request.session_id,
        workspace_id=workspace_id,
        channel=request.channel or "console",
    )


async def _touch_runtime_run_heartbeat(
    *,
    workspace_dir: str,
    run_id: str,
) -> bool:
    store = RuntimeRunStore(workspace_dir)
    try:
        existing = await store.load_contract(run_id)
    except RuntimeRunStoreError:
        return False
    if (
        existing is None
        or existing.status == "queued"
        or existing.status in TERMINAL_RUNTIME_RUN_STATUSES
    ):
        return False

    try:
        touched = reduce_runtime_run(
            existing,
            RuntimeRunReducerAction(
                type="heartbeat",
                expected_attempt_id=existing.attempt_id,
                expected_revision=existing.revision,
                lease_owner=existing.lease_owner,
            ),
        ).run
        await store.save_contract(
            touched,
            expected_revision=existing.revision,
            expected_attempt_id=existing.attempt_id,
            lease_owner=existing.lease_owner,
        )
    except (
        RuntimeRunStoreConflictError,
        RuntimeRunStoreError,
        RuntimeRunTransitionError,
    ):
        return False
    return True


async def _runtime_run_heartbeat_loop(
    *,
    workspace_dir: str,
    run_id: str,
    interval_seconds: float,
) -> None:
    if interval_seconds <= 0:
        return
    while True:
        await asyncio.sleep(interval_seconds)
        touched = await _touch_runtime_run_heartbeat(
            workspace_dir=workspace_dir,
            run_id=run_id,
        )
        if not touched:
            return


def _start_runtime_run_heartbeat(
    *,
    runtime_input: RuntimeInput,
    turn_id: str,
    task_name: str,
    interval_seconds: float,
) -> asyncio.Task[None] | None:
    workspace_dir = _runtime_state_workspace_dir(runtime_input)
    run_id = str(runtime_input.request.run_id or turn_id or "").strip()
    if not workspace_dir or not run_id:
        return None
    return asyncio.create_task(
        _runtime_run_heartbeat_loop(
            workspace_dir=workspace_dir,
            run_id=run_id,
            interval_seconds=interval_seconds,
        ),
        name=task_name,
    )


async def _cancel_runtime_run_heartbeat(
    heartbeat_task: asyncio.Task[None] | None,
) -> None:
    if heartbeat_task is None:
        return
    heartbeat_task.cancel()
    with suppress(asyncio.CancelledError):
        await heartbeat_task


def _approval_binding_for_tool_call(
    *,
    tool_call: ToolCall,
    spec: ToolSpec,
    outcome: ToolPolicyOutcome,
    runtime_input: RuntimeInput,
    turn_id: str,
) -> RuntimeApprovalBinding:
    request = runtime_input.request
    metadata = _runtime_metadata(runtime_input)
    run_id = str(request.run_id or turn_id or request.session_id).strip()
    attempt_id = str(
        tool_call.metadata.get("attempt_id")
        or metadata.get("attempt_id")
        or f"{run_id}:native-attempt",
    ).strip()
    effect_id = str(
        tool_call.metadata.get("effect_id")
        or metadata.get("effect_id")
        or f"effect:{tool_call.id}",
    ).strip()
    contract_hash = str(
        metadata.get("contract_hash")
        or _stable_sha256(
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "session_id": request.session_id,
                "turn_id": turn_id,
            },
        ),
    )
    policy_snapshot_hash = str(
        metadata.get("policy_snapshot_hash")
        or outcome.metadata.get("policy_snapshot_hash")
        or _stable_sha256(
            {
                "tool_name": tool_call.name,
                "tool_call_id": tool_call.id,
                "policy": outcome.policy_payload,
                "guard": outcome.guard_payload,
                "effect_recovery": spec.effect_recovery.model_dump(mode="json"),
            },
        ),
    )
    return RuntimeApprovalBinding(
        run_id=run_id,
        attempt_id=attempt_id,
        effect_id=effect_id,
        contract_hash=contract_hash,
        policy_snapshot_hash=policy_snapshot_hash,
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
    )


class _DeadlineBudget:
    def __init__(self, deadline_monotonic: float | None) -> None:
        self.deadline_monotonic = deadline_monotonic

    @classmethod
    def from_runtime_input(
        cls,
        runtime_input: RuntimeInput,
        *,
        default_timeout_seconds: float | None,
    ) -> "_DeadlineBudget":
        metadata = _runtime_metadata(runtime_input)
        deadline = _first_float(metadata, _TURN_DEADLINE_KEYS)
        if deadline is None:
            explicit_timeout = _first_float(metadata, _TURN_TIMEOUT_KEYS)
            # Persisted task budgets from older releases are intentionally
            # ignored. Only an explicit caller deadline (tests/diagnostics) or
            # the adapter's own opt-in default may bound the whole turn.
            timeout_seconds = (
                explicit_timeout
                if explicit_timeout is not None
                else default_timeout_seconds
            )
            if timeout_seconds is not None:
                deadline = asyncio.get_running_loop().time() + timeout_seconds
        return cls(deadline)

    def remaining(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        remaining = self.deadline_monotonic - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise _TurnDeadlineExceeded()
        return remaining

    def expired(self) -> bool:
        return (
            self.deadline_monotonic is not None
            and self.deadline_monotonic - asyncio.get_running_loop().time() <= 0
        )

    def timeout(self, *limits: float | None) -> float | None:
        candidates = [
            value
            for value in (_coerce_non_negative_float(limit) for limit in limits)
            if value is not None
        ]
        remaining = self.remaining()
        if remaining is not None:
            candidates.append(remaining)
        if not candidates:
            return None
        return min(candidates)

    def pause_for(self, seconds: float) -> None:
        """Exclude an externally blocked wait from the active turn budget."""

        if self.deadline_monotonic is None:
            return
        self.deadline_monotonic += max(0.0, seconds)


def _runtime_usage(usage: ModelUsage | None) -> RuntimeUsage | None:
    if usage is None:
        return None
    return RuntimeUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_tokens=usage.cached_tokens,
    )


def _without_empty_values(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value not in (None, "", {}, [])
    }


def _turn_started_payload(
    request: RuntimeRequestContext,
    *,
    turn_id: str,
    task_contract: dict[str, Any] | None = None,
    turn_intelligence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    public_contract = None
    phase = None
    if isinstance(task_contract, dict):
        work_method_id = str(
            task_contract.get("work_method_id") or "standard"
        ).strip().lower()
        public_contract = _without_empty_values(
            {
                "work_method_id": work_method_id,
                "complexity": task_contract.get("complexity"),
                "checkpoint_policy": task_contract.get("checkpoint_policy"),
                "acceptance": task_contract.get("acceptance"),
            }
        )
        phase = {
            "deep_research": "framing",
            "sustained_execution": "planning",
            "collaboration": "preparing",
        }.get(work_method_id)
    return _without_empty_values(
        {
            "engine": "native",
            "agent_id": request.agent_id,
            "session_id": request.session_id,
            "root_session_id": request.root_session_id,
            "chat_id": request.chat_id,
            "run_id": turn_id,
            "task_contract": public_contract,
            "turn_intelligence": (
                _without_empty_values(
                    {
                        "status": turn_intelligence.get("status"),
                        "interaction_mode": turn_intelligence.get(
                            "interaction_mode"
                        ),
                        "durable_artifact_intent": turn_intelligence.get(
                            "durable_artifact_intent"
                        ),
                        "confidence": turn_intelligence.get("confidence"),
                        "model": turn_intelligence.get("model"),
                        "decision_origin": turn_intelligence.get(
                            "decision_origin"
                        ),
                        "failure_reason": turn_intelligence.get("failure_reason"),
                    }
                )
                if isinstance(turn_intelligence, dict)
                else None
            ),
            "phase": phase,
        }
    )


def _tool_argument_snapshot(arguments: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(str(key) for key in arguments)
    return {
        "argument_keys": keys,
        "arguments_redacted": bool(keys),
    }


def _safe_collaboration_arguments(tool_call: ToolCall) -> dict[str, Any]:
    """Project only user-visible collaboration identity into Runtime events."""

    if tool_call.name not in _SAFE_IDENTITY_TOOLS:
        return {}
    arguments = dict(tool_call.arguments)
    if tool_call.name != _SUBAGENT_BATCH_TOOL:
        to_agent = str(arguments.get("to_agent") or "").strip()
        return {"to_agent": to_agent} if to_agent else {}

    safe_tasks: list[dict[str, str]] = []
    tasks = arguments.get("tasks")
    if not isinstance(tasks, list):
        return {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        to_agent = str(task.get("to_agent") or "").strip()
        label = str(task.get("label") or "").strip()
        if not to_agent:
            continue
        safe_task = {"to_agent": to_agent}
        if label:
            safe_task["label"] = label
        safe_tasks.append(safe_task)
    return {"tasks": safe_tasks} if safe_tasks else {}


def _terminal_collaboration_snapshot(
    tool_call: ToolCall,
    *,
    latest: dict[str, Any] | None,
    error_code: str,
    member_status: str = "failed",
) -> dict[str, Any] | None:
    """Close a collaboration room without exposing its private task text.

    The progressive tool can fail outside the hosted coordinator (for example
    when the parent runtime times out).  Preserve the last public projection,
    but always emit an explicit room phase and closure assessment so replayed
    runs never leave an apparently active room behind.
    """

    source = dict(latest or {})
    raw_members = source.get("members")
    members = [
        dict(member)
        for member in (raw_members if isinstance(raw_members, list) else [])
        if isinstance(member, dict)
    ]
    if not members:
        safe_arguments = _safe_collaboration_arguments(tool_call)
        tasks = safe_arguments.get("tasks")
        if not isinstance(tasks, list):
            return None
        members = [
            {
                "member_id": f"member-{index + 1}",
                "agent_id": str(task.get("to_agent") or ""),
                "label": str(task.get("label") or ""),
                "status": member_status,
                "attempt": 0,
                "duration_ms": 0,
                "error_code": error_code,
            }
            for index, task in enumerate(tasks)
            if isinstance(task, dict) and task.get("to_agent")
        ]
    else:
        for member in members:
            if str(member.get("status") or "") in {
                "queued",
                "running",
                "retrying",
            }:
                member["status"] = member_status
                member["error_code"] = error_code
    if not members:
        return None

    return close_public_room_snapshot(
        source=source,
        batch_id=str(source.get("batch_id") or tool_call.id),
        members=members,
    )


def _collaboration_failure_metadata(
    tool_call: ToolCall,
    *,
    latest: dict[str, Any] | None = None,
    error_code: str,
) -> dict[str, Any]:
    if tool_call.name != _SUBAGENT_BATCH_TOOL:
        return {}
    # No hosted snapshot means validation/policy/execution failed before the
    # coordinator proved that a room existed.  Projecting task arguments as a
    # closed room invents members, attempts, and a closure that never happened.
    if latest is None:
        return {}
    snapshot = _terminal_collaboration_snapshot(
        tool_call,
        latest=latest,
        error_code=error_code,
        member_status="cancelled" if error_code == "steered" else "failed",
    )
    return {"collaboration": snapshot} if snapshot is not None else {}


def _tool_call_public_payload(tool_call: ToolCall) -> dict[str, Any]:
    payload = {
        "id": tool_call.id,
        "name": tool_call.name,
        "tool_name": tool_call.name,
        **_tool_argument_snapshot(dict(tool_call.arguments)),
    }
    safe_arguments = _safe_collaboration_arguments(tool_call)
    if safe_arguments:
        payload["arguments"] = safe_arguments
    return payload


def _public_tool_result_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    public_keys = {
        "approval",
        "error_code",
        "error_type",
        "exit_code",
        "invocation_status",
        "side_effect_outcome",
        "effect_settlement_status",
        "automatic_retry_allowed",
        "requires_probe",
        "requires_human",
        "external_effect_status",
        "external_dispatch_status",
        "resolved_effect_id",
        "policy_source",
        "job_id",
        "job_status",
        "publish_ready",
        "requires_publish",
        "delivery_status",
        "failure_categories",
        "recovery_actions",
        "verification_status",
        "verification_attestation",
    }
    return {
        key: _json_safe(value)
        for key, value in metadata.items()
        if key in public_keys
    }


def _public_guard_payload(guard_result: ToolGuardResult) -> dict[str, Any]:
    payload = guard_result.to_dict()
    payload.pop("params", None)
    keys = sorted(str(key) for key in guard_result.params)
    payload["param_keys"] = keys
    payload["params_redacted"] = bool(keys)
    return payload


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except TypeError:
            pass
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _block_to_tool_output(block: Any) -> dict[str, Any]:
    payload = _json_safe(block)
    if isinstance(payload, dict):
        block_type = str(payload.get("type") or "text")
        if block_type == "text":
            return {"type": "text", "text": str(payload.get("text") or "")}
        return payload
    return {"type": "text", "text": str(payload)}


def _normalise_tool_result(
    call: ToolCall,
    raw: Any,
    *,
    ok: bool = True,
    error: str | None = None,
) -> ToolResult:
    if isinstance(raw, ToolResult):
        return raw

    content: list[dict[str, Any]] = []
    raw_content = getattr(raw, "content", None)
    if isinstance(raw_content, list):
        content = [_block_to_tool_output(block) for block in raw_content]
    elif raw is not None:
        payload = _json_safe(raw)
        if isinstance(payload, dict) and "content" in payload:
            nested = payload.get("content")
            if isinstance(nested, list):
                content = [_block_to_tool_output(block) for block in nested]
        if not content:
            text = (
                payload
                if isinstance(payload, str)
                else json.dumps(payload, ensure_ascii=False)
            )
            content = [{"type": "text", "text": text}]

    detected_error = next(
        (
            str(block.get("text") or "").strip()
            for block in content
            if block.get("type") == "text"
            and re.match(
                (
                    r"^(?:error\s*:|command failed with exit code\b|"
                    r"tool policy blocked\b)"
                ),
                str(block.get("text") or "").strip(),
                flags=re.IGNORECASE,
            )
        ),
        "",
    )
    is_error = bool(
        getattr(raw, "isError", False)
        or getattr(raw, "is_error", False)
        or detected_error
    )
    if is_error and not error:
        error = detected_error or next(
            (
                str(block.get("text") or "").strip()
                for block in content
                if block.get("type") == "text"
                and str(block.get("text") or "").strip()
            ),
            "Tool call failed.",
        )
    metadata = {
        "raw": _json_safe(raw),
        **mcp_structured_result_metadata(raw),
    }
    raw_metadata = getattr(raw, "metadata", None)
    if isinstance(raw_metadata, dict):
        metadata.update(raw_metadata)
    return ToolResult(
        call_id=call.id,
        name=call.name,
        ok=ok and not is_error,
        content=content,
        error=error,
        metadata=metadata,
    )


def _tool_result_text(result: ToolResult) -> str:
    if result.error:
        return result.error
    parts: list[str] = []
    for block in result.content:
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _tool_result_output(result: ToolResult) -> list[dict[str, Any]]:
    envelope = parse_or_build_tool_envelope(result)
    return [dict(block) for block in envelope.model_projection.blocks]


def _tool_result_payload(result: ToolResult) -> dict[str, Any]:
    """Project one immutable tool result into its public event payload."""

    envelope = parse_or_build_tool_envelope(result)
    payload = {
        "id": result.call_id,
        "name": result.name,
        "tool_name": result.name,
        "output": _tool_result_output(result),
        "ok": result.ok,
        "error": result.error,
        "metadata": _public_tool_result_metadata(result.metadata),
        "canonical_tool": {
            "envelope_id": envelope.envelope_id,
            "integrity_sha256": envelope.integrity_sha256,
            "canonical": envelope.canonical.model_dump(mode="json"),
            "model_projection": envelope.model_projection.model_dump(
                mode="json"
            ),
            "ui_intent": envelope.ui_intent.model_dump(mode="json"),
            "effect_id": envelope.effect_id,
            "effect_attempt_id": envelope.effect_attempt_id,
            "effect_revision": envelope.effect_revision,
            "effect_status": envelope.effect_status,
        },
    }
    collaboration = result.metadata.get("collaboration")
    if isinstance(collaboration, dict):
        payload["collaboration"] = _json_safe(collaboration)
    artifact_effects = result.metadata.get("artifact_effects")
    if isinstance(artifact_effects, list):
        payload["artifact_effects"] = _json_safe(artifact_effects)
    for key in ("effect_id", "attempt_id"):
        value = result.metadata.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = value.strip()
    error_code = result.metadata.get("error_code")
    if isinstance(error_code, str) and error_code.strip():
        payload["error_code"] = error_code.strip()
    return payload


def _tool_failure_fingerprint(tool_call: ToolCall, result: ToolResult) -> str:
    """Fingerprint one failed strategy, including its concrete arguments.

    Different arguments are evidence that the model changed strategy. Treating
    every failure from the same tool as identical used to stop valid recovery
    attempts after only two calls.
    """

    metadata = dict(result.metadata or {})
    nested_policy = metadata.get("policy")
    policy = nested_policy if isinstance(nested_policy, dict) else {}
    error_code = str(
        metadata.get("error_code")
        or policy.get("error_code")
        or metadata.get("code")
        or "tool_error"
    ).strip().lower()
    policy_source = str(
        metadata.get("policy_source")
        or policy.get("source")
        or metadata.get("source")
        or "runtime"
    ).strip().lower()
    exit_code = metadata.get("exit_code")
    exit_signature = (
        f"process_exit:{exit_code}"
        if isinstance(exit_code, int) and not isinstance(exit_code, bool)
        else ""
    )
    failure = _tool_result_text(result).strip().lower()
    failure = re.sub(
        r"(?:[a-z]:)?[/\\](?:[^\s'\"`]+[/\\])*[^\s'\"`]*",
        "<path>",
        failure,
    )
    failure = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b",
        "<id>",
        failure,
        flags=re.IGNORECASE,
    )
    failure = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", failure)
    failure = re.sub(r"\s+", " ", failure).strip()[:512]
    arguments = json.dumps(
        _json_safe(tool_call.arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    raw = "|".join(
        (
            tool_call.name,
            arguments,
            error_code,
            policy_source,
            exit_signature,
            failure,
        )
    )
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _tool_result_error_code(result: ToolResult) -> str:
    metadata = dict(result.metadata or {})
    policy = metadata.get("policy")
    policy_metadata = policy if isinstance(policy, dict) else {}
    return str(
        metadata.get("error_code")
        or policy_metadata.get("error_code")
        or metadata.get("code")
        or "tool_failed"
    ).strip().lower()


def _tool_call_fingerprint(tool_call: ToolCall) -> str:
    """Identify one concrete tool strategy independent of provider call IDs."""

    arguments = json.dumps(
        _json_safe(tool_call.arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    raw = f"{tool_call.name}|{arguments}"
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _runtime_message_from_model_response(
    response: ModelResponse,
    *,
    strip_tool_public_progress: bool = False,
) -> RuntimeMessage:
    blocks: list[ContentBlock] = []
    for block in response.message.content:
        if block.type in {"text", "thinking", "tool_call", "tool_result", "error"}:
            data = (
                without_public_progress_block_data(block.data)
                if block.type == "tool_call" and strip_tool_public_progress
                else dict(block.data)
            )
            blocks.append(
                ContentBlock(
                    type=block.type,
                    text=block.text,
                    data=data,
                    metadata=without_runtime_public_progress_claims(
                        dict(block.metadata),
                    ),
                ),
            )
        elif block.type in {"image", "file"}:
            blocks.append(
                ContentBlock(
                    type=block.type,
                    text=block.text,
                    data=dict(block.data),
                    metadata=without_runtime_public_progress_claims(
                        dict(block.metadata),
                    ),
                ),
            )
        elif block.type in {"audio", "video"}:
            blocks.append(
                ContentBlock(
                    type="file",
                    text=block.text,
                    data={"media_type": block.type, **dict(block.data)},
                    metadata=without_runtime_public_progress_claims(
                        dict(block.metadata),
                    ),
                ),
            )
    return RuntimeMessage(
        id=response.message.id,
        role=response.message.role,
        name=response.message.name,
        content=blocks,
        metadata=without_runtime_public_progress_claims(
            dict(response.message.metadata),
        ),
    )


def _model_response_has_output(response: ModelResponse) -> bool:
    for block in response.message.content:
        if block.text and block.text.strip():
            return True
        if block.data:
            return True
    return False


def _model_response_has_visible_output(response: ModelResponse) -> bool:
    """Whether a response contains a deliverable, non-reasoning answer.

    Thinking blocks are persisted and may be shown in the dedicated Thinking
    UI, but they are not an answer to the user.  Treating them as one lets a
    provider finish a turn without any visible reply, then makes the console
    fail later at its privacy boundary.
    """
    for block in response.message.content:
        if block.type in {"thinking", "reasoning"}:
            continue
        if block.text and block.text.strip():
            return True
        if block.data:
            return True
    return False


def _validate_tool_call_arguments(
    tool_call: ToolCall,
    *,
    specs_by_name: dict[str, ToolSpec],
) -> None:
    arguments = getattr(tool_call, "arguments", None)
    if not isinstance(arguments, dict):
        raise _ModelProtocolError(
            "Native runtime model protocol error: tool call "
            f"{tool_call.name} arguments must be an object."
        )

    spec = specs_by_name.get(tool_call.name)
    schema = spec.input_schema if spec is not None else {}
    if not isinstance(schema, dict) or not schema:
        return
    feedback = tool_argument_schema_feedback(schema, arguments)
    if feedback is None:
        return
    raise _ModelProtocolError(
        "Native runtime model protocol error: tool call "
        f"{tool_call.name} {feedback}"
    )


def _normalize_tool_call_argument_aliases(
    tool_call: ToolCall,
    *,
    specs_by_name: dict[str, ToolSpec],
) -> ToolCall:
    """Normalize narrow, schema-backed aliases before protocol validation.

    OpenAI-compatible providers commonly emit ``path`` or ``filePath`` for a
    tool whose canonical schema says ``file_path``. This is transport shape,
    not user intent, so Runtime may repair it deterministically. Unknown or
    ambiguous missing values still go through the model-owned repair round.
    """

    spec = specs_by_name.get(tool_call.name)
    schema = spec.input_schema if spec is not None else {}
    required = schema.get("required") if isinstance(schema, dict) else None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(required, list) or not isinstance(properties, dict):
        return tool_call
    arguments = dict(tool_call.arguments)
    if "file_path" not in required or "file_path" in arguments:
        return tool_call
    aliases = [
        alias
        for alias in ("path", "filePath", "filepath")
        if alias in arguments and alias not in properties
    ]
    if len(aliases) != 1:
        return tool_call
    alias = aliases[0]
    arguments["file_path"] = arguments.pop(alias)
    return ToolCall(
        id=tool_call.id,
        name=tool_call.name,
        arguments=arguments,
        metadata={
            **dict(tool_call.metadata),
            "normalized_argument_aliases": {alias: "file_path"},
        },
    )


def _message_from_delta(
    *,
    message_id: str | None,
    block_type: str,
    delta: str,
) -> RuntimeMessage:
    return RuntimeMessage(
        id=message_id,
        role="assistant",
        content=[
            ContentBlock(
                type=block_type,  # type: ignore[arg-type]
                text=delta,
            ),
        ],
    )


def _tool_preamble_public_progress_event(
    response: ModelResponse,
    *,
    runtime_input: RuntimeInput,
    turn_id: str,
    seq: int,
    client_turn_id: str = "",
    allow_structured: bool = True,
) -> RuntimeEvent | None:
    """Project an already-public tool preamble into compact progress.

    This reads either ordinary assistant ``text`` before the first tool or the
    dedicated model-authored public field inside that same tool call. Provider
    thinking is neither inspected nor rewritten, and Runtime does not author
    fallback copy.
    """

    projection = model_tool_public_progress(
        response.message.content,
        allow_structured=allow_structured,
    )
    if projection is None:
        return None
    public_text, tool_call_ids, public_parts = projection
    source_message_id = str(response.message.id or "").strip()
    if not source_message_id:
        stable_material = json.dumps(
            {
                "text": public_parts,
                "tools": [
                    {
                        "name": str(block.data.get("name") or ""),
                        "arguments": _json_safe(block.data.get("arguments")),
                    }
                    for block in response.message.content
                    if block.type == "tool_call" and isinstance(block.data, dict)
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        source_message_id = hashlib.sha256(
            stable_material.encode("utf-8"),
        ).hexdigest()[:24]
    logical_turn_material = json.dumps(
        {
            "client_turn_id": client_turn_id,
            "source_message_id": source_message_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    logical_turn_token = hashlib.sha256(
        logical_turn_material.encode("utf-8"),
    ).hexdigest()[:24]
    progress_id = f"public-progress:{turn_id}:{logical_turn_token}"
    progress_metadata = {
        "schema_version": 2,
        "id": progress_id,
        "revision": 1,
        "source": "runtime_facts",
        "text_origin": "assistant_public_text",
        "tool_call_ids": tool_call_ids,
    }
    message = _message_from_delta(
        message_id=progress_id,
        block_type="thinking",
        delta=public_text,
    )
    message.metadata = {
        "source_message_id": progress_id,
        "run_id": turn_id,
        "runtime_turn_id": turn_id,
        "runtime_presentation_only": True,
        **_agent_presentation_context(runtime_input),
        **({"client_turn_id": client_turn_id} if client_turn_id else {}),
    }
    message.content[0].metadata = {
        "public_projection": "runtime_progress_v2",
        "source": "runtime_facts",
        "private_reasoning_exposed": False,
        "public_progress_id": progress_id,
        "public_progress_revision": 1,
        "text_origin": "assistant_public_text",
        "tool_call_ids": tool_call_ids,
    }
    return RuntimeEvent(
        id=str(uuid4()),
        seq=seq,
        type="message.completed",
        status="completed",
        source="assistant",
        session_id=runtime_input.request.session_id,
        turn_id=turn_id,
        item_id=message.id,
        summary=public_text,
        payload={
            "message": message.model_dump(mode="json"),
            "source_message_id": progress_id,
            "content_type": "thinking",
            "stream_message_type": "reasoning",
            "public_progress": progress_metadata,
            "run_id": turn_id,
            **({"client_turn_id": client_turn_id} if client_turn_id else {}),
        },
    )


def _presentation(
    tool_name: str,
    *,
    payload: dict[str, Any] | None = None,
    summary: str = "",
    is_approval: bool = False,
) -> RuntimePresentation:
    product = tool_presentation_for_tool_name(
        tool_name,
        payload=payload,
        summary=summary,
        is_approval=is_approval,
    )
    return RuntimePresentation.model_validate(product.model_dump())


def _agent_presentation_context(runtime_input: RuntimeInput) -> dict[str, str]:
    """Project request identity needed by the product presentation policy."""

    request = runtime_input.request
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    legacy_context = metadata.get("legacy_request_context")
    context = legacy_context if isinstance(legacy_context, dict) else {}
    result = {"agent_id": str(request.agent_id or "")}
    for key in (
        "presentation_context",
        "skill_exposure",
        "skill_invocation_origin",
    ):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    return {key: value for key, value in result.items() if value}


def _tool_payload_with_agent_presentation_context(
    payload: dict[str, Any],
    runtime_input: RuntimeInput,
) -> dict[str, Any]:
    result = dict(payload)
    metadata = (
        dict(result.get("metadata") or {})
        if isinstance(result.get("metadata"), dict)
        else {}
    )
    metadata.update(_agent_presentation_context(runtime_input))
    result["metadata"] = metadata
    return result


def _synthetic_guard_result(
    *,
    tool_call: ToolCall,
    outcome: ToolPolicyOutcome,
) -> ToolGuardResult:
    guard_payload = outcome.guard_payload if isinstance(outcome.guard_payload, dict) else {}
    findings: list[GuardFinding] = []
    raw_findings = guard_payload.get("findings") if isinstance(guard_payload, dict) else None
    if isinstance(raw_findings, list):
        for index, raw in enumerate(raw_findings):
            if not isinstance(raw, dict):
                continue
            severity = str(raw.get("severity") or outcome.risk or "MEDIUM").upper()
            try:
                parsed_severity = GuardSeverity(severity)
            except ValueError:
                parsed_severity = GuardSeverity.MEDIUM
            category = str(raw.get("category") or "resource_abuse")
            try:
                parsed_category = GuardThreatCategory(category)
            except ValueError:
                parsed_category = GuardThreatCategory.RESOURCE_ABUSE
            findings.append(
                GuardFinding(
                    id=str(raw.get("id") or f"native_policy_{index}"),
                    rule_id=str(raw.get("rule_id") or "native_policy"),
                    category=parsed_category,
                    severity=parsed_severity,
                    title=str(raw.get("title") or "Tool approval required"),
                    description=str(raw.get("description") or outcome.reason),
                    tool_name=tool_call.name,
                    metadata=dict(raw.get("metadata") or {}),
                ),
            )

    if not findings:
        risk = str(outcome.risk or "medium").upper()
        severity = GuardSeverity.MEDIUM
        if risk in {"CRITICAL", "HIGH"}:
            severity = GuardSeverity.HIGH
        elif risk in {"LOW", "INFO"}:
            severity = GuardSeverity.LOW
        findings.append(
            GuardFinding(
                id="native_policy_prompt",
                rule_id="native_policy_prompt",
                category=GuardThreatCategory.RESOURCE_ABUSE,
                severity=severity,
                title="Tool approval required",
                description=outcome.reason or f"Approve tool call: {tool_call.name}",
                tool_name=tool_call.name,
                metadata={"policy_decision": outcome.policy_payload or {}},
            ),
        )

    return ToolGuardResult(
        tool_name=tool_call.name,
        params=dict(tool_call.arguments),
        findings=findings,
    )




__all__ = [
    'logger',
    'ModelClientFactory',
    '_resumed_pending_tool_model_events',
    '_model_message_transaction_tail',
    'DEFAULT_NATIVE_TURN_TIMEOUT_SECONDS',
    'DEFAULT_NATIVE_MODEL_FIRST_EVENT_TIMEOUT_SECONDS',
    'DEFAULT_NATIVE_MODEL_IDLE_TIMEOUT_SECONDS',
    'DEFAULT_NATIVE_MODEL_REQUEST_TIMEOUT_SECONDS',
    'DEFAULT_NATIVE_MODEL_STREAM_CLOSE_TIMEOUT_SECONDS',
    'DEFAULT_NATIVE_TOOL_TIMEOUT_SECONDS',
    'DEFAULT_NATIVE_TOOL_HEARTBEAT_INTERVAL_SECONDS',
    '_CONVERGENCE_REVIEW_TIMEOUT_SECONDS',
    '_MAIN_RESPONSE_MAX_TOKENS',
    '_COLLABORATION_STAFFING_MAX_TOKENS',
    '_COLLABORATION_STAFFING_SYSTEM_TEXT',
    '_COLLABORATION_MEMBER_SYSTEM_TEXT',
    '_DEEP_RESEARCH_SYSTEM_TEXT',
    '_MIN_RESPONSE_MAX_TOKENS',
    '_MAIN_OUTPUT_RECOVERY_LIMIT',
    '_MAIN_STREAM_IDLE_RECOVERY_LIMIT',
    '_QUQU_LINE_RE',
    '_FENCED_CODE_RE',
    '_PROGRESSIVE_TOOL_NAME',
    '_PROGRESSIVE_TOOL_GROUPS',
    '_DECLARE_DELIVERABLE_CONTRACT_TOOL',
    '_RESOLVE_PENDING_DELIVERABLE_TOOL',
    '_SUBAGENT_SUBMIT_TOOL',
    '_SUBAGENT_CHECK_TOOL',
    '_SUBAGENT_FOREGROUND_TOOL',
    '_SUBAGENT_BATCH_TOOL',
    '_SAFE_IDENTITY_TOOLS',
    '_SUBAGENT_TASK_ID_RE',
    '_SUBAGENT_STATUS_RE',
    '_TERMINAL_SUBAGENT_STATUSES',
    '_TURN_DEADLINE_KEYS',
    '_TURN_TIMEOUT_KEYS',
    '_MODEL_IDLE_TIMEOUT_KEYS',
    '_MODEL_FIRST_EVENT_TIMEOUT_KEYS',
    '_MODEL_REQUEST_TIMEOUT_KEYS',
    '_TOOL_TIMEOUT_KEYS',
    '_APPROVAL_TIMEOUT_KEYS',
    '_TurnDeadlineExceeded',
    '_ModelStreamDeadlineExceeded',
    '_ModelStreamIdleInterruption',
    '_ModelStreamControlInterruption',
    '_ProviderModelStreamTimeout',
    '_project_model_stream_boundaries',
    '_ToolSteered',
    '_ToolArgumentRepairExhausted',
    '_IncompleteModelOutput',
    '_MissingVisibleFinalAnswer',
    '_DegenerateModelOutput',
    '_TaskBudgetExceeded',
    '_RepeatedToolFailure',
    '_RequiredCapabilityFailed',
    '_RuntimeDurabilityBlocked',
    '_progressive_tool_group',
    '_progressive_group_description',
    '_progressive_loader_spec',
    '_tools_for_turn_intelligence',
    '_model_classified_durable_artifact_turn',
    '_explicit_model_multimodal_support',
    '_is_visual_creation_request',
    '_pptx_skill_output_candidate',
    '_file_fingerprint',
    '_tool_declared_delivery_candidates',
    '_model_declared_generated_paths',
    '_coerce_non_negative_float',
    '_first_float',
    '_timeout_from_metadata',
    '_runtime_metadata',
    '_main_response_max_tokens',
    '_internal_execution_messages',
    '_collaboration_staffing_messages',
    '_collaboration_member_messages',
    '_deep_research_messages',
    '_runtime_event_has_visible_completed_message',
    '_event_with_persona_delivery',
    '_event_with_outcome_status',
    '_synthesis_boundary_is_incomplete',
    '_latest_user_text',
    '_prose_requests_hosted_collaboration',
    '_prose_requests_roundtable',
    '_required_collaboration_contract_message',
    '_primary_turn_ownership_message',
    '_pending_deliverable_context_message',
    '_model_output_recovery_message',
    '_model_stream_idle_recovery_message',
    '_extract_ququ_line',
    '_ququ_line_fingerprint',
    '_ququ_fingerprints_overlap',
    '_ququ_repeats_history',
    '_subagent_task_id',
    '_subagent_status',
    '_runtime_state_workspace_dir',
    '_stable_sha256',
    '_runtime_release_edition',
    '_runtime_owner',
    '_touch_runtime_run_heartbeat',
    '_runtime_run_heartbeat_loop',
    '_start_runtime_run_heartbeat',
    '_cancel_runtime_run_heartbeat',
    '_approval_binding_for_tool_call',
    '_DeadlineBudget',
    '_runtime_usage',
    '_without_empty_values',
    '_turn_started_payload',
    '_tool_argument_snapshot',
    '_safe_collaboration_arguments',
    '_terminal_collaboration_snapshot',
    '_collaboration_failure_metadata',
    '_tool_call_public_payload',
    '_public_tool_result_metadata',
    '_public_guard_payload',
    '_json_safe',
    '_block_to_tool_output',
    '_normalise_tool_result',
    '_tool_result_text',
    '_tool_result_output',
    '_tool_failure_fingerprint',
    '_tool_result_error_code',
    '_tool_call_fingerprint',
    '_runtime_message_from_model_response',
    '_model_response_has_output',
    '_model_response_has_visible_output',
    '_validate_tool_call_arguments',
    '_normalize_tool_call_argument_aliases',
    '_message_from_delta',
    '_tool_preamble_public_progress_event',
    '_presentation',
    '_agent_presentation_context',
    '_tool_payload_with_agent_presentation_context',
    '_synthetic_guard_result',
]
