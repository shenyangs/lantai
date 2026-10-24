# -*- coding: utf-8 -*-
"""Authoritative validate-authorize-execute-settle-project tool transaction."""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import uuid4

from ..security.tool_guard.approval import ApprovalDecision
from ..utils.async_settlement import await_settled
from .errors import runtime_exception_message as _exception_message
from .artifact_effects import (
    RuntimeArtifactEffectJournal,
    RuntimeArtifactEffectRecord,
    artifact_effect_intents as build_artifact_effect_intents,
    artifact_effect_intents_from_result,
    artifact_effect_public_payload,
    settle_artifact_effects,
)
from .effects import (
    RuntimeEffectJournalCorruptionError,
    RuntimeEffectJournalError,
    RuntimeEffectRecoveryCapability,
    RuntimeEffectUnresolvedError,
)
from .effect_context import RuntimeEffectContext, bind_runtime_effect_context
from .events import RuntimeEvent
from .file_delivery import (
    FILE_DELIVERY_TOOL_NAME,
    expected_file_type_label,
    file_path_matches_extensions,
)
from .input import RuntimeInput
from .native_turn_support import (
    _PROGRESSIVE_TOOL_GROUPS,
    _PROGRESSIVE_TOOL_NAME,
    _SUBAGENT_BATCH_TOOL,
    _SUBAGENT_CHECK_TOOL,
    _SUBAGENT_FOREGROUND_TOOL,
    _TOOL_TIMEOUT_KEYS,
    _DeadlineBudget,
    _ToolSteered,
    _TurnDeadlineExceeded,
    _cancel_runtime_run_heartbeat,
    _coerce_non_negative_float,
    _collaboration_failure_metadata,
    _json_safe,
    _normalise_tool_result,
    _presentation,
    _runtime_metadata,
    _start_runtime_run_heartbeat,
    _timeout_from_metadata,
    _tool_call_public_payload,
    _tool_payload_with_agent_presentation_context,
)
from .progressive_tools import transition_progressive_tool_groups
from .runs import RuntimeEffectRecord
from .tools import (
    LantaiToolRegistry,
    McpClientSpec,
    ToolCall,
    ToolPolicyContext,
    ToolPolicyOutcome,
    ToolResult,
    ToolSpec,
)
from .turn_control import RuntimeTurnControlStore

ToolTransactionPhase = Literal[
    "validate", "authorize", "execute", "settle", "project"
]


def _unconfirmed_receipt_result(
    result: ToolResult, effect: RuntimeEffectRecord | None,
    recovery: RuntimeEffectRecoveryCapability, exc: Exception, message: str,
    *, artifact: bool = False, settled_effect: bool = False,
) -> ToolResult:
    metadata = {
        **result.metadata,
        "artifact_effect_journal_error" if artifact else "effect_journal_error": type(exc).__name__,
        "original_tool_result_ok": result.metadata.get("original_tool_result_ok", result.ok),
        "effect_settlement_status": "unconfirmed",
    }
    external_started = recovery.external_side_effect and metadata.get("invocation_status") != "not_started"
    if external_started and result.ok:
        metadata["side_effect_outcome"] = "committed"
    metadata.update({
        "automatic_retry_allowed": not external_started,
        "requires_probe": external_started and recovery.probe not in {"none", "not_required"},
        "requires_human": external_started and recovery.on_ambiguity == "manual_disambiguation",
    })
    if effect is not None:
        fields = {"effect_id": effect.effect_id, "attempt_id": effect.attempt_id, "effect_revision": effect.revision, "effect_status": effect.status}
        for key, value in fields.items():
            if settled_effect:
                metadata.setdefault(key, value)
            else:
                metadata[key] = value
    metadata.pop("canonical_tool_envelope", None)
    return result.model_copy(update={"ok": False, "error": message, "metadata": metadata})

_LOGGER = logging.getLogger(__name__)


def _pending_control_input_before_effect(
    *,
    runtime_input: RuntimeInput,
    turn_id: str,
    after_seq: int,
) -> bool:
    """Observe a durable same-run input without claiming or reordering it."""

    metadata = _runtime_metadata(runtime_input)
    state_workspace_dir = str(
        metadata.get("state_workspace_dir")
        or runtime_input.request.workspace_dir
        or ""
    ).strip()
    if not state_workspace_dir:
        return False
    expected_attempt_id = str(metadata.get("attempt_id") or "").strip()
    store = RuntimeTurnControlStore(state_workspace_dir)
    for item in store.read_admissible_inbox(turn_id, after_seq=after_seq):
        target = item.target
        if target is not None and (
            target.kind != "run" or target.target_id != turn_id
        ):
            # A queued next task must never interrupt the current tool.
            continue
        if (
            expected_attempt_id
            and item.attempt_id is not None
            and item.attempt_id != expected_attempt_id
        ):
            continue
        return True
    return False


def _collaboration_with_execution_scope(
    collaboration: dict[str, Any],
    *,
    runtime_input: RuntimeInput,
    turn_id: str,
) -> dict[str, Any]:
    """Bind a public room snapshot to its authoritative parent execution.

    The collaboration room can stay open while the user navigates to another
    chat.  Controls therefore travel with the snapshot instead of being
    reconstructed from whichever chat happens to be selected in the UI.
    """

    metadata = _runtime_metadata(runtime_input)
    existing_scope = collaboration.get("execution_scope")
    scope: dict[str, Any] = {
        **(existing_scope if isinstance(existing_scope, dict) else {}),
        "parent_run_id": turn_id,
        "attempt_id": str(metadata.get("attempt_id") or "").strip(),
        "runtime_generation": metadata.get("runtime_generation"),
        "chat_id": str(runtime_input.request.chat_id or "").strip(),
        "session_id": str(runtime_input.request.session_id or "").strip(),
        "workspace_dir": str(
            metadata.get("state_workspace_dir")
            or runtime_input.request.workspace_dir
            or ""
        ).strip(),
    }
    return {
        **collaboration,
        "execution_scope": {
            key: value
            for key, value in scope.items()
            if value is not None and value != ""
        },
    }


def _scope_collaboration_result(
    result: ToolResult,
    *,
    runtime_input: RuntimeInput,
    turn_id: str,
) -> ToolResult:
    collaboration = result.metadata.get("collaboration")
    if not isinstance(collaboration, dict):
        return result
    return result.model_copy(
        update={
            "metadata": {
                **result.metadata,
                "collaboration": _collaboration_with_execution_scope(
                    collaboration,
                    runtime_input=runtime_input,
                    turn_id=turn_id,
                ),
            },
        },
    )


def _steered_before_effect_result(tool_call: ToolCall) -> ToolResult:
    return ToolResult(
        call_id=tool_call.id,
        name=tool_call.name,
        ok=False,
        error=(
            "Tool execution was superseded by newer user input before any "
            "effect started."
        ),
        metadata={
            "error_code": "steered_before_effect",
            "invocation_status": "not_started",
            "side_effect_outcome": "known_not_applied",
            "automatic_retry_allowed": True,
            "superseded_by_control_input": True,
        },
    )


async def _cancel_and_join_collaboration(tasks: Sequence[asyncio.Future[Any]]) -> None:
    """Retain ownership until every local child has finished its cleanup.

    Cancel each child once. Repeated parent stops must not inject another
    cancellation into a child's finally block. This cannot stop a synchronous
    thread or a remote effect, and deliberately has no pretend-success timeout.
    """
    # Preserve the caller's ownership order.  Set iteration made cleanup
    # timing depend on Future hashes and could cancel a shield/wait wrapper
    # before the underlying collaboration operation had begun its own remote
    # cleanup.
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    joined = asyncio.gather(*tasks, return_exceptions=True)
    interrupted = None
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError as exc:
            interrupted = exc
    logged_errors: set[int] = set()
    for result in joined.result():
        if (
            isinstance(result, Exception)
            and not isinstance(result, StopAsyncIteration)
            and id(result) not in logged_errors
        ):
            # Completion or parent cancellation may win the race, but must
            # not hide a concurrent watcher/cleanup failure.
            logged_errors.add(id(result))
            _LOGGER.error(
                "Owned collaboration task failed during completion or cleanup",
                exc_info=(type(result), result, result.__traceback__),
            )
    if interrupted is not None:
        raise interrupted


async def execute_owned_sync(executor: Callable[..., Any], arguments: dict[str, Any]) -> Any:
    """Cancellation cannot release an attempt while its synchronous worker runs."""
    worker = asyncio.create_task(asyncio.to_thread(executor, **arguments))
    return await await_settled(worker)


async def close_owned_async_iterator(
    iterator: Any,
    *,
    task_name: str = "owned-async-iterator-close",
) -> None:
    """Close an owned async iterator without abandoning its cleanup."""

    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    # When this helper runs from an async generator's ``finally`` during
    # ``aclose()``, GeneratorExit remains the authoritative close signal.
    # Re-raising a concurrent task cancellation here would make the outer
    # generator enter its normal cancellation handler and try to yield an
    # interruption event while it is closing, which Python rejects as
    # ``async generator ignored GeneratorExit``.
    closing_generator = sys.exc_info()[0] is GeneratorExit
    cleanup = asyncio.create_task(close(), name=task_name)
    interrupted = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            # A repeated stop may arrive while the iterator is cancelling its
            # remote members. Retain ownership until that cleanup settles.
            interrupted = exc
    cleanup.result()
    if interrupted is not None and not closing_generator:
        raise interrupted


@dataclass(frozen=True, slots=True)
class ToolBatchValidationOutcome:
    """Atomic validation result for one model-emitted tool-call batch."""

    tool_calls: tuple[ToolCall, ...]
    errors_by_call_id: Mapping[str, str]

    @property
    def requires_repair(self) -> bool:
        return bool(self.errors_by_call_id)


def validate_tool_call_batch(
    tool_calls: Sequence[ToolCall],
    *,
    normalize: Callable[[ToolCall], ToolCall],
    validate: Callable[[ToolCall], None],
    finish_reason: str | None = None,
) -> ToolBatchValidationOutcome:
    """Check response integrity, then validate the batch before execution."""

    if str(finish_reason or "").strip().lower() in {
        "length",
        "max_tokens",
        "max_output_tokens",
        "token_limit",
    }:
        # Parseable arguments can still omit intended content when generation
        # was cut short. Reject even individually valid calls in this batch.
        return ToolBatchValidationOutcome(
            tool_calls=tuple(tool_calls),
            errors_by_call_id={
                call.id: (
                    "Model output was truncated at its token limit. No call "
                    "in this batch was executed because arguments may be "
                    "incomplete. Re-issue the complete tool-call batch."
                )
                for call in tool_calls
            },
        )

    normalized = tuple(normalize(tool_call) for tool_call in tool_calls)
    errors: dict[str, str] = {}
    for tool_call in normalized:
        try:
            validate(tool_call)
        except Exception as exc:  # validation is deliberately fail-closed
            errors[tool_call.id] = str(exc)
    return ToolBatchValidationOutcome(
        tool_calls=normalized,
        errors_by_call_id=errors,
    )


@dataclass(slots=True)
class ToolArgumentRepairBudget:
    """Bounded, explicit repair budget shared by the turn orchestrator."""

    maximum_attempts: int = 2
    attempts: int = 0

    def record_failed_batch(self) -> int:
        self.attempts += 1
        return self.attempts

    @property
    def exhausted(self) -> bool:
        return self.attempts > self.maximum_attempts


@dataclass(frozen=True, slots=True)
class ToolTransactionOutcome:
    """One immutable projection from a tool transaction."""

    phase: ToolTransactionPhase
    event: RuntimeEvent
    result: ToolResult
    terminal: bool


class ToolTransactionRuntimePort(Protocol):
    """Narrow execution boundary supplied by the authoritative Run owner."""

    @property
    def progressive_tools_enabled(self) -> bool: ...

    @property
    def equipped_tool_groups(self) -> set[str]: ...

    @equipped_tool_groups.setter
    def equipped_tool_groups(self, value: set[str]) -> None: ...

    @property
    def tool_registry(self) -> LantaiToolRegistry: ...

    @property
    def approval_service(self) -> Any | None: ...

    @property
    def artifact_effect_journal(
        self,
    ) -> RuntimeArtifactEffectJournal | None: ...

    @property
    def tool_timeout_seconds(self) -> float | None: ...

    @property
    def runtime_heartbeat_interval_seconds(self) -> float: ...

    def protected_write_denial_reason(self, capability: str) -> str | None: ...

    async def find_mcp_tool(
        self,
        name: str,
    ) -> tuple[ToolSpec, McpClientSpec] | None: ...

    def tool_completed_event(
        self,
        result: ToolResult,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        client_turn_id: str,
        seq: int,
    ) -> RuntimeEvent: ...

    async def evaluate_tool_policy(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        *,
        policy_context: ToolPolicyContext,
        budget: _DeadlineBudget,
        policy_observation_invocation: Any | None = None,
    ) -> ToolPolicyOutcome: ...

    async def request_tool_approval(
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
    ) -> dict[str, Any]: ...

    async def wait_for_tool_approval(
        self,
        approval: dict[str, Any],
        *,
        runtime_input: RuntimeInput,
        budget: _DeadlineBudget,
    ) -> ApprovalDecision: ...

    def approval_resolved_event(
        self,
        tool_call: ToolCall,
        approval: dict[str, Any],
        decision: ApprovalDecision,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        seq: int,
    ) -> RuntimeEvent: ...

    def record_tool_effect_intent(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        outcome: ToolPolicyOutcome,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        approval_id: str | None = None,
    ) -> RuntimeEffectRecord | None: ...

    def record_tool_effect_started(
        self,
        effect: RuntimeEffectRecord | None,
        *,
        recovery: RuntimeEffectRecoveryCapability,
    ) -> RuntimeEffectRecord | None: ...

    def record_tool_effect_settled(
        self,
        effect: RuntimeEffectRecord | None,
        result: ToolResult,
        *,
        recovery: RuntimeEffectRecoveryCapability,
    ) -> ToolResult: ...

    def confirm_tool_effect_receipt(self, effect: RuntimeEffectRecord | None, result: ToolResult) -> None: ...

    async def await_with_turn_budget(
        self,
        awaitable: Awaitable[Any],
        *,
        budget: _DeadlineBudget,
        timeout_seconds: float | None = None,
    ) -> Any: ...

    async def execute_callable(
        self,
        executor: Callable[..., Any],
        arguments: dict[str, Any],
    ) -> Any: ...

    async def execute_mcp_tool(
        self,
        spec: McpClientSpec,
        tool_call: ToolCall,
    ) -> Any: ...


class ToolTransactionRuntimePortMixin:
    """Forward the public transaction port to one runtime owner's internals."""

    async def find_mcp_tool(
        self,
        name: str,
    ) -> tuple[ToolSpec, McpClientSpec] | None:
        return await self._find_mcp_tool(name)

    def tool_completed_event(
        self,
        result: ToolResult,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        client_turn_id: str,
        seq: int,
    ) -> RuntimeEvent:
        return self._tool_completed_event(
            result,
            runtime_input=runtime_input,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            seq=seq,
        )

    async def evaluate_tool_policy(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        *,
        policy_context: ToolPolicyContext,
        budget: _DeadlineBudget,
        policy_observation_invocation: Any | None = None,
    ) -> ToolPolicyOutcome:
        return await self._evaluate_policy(
            tool_call,
            spec,
            policy_context=policy_context,
            budget=budget,
            policy_observation_invocation=policy_observation_invocation,
        )

    async def request_tool_approval(
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
        return await self._request_approval(
            tool_call,
            spec,
            outcome,
            runtime_input=runtime_input,
            turn_id=turn_id,
            seq_start=seq_start,
            policy_context=policy_context,
            budget=budget,
        )

    async def wait_for_tool_approval(
        self,
        approval: dict[str, Any],
        *,
        runtime_input: RuntimeInput,
        budget: _DeadlineBudget,
    ) -> ApprovalDecision:
        return await self._wait_for_approval_decision(
            approval,
            runtime_input=runtime_input,
            budget=budget,
        )

    def approval_resolved_event(
        self,
        tool_call: ToolCall,
        approval: dict[str, Any],
        decision: ApprovalDecision,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        seq: int,
    ) -> RuntimeEvent:
        return self._approval_resolved_event(
            tool_call,
            approval,
            decision,
            runtime_input=runtime_input,
            turn_id=turn_id,
            seq=seq,
        )

    def record_tool_effect_intent(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        outcome: ToolPolicyOutcome,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        approval_id: str | None = None,
    ) -> RuntimeEffectRecord | None:
        return self._record_tool_effect_intent(
            tool_call,
            spec,
            outcome,
            runtime_input=runtime_input,
            turn_id=turn_id,
            approval_id=approval_id,
        )

    def record_tool_effect_started(
        self,
        effect: RuntimeEffectRecord | None,
        *,
        recovery: RuntimeEffectRecoveryCapability,
    ) -> RuntimeEffectRecord | None:
        return self._record_tool_effect_started(effect, recovery=recovery)

    def record_tool_effect_settled(
        self,
        effect: RuntimeEffectRecord | None,
        result: ToolResult,
        *,
        recovery: RuntimeEffectRecoveryCapability,
    ) -> ToolResult:
        return self._record_tool_effect_settled(
            effect,
            result,
            recovery=recovery,
        )

    def confirm_tool_effect_receipt(self, effect: RuntimeEffectRecord | None, result: ToolResult) -> None:
        self._queue_tool_effect_receipt(effect, result)

    async def await_with_turn_budget(
        self,
        awaitable: Awaitable[Any],
        *,
        budget: _DeadlineBudget,
        timeout_seconds: float | None = None,
    ) -> Any:
        return await self._await_with_turn_budget(
            awaitable,
            budget=budget,
            timeout_seconds=timeout_seconds,
        )

    async def execute_callable(
        self,
        executor: Callable[..., Any],
        arguments: dict[str, Any],
    ) -> Any:
        return await self._execute_callable(executor, arguments)

    async def execute_mcp_tool(
        self,
        spec: McpClientSpec,
        tool_call: ToolCall,
    ) -> Any:
        return await self._execute_mcp_tool(spec, tool_call)


class ToolTransaction:
    """Own one tool call from receipt through its terminal projection."""

    def __init__(self, runtime: ToolTransactionRuntimePort) -> None:
        self._runtime = runtime

    @staticmethod
    def _phase_for_event(event: RuntimeEvent) -> ToolTransactionPhase:
        if event.type == "approval.requested" or event.type == "approval.resolved":
            return "authorize"
        if event.type == "tool.started" or event.type == "tool.call.delta":
            return "execute"
        if event.type == "tool.call.completed":
            return "project"
        return "validate"

    async def run(self, *args: Any, **kwargs: Any) -> AsyncIterator[ToolTransactionOutcome]:
        stages = self._execute_stages(*args, **kwargs)
        try:
            async for event, result in stages:
                yield ToolTransactionOutcome(
                    phase=self._phase_for_event(event),
                    event=event,
                    result=result,
                    terminal=event.type == "tool.call.completed",
                )
        finally:
            await close_owned_async_iterator(
                stages,
                task_name="tool-transaction-stage-close",
            )

    async def _execute_stages(
        self,
        tool_call: ToolCall,
        *,
        runtime_input: RuntimeInput,
        turn_id: str,
        client_turn_id: str = "",
        seq_start: int,
        policy_context: ToolPolicyContext,
        policy_observation_invocation: Any | None = None,
        budget: _DeadlineBudget,
        expected_file_extensions: frozenset[str] = frozenset(),
        inbox_after_seq: int = 0,
        collaboration_preparing_already_emitted: bool = False,
    ) -> AsyncIterator[tuple[RuntimeEvent, ToolResult]]:
        runtime = self._runtime
        request = runtime_input.request
        seq = seq_start
        payload = _tool_payload_with_agent_presentation_context(
            _tool_call_public_payload(tool_call),
            runtime_input,
        )
        if client_turn_id:
            payload = {**payload, "client_turn_id": client_turn_id}
        summary = f"tool call: {tool_call.name}"
        seq += 1
        yield (
            RuntimeEvent(
                id=str(uuid4()),
                seq=seq,
                type="tool.call.started",
                status="running",
                source="tool",
                session_id=request.session_id,
                turn_id=turn_id,
                item_id=tool_call.id,
                summary=summary,
                payload=payload,
                presentation=_presentation(tool_call.name, payload=payload, summary=summary),
            ),
            ToolResult(call_id=tool_call.id, name=tool_call.name),
        )

        if (
            tool_call.name == _SUBAGENT_BATCH_TOOL
            and not collaboration_preparing_already_emitted
        ):
            # Standard-mode requests can enter hosted collaboration directly
            # through the primary model, without a pre-resolved collaboration
            # work method. Publish the same visible receipt at the
            # authoritative tool boundary so the UI never stays in a generic
            # waiting state while the room is already being assembled.
            seq += 1
            yield (
                RuntimeEvent(
                    id=str(uuid4()),
                    seq=seq,
                    type="runtime.warning",
                    status="running",
                    source="system",
                    session_id=request.session_id,
                    turn_id=turn_id,
                    item_id=tool_call.id,
                    summary="沈行知正在组建协作室。",
                    payload={
                        "kind": "collaboration_preparing",
                        "trace_title": "正在组建协作室",
                        "trace_detail": "沈行知正在选择成员与分工",
                    },
                ),
                ToolResult(call_id=tool_call.id, name=tool_call.name),
            )

        if runtime.progressive_tools_enabled and tool_call.name == _PROGRESSIVE_TOOL_NAME:
            try:
                transition = transition_progressive_tool_groups(
                    runtime.equipped_tool_groups,
                    tool_call.arguments,
                    available_groups=frozenset(_PROGRESSIVE_TOOL_GROUPS),
                )
            except ValueError as exc:
                result = ToolResult(
                    call_id=tool_call.id,
                    name=tool_call.name,
                    ok=False,
                    error=str(exc),
                )
            else:
                runtime.equipped_tool_groups = set(transition.active_groups)
                equipped = sorted(runtime.equipped_tool_groups)
                text = (
                    "Equipped tool groups: " + ", ".join(equipped)
                    if equipped
                    else "All progressive tool groups are deactivated."
                )
                result = ToolResult(
                    call_id=tool_call.id,
                    name=tool_call.name,
                    content=[{"type": "text", "text": text}],
                    metadata={
                        "equipped_tool_groups": equipped,
                        "newly_equipped_tool_groups": sorted(
                            transition.newly_activated_groups,
                        ),
                        "progress_made": transition.changed,
                    },
                )
            seq += 1
            yield runtime.tool_completed_event(
                result,
                runtime_input=runtime_input,
                turn_id=turn_id,
                client_turn_id=client_turn_id,
                seq=seq,
            ), result
            return

        spec_executor = runtime.tool_registry.get_tool(tool_call.name)
        mcp_spec = None
        if spec_executor is None:
            mcp_spec = await runtime.find_mcp_tool(tool_call.name)
            if mcp_spec is None:
                result = ToolResult(
                    call_id=tool_call.id,
                    name=tool_call.name,
                    ok=False,
                    error=f"Tool not found: {tool_call.name}",
                )
                seq += 1
                yield runtime.tool_completed_event(
                    result,
                    runtime_input=runtime_input,
                    turn_id=turn_id,
                    client_turn_id=client_turn_id,
                    seq=seq,
                ), result
                return
            spec = mcp_spec[0]
        else:
            spec = spec_executor[0]

        if _pending_control_input_before_effect(
            runtime_input=runtime_input,
            turn_id=turn_id,
            after_seq=inbox_after_seq,
        ):
            result = _steered_before_effect_result(tool_call)
            seq += 1
            yield runtime.tool_completed_event(
                result,
                runtime_input=runtime_input,
                turn_id=turn_id,
                client_turn_id=client_turn_id,
                seq=seq,
            ), result
            return

        async def wait_for_approval_or_control(
            approval: dict[str, Any],
        ) -> ApprovalDecision:
            approval_wait = asyncio.create_task(
                runtime.wait_for_tool_approval(
                    approval,
                    runtime_input=runtime_input,
                    budget=budget,
                ),
                name=f"tool-approval-wait-{tool_call.id}",
            )

            async def watch_for_control() -> None:
                while not _pending_control_input_before_effect(
                    runtime_input=runtime_input,
                    turn_id=turn_id,
                    after_seq=inbox_after_seq,
                ):
                    await asyncio.sleep(0.05)

            watcher = asyncio.create_task(
                watch_for_control(),
                name=f"tool-approval-control-watch-{tool_call.id}",
            )
            try:
                await asyncio.wait(
                    {approval_wait, watcher},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # A real approval result wins a same-loop scheduling tie.
                if approval_wait.done():
                    return await approval_wait
                await watcher
                if approval_wait.done():
                    return await approval_wait
                approval_wait.cancel()
                await asyncio.gather(approval_wait, return_exceptions=True)
                raise _ToolSteered(
                    "Pending tool approval was superseded by newer user input."
                )
            finally:
                pending = [
                    task
                    for task in (approval_wait, watcher)
                    if not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        if (
            tool_call.name == FILE_DELIVERY_TOOL_NAME
            and expected_file_extensions
            and not file_path_matches_extensions(
                str(tool_call.arguments.get("file_path") or ""),
                expected_file_extensions,
            )
        ):
            attempted_path = str(
                tool_call.arguments.get("file_path") or "未命名文件",
            )
            expected_label = expected_file_type_label(
                expected_file_extensions,
            )
            result = ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                ok=False,
                error=(
                    f"用户要求交付 {expected_label} 文件，但当前尝试发送的是 "
                    f"{attempted_path}。请先生成并验证正确的 "
                    f"{expected_label} 文件，再发送。"
                ),
                metadata={"file_delivery_type_mismatch": True},
            )
            seq += 1
            yield runtime.tool_completed_event(
                result,
                runtime_input=runtime_input,
                turn_id=turn_id,
                client_turn_id=client_turn_id,
                seq=seq,
            ), result
            return

        outcome = await runtime.evaluate_tool_policy(
            tool_call,
            spec,
            policy_context=policy_context,
            budget=budget,
            policy_observation_invocation=policy_observation_invocation,
        )
        if outcome.action == "deny":
            policy_source = str(outcome.metadata.get("source") or "runtime_policy")
            error_code = (
                "approval_ui_unavailable"
                if policy_source == "approval_unavailable"
                else "policy_denied"
            )
            result = ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                ok=False,
                error=outcome.reason or "Tool call denied by policy.",
                metadata={
                    "policy": outcome.policy_payload,
                    "guard": outcome.guard_payload,
                    "error_code": error_code,
                    "policy_source": policy_source,
                    **_collaboration_failure_metadata(
                        tool_call,
                        error_code=error_code,
                    ),
                },
            )
            seq += 1
            yield runtime.tool_completed_event(
                result,
                runtime_input=runtime_input,
                turn_id=turn_id,
                client_turn_id=client_turn_id,
                seq=seq,
            ), result
            return

        if outcome.action == "needs_approval":
            if runtime.approval_service is None:
                result = ToolResult(
                    call_id=tool_call.id,
                    name=tool_call.name,
                    ok=False,
                    error=(
                        "Tool approval UI is unavailable. The user did not deny "
                        "this action."
                    ),
                    metadata={
                        "policy": outcome.policy_payload,
                        "guard": outcome.guard_payload,
                        "error_code": "approval_ui_unavailable",
                        "policy_source": "approval_unavailable",
                        **_collaboration_failure_metadata(
                            tool_call,
                            error_code="approval_ui_unavailable",
                        ),
                    },
                )
                seq += 1
                yield runtime.tool_completed_event(
                    result,
                    runtime_input=runtime_input,
                    turn_id=turn_id,
                    client_turn_id=client_turn_id,
                    seq=seq,
                ), result
                return
            approval = await runtime.request_tool_approval(
                tool_call,
                spec,
                outcome,
                runtime_input=runtime_input,
                turn_id=turn_id,
                seq_start=seq,
                policy_context=policy_context,
                budget=budget,
            )
            if approval.get("auto_approved"):
                decision = ApprovalDecision.APPROVED
            else:
                requested_event = approval["requested_event"]
                seq = requested_event.seq
                yield requested_event, ToolResult(
                    call_id=tool_call.id,
                    name=tool_call.name,
                )

                try:
                    decision = await wait_for_approval_or_control(approval)
                except _ToolSteered:
                    decision = ApprovalDecision.DENIED
                    seq += 1
                    resolved_event = runtime.approval_resolved_event(
                        tool_call,
                        approval,
                        decision,
                        runtime_input=runtime_input,
                        turn_id=turn_id,
                        seq=seq,
                    )
                    resolved_event = resolved_event.model_copy(
                        update={
                            "summary": (
                                "Approval superseded by newer user input."
                            ),
                            "payload": {
                                **resolved_event.payload,
                                "resolution_reason": (
                                    "superseded_by_control_input"
                                ),
                                "superseded_by_control_input": True,
                            },
                        },
                    )
                    yield resolved_event, ToolResult(
                        call_id=tool_call.id,
                        name=tool_call.name,
                    )
                    result = _steered_before_effect_result(tool_call)
                    seq += 1
                    yield runtime.tool_completed_event(
                        result,
                        runtime_input=runtime_input,
                        turn_id=turn_id,
                        client_turn_id=client_turn_id,
                        seq=seq,
                    ), result
                    return
                seq += 1
                resolved_event = runtime.approval_resolved_event(
                    tool_call,
                    approval,
                    decision,
                    runtime_input=runtime_input,
                    turn_id=turn_id,
                    seq=seq,
                )
                yield resolved_event, ToolResult(
                    call_id=tool_call.id,
                    name=tool_call.name,
                )

            if decision != ApprovalDecision.APPROVED:
                result = ToolResult(
                    call_id=tool_call.id,
                    name=tool_call.name,
                    ok=False,
                    error=(
                        "Tool approval timed out."
                        if decision == ApprovalDecision.TIMEOUT
                        else "Tool approval denied."
                    ),
                    metadata={
                        "approval": resolved_event.payload,
                        "error_code": (
                            "approval_timeout"
                            if decision == ApprovalDecision.TIMEOUT
                            else "approval_denied"
                        ),
                        "policy_source": "approval_service",
                        **_collaboration_failure_metadata(
                            tool_call,
                            error_code=(
                                "approval_timeout"
                                if decision == ApprovalDecision.TIMEOUT
                                else "approval_denied"
                            ),
                        ),
                    },
                )
                seq += 1
                yield runtime.tool_completed_event(
                    result,
                    runtime_input=runtime_input,
                    turn_id=turn_id,
                    client_turn_id=client_turn_id,
                    seq=seq,
                ), result
                return

        if _pending_control_input_before_effect(
            runtime_input=runtime_input,
            turn_id=turn_id,
            after_seq=inbox_after_seq,
        ):
            result = _steered_before_effect_result(tool_call)
            seq += 1
            yield runtime.tool_completed_event(
                result,
                runtime_input=runtime_input,
                turn_id=turn_id,
                client_turn_id=client_turn_id,
                seq=seq,
            ), result
            return

        approval_id = None
        if outcome.action == "needs_approval":
            approval_id = str(approval.get("request_id") or "") or None
        if spec.effect_recovery.external_side_effect:
            admission = getattr(runtime, "protected_write_denial_reason", None)
            beta_denial_reason = (
                admission(f"tool.external_write:{tool_call.name}")
                if callable(admission)
                else None
            )
            if beta_denial_reason is not None:
                result = ToolResult(
                    call_id=tool_call.id,
                    name=tool_call.name,
                    ok=False,
                    error=beta_denial_reason,
                    metadata={
                        "error_code": "beta_access_denied",
                        "policy_source": "beta_access",
                        "invocation_status": "not_started",
                        "side_effect_outcome": "known_not_applied",
                        **_collaboration_failure_metadata(
                            tool_call,
                            error_code="beta_access_denied",
                        ),
                    },
                )
                seq += 1
                yield runtime.tool_completed_event(
                    result,
                    runtime_input=runtime_input,
                    turn_id=turn_id,
                    client_turn_id=client_turn_id,
                    seq=seq,
                ), result
                return
        try:
            effect_record = runtime.record_tool_effect_intent(
                tool_call,
                spec,
                outcome,
                runtime_input=runtime_input,
                turn_id=turn_id,
                approval_id=approval_id,
            )
            artifact_effect_intents = ()
            if (
                runtime.artifact_effect_journal is not None
                and effect_record is not None
            ):
                artifact_effect_intents = build_artifact_effect_intents(
                    source_effect=effect_record,
                    tool_call=tool_call,
                    tool_spec=spec,
                    workspace_dir=runtime_input.request.workspace_dir or "",
                )
                for artifact_intent in artifact_effect_intents:
                    runtime.artifact_effect_journal.append(artifact_intent)
        except Exception as exc:  # pylint: disable=broad-except
            journal_corrupt = isinstance(exc, RuntimeEffectJournalCorruptionError)
            blocks_retry = isinstance(
                exc,
                (RuntimeEffectUnresolvedError, RuntimeEffectJournalCorruptionError),
            )
            result = ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                ok=False,
                error=(str(exc) if isinstance(exc, RuntimeEffectUnresolvedError)
                       else "Tool effect journal failed before execution."),
                metadata={
                    "effect_journal_error": type(exc).__name__,
                    **_collaboration_failure_metadata(
                        tool_call,
                        error_code="effect_journal_failed",
                    ),
                    "invocation_status": "not_started",
                    "side_effect_outcome": (
                        "committed" if isinstance(exc, RuntimeEffectUnresolvedError) and exc.effect.status == "committed"
                        else "unknown" if isinstance(exc, RuntimeEffectUnresolvedError)
                        else "known_not_applied"
                    ),
                    "automatic_retry_allowed": not blocks_retry,
                    "requires_human": blocks_retry,
                    # No invocation here does not settle a corrupt prior ledger.
                    **({
                        "effect_settlement_status": "unconfirmed",
                        "error_code": "effect_journal_corrupt",
                    } if journal_corrupt else {}),
                    **({"effect_id": exc.effect.effect_id, "attempt_id": exc.effect.attempt_id, "effect_revision": exc.effect.revision, "effect_status": exc.effect.status} if isinstance(exc, RuntimeEffectUnresolvedError) else {}),
                },
            )
            seq += 1
            yield runtime.tool_completed_event(
                result,
                runtime_input=runtime_input,
                turn_id=turn_id,
                client_turn_id=client_turn_id,
                seq=seq,
            ), result
            return

        heartbeat_task = None
        latest_collaboration: dict[str, Any] | None = None
        invocation_started = False
        invocation_completed = False
        invocation_result_ok = False

        def settle_result(result: ToolResult) -> ToolResult:
            result = _scope_collaboration_result(
                result,
                runtime_input=runtime_input,
                turn_id=turn_id,
            )
            invocation_status = (
                "completed"
                if invocation_completed
                else "interrupted"
                if invocation_started
                else "not_started"
            )
            if not invocation_started:
                side_effect_outcome = "known_not_applied"
            elif spec.effect_recovery.external_side_effect:
                # Plugin/MCP output is untrusted: a tool may claim rejection after
                # its write already happened. A narrowly opted-in Runtime-owned
                # gate may, however, authoritatively report that it rejected the
                # operation before applying any effect.
                trusted_rejection = bool(
                    invocation_completed
                    and not result.ok
                    and spec.metadata.get("runtime_owned") is True
                    and spec.metadata.get("trusted_effect_settlement") is True
                    and result.metadata.get("side_effect_outcome")
                    == "known_not_applied"
                )
                side_effect_outcome = (
                    "committed"
                    if invocation_result_ok
                    else "known_not_applied"
                    if trusted_rejection
                    else "unknown"
                )
            else:
                side_effect_outcome = "not_applicable" if result.ok else "known_not_applied"
            result = result.model_copy(
                update={
                    "metadata": {
                        **result.metadata,
                        "invocation_status": invocation_status,
                        "side_effect_outcome": side_effect_outcome,
                    },
                },
            )

            try:
                result = runtime.record_tool_effect_settled(
                    effect_record,
                    result,
                    recovery=spec.effect_recovery,
                )
            except Exception as exc:  # pylint: disable=broad-except
                result = _unconfirmed_receipt_result(
                    result, effect_record, spec.effect_recovery, exc,
                    "Tool effect journal settlement failed after execution.",
                )

            try:
                if (
                    runtime.artifact_effect_journal is not None
                    and artifact_effect_intents
                ):
                    settled_artifacts = settle_artifact_effects(
                        artifact_effect_intents,
                        result=result,
                        workspace_dir=runtime_input.request.workspace_dir or "",
                    )
                    for artifact_record in settled_artifacts:
                        runtime.artifact_effect_journal.append(artifact_record)
                    result = result.model_copy(
                        update={
                            "metadata": {
                                **result.metadata,
                                "artifact_effects": [
                                    artifact_effect_public_payload(record)
                                    for record in settled_artifacts
                                ],
                            },
                        },
                    )
            except Exception as exc:  # pylint: disable=broad-except
                result = _unconfirmed_receipt_result(
                    result, effect_record, spec.effect_recovery, exc,
                    "Artifact effect journal settlement failed after execution.",
                    artifact=True, settled_effect=True,
                )

            if result.metadata.get("effect_settlement_status") != "unconfirmed":
                try:
                    runtime.confirm_tool_effect_receipt(effect_record, result)
                except Exception as exc:  # pylint: disable=broad-except
                    result = _unconfirmed_receipt_result(
                        result, effect_record, spec.effect_recovery, exc,
                        "Tool receipt confirmation failed after execution.",
                        settled_effect=True,
                    )
            return result

        async def invoke_with_effect(
            operation: Callable[[], Awaitable[Any]],
        ) -> Any:
            nonlocal effect_record, invocation_started
            if _pending_control_input_before_effect(
                runtime_input=runtime_input,
                turn_id=turn_id,
                after_seq=inbox_after_seq,
            ):
                raise _ToolSteered(
                    "Tool execution superseded before its effect started."
                )
            effect_record = runtime.record_tool_effect_started(
                effect_record, recovery=spec.effect_recovery,
            )
            invocation_started = True
            state_workspace_dir = str(
                _runtime_metadata(runtime_input).get("state_workspace_dir")
                or request.workspace_dir
                or getattr(getattr(runtime, "_effect_journal", None), "workspace_dir", "")
                or ""
            ).strip()
            if effect_record is None or not state_workspace_dir:
                return await operation()
            with bind_runtime_effect_context(
                RuntimeEffectContext(
                    run_id=effect_record.run_id,
                    attempt_id=effect_record.attempt_id,
                    effect_id=effect_record.effect_id,
                    state_workspace_dir=state_workspace_dir,
                    idempotency_key=effect_record.idempotency_key,
                )
            ):
                return await operation()

        async def await_interruptible_tool(
            awaitable: Awaitable[Any],
            *,
            timeout_seconds: float | None,
        ) -> Any:
            state_workspace_dir = str(
                _runtime_metadata(runtime_input).get("state_workspace_dir")
                or request.workspace_dir
                or ""
            ).strip()
            is_waitable_collaboration = tool_call.name in {
                _SUBAGENT_FOREGROUND_TOOL,
                _SUBAGENT_BATCH_TOOL,
                _SUBAGENT_CHECK_TOOL,
            }
            local_executor = spec_executor[1] if spec_executor is not None else None
            is_cancellable_read_only = spec.read_only and (
                mcp_spec is not None
                or (
                    local_executor is not None
                    and (
                        inspect.iscoroutinefunction(local_executor)
                        or inspect.isasyncgenfunction(local_executor)
                    )
                )
            )
            if (
                not is_waitable_collaboration
                and not is_cancellable_read_only
            ) or not state_workspace_dir:
                return await runtime.await_with_turn_budget(
                    awaitable,
                    budget=budget,
                    timeout_seconds=timeout_seconds,
                )

            try:
                # Admission must happen before scheduling the operation:
                # an exhausted budget must never start an external action.
                admitted_timeout = budget.timeout(timeout_seconds)
                if admitted_timeout is not None and admitted_timeout <= 0:
                    raise asyncio.TimeoutError()
            except Exception:
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                raise

            async def run_admitted_operation() -> Any:
                # Ready callbacks may exhaust the turn deadline between
                # admission and this task's first step. Recheck immediately
                # before entering the tool, without another scheduling gap.
                budget.timeout(admitted_timeout)
                return await awaitable

            task_prefix = (
                "collaboration"
                if is_waitable_collaboration
                else "interruptible-tool"
            )
            operation = asyncio.create_task(
                run_admitted_operation(),
                name=f"{task_prefix}-operation-{tool_call.id}",
            )
            protected_operation = asyncio.shield(operation)
            execution = asyncio.create_task(
                runtime.await_with_turn_budget(
                    # Own the operation independently of wait_for's timeout
                    # wrapper (including its Python 3.10 cancellation path).
                    # Only the transaction below cancels/drains the operation.
                    protected_operation,
                    budget=budget,
                    timeout_seconds=admitted_timeout,
                ),
                name=(
                    f"waitable-collaboration-{tool_call.id}"
                    if is_waitable_collaboration
                    else f"interruptible-tool-wait-{tool_call.id}"
                ),
            )

            async def watch_for_steer() -> None:
                while True:
                    if _pending_control_input_before_effect(
                        runtime_input=runtime_input,
                        turn_id=turn_id,
                        after_seq=inbox_after_seq,
                    ):
                        return
                    await asyncio.sleep(0.1)

            watcher = asyncio.create_task(
                watch_for_steer(),
                name=(
                    f"collaboration-steer-watch-{tool_call.id}"
                    if is_waitable_collaboration
                    else f"interruptible-tool-steer-watch-{tool_call.id}"
                ),
            )
            try:
                done, _pending = await asyncio.wait(
                    {execution, watcher},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if execution in done:
                    return await execution
                # Observe watcher failures instead of mislabelling them as a
                # user steer. Completion wins if both tasks finish together.
                await watcher
            finally:
                try:
                    await _cancel_and_join_collaboration(
                        # Begin the owned operation's cleanup first. Wrappers
                        # and the steer watcher cannot settle the transaction
                        # until that cleanup has actually completed.
                        (operation, execution, watcher, protected_operation),
                    )
                finally:
                    # A rejected first-step admission never awaited this
                    # object; close it only after its owner has been joined.
                    close = getattr(awaitable, "close", None)
                    if callable(close):
                        close()
            if not operation.cancelled():
                # A cleanup failure must remain a tool failure, not a
                # successful steer. Late values after cancellation are ignored.
                operation.result()
            raise _ToolSteered(
                "Tool wait superseded by new user input."
            )
        try:
            seq += 1
            yield (
                RuntimeEvent(
                    id=str(uuid4()),
                    seq=seq,
                    type="tool.started",
                    status="running",
                    source="tool",
                    session_id=request.session_id,
                    turn_id=turn_id,
                    item_id=tool_call.id,
                    summary=f"running {tool_call.name}",
                    payload=payload,
                    presentation=_presentation(tool_call.name, payload=payload),
                ),
                ToolResult(call_id=tool_call.id, name=tool_call.name),
            )

            heartbeat_task = _start_runtime_run_heartbeat(
                runtime_input=runtime_input,
                turn_id=turn_id,
                task_name=f"native-tool-heartbeat-{tool_call.id}",
                interval_seconds=runtime.runtime_heartbeat_interval_seconds,
            )

            tool_timeout = _timeout_from_metadata(
                _runtime_metadata(runtime_input),
                _TOOL_TIMEOUT_KEYS,
                runtime.tool_timeout_seconds,
            )
            if spec.metadata.get("progressive_tool") == "collaboration":
                progressive_timeout = _coerce_non_negative_float(
                    spec.metadata.get("progressive_timeout_seconds"),
                )
                if progressive_timeout is not None:
                    tool_timeout = max(tool_timeout or 0.0, progressive_timeout)
            if mcp_spec is not None:
                timeout = budget.timeout(mcp_spec[1].execution_timeout, tool_timeout)
                raw_result = await await_interruptible_tool(
                    invoke_with_effect(
                        lambda: runtime.execute_mcp_tool(mcp_spec[1], tool_call),
                    ),
                    timeout_seconds=timeout,
                )
            elif spec.metadata.get("progressive_tool") == "collaboration":
                async def start_progressive_tool() -> Any:
                    progressive = spec_executor[1](**tool_call.arguments)
                    if isinstance(progressive, Awaitable):
                        progressive = await progressive
                    return progressive

                progressive_result = await runtime.await_with_turn_budget(
                    invoke_with_effect(start_progressive_tool),
                    budget=budget,
                    timeout_seconds=tool_timeout,
                )
                if not hasattr(progressive_result, "__aiter__"):
                    raise TypeError(
                        "Progressive collaboration tool must return an async iterator",
                    )

                raw_result = None
                progressive_deadline = (
                    asyncio.get_running_loop().time() + tool_timeout
                    if tool_timeout is not None
                    else None
                )
                iterator = progressive_result.__aiter__()
                try:
                    while True:
                        remaining_timeout = None
                        if progressive_deadline is not None:
                            remaining_timeout = max(
                                0.0,
                                progressive_deadline
                                - asyncio.get_running_loop().time(),
                            )
                        try:
                            update = await await_interruptible_tool(
                                iterator.__anext__(),
                                timeout_seconds=remaining_timeout,
                            )
                        except StopAsyncIteration:
                            break

                        update_metadata = getattr(update, "metadata", None)
                        collaboration = (
                            update_metadata.get("collaboration")
                            if isinstance(update_metadata, dict)
                            else None
                        )
                        if isinstance(collaboration, dict):
                            collaboration = _collaboration_with_execution_scope(
                                collaboration,
                                runtime_input=runtime_input,
                                turn_id=turn_id,
                            )
                            latest_collaboration = dict(collaboration)
                        is_progress = bool(
                            getattr(update, "stream", False)
                        ) and not bool(getattr(update, "is_last", True))
                        if is_progress:
                            if isinstance(collaboration, dict):
                                delta_payload = {
                                    "id": tool_call.id,
                                    "name": tool_call.name,
                                    "tool_name": tool_call.name,
                                    "collaboration": _json_safe(collaboration),
                                    **(
                                        {"client_turn_id": client_turn_id}
                                        if client_turn_id
                                        else {}
                                    ),
                                }
                                seq += 1
                                yield (
                                    RuntimeEvent(
                                        id=str(uuid4()),
                                        seq=seq,
                                        type="tool.call.delta",
                                        status="running",
                                        source="tool",
                                        session_id=request.session_id,
                                        turn_id=turn_id,
                                        item_id=tool_call.id,
                                        summary="collaboration progress",
                                        payload=delta_payload,
                                        presentation=_presentation(
                                            tool_call.name,
                                            payload=delta_payload,
                                        ),
                                    ),
                                    ToolResult(
                                        call_id=tool_call.id,
                                        name=tool_call.name,
                                    ),
                                )
                            continue
                        raw_result = update
                finally:
                    await close_owned_async_iterator(
                        iterator,
                        task_name=f"collaboration-close-{tool_call.id}",
                    )
                if raw_result is None:
                    raise RuntimeError(
                        "Progressive collaboration tool completed without a final result",
                    )
            else:
                raw_result = await await_interruptible_tool(
                    invoke_with_effect(
                        lambda: runtime.execute_callable(
                            spec_executor[1],
                            tool_call.arguments,
                        ),
                    ),
                    timeout_seconds=tool_timeout,
                )
            invocation_completed = True
            result = _normalise_tool_result(tool_call, raw_result)
            invocation_result_ok = result.ok
            result = result.model_copy(update={"metadata": {
                key: value for key, value in result.metadata.items()
                if key not in {"effect_settlement_status", "original_tool_result_ok",
                               "effect_journal_error", "artifact_effect_journal_error"}
            }})
            if (
                runtime.artifact_effect_journal is not None
                and effect_record is not None
                and result.ok
            ):
                try:
                    result_intents = artifact_effect_intents_from_result(
                        source_effect=effect_record, tool_call=tool_call,
                        tool_spec=spec, result=result,
                        workspace_dir=runtime_input.request.workspace_dir or "",
                    )
                    known_effect_ids = {record.artifact_effect_id for record in artifact_effect_intents}
                    for result_intent in result_intents:
                        if result_intent.artifact_effect_id in known_effect_ids:
                            continue
                        runtime.artifact_effect_journal.append(result_intent)
                        artifact_effect_intents += (result_intent,)
                        known_effect_ids.add(result_intent.artifact_effect_id)
                except Exception as exc:  # pylint: disable=broad-except
                    result = _unconfirmed_receipt_result(
                        result, effect_record, spec.effect_recovery, exc,
                        "Artifact receipt failed after execution.", artifact=True,
                    )
        except (asyncio.CancelledError, GeneratorExit, _TurnDeadlineExceeded) as exc:
            # The owned invocation has finished its real cleanup before this
            # boundary exits. Persist its uncertainty without yielding while
            # an async generator closes or replacing the caller's stop signal.
            interrupted_result = settle_result(ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                ok=False,
                error="Tool execution ended before its result was received.",
                metadata={
                    "error_type": type(exc).__name__,
                    "error_code": (
                        "turn_deadline_exceeded"
                        if isinstance(exc, _TurnDeadlineExceeded)
                        else "tool_interrupted"
                    ),
                },
            ))
            if interrupted_result.metadata.get("effect_settlement_status") == "unconfirmed":
                _LOGGER.error(
                    "Interrupted tool settlement could not be confirmed",
                    extra={
                        "tool_call_id": tool_call.id,
                        "effect_journal_error": interrupted_result.metadata.get("effect_journal_error"),
                        "artifact_effect_journal_error": interrupted_result.metadata.get("artifact_effect_journal_error"),
                    },
                )
            raise
        except Exception as exc:  # pylint: disable=broad-except
            if not invocation_started and isinstance(exc, RuntimeEffectUnresolvedError):
                # The atomic running claim can reject an intent admitted before
                # another invocation started. Preserve that invocation's receipt;
                # settling this unused intent must not make its write retryable.
                previous = exc.effect
                result = ToolResult(
                    call_id=tool_call.id, name=tool_call.name, ok=False,
                    error=str(exc),
                    metadata={
                        "invocation_status": "not_started",
                        "side_effect_outcome": "committed" if previous.status == "committed" else "unknown",
                        "automatic_retry_allowed": False, "requires_human": True,
                        "effect_id": previous.effect_id, "attempt_id": previous.attempt_id,
                        "effect_revision": previous.revision, "effect_status": previous.status,
                    },
                )
                seq += 1
                yield runtime.tool_completed_event(
                    result, runtime_input=runtime_input, turn_id=turn_id,
                    client_turn_id=client_turn_id, seq=seq,
                ), result
                return
            if invocation_started and not isinstance(
                exc,
                (_ToolSteered, asyncio.TimeoutError),
            ):
                invocation_completed = True
            error = (
                "Tool effect journal failed before execution."
                if not invocation_started and isinstance(exc, (RuntimeEffectJournalError, OSError))
                else _exception_message(exc, fallback="Tool call")
            )
            error_code = (
                (
                    "steered"
                    if invocation_started
                    else "steered_before_effect"
                )
                if isinstance(exc, _ToolSteered)
                else "tool_timeout"
                if isinstance(exc, asyncio.TimeoutError)
                else "validation_failed"
                if isinstance(exc, (TypeError, ValueError))
                else "tool_failed"
            )
            result = ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                ok=False,
                error=error,
                metadata={
                    "error_type": type(exc).__name__,
                    "error_code": error_code,
                    **_collaboration_failure_metadata(
                        tool_call,
                        latest=latest_collaboration,
                        error_code=error_code,
                    ),
                },
            )
        finally:
            await _cancel_runtime_run_heartbeat(heartbeat_task)

        result = settle_result(result)

        seq += 1
        yield runtime.tool_completed_event(
            result,
            runtime_input=runtime_input,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            seq=seq,
        ), result



__all__ = [
    "ToolArgumentRepairBudget",
    "ToolBatchValidationOutcome",
    "ToolTransaction",
    "ToolTransactionOutcome",
    "ToolTransactionPhase",
    "ToolTransactionRuntimePort",
    "validate_tool_call_batch",
]
