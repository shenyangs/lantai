# -*- coding: utf-8 -*-
"""Append-only chat experience event storage and preview helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from bisect import bisect_right
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from lantai.runtime.events import (
    RuntimeEvent as RuntimeContractEvent,
    RuntimeBudgetChangedEventPayload,
    RuntimeEventContractViolation,
    RuntimeNotificationEventPayload,
    RuntimeEventType,
    validate_runtime_event_sequence,
)
from lantai.runtime.turn_outcomes import normalize_turn_outcome_status
from lantai.runtime.effects import (
    RuntimeEffectJournal,
    RuntimeEffectRecoveryCapability,
    assess_runtime_effect_replay,
)
from lantai.runtime.runs import (
    TERMINAL_RUNTIME_RUN_STATUSES,
    RuntimeCheckpoint,
    RuntimeEffectRecord,
    RuntimeRunContract,
    RuntimeRunReducerAction,
    RuntimeRunTransitionError,
    RuntimeStepRef,
    reduce_runtime_run,
)
from lantai.runtime.loops import LoopBudgetChangeRecord
from lantai.runtime.sanitizer import sanitize_runtime_payload
from lantai.security.storage_keys import (
    append_text_no_follow,
    read_text_no_follow,
    write_text_atomic_no_follow,
)
from lantai.runtime.store import (
    RuntimeRunStore,
    RuntimeRunStoreConflictError,
    RuntimeRunStoreError,
)

from .failure_diagnosis import failure_diagnosis_for_event
from .presentation import tool_presentation_for_event
from .schemas import (
    ChatExperienceEvent,
    DebugTurnEventRecord,
    DebugTurnEventsResponse,
    RuntimeEvent,
    RuntimeEventsResponse,
    RuntimeRunEventsResponse,
    RuntimeRunSummary,
    RuntimeRunsResponse,
    RuntimeUsageSummary,
    ToolPresentation,
    TurnEventPreview,
    TurnEventPreviewEvent,
)
from .runtime_runs import (
    record_cron_runtime_run_settled,
    record_cron_runtime_run_started,
    record_chat_runtime_run_settled,
    record_chat_runtime_run_started,
)

logger = logging.getLogger(__name__)

INTERNAL_RUNTIME_DIR_NAME = ".lantai"
EXPERIENCE_DIR_NAME = "experience"
RUNTIME_DIR_NAME = "runtime"
RUNTIME_STORE_VERSION_DIR_NAME = "v1"
RUNTIME_EVENTS_DIR_NAME = "events"
EVENTS_FILE_NAME = "events.jsonl"
EVENTS_INDEX_FILE_NAME = "events.index.json"
EVENTS_SEGMENTS_FILE_NAME = "events.segments.json"
EVENT_LEDGER_INDEX_SCHEMA_VERSION = 1
EVENT_LEDGER_INDEX_WRITER_VERSION = "lantai-experience-events-index/2"
EVENT_LEDGER_SEGMENTS_SCHEMA_VERSION = 1
EVENT_LEDGER_SEGMENTS_WRITER_VERSION = "lantai-experience-events-segments/1"
EVENT_LEDGER_SEGMENT_MAX_BYTES = 256 * 1024
EVENT_INDEX_CACHE_MAX_ENTRIES = 64
MAX_PREVIEW_EVENTS = 6
MAX_TEXT_PREVIEW = 120
CONTRACT_RUNTIME_EVENT_TYPES = set(get_args(RuntimeEventType))
RUNTIME_SEQUENCE_TERMINAL_TYPES = {
    "turn.completed",
    "turn.failed",
    "turn.interrupted",
    "turn.stalled",
}
DEFERRED_INCREMENTAL_SEQUENCE_VIOLATIONS = {
    "tool_started_without_completion",
    "approval_requested_without_resolution",
}
IMAGE_PENDING_STATUSES = {
    "pending",
    "queued",
    "running",
    "processing",
    "in_progress",
}

_LOCK_GUARD = asyncio.Lock()
_FILE_LOCKS: dict[str, asyncio.Lock] = {}
_RUNTIME_EVENT_STREAM_SIGNALS: dict[str, asyncio.Event] = {}
_RUNTIME_EVENT_STREAM_GENERATIONS: dict[str, int] = {}
_EVENT_INDEX_CACHE_LOCK = threading.RLock()
_EVENT_INDEX_CACHE: OrderedDict[
    str,
    tuple[
        tuple[int, int, int, int] | None,
        tuple[int, int, int, int] | None,
        _EventLedgerIndex,
    ],
] = OrderedDict()


class RuntimeEventSequenceAppendError(ValueError):
    """Raised when a production event append would violate RuntimeEvent order."""

    def __init__(
        self,
        violations: list[RuntimeEventContractViolation],
    ) -> None:
        self.violations = [
            violation.model_dump(mode="json") for violation in violations
        ]
        self.codes = [str(item["code"]) for item in self.violations]
        super().__init__(
            "Runtime event sequence validation failed: "
            + ", ".join(self.codes),
        )


class _EventLedgerIndex(BaseModel):
    """Versioned sidecar index for the append-only experience event ledger."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = EVENT_LEDGER_INDEX_SCHEMA_VERSION
    writer_version: str = EVENT_LEDGER_INDEX_WRITER_VERSION
    ledger_size: int = 0
    line_count: int = 0
    latest_seq: int = 0
    next_seq: int = 1
    offset_by_seq: dict[int, int] = Field(default_factory=dict)
    offsets_by_turn_id: dict[str, list[int]] = Field(default_factory=dict)


class _EventLedgerSegment(BaseModel):
    """One immutable rolled event ledger segment."""

    model_config = ConfigDict(extra="forbid")

    path: str
    start_seq: int
    latest_seq: int
    ledger_size: int


class _EventLedgerSegmentsManifest(BaseModel):
    """Manifest of rolled ledger segments plus the active ledger name."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = EVENT_LEDGER_SEGMENTS_SCHEMA_VERSION
    writer_version: str = EVENT_LEDGER_SEGMENTS_WRITER_VERSION
    active: str = EVENTS_FILE_NAME
    segments: list[_EventLedgerSegment] = Field(default_factory=list)


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _short_text(value: str, limit: int = MAX_TEXT_PREVIEW) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _runtime_type_for_kind(kind: str, status: str) -> str:
    """Map legacy experience kinds onto RuntimeEvent v1 type names."""

    if kind == "chat.turn.started":
        return "turn.started"
    if kind == "chat.turn.stop_requested":
        return "runtime.warning"
    if kind == "chat.turn.continue_requested":
        return "turn.continue_requested"
    if kind == "chat.turn.settled":
        if status == "interrupted":
            return "turn.interrupted"
        if status == "failed":
            return "turn.failed"
        if status == "stalled":
            return "turn.stalled"
        return "turn.completed"
    if kind == "chat.tool_call.started":
        return "tool.started"
    if kind in {"chat.tool_call.finished", "chat.tool_result"}:
        return "tool.completed"
    if kind == "chat.approval.requested":
        return "approval.requested"
    if kind == "chat.approval.resolved":
        return "approval.resolved"
    if kind == "chat.model.routing_decision":
        return "model.routing_decision"
    if kind == "loop.budget.changed":
        return "runtime.budget_changed"
    if kind == "runtime.notification":
        return "runtime.notification"
    if kind == "chat.message.started":
        return "message.started"
    if kind == "chat.message.delta":
        return "message.delta"
    if kind == "chat.message.completed":
        return "message.completed"
    if kind == "chat.system.event" and status == "stalled":
        return "turn.stalled"
    if kind == "chat.system.event":
        return "item.completed"
    if kind == "chat.final_answer":
        return "item.completed"
    if kind == "chat.error":
        return "runtime.error"
    if kind in {
        "chat.artifact.created",
        "chat.checkpoint.created",
        "chat.checkpoint.restored",
        "chat.replay.settled",
        "skills.changed",
    }:
        return "item.completed"
    if kind == "chat.replay.started":
        return "runtime.warning"
    if kind.endswith(".started"):
        return "tool.started"
    if kind.endswith(".settled"):
        return "tool.completed"
    return "runtime.warning"


def _is_child_run_projection_without_tool_identity(event: ChatExperienceEvent) -> bool:
    """Return whether this is a child-run summary, not a tool lifecycle row."""

    if event.kind not in {"chat.tool_call.finished", "chat.tool_result"}:
        return False
    if _tool_call_identity_for_event(event):
        return False
    return _parent_run_id_for_event(event) is not None


def _runtime_source_for_kind(kind: str) -> str:
    if (
        kind.startswith("chat.tool")
        or kind.startswith("cron.")
        or kind.startswith("heartbeat.")
        or kind.startswith("image.")
    ):
        return "tool"
    if kind in {"chat.model.routing_decision", "chat.system.event", "skills.changed"}:
        return "system"
    if kind == "loop.budget.changed":
        return "system"
    if kind == "runtime.notification":
        return "system"
    if kind == "chat.turn.started":
        return "user"
    if kind == "chat.turn.continue_requested":
        return "user"
    if kind.startswith("chat.message.") or kind == "chat.final_answer":
        return "assistant"
    if kind == "chat.error":
        return "system"
    return "system"


def _runtime_progress_step_kind(event: ChatExperienceEvent) -> str | None:
    kind = event.kind
    if kind in {"chat.tool_call.started", "chat.tool_call.finished", "chat.tool_result"}:
        return "tool"
    if kind in {"chat.message.started", "chat.message.completed", "chat.final_answer"}:
        return "message"
    if kind == "chat.model.routing_decision":
        return "model_call"
    if kind.startswith("chat.approval."):
        return "approval"
    if kind.startswith("chat.checkpoint."):
        return "checkpoint"
    if kind in {"chat.artifact.created", "image.task.settled"}:
        return "artifact"
    return None


def _is_runtime_progress_touch_event(event: ChatExperienceEvent) -> bool:
    if event.kind in {
        "chat.turn.started",
        "chat.turn.stop_requested",
        "chat.turn.continue_requested",
        "loop.budget.changed",
        "runtime.notification",
    }:
        return False
    if event.kind.startswith(("cron.", "heartbeat.")):
        return False
    if event.type in RUNTIME_SEQUENCE_TERMINAL_TYPES | {
        "runtime.budget_changed",
        "turn.continue_requested",
    }:
        return False
    return True


def _runtime_progress_step_token(event: ChatExperienceEvent, kind: str) -> str:
    payload = event.payload or {}
    if kind == "tool":
        return _tool_call_identity_for_event(event) or event.item_id or str(event.seq)
    if kind == "approval":
        for key in ("request_id", "approval_request_id", "approval_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if kind == "artifact":
        for key in ("artifact_id", "path", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if kind == "checkpoint":
        for key in ("checkpoint_id", "event_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if kind == "model_call":
        for key in ("selected_model", "model", "provider", "route"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return event.item_id or event.id


def _runtime_progress_step_title(event: ChatExperienceEvent, kind: str) -> str:
    payload = event.payload or {}
    for key in ("title", "tool_name", "name", "selected_model", "model"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _short_text(value, limit=80)
    if event.summary:
        return _short_text(event.summary, limit=80)
    return kind


def _runtime_progress_step_index(
    event: ChatExperienceEvent,
    previous: RuntimeStepRef | None,
    *,
    step_id: str,
) -> int:
    payload = event.payload or {}
    for key in ("step_index", "index"):
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    if previous is not None and previous.step_id == step_id:
        return previous.index
    if previous is not None:
        return previous.index + 1
    return 0


def _runtime_progress_step_for_event(
    event: ChatExperienceEvent,
    previous: RuntimeStepRef | None,
) -> RuntimeStepRef | None:
    kind = _runtime_progress_step_kind(event)
    if kind is None:
        return None
    step_token = _runtime_progress_step_token(event, kind)
    step_id = f"{kind}:{step_token}"
    return RuntimeStepRef(
        step_id=step_id,
        index=_runtime_progress_step_index(event, previous, step_id=step_id),
        kind=kind,  # type: ignore[arg-type]
        title=_runtime_progress_step_title(event, kind),
    )


def _runtime_checkpoint_for_event(
    event: ChatExperienceEvent,
    previous: RuntimeCheckpoint | None,
) -> RuntimeCheckpoint | None:
    """Project a completed, identified tool call into a resumable checkpoint."""

    if event.kind != "chat.tool_call.finished" or event.status not in {
        "completed",
        "finished",
    }:
        return None
    if _is_child_run_projection_without_tool_identity(event):
        return None
    if not (_tool_call_identity_for_event(event) or event.item_id):
        return None
    payload = event.payload or {}
    artifact_id = payload.get("artifact_id")
    effect_id = payload.get("effect_id")
    effect_ids = list(previous.effect_ids) if previous is not None else []
    if isinstance(effect_id, str) and effect_id.strip() and effect_id not in effect_ids:
        effect_ids.append(effect_id.strip())
    return RuntimeCheckpoint(
        checkpoint_id=f"event:{event.id}",
        event_id=event.id,
        seq=event.seq,
        artifact_id=artifact_id if isinstance(artifact_id, str) else None,
        effect_ids=tuple(effect_ids),
        summary=event.summary,
    )


async def reconcile_runtime_run_checkpoint_from_events(
    *,
    workspace_dir: Path | str,
    run_id: str,
    attempt_id: str,
    committed_effect_ids: frozenset[str],
) -> RuntimeRunContract | None:
    """Restore missing effect acknowledgements from durable tool completion rows.

    The event ledger is written before its RuntimeRun projection. A process can
    therefore stop in the tiny interval after the completion row is durable but
    before the checkpoint snapshot is saved. This reconciliation only rebuilds
    a local checkpoint; it never replays a tool or changes run status.
    """

    store = RuntimeRunStore(workspace_dir)
    try:
        contract = await store.load_contract(run_id)
    except RuntimeRunStoreError:
        return None
    if contract is None or contract.attempt_id != attempt_id or not committed_effect_ids:
        return contract

    matching_events = [
        event
        for event in _iter_events_for_turn_from_workspace(workspace_dir, run_id)
        if event.kind == "chat.tool_call.finished"
        and event.status in {"completed", "finished"}
        and isinstance((event.payload or {}).get("effect_id"), str)
        and (event.payload or {}).get("effect_id") in committed_effect_ids
        and (event.payload or {}).get("attempt_id") == attempt_id
    ]
    if not matching_events:
        return contract

    acknowledged_effect_ids = set(
        contract.checkpoint.effect_ids if contract.checkpoint is not None else (),
    )
    acknowledged_effect_ids.update(
        str((event.payload or {}).get("effect_id")) for event in matching_events
    )
    if (
        contract.checkpoint is not None
        and acknowledged_effect_ids == set(contract.checkpoint.effect_ids)
    ):
        return contract

    latest_event = max(matching_events, key=lambda event: event.seq)
    payload = latest_event.payload or {}
    artifact_id = payload.get("artifact_id")
    checkpoint = RuntimeCheckpoint(
        checkpoint_id=f"event:{latest_event.id}",
        event_id=latest_event.id,
        seq=latest_event.seq,
        artifact_id=artifact_id if isinstance(artifact_id, str) else None,
        effect_ids=tuple(sorted(acknowledged_effect_ids)),
        summary=latest_event.summary,
    )
    repaired = contract.model_copy(
        update={
            "revision": contract.revision + 1,
            "heartbeat_at": _parse_event_ts(latest_event.ts)
            or datetime.now(timezone.utc),
            "checkpoint": checkpoint,
        },
    )
    try:
        return await store.save_contract(
            repaired,
            expected_revision=contract.revision,
            expected_attempt_id=contract.attempt_id,
            lease_owner=contract.lease_owner,
        )
    except (RuntimeRunStoreConflictError, RuntimeRunStoreError):
        logger.debug(
            "Skipped RuntimeRun checkpoint reconciliation after concurrent update",
            extra={"run_id": run_id, "attempt_id": attempt_id},
        )
        try:
            return await store.load_contract(run_id)
        except RuntimeRunStoreError:
            return contract


async def _touch_runtime_run_progress_after_append(
    workspace_dir: Path | str,
    event: ChatExperienceEvent,
) -> None:
    """Update RuntimeRun heartbeat/progress after an event has reached the ledger."""

    if not _is_runtime_progress_touch_event(event):
        return
    run_id = _group_run_key_for_event(event)
    if not run_id:
        return
    store = RuntimeRunStore(workspace_dir)
    try:
        contract = await store.load_contract(run_id)
    except RuntimeRunStoreError:
        return
    if (
        contract is None
        or contract.status == "queued"
        or contract.status in TERMINAL_RUNTIME_RUN_STATUSES
    ):
        return
    event_time = _parse_event_ts(event.ts) or datetime.now(timezone.utc)
    step = _runtime_progress_step_for_event(event, contract.current_step)
    checkpoint = _runtime_checkpoint_for_event(event, contract.checkpoint)
    try:
        result = reduce_runtime_run(
            contract,
            RuntimeRunReducerAction(
                type="heartbeat",
                expected_attempt_id=contract.attempt_id,
                expected_revision=contract.revision,
                lease_owner=contract.lease_owner,
                checkpoint=checkpoint,
                current_step=step,
                now=event_time,
            ),
        )
        await store.save_contract(
            result.run,
            expected_revision=result.before_revision,
            expected_attempt_id=contract.attempt_id,
            lease_owner=contract.lease_owner,
        )
    except (
        RuntimeRunStoreConflictError,
        RuntimeRunStoreError,
        RuntimeRunTransitionError,
    ):
        logger.debug(
            "Skipped RuntimeRun heartbeat/progress touch after event append",
            extra={"run_id": run_id, "event_id": event.id, "event_kind": event.kind},
        )


def _turn_id_for_event(event: ChatExperienceEvent) -> str | None:
    payload = event.payload or {}
    for key in ("turn_id", "run_id"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return event.run_id


def _usage_for_payload(payload: dict | None, usage: dict | None = None) -> dict | None:
    """Normalize provider/tool usage values into RuntimeEvent v1 usage keys."""

    source = usage if isinstance(usage, dict) else None
    if source is None and isinstance(payload, dict):
        raw = payload.get("usage")
        if isinstance(raw, dict):
            source = raw
    if not source:
        return None

    def read_number(*keys: str) -> int | float | None:
        for key in keys:
            value = source.get(key)
            if value is None or value == "":
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number.is_integer():
                return int(number)
            return number
        return None

    normalized = {
        "input_tokens": read_number("input_tokens", "prompt_tokens", "inputTokens"),
        "output_tokens": read_number(
            "output_tokens",
            "completion_tokens",
            "outputTokens",
        ),
        "cache_tokens": read_number(
            "cache_tokens",
            "cached_tokens",
            "cache_read_input_tokens",
            "cached_input_tokens",
        ),
        "context_tokens": read_number(
            "context_tokens",
            "current_context_tokens",
            "estimated_context_tokens",
            "contextTokens",
            "estimatedContextTokens",
        ),
        "cost_points": read_number(
            "cost_points",
            "estimated_cost",
            "estimated_cost_points",
            "estimatedCost",
            "estimatedCostPoints",
            "credits",
            "points",
        ),
    }
    return {key: value for key, value in normalized.items() if value is not None} or None


def _trace_phase_for_event(
    event: ChatExperienceEvent,
    payload: dict[str, Any] | None = None,
) -> str:
    """Return the ProcessTrace phase for a runtime event."""

    payload = payload or event.payload or {}
    explicit = str(payload.get("phase") or "").strip()
    if explicit:
        return explicit
    kind = event.kind
    if kind == "chat.turn.started":
        return "request"
    if kind == "chat.turn.continue_requested":
        return "request"
    if kind == "chat.model.routing_decision":
        return "model"
    if kind == "loop.budget.changed":
        return "budget"
    if kind.startswith("chat.message."):
        return "message"
    if kind == "chat.system.event":
        return str(payload.get("phase") or "model")
    if kind.startswith("chat.approval."):
        return "approval"
    if any(token in kind for token in ("checkpoint", "snapshot", "fork", "replay")):
        return "checkpoint"
    if kind.startswith("chat.tool"):
        presentation = tool_presentation_for_event(event, payload)
        if presentation is not None and presentation.surface == "subagent":
            return "subtask"
        tool_name = str(payload.get("tool_name") or "").lower()
        if tool_name == "execute_shell_command" or payload.get("command"):
            return "command"
        if payload.get("files_changed") or tool_name in {
            "write_file",
            "edit_file",
            "append_file",
        }:
            return "file"
        return "tool"
    if kind == "chat.artifact.created":
        return "artifact"
    if kind == "chat.final_answer":
        return "result"
    if kind == "chat.error" or event.status == "failed":
        return "failure"
    if kind.startswith("cron.") or kind.startswith("heartbeat."):
        return "task"
    if kind.startswith("image."):
        return "artifact"
    return "model"


def _preview_payload_for_event(event: ChatExperienceEvent) -> dict[str, Any]:
    """Return a bounded payload subset for chat ProcessTrace previews."""

    payload = dict(event.payload or {})
    allowed = {
        "phase",
        "trace_title",
        "trace_detail",
        "duration_ms",
        "elapsed_seconds",
        "threshold_seconds",
        "last_activity_at",
        "command",
        "cwd",
        "exit_code",
        "stdout",
        "stderr",
        "output_summary",
        "result_preview",
        "preview_url",
        "preview_harness",
        "browser_engine",
        "screenshot_path",
        "console_errors",
        "page_errors",
        "network_errors",
        "files_changed",
        "sub_run",
        "parent_run_id",
        "agent_name",
        "agent_role",
        "delegation_reason",
        "permission_profile",
        "inherited_context_summary",
        "current_tool",
        "result_summary",
        "failure_reason",
        "source_run_id",
        "source_agent_name",
        "policy_decision",
        "routing_decision",
        "tool_name",
        "guard",
        "approval_request_id",
        "severity",
        "findings_count",
        "checkpoint_id",
        "source_checkpoint_id",
        "source_seq",
        "target_seq",
        "replay_from_seq",
        "replay_to_seq",
        "request_id",
        "mode",
        "requested_run_id",
        "requested_chat_id",
        "requested_session_id",
        "client_turn_id",
        "attempt_id",
        "requested_by",
        "reason",
        "revision",
        "after_seq",
        "latest_seq",
        "stage_label",
        "outcome_status",
    }
    result = {key: payload[key] for key in allowed if key in payload}
    presentation = tool_presentation_for_event(event, payload)
    if presentation is not None:
        result["presentation"] = presentation.model_dump(mode="json")
    diagnosis = failure_diagnosis_for_event(event)
    if diagnosis is not None:
        result["failure_diagnosis"] = diagnosis
    result["legacy_kind"] = event.kind
    return result


def _event_to_runtime(event: ChatExperienceEvent) -> RuntimeEvent:
    payload = dict(event.payload or {})
    event_type = event.type or _runtime_type_for_kind(event.kind, event.status)
    if _is_child_run_projection_without_tool_identity(event):
        event_type = "item.completed"
    if event_type not in CONTRACT_RUNTIME_EVENT_TYPES:
        payload.setdefault("legacy_runtime_type", event_type)
        event_type = "runtime.warning"
    presentation = tool_presentation_for_event(event, payload)
    if presentation is not None and "presentation" not in payload:
        payload["presentation"] = presentation.model_dump(mode="json")
    payload.setdefault("phase", _trace_phase_for_event(event, payload))
    payload.setdefault("trace_title", _label_for_kind(event.kind))
    payload.setdefault("trace_detail", _detail_for_event(event))
    diagnosis = failure_diagnosis_for_event(event)
    if diagnosis is not None and "failure_diagnosis" not in payload:
        payload["failure_diagnosis"] = diagnosis
    return RuntimeEvent(
        id=event.id,
        session_id=event.session_id,
        turn_id=_turn_id_for_event(event),
        item_id=event.item_id,
        seq=event.seq,
        type=event_type,
        status=event.status,
        source=event.source or _runtime_source_for_kind(event.kind),
        presentation=(
            ToolPresentation.model_validate(payload["presentation"])
            if isinstance(payload.get("presentation"), dict)
            else presentation
        ),
        payload={
            **payload,
            "legacy_kind": event.kind,
            "summary": event.summary,
            "label": _label_for_kind(event.kind),
            "detail": _detail_for_event(event),
        },
        usage=_usage_for_payload(payload, event.usage),
        created_at=event.ts,
    )


def _event_to_runtime_contract(
    event: ChatExperienceEvent,
) -> RuntimeContractEvent:
    runtime_event = _event_to_runtime(event)
    payload = runtime_event.model_dump(mode="json")
    if payload.get("type") not in CONTRACT_RUNTIME_EVENT_TYPES:
        payload["type"] = "runtime.warning"
    return RuntimeContractEvent.model_validate(payload)


def _is_runtime_sequence_terminal(event: RuntimeContractEvent) -> bool:
    return event.type in RUNTIME_SEQUENCE_TERMINAL_TYPES


def _enforced_incremental_sequence_violations(
    violations: list[RuntimeEventContractViolation],
    *,
    candidate_is_terminal: bool,
) -> list[RuntimeEventContractViolation]:
    if candidate_is_terminal:
        return violations
    return [
        violation
        for violation in violations
        if violation.code not in DEFERRED_INCREMENTAL_SEQUENCE_VIOLATIONS
    ]


def _validate_runtime_event_append(
    *,
    workspace_dir: Path | str,
    candidate: ChatExperienceEvent,
) -> None:
    if candidate.scope != "chat":
        return
    turn_id = _turn_id_for_event(candidate)
    if not turn_id:
        return

    related_events = _iter_events_for_turn_from_workspace(
        workspace_dir,
        turn_id,
        candidate_is_start=candidate.type == "turn.started",
    )
    contract_events = [
        _event_to_runtime_contract(event) for event in related_events
    ]
    candidate_contract = _event_to_runtime_contract(candidate)
    contract_events.append(candidate_contract)

    violations = validate_runtime_event_sequence(
        contract_events,
        allow_legacy_tool_completed=True,
    )
    enforced = _enforced_incremental_sequence_violations(
        violations,
        candidate_is_terminal=_is_runtime_sequence_terminal(candidate_contract),
    )
    if not enforced:
        return

    error = RuntimeEventSequenceAppendError(enforced)
    logger.warning(
        "Rejected runtime event append because the sequence is invalid",
        extra={
            "run_id": turn_id,
            "event_kind": candidate.kind,
            "event_type": candidate.type,
            "event_seq": candidate.seq,
            "runtime_sequence_violation_codes": error.codes,
        },
    )
    raise error


def is_internal_runtime_path(relative_path: Path) -> bool:
    """Return whether a relative workspace path targets the internal runtime dir."""

    return bool(relative_path.parts) and relative_path.parts[0] == INTERNAL_RUNTIME_DIR_NAME


def experience_dir_for_workspace(workspace_dir: Path | str) -> Path:
    return Path(workspace_dir).expanduser() / INTERNAL_RUNTIME_DIR_NAME / EXPERIENCE_DIR_NAME


def legacy_events_path_for_workspace(workspace_dir: Path | str) -> Path:
    return experience_dir_for_workspace(workspace_dir) / EVENTS_FILE_NAME


def runtime_events_dir_for_workspace(workspace_dir: Path | str) -> Path:
    return (
        Path(workspace_dir).expanduser()
        / INTERNAL_RUNTIME_DIR_NAME
        / RUNTIME_DIR_NAME
        / RUNTIME_STORE_VERSION_DIR_NAME
        / RUNTIME_EVENTS_DIR_NAME
    )


def events_path_for_workspace(workspace_dir: Path | str) -> Path:
    return runtime_events_dir_for_workspace(workspace_dir) / EVENTS_FILE_NAME


def _event_index_path(events_path: Path) -> Path:
    if events_path.name != EVENTS_FILE_NAME:
        return events_path.with_name(f"{events_path.name}.index.json")
    return events_path.with_name(EVENTS_INDEX_FILE_NAME)


def _event_segments_manifest_path(events_path: Path) -> Path:
    return events_path.with_name(EVENTS_SEGMENTS_FILE_NAME)


def events_index_path_for_workspace(workspace_dir: Path | str) -> Path:
    return experience_dir_for_workspace(workspace_dir) / EVENTS_INDEX_FILE_NAME


def resolve_effective_workspace_dir_from_meta(
    meta: dict | None,
    *,
    root_workspace_dir: Path | str | None = None,
) -> Path | None:
    """Resolve the effective workspace dir from chat metadata."""

    if not isinstance(meta, dict):
        return None

    root: Path | None = None
    if root_workspace_dir is not None:
        try:
            root = Path(root_workspace_dir).expanduser().resolve(strict=False)
        except OSError:
            return None

    for key in ("manual_workspace_dir", "topic_workspace_dir"):
        raw = meta.get(key)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            if root is None:
                return candidate
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                resolved = candidate.resolve(strict=False)
            except OSError:
                continue
            if resolved == root or root in resolved.parents:
                return resolved
    return None


async def _file_lock(path: Path) -> asyncio.Lock:
    key = str(path)
    async with _LOCK_GUARD:
        existing = _FILE_LOCKS.get(key)
        if existing is not None:
            return existing
        lock = asyncio.Lock()
        _FILE_LOCKS[key] = lock
        return lock


def _runtime_event_stream_key(workspace_dir: Path | str) -> str:
    return str(Path(workspace_dir).expanduser().resolve(strict=False))


def runtime_event_stream_generation(workspace_dir: Path | str) -> int:
    """Return the in-process append generation for one workspace ledger."""

    return _RUNTIME_EVENT_STREAM_GENERATIONS.get(
        _runtime_event_stream_key(workspace_dir),
        0,
    )


def _notify_runtime_event_stream(workspace_dir: Path | str) -> None:
    key = _runtime_event_stream_key(workspace_dir)
    _RUNTIME_EVENT_STREAM_GENERATIONS[key] = (
        _RUNTIME_EVENT_STREAM_GENERATIONS.get(key, 0) + 1
    )
    signal = _RUNTIME_EVENT_STREAM_SIGNALS.get(key)
    if signal is not None:
        signal.set()


async def wait_for_runtime_event_append(
    workspace_dir: Path | str,
    *,
    after_generation: int,
    timeout_seconds: float,
) -> int:
    """Wait until this process appends to a workspace ledger or heartbeat expires.

    Event streams previously re-read the on-disk ledger every 120 ms forever,
    including for completed chats. The append signal keeps active delivery
    immediate while making an idle stream consume no file or worker-pool I/O.
    """

    key = _runtime_event_stream_key(workspace_dir)
    current = _RUNTIME_EVENT_STREAM_GENERATIONS.get(key, 0)
    if current != after_generation:
        return current
    signal = _RUNTIME_EVENT_STREAM_SIGNALS.setdefault(key, asyncio.Event())
    signal.clear()
    current = _RUNTIME_EVENT_STREAM_GENERATIONS.get(key, 0)
    if current != after_generation:
        return current
    try:
        await asyncio.wait_for(
            signal.wait(),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        pass
    return _RUNTIME_EVENT_STREAM_GENERATIONS.get(key, 0)


async def append_chat_event(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    chat_id: str,
    session_id: str,
    kind: str,
    status: str,
    summary: str,
    payload: dict | None = None,
    run_id: str | None = None,
    scope: str = "chat",
    item_id: str | None = None,
    type: str | None = None,  # pylint: disable=redefined-builtin
    source: str | None = None,
    usage: dict | None = None,
    event_id: str | None = None,
) -> ChatExperienceEvent:
    """Append one chat event to the workspace event ledger."""

    path = events_path_for_workspace(workspace_dir)
    event_payload = sanitize_runtime_payload(payload or {})
    path.parent.mkdir(parents=True, exist_ok=True)

    lock = await _file_lock(path)
    async with lock:
        _roll_event_ledger_if_needed(path)
        index = _load_active_event_index_for_append(path)
        seq = index.next_seq
        if index.line_count == 0 and index.next_seq <= 1:
            seq = max(seq, _next_seq_for_workspace_append(workspace_dir))
        event = ChatExperienceEvent(
            id=str(event_id or uuid4()),
            seq=seq,
            ts=_now_iso(),
            agent_id=agent_id,
            chat_id=chat_id,
            session_id=session_id,
            run_id=run_id,
            item_id=item_id,
            scope=scope,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            type=type or _runtime_type_for_kind(kind, status),
            status=status,  # type: ignore[arg-type]
            source=source or _runtime_source_for_kind(kind),
            summary=_short_text(summary, limit=80) or kind,
            payload=event_payload,
            usage=_usage_for_payload(event_payload, usage),
        )
        _validate_runtime_event_append(workspace_dir=workspace_dir, candidate=event)
        offset = path.stat().st_size if path.exists() else 0
        append_text_no_follow(
            path,
            json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n",
        )
        _safe_write_event_index(
            path,
            _event_index_after_append(
                index,
                event=event,
                offset=offset,
                ledger_size=path.stat().st_size,
            ),
        )
    _notify_runtime_event_stream(workspace_dir)
    await _touch_runtime_run_progress_after_append(workspace_dir, event)
    return event


async def append_task_event(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    kind: str,
    status: str,
    summary: str,
    payload: dict | None = None,
    usage: dict | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
) -> ChatExperienceEvent:
    """Append one task-scoped event to the workspace event ledger."""

    return await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=task_id,
        session_id=session_id,
        kind=kind,
        status=status,
        summary=summary,
        payload=payload,
        usage=usage,
        run_id=run_id,
        item_id=task_id,
        scope="task",
    )


async def record_chat_topic_workspace_bound(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    chat_id: str,
    session_id: str,
    topic_workspace_dir: str,
    manual_workspace_dir: str | None,
    effective_workspace_dir: str,
    binding_mode: str,
    run_id: str | None = None,
) -> ChatExperienceEvent:
    return await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        kind="chat.topic_workspace.bound",
        status="info",
        summary="已绑定当前工作区",
        payload={
            "topic_workspace_dir": topic_workspace_dir,
            "manual_workspace_dir": manual_workspace_dir,
            "effective_workspace_dir": effective_workspace_dir,
            "binding_mode": binding_mode,
        },
    )


async def record_chat_turn_started(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    chat_id: str,
    session_id: str,
    text_preview: str,
    effective_workspace_dir: str,
    run_id: str,
    task_contract=None,
) -> ChatExperienceEvent:
    contract = await record_chat_runtime_run_started(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        effective_workspace_dir=effective_workspace_dir,
        task_contract=task_contract,
    )
    return await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={
            "text_preview": _short_text(text_preview),
            "workspace_dir": effective_workspace_dir,
            "attempt_id": contract.attempt_id,
            "task_contract": (
                contract.task_contract.model_dump(mode="json")
                if contract.task_contract is not None
                else None
            ),
        },
    )


async def record_model_routing_decision(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    chat_id: str,
    session_id: str,
    decision: dict[str, Any],
    run_id: str,
) -> ChatExperienceEvent:
    """Record the model/provider choice that will be used for this turn."""

    payload = dict(decision or {})
    selected_model = str(payload.get("selected_model") or "").strip()
    route = str(payload.get("route") or "").strip()
    summary = selected_model or route or "默认模型"
    usage = {
        key: payload[key]
        for key in ("estimated_context_tokens", "estimated_cost_points")
        if payload.get(key) is not None
    }
    return await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        kind="chat.model.routing_decision",
        type="model.routing_decision",
        source="system",
        status="info",
        summary=f"模型路由：{summary}",
        payload=payload,
        usage=usage or None,
    )


async def record_loop_budget_changed(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    target_id: str,
    session_id: str | None,
    run_id: str,
    record: LoopBudgetChangeRecord,
) -> ChatExperienceEvent:
    """Record a structured Loop budget mutation in the runtime event ledger."""

    return await record_runtime_budget_changed(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        target_id=target_id,
        session_id=session_id,
        run_id=run_id,
        payload=record.model_dump(mode="json"),
    )


async def record_runtime_budget_changed(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    target_id: str,
    session_id: str | None,
    run_id: str,
    payload: RuntimeBudgetChangedEventPayload | dict[str, Any],
) -> ChatExperienceEvent:
    """Record a structured budget mutation in the runtime event ledger."""

    event_payload = (
        payload
        if isinstance(payload, RuntimeBudgetChangedEventPayload)
        else RuntimeBudgetChangedEventPayload.model_validate(payload)
    )

    return await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=target_id,
        session_id=session_id,
        run_id=run_id,
        kind="loop.budget.changed",
        type="runtime.budget_changed",
        status="info",
        source="system",
        summary="Runtime 预算已变更",
        payload=event_payload.model_dump(mode="json"),
        scope="task",
    )


def _default_runtime_notification_text(
    *,
    run: RuntimeRunContract,
    notification_kind: str,
) -> tuple[str, str, str, bool]:
    step_title = run.current_step.title if run.current_step else ""
    status = run.status
    if notification_kind == "waiting_approval":
        detail = step_title or "当前步骤需要人工确认。"
        return (
            "运行等待审批",
            detail,
            "high",
            False,
        )
    if notification_kind == "ambiguous_effect":
        return (
            "运行副作用待判定",
            "外部动作可能已经执行，需要人工探测或消歧后再继续。",
            "high",
            True,
        )
    if notification_kind == "budget_exhausted":
        reason = run.stop_reason or run.error
        detail = reason.message if reason and reason.message else "运行预算或停止条件已触发。"
        return (
            "运行预算已耗尽",
            detail,
            "high",
            True,
        )
    if notification_kind == "final_complete":
        return (
            "运行已完成",
            f"运行以 {status} 状态结束。",
            "normal",
            True,
        )
    return ("运行状态提醒", f"运行当前状态：{status}", "normal", False)


def build_runtime_notification_payload(
    *,
    run: RuntimeRunContract,
    notification_kind: str,
    contract_id: str | None = None,
    contract_hash: str | None = None,
    title: str | None = None,
    message: str | None = None,
    trigger_reason: str = "",
    action_url: str | None = None,
    export_recommended: bool | None = None,
) -> RuntimeNotificationEventPayload:
    """Build a strict notification advisory payload from Runtime state."""

    default_title, default_message, priority, default_export = (
        _default_runtime_notification_text(
            run=run,
            notification_kind=notification_kind,
        )
    )
    source_status = str(run.status)
    dedupe_parts = [
        "runtime-notification",
        run.run_id,
        run.attempt_id,
        notification_kind,
        source_status,
    ]
    if trigger_reason:
        dedupe_parts.append(trigger_reason)
    dedupe_key = ":".join(part for part in dedupe_parts if part)
    return RuntimeNotificationEventPayload(
        schema_version=1,
        notification_id=f"{run.run_id}:{run.attempt_id}:{notification_kind}",
        notification_kind=notification_kind,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        title=_short_text(title or default_title, limit=80),
        message=_short_text(message or default_message, limit=240),
        dedupe_key=dedupe_key,
        run_id=run.run_id,
        attempt_id=run.attempt_id,
        contract_id=contract_id,
        contract_hash=contract_hash or run.contract_hash or None,
        source_status=source_status,
        trigger_reason=trigger_reason,
        export_recommended=(
            default_export if export_recommended is None else export_recommended
        ),
        action_url=action_url,
    )


async def record_runtime_notification(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    target_id: str,
    session_id: str | None,
    run: RuntimeRunContract,
    notification_kind: str,
    contract_id: str | None = None,
    contract_hash: str | None = None,
    title: str | None = None,
    message: str | None = None,
    trigger_reason: str = "",
    action_url: str | None = None,
    export_recommended: bool | None = None,
) -> ChatExperienceEvent:
    """Record a structured Runtime notification advisory in the event ledger."""

    payload = build_runtime_notification_payload(
        run=run,
        notification_kind=notification_kind,
        contract_id=contract_id,
        contract_hash=contract_hash,
        title=title,
        message=message,
        trigger_reason=trigger_reason,
        action_url=action_url,
        export_recommended=export_recommended,
    )
    return await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=target_id,
        session_id=session_id or run.owner.session_id,
        run_id=run.run_id,
        item_id=payload.notification_id,
        kind="runtime.notification",
        type="runtime.notification",
        status="info",
        source="system",
        summary=payload.title,
        payload=payload.model_dump(mode="json"),
        scope="task",
    )


async def record_chat_turn_stop_requested(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    chat_id: str,
    session_id: str,
    requested_chat_id: str,
    resolved_chat_id: str,
) -> ChatExperienceEvent:
    return await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=chat_id,
        session_id=session_id,
        kind="chat.turn.stop_requested",
        status="interrupted",
        summary="本轮已请求停止",
        payload={
            "requested_chat_id": requested_chat_id,
            "resolved_chat_id": resolved_chat_id,
        },
    )


async def record_chat_turn_continue_requested(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    chat_id: str,
    session_id: str,
    run_id: str,
    mode: str,
    reason: str,
    status: str = "info",
    requested_chat_id: str | None = None,
    requested_session_id: str | None = None,
    after_seq: int = 0,
    latest_seq: int = 0,
    stage_label: str | None = None,
    active_run: bool | None = None,
    attempt_id: str | None = None,
    revision: int | None = None,
    requested_by: str = "user",
    workspace_path: str | None = None,
    summary: str | None = None,
) -> ChatExperienceEvent:
    """Record a canonical user request to reconnect/retry/resume/takeover."""

    return await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        kind="chat.turn.continue_requested",
        type="turn.continue_requested",
        status=status,
        source="user",
        summary=summary or "请求继续运行",
        payload={
            "request_id": f"continue:{uuid4()}",
            "mode": mode,
            "run_id": run_id,
            "requested_run_id": run_id,
            "requested_chat_id": requested_chat_id,
            "requested_session_id": requested_session_id,
            "attempt_id": attempt_id,
            "revision": revision,
            "after_seq": after_seq,
            "latest_seq": latest_seq,
            "stage_label": stage_label,
            "active_run": active_run,
            "workspace_dir": workspace_path,
            "requested_by": requested_by,
            "reason": reason,
        },
    )


async def record_chat_turn_settled(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    chat_id: str,
    session_id: str,
    final_task_status: str,
    pending_turn_cleared: bool,
    active_run_event_count: int,
    run_id: str,
    outcome_status: str = "unknown",
    delivery_status="pending",
    completion_evidence=None,
) -> ChatExperienceEvent:
    await record_chat_runtime_run_settled(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        effective_workspace_dir=str(workspace_dir),
        final_task_status=final_task_status,
        delivery_status=delivery_status,
        completion_evidence=completion_evidence,
    )
    settled_status = (
        final_task_status
        if final_task_status in {"interrupted", "failed", "stalled"}
        else "finished"
    )
    normalized_outcome = normalize_turn_outcome_status(outcome_status)
    summary = {
        "failed": "本轮运行失败",
        "interrupted": "本轮已中断",
        "stalled": "本轮已卡住",
    }.get(settled_status, "本轮已结束")
    return await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        chat_id=chat_id,
        session_id=session_id,
        run_id=run_id,
        kind="chat.turn.settled",
        status=settled_status,
        summary=summary,
        payload={
            "final_task_status": final_task_status,
            "outcome_status": normalized_outcome,
            "delivery_status": delivery_status,
            "pending_turn_cleared": pending_turn_cleared,
            "active_run_event_count": active_run_event_count,
        },
    )


async def record_cron_job_started(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    job_id: str,
    job_name: str,
    task_type: str,
    channel: str,
    run_id: str | None = None,
) -> ChatExperienceEvent:
    run_id = run_id or f"cron:{job_id}:{uuid4().hex}"
    contract = await record_cron_runtime_run_started(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        job_id=job_id,
        job_name=job_name,
        task_type=task_type,
        channel=channel,
        run_id=run_id,
    )
    return await append_task_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        task_id=f"cron:{job_id}",
        run_id=run_id,
        kind="cron.job.started",
        status="running",
        summary="定时任务已开始",
        payload={
            "job_id": job_id,
            "job_name": job_name,
            "task_type": task_type,
            "channel": channel,
            "invocation_id": run_id,
            "attempt_id": contract.attempt_id,
        },
    )


async def record_cron_job_settled(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    job_id: str,
    job_name: str,
    task_type: str,
    channel: str,
    final_status: str,
    error_text: str | None = None,
    run_id: str | None = None,
) -> ChatExperienceEvent:
    invocation_id = run_id or f"cron:{job_id}:{uuid4().hex}"
    await record_cron_runtime_run_settled(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        job_id=job_id,
        job_name=job_name,
        task_type=task_type,
        channel=channel,
        final_status=final_status,
        run_id=invocation_id,
        error_text=error_text,
    )
    normalized_status = (
        "interrupted" if final_status in {"cancelled", "skipped"} else "finished"
    )
    summary = "定时任务已跳过" if final_status == "skipped" else "定时任务已结束"
    return await append_task_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        task_id=f"cron:{job_id}",
        run_id=invocation_id,
        kind="cron.job.settled",
        status=normalized_status,
        summary=summary,
        payload={
            "job_id": job_id,
            "job_name": job_name,
            "task_type": task_type,
            "channel": channel,
            "final_status": final_status,
            "invocation_id": invocation_id,
            "error_text": _short_text(error_text or "", limit=160),
        },
    )


async def record_heartbeat_run_started(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    target: str,
    query_path: str,
) -> ChatExperienceEvent:
    return await append_task_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        task_id=f"heartbeat:{agent_id}",
        kind="heartbeat.run.started",
        status="running",
        summary="心跳任务已开始",
        payload={
            "target": target,
            "query_path": query_path,
        },
    )


async def record_heartbeat_run_settled(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    target: str,
    query_path: str,
    final_status: str,
    result_reason: str | None = None,
) -> ChatExperienceEvent:
    normalized_status = "interrupted" if final_status == "cancelled" else "finished"
    summary = "心跳任务已结束" if final_status == "completed" else "心跳任务已跳过"
    return await append_task_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        task_id=f"heartbeat:{agent_id}",
        kind="heartbeat.run.settled",
        status=normalized_status,
        summary=summary,
        payload={
            "target": target,
            "query_path": query_path,
            "final_status": final_status,
            "result_reason": _short_text(result_reason or "", limit=160),
        },
    )


async def record_skills_changed(
    *,
    workspace_dir: Path | str,
    agent_id: str,
    change_type: str,
    skill_name: str | None = None,
    source: str = "workspace",
    details: dict | None = None,
) -> ChatExperienceEvent:
    """Record a lightweight skills index invalidation event."""

    name = skill_name or "技能库"
    return await append_task_event(
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        task_id="skills",
        kind="skills.changed",
        status="completed",
        summary=f"技能已更新：{name}",
        payload={
            "change_type": change_type,
            "skill_name": skill_name,
            "source": source,
            "details": details or {},
        },
        run_id=f"skills:{change_type}:{skill_name or 'all'}",
    )


def _label_for_kind(kind: str) -> str:
    return {
        "chat.topic_workspace.bound": "工作区绑定",
        "chat.turn.started": "开始运行",
        "chat.turn.stop_requested": "请求停止",
        "chat.turn.continue_requested": "请求继续",
        "chat.turn.settled": "本轮结束",
        "chat.tool_call.started": "工具调用",
        "chat.tool_call.finished": "工具结果",
        "chat.tool_result": "工具结果",
        "chat.approval.requested": "请求确认",
        "chat.approval.resolved": "确认完成",
        "chat.artifact.created": "产物生成",
        "chat.model.routing_decision": "模型路由",
        "chat.message.started": "回复开始",
        "chat.message.delta": "回复片段",
        "chat.message.completed": "回复完成",
        "chat.system.event": "系统事件",
        "chat.final_answer": "最终回复",
        "chat.error": "运行错误",
        "cron.job.started": "定时开始",
        "cron.job.settled": "定时结束",
        "heartbeat.run.started": "心跳开始",
        "heartbeat.run.settled": "心跳结束",
        "image.task.started": "历史图片任务开始",
        "image.task.settled": "历史图片任务结束",
        "skills.changed": "技能更新",
    }.get(kind, kind)


def _detail_for_event(event: ChatExperienceEvent) -> str:
    payload = event.payload or {}

    if event.kind == "chat.topic_workspace.bound":
        mode = "手动工作区" if payload.get("binding_mode") == "manual" else "话题工作区"
        workspace_name = Path(
            str(payload.get("effective_workspace_dir") or "")
        ).name
        return f"{mode} · {workspace_name}" if workspace_name else mode

    if event.kind == "chat.turn.started":
        preview = _short_text(str(payload.get("text_preview") or ""))
        return f"文本：{preview}" if preview else "已进入当前轮运行"

    if event.kind == "chat.model.routing_decision":
        provider = str(payload.get("selected_provider") or "").strip()
        model = str(payload.get("selected_model") or "").strip()
        route = str(payload.get("route") or "").strip()
        reasons = payload.get("reasons")
        inherited = bool(payload.get("previous_route_inherited"))
        parts: list[str] = []
        if provider or model:
            parts.append("/".join(part for part in (provider, model) if part))
        if route:
            parts.append("云端" if route == "cloud" else "本地")
        if inherited:
            parts.append("沿用上一轮")
        if isinstance(reasons, list) and reasons:
            parts.append(_short_text(str(reasons[0]), 48))
        return " · ".join(parts) or "已记录本轮模型选择"

    if event.kind == "chat.turn.stop_requested":
        requested = str(payload.get("requested_chat_id") or "")
        resolved = str(payload.get("resolved_chat_id") or "")
        if requested and resolved and requested != resolved:
            return "停止请求已转到当前会话"
        return "已请求停止当前运行"

    if event.kind == "chat.turn.continue_requested":
        mode = str(payload.get("mode") or "").strip()
        labels = {
            "reconnect": "重新连接",
            "retry": "重试",
            "resume": "恢复",
            "takeover": "接管",
        }
        reason = str(payload.get("reason") or "").strip()
        label = labels.get(mode, "继续")
        return f"{label} · {_short_text(reason, 56)}" if reason else label

    if event.kind == "chat.turn.settled":
        count = int(payload.get("active_run_event_count") or 0)
        final_status = payload.get("final_task_status")
        if final_status == "interrupted":
            prefix = "运行已中断"
        elif final_status == "failed":
            prefix = "运行失败"
        elif final_status == "stalled":
            prefix = "运行已卡住"
        else:
            prefix = "运行已结束"
        if count > 0:
            return f"{prefix} · {count} 条流式事件"
        return prefix

    if event.kind == "chat.system.event" and (
        event.status == "stalled" or event.type == "turn.stalled"
    ):
        detail = str(payload.get("trace_detail") or "").strip()
        if detail:
            return detail
        elapsed = payload.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)) and elapsed > 0:
            return f"已 {int(elapsed)} 秒没有新的运行事件"
        return "后端没有找到仍在执行的活动任务"

    if event.kind.startswith("chat.tool"):
        sub_run = payload.get("sub_run")
        is_subtask = (
            payload.get("phase") == "subtask"
            or isinstance(sub_run, dict)
        )
        if is_subtask:
            sub_payload = sub_run if isinstance(sub_run, dict) else {}
            agent_name = str(
                payload.get("agent_name")
                or sub_payload.get("agent_name")
                or payload.get("tool_name")
                or "子任务",
            ).strip()
            agent_role = str(
                payload.get("agent_role")
                or sub_payload.get("agent_role")
                or "",
            ).strip()
            delegation_reason = str(
                payload.get("delegation_reason")
                or sub_payload.get("delegation_reason")
                or "",
            ).strip()
            current_tool = str(
                payload.get("current_tool")
                or sub_payload.get("current_tool")
                or payload.get("tool_name")
                or "",
            ).strip()
            result_summary = str(
                payload.get("result_summary")
                or sub_payload.get("result_summary")
                or payload.get("output_summary")
                or "",
            ).strip()
            failure_reason = str(
                payload.get("failure_reason")
                or sub_payload.get("failure_reason")
                or payload.get("error")
                or "",
            ).strip()
            parts = [agent_name]
            if agent_role:
                parts.append(_short_text(agent_role, 36))
            if delegation_reason:
                parts.append(_short_text(delegation_reason, 48))
            if current_tool:
                parts.append(f"当前工具 {current_tool}")
            if failure_reason:
                parts.append(f"失败：{_short_text(failure_reason, 48)}")
            elif result_summary:
                parts.append(_short_text(result_summary, 56))
            return " · ".join(part for part in parts if part)

        tool_name = str(payload.get("tool_name") or "工具").strip()
        command = str(payload.get("command") or "").strip()
        cwd = str(payload.get("cwd") or "").strip()
        files_changed = payload.get("files_changed")
        error = str(payload.get("error") or "").strip()
        result_preview = str(payload.get("result_preview") or "").strip()
        if command:
            head = command.split()[0] if command.split() else command
            parts = [f"命令：{_short_text(head, 36)}"]
            if cwd:
                parts.append(Path(cwd).name or cwd)
            if payload.get("exit_code") is not None:
                parts.append(f"退出码 {payload.get('exit_code')}")
            if error:
                parts.append(f"失败：{_short_text(error, 48)}")
            return " · ".join(parts)
        if isinstance(files_changed, list) and files_changed:
            count = len(files_changed)
            first = files_changed[0]
            first_path = (
                first.get("path")
                if isinstance(first, dict)
                else str(first)
            )
            label = Path(str(first_path or "")).name or "文件"
            suffix = f" 等 {count} 个文件" if count > 1 else ""
            return f"文件变更：{label}{suffix}"
        if error:
            return f"{tool_name} · 失败：{_short_text(error, 64)}"
        if result_preview:
            return f"{tool_name} · {_short_text(result_preview, 72)}"
        return tool_name

    if event.kind.startswith("chat.approval."):
        tool_name = str(payload.get("tool_name") or "工具").strip()
        severity = str(payload.get("severity") or "").strip()
        decision = str(payload.get("decision") or "").strip()
        findings = payload.get("findings_count")
        parts = [tool_name]
        if severity:
            parts.append(f"风险 {severity}")
        if findings:
            parts.append(f"{findings} 条命中")
        if decision:
            parts.append(f"结果 {decision}")
        return " · ".join(parts)

    if event.kind == "cron.job.started":
        payload = event.payload or {}
        return f"{payload.get('job_name') or payload.get('job_id') or '定时任务'} · {payload.get('task_type') or 'agent'}"

    if event.kind == "cron.job.settled":
        payload = event.payload or {}
        final_status = str(payload.get("final_status") or "")
        label = payload.get("job_name") or payload.get("job_id") or "定时任务"
        if final_status == "error" and payload.get("error_text"):
            return f"{label} · 失败：{payload.get('error_text')}"
        if final_status == "cancelled":
            return f"{label} · 已取消"
        return f"{label} · 已完成"

    if event.kind == "heartbeat.run.started":
        payload = event.payload or {}
        target = str(payload.get("target") or "main")
        return f"目标：{target}"

    if event.kind == "heartbeat.run.settled":
        payload = event.payload or {}
        final_status = str(payload.get("final_status") or "")
        reason = str(payload.get("result_reason") or "")
        if final_status == "skipped" and reason:
            return f"已跳过 · {reason}"
        if final_status == "timeout":
            return "已超时"
        if final_status == "error":
            return f"失败 · {reason}" if reason else "失败"
        if final_status == "cancelled":
            return "已取消"
        return "已完成"

    if event.kind == "image.task.started":
        payload = event.payload or {}
        parts: list[str] = []
        model = str(payload.get("model") or "").strip()
        quantity = payload.get("quantity")
        size = str(payload.get("size") or "").strip()
        quality = str(payload.get("quality") or "").strip()
        if model:
            parts.append(model)
        if quantity:
            parts.append(f"{quantity} 张")
        if size:
            parts.append(size)
        if quality:
            parts.append(quality)
        return " · ".join(parts) if parts else "历史任务记录"

    if event.kind == "image.task.settled":
        payload = event.payload or {}
        final_status = str(payload.get("final_status") or "")
        error_text = str(payload.get("error_text") or "")
        if final_status in {"failed", "failure", "error", "rejected"}:
            return f"失败 · {error_text}" if error_text else "失败"
        if final_status in IMAGE_PENDING_STATUSES:
            return "生成中"
        if final_status in {"cancelled", "canceled", "interrupted"}:
            return "已取消"

        parts = ["已完成"]
        asset_count = payload.get("asset_count")
        if asset_count:
            parts.append(f"{asset_count} 张")
        usage = _usage_for_payload(payload, event.usage) or {}
        cost_points = usage.get("cost_points")
        if cost_points:
            parts.append(f"{cost_points:g} 点")
        remaining_points = payload.get("remaining_points")
        if remaining_points is not None:
            parts.append(f"剩余 {remaining_points:g} 点")
        return " · ".join(parts)

    if event.kind == "skills.changed":
        payload = event.payload or {}
        skill_name = str(payload.get("skill_name") or "技能库")
        change_type = str(payload.get("change_type") or "changed")
        source = str(payload.get("source") or "workspace")
        return f"{skill_name} · {source} · {change_type}"

    return _short_text(event.summary)


def _open_ledger_read_fd(path: Path) -> int:
    return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))


def _iter_events_from_file_from_offset(
    path: Path,
    *,
    offset: int = 0,
) -> list[ChatExperienceEvent]:
    return list(_stream_events_from_file_from_offset(path, offset=offset))


def _stream_events_from_file_from_offset(
    path: Path,
    *,
    offset: int = 0,
) -> Iterator[ChatExperienceEvent]:
    if not path.exists():
        return

    try:
        with os.fdopen(_open_ledger_read_fd(path), "rb") as handle:
            if offset > 0:
                handle.seek(offset)
            while True:
                line = handle.readline()
                if not line:
                    break
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                    yield ChatExperienceEvent.model_validate(payload)
                except Exception:
                    logger.warning("Failed to parse experience event line")
    except FileNotFoundError:
        # The single writer may roll the active ledger after discovery but
        # before this reader opens it. Missing one snapshot is recoverable;
        # failing the chat list or SSE stream is not.
        return


def _event_from_file_at_offset(
    path: Path,
    offset: int,
) -> ChatExperienceEvent | None:
    if not path.exists():
        return None

    try:
        with os.fdopen(_open_ledger_read_fd(path), "rb") as handle:
            handle.seek(max(0, offset))
            raw = handle.readline().strip()
            if not raw:
                return None
            try:
                return ChatExperienceEvent.model_validate(json.loads(raw))
            except Exception:
                logger.warning("Failed to parse experience event line")
                return None
    except FileNotFoundError:
        return None


def _iter_events_from_file(path: Path) -> list[ChatExperienceEvent]:
    return _iter_events_from_file_from_offset(path)


def _rebuild_event_index(path: Path) -> _EventLedgerIndex:
    latest_seq = 0
    max_seq = 0
    line_count = 0
    offset_by_seq: dict[int, int] = {}
    offsets_by_turn_id: dict[str, list[int]] = {}
    ledger_size = path.stat().st_size if path.exists() else 0

    if not path.exists():
        return _EventLedgerIndex(ledger_size=0)

    try:
        with os.fdopen(_open_ledger_read_fd(path), "rb") as handle:
            while True:
                offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                line_count += 1
                fallback_seq = line_count
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                    event = ChatExperienceEvent.model_validate(payload)
                    seq = int(event.seq or fallback_seq)
                    latest_seq = max(latest_seq, seq)
                    offset_by_seq.setdefault(seq, offset)
                    turn_id = _turn_id_for_event(event)
                    if turn_id:
                        offsets_by_turn_id.setdefault(turn_id, []).append(offset)
                except Exception:
                    logger.warning("Failed to parse experience event line")
                    seq = fallback_seq
                max_seq = max(max_seq, seq)
    except FileNotFoundError:
        return _EventLedgerIndex(ledger_size=0)

    return _EventLedgerIndex(
        ledger_size=ledger_size,
        line_count=line_count,
        latest_seq=latest_seq,
        next_seq=max_seq + 1 if max_seq else 1,
        offset_by_seq=offset_by_seq,
        offsets_by_turn_id=offsets_by_turn_id,
    )


def _write_event_index(path: Path, index: _EventLedgerIndex) -> None:
    write_text_atomic_no_follow(
        _event_index_path(path),
        _json_dumps(index.model_dump(mode="json")),
    )
    _remember_event_index(path, index)


def _safe_write_event_index(path: Path, index: _EventLedgerIndex) -> None:
    try:
        _write_event_index(path, index)
    except Exception:
        logger.warning("Failed to write experience event index", exc_info=True)


def _event_index_cache_key(path: Path) -> str:
    return os.path.abspath(os.fspath(path.expanduser()))


def _event_index_file_signature(
    path: Path,
) -> tuple[int, int, int, int] | None:
    try:
        stat = os.lstat(path)
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _remember_event_index(path: Path, index: _EventLedgerIndex) -> None:
    key = _event_index_cache_key(path)
    with _EVENT_INDEX_CACHE_LOCK:
        _EVENT_INDEX_CACHE[key] = (
            _event_index_file_signature(path),
            _event_index_file_signature(_event_index_path(path)),
            index,
        )
        _EVENT_INDEX_CACHE.move_to_end(key)
        while len(_EVENT_INDEX_CACHE) > EVENT_INDEX_CACHE_MAX_ENTRIES:
            _EVENT_INDEX_CACHE.popitem(last=False)


def _cached_event_index(path: Path) -> _EventLedgerIndex | None:
    key = _event_index_cache_key(path)
    with _EVENT_INDEX_CACHE_LOCK:
        cached = _EVENT_INDEX_CACHE.get(key)
        if cached is None:
            return None
        ledger_signature, index_signature, index = cached
        if (
            ledger_signature != _event_index_file_signature(path)
            or index_signature
            != _event_index_file_signature(_event_index_path(path))
        ):
            _EVENT_INDEX_CACHE.pop(key, None)
            return None
        _EVENT_INDEX_CACHE.move_to_end(key)
        return index


def _load_event_index(path: Path) -> _EventLedgerIndex:
    cached = _cached_event_index(path)
    if cached is not None:
        return cached
    raw = json.loads(read_text_no_follow(_event_index_path(path)))
    index = _EventLedgerIndex.model_validate(raw)
    if index.writer_version != EVENT_LEDGER_INDEX_WRITER_VERSION:
        raise ValueError("event ledger index writer is stale")
    ledger_size = path.stat().st_size if path.exists() else 0
    if index.ledger_size != ledger_size:
        raise ValueError("event ledger index is stale")
    _remember_event_index(path, index)
    return index


def _load_or_rebuild_event_index(
    path: Path,
    *,
    persist_rebuild: bool = False,
) -> _EventLedgerIndex:
    """Load an index without allowing readers to race the single writer.

    Chat previews and SSE readers run in worker threads. They may observe a
    sidecar between ledger append and index replacement, so a stale index is
    rebuilt in memory. Only append/roll code is allowed to persist that
    rebuild; reader-side writes previously corrupted active and rolled
    indexes under load.
    """

    try:
        return _load_event_index(path)
    except FileNotFoundError:
        pass
    except (ValueError, json.JSONDecodeError):
        logger.debug("Rebuilding stale experience event index", exc_info=True)
    except Exception:
        logger.warning("Rebuilding unreadable experience event index", exc_info=True)

    index = _rebuild_event_index(path)
    if persist_rebuild:
        _safe_write_event_index(path, index)
    return index


def _segment_file_name(start_seq: int, latest_seq: int) -> str:
    return f"events.{start_seq:012d}-{latest_seq:012d}.jsonl"


def _segment_path(active_path: Path, segment: _EventLedgerSegment) -> Path:
    if "/" in segment.path or "\\" in segment.path:
        raise ValueError("Event ledger segment path must be a filename")
    return active_path.with_name(segment.path)


def _discover_event_segments(active_path: Path) -> list[_EventLedgerSegment]:
    segments: list[_EventLedgerSegment] = []
    for candidate in active_path.parent.glob("events.*-*.jsonl"):
        if candidate.name == active_path.name or not candidate.is_file():
            continue
        index = _load_or_rebuild_event_index(candidate)
        if index.latest_seq <= 0:
            continue
        start_seq = min(index.offset_by_seq) if index.offset_by_seq else index.latest_seq
        segments.append(
            _EventLedgerSegment(
                path=candidate.name,
                start_seq=start_seq,
                latest_seq=index.latest_seq,
                ledger_size=index.ledger_size,
            )
        )
    segments.sort(key=lambda item: (item.start_seq, item.latest_seq, item.path))
    return segments


def _load_segments_manifest(active_path: Path) -> _EventLedgerSegmentsManifest:
    raw = json.loads(read_text_no_follow(_event_segments_manifest_path(active_path)))
    manifest = _EventLedgerSegmentsManifest.model_validate(raw)
    if manifest.active != EVENTS_FILE_NAME:
        raise ValueError("event ledger manifest active file mismatch")
    return manifest


def _write_segments_manifest(
    active_path: Path,
    manifest: _EventLedgerSegmentsManifest,
) -> None:
    write_text_atomic_no_follow(
        _event_segments_manifest_path(active_path),
        _json_dumps(manifest.model_dump(mode="json")),
    )


def _safe_write_segments_manifest(
    active_path: Path,
    manifest: _EventLedgerSegmentsManifest,
) -> None:
    try:
        _write_segments_manifest(active_path, manifest)
    except Exception:
        logger.warning("Failed to write experience event segments", exc_info=True)


def _load_or_rebuild_segments_manifest(
    active_path: Path,
    *,
    persist_rebuild: bool = False,
) -> _EventLedgerSegmentsManifest:
    try:
        return _load_segments_manifest(active_path)
    except FileNotFoundError:
        pass
    except (ValueError, json.JSONDecodeError):
        logger.debug("Rebuilding experience event segments manifest", exc_info=True)
    except Exception:
        logger.warning("Rebuilding unreadable event segments manifest", exc_info=True)

    manifest = _EventLedgerSegmentsManifest(
        segments=_discover_event_segments(active_path),
    )
    if persist_rebuild and manifest.segments:
        _safe_write_segments_manifest(active_path, manifest)
    return manifest


def _latest_segment_seq(
    active_path: Path,
    *,
    persist_rebuild: bool = False,
) -> int:
    manifest = _load_or_rebuild_segments_manifest(
        active_path,
        persist_rebuild=persist_rebuild,
    )
    latest = 0
    for segment in manifest.segments:
        latest = max(latest, segment.latest_seq)
    return latest


def _load_active_event_index_for_append(active_path: Path) -> _EventLedgerIndex:
    index = _load_or_rebuild_event_index(active_path, persist_rebuild=True)
    if index.line_count > 0 or index.next_seq > 1:
        return index
    next_seq = max(
        index.next_seq,
        _latest_segment_seq(active_path, persist_rebuild=True) + 1,
    )
    if next_seq == index.next_seq:
        return index
    updated = index.model_copy(update={"next_seq": next_seq})
    _safe_write_event_index(active_path, updated)
    return updated


def _roll_event_ledger_if_needed(active_path: Path) -> None:
    if not active_path.exists():
        return
    try:
        size = active_path.stat().st_size
    except OSError:
        return
    if size < EVENT_LEDGER_SEGMENT_MAX_BYTES:
        return

    index = _load_or_rebuild_event_index(active_path, persist_rebuild=True)
    if index.latest_seq <= 0:
        return

    start_seq = min(index.offset_by_seq) if index.offset_by_seq else index.latest_seq
    segment_name = _segment_file_name(start_seq, index.latest_seq)
    segment_path = active_path.with_name(segment_name)
    if segment_path.exists():
        logger.warning("Event ledger segment already exists: %s", segment_path)
        return

    os.replace(active_path, segment_path)
    active_index_path = _event_index_path(active_path)
    segment_index_path = _event_index_path(segment_path)
    if active_index_path.exists():
        os.replace(active_index_path, segment_index_path)
    else:
        _safe_write_event_index(segment_path, index)

    manifest = _load_or_rebuild_segments_manifest(
        active_path,
        persist_rebuild=True,
    )
    segment = _EventLedgerSegment(
        path=segment_name,
        start_seq=start_seq,
        latest_seq=index.latest_seq,
        ledger_size=index.ledger_size,
    )
    if all(existing.path != segment.path for existing in manifest.segments):
        manifest.segments.append(segment)
    manifest.segments.sort(key=lambda item: (item.start_seq, item.latest_seq, item.path))
    _safe_write_segments_manifest(active_path, manifest)
    _safe_write_event_index(
        active_path,
        _EventLedgerIndex(next_seq=max(index.next_seq, index.latest_seq + 1)),
    )


def _event_ledger_paths_for_active_path(active_path: Path) -> list[Path]:
    manifest = _load_or_rebuild_segments_manifest(active_path)
    paths: list[Path] = []
    for segment in manifest.segments:
        try:
            segment_path = _segment_path(active_path, segment)
        except ValueError:
            continue
        if segment_path.exists():
            paths.append(segment_path)
    if active_path.exists():
        paths.append(active_path)
    return paths


def _event_ledger_paths_for_active_path_after_seq(
    active_path: Path,
    after_seq: int,
) -> list[Path]:
    manifest = _load_or_rebuild_segments_manifest(active_path)
    paths: list[Path] = []
    for segment in manifest.segments:
        if segment.latest_seq <= after_seq:
            continue
        try:
            segment_path = _segment_path(active_path, segment)
        except ValueError:
            continue
        if segment_path.exists():
            paths.append(segment_path)
    if active_path.exists():
        paths.append(active_path)
    return paths


def _unique_event_ledger_paths_for_active_paths(active_paths: list[Path]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for active_path in active_paths:
        for path in _event_ledger_paths_for_active_path(active_path):
            key = str(path.expanduser().resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def _event_ledger_paths_for_workspace(workspace_dir: Path | str) -> list[Path]:
    return _unique_event_ledger_paths_for_active_paths(
        [
            events_path_for_workspace(workspace_dir),
            legacy_events_path_for_workspace(workspace_dir),
        ]
    )


def _event_ledger_paths_for_workspace_after_seq(
    workspace_dir: Path | str,
    after_seq: int,
) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for active_path in (
        events_path_for_workspace(workspace_dir),
        legacy_events_path_for_workspace(workspace_dir),
    ):
        for path in _event_ledger_paths_for_active_path_after_seq(
            active_path,
            after_seq,
        ):
            key = str(path.expanduser().resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def _iter_events_for_workspace(workspace_dir: Path | str) -> list[ChatExperienceEvent]:
    events: list[ChatExperienceEvent] = []
    for path in _event_ledger_paths_for_workspace(workspace_dir):
        events.extend(_iter_events_from_file(path))
    return events


def collect_workspace_events(
    workspace_dir: Path | str,
) -> list[ChatExperienceEvent]:
    """Return all persisted runtime events for one workspace.

    This is the supported read-only entry point for product surfaces that
    aggregate the runtime ledger (for example the operations statistics
    page).  Keeping the ledger layout private prevents callers from depending
    on active/rolled segment file names.
    """

    return _iter_events_for_workspace(workspace_dir)


def _iter_events_for_turn_from_active_path(
    active_path: Path,
    turn_id: str,
    *,
    candidate_is_start: bool = False,
) -> list[ChatExperienceEvent]:
    if not active_path.exists():
        return []

    events: list[ChatExperienceEvent] = []
    active_index = _load_or_rebuild_event_index(active_path)
    for offset in active_index.offsets_by_turn_id.get(turn_id, []):
        event = _event_from_file_at_offset(active_path, offset)
        if event is not None and _turn_id_for_event(event) == turn_id:
            events.append(event)

    if candidate_is_start:
        events.sort(key=lambda item: item.seq)
        return events

    if any(event.type == "turn.started" for event in events):
        events.sort(key=lambda item: item.seq)
        return events

    for path in _event_ledger_paths_for_active_path(active_path):
        if path == active_path:
            continue
        index = _load_or_rebuild_event_index(path)
        for offset in index.offsets_by_turn_id.get(turn_id, []):
            event = _event_from_file_at_offset(path, offset)
            if event is not None and _turn_id_for_event(event) == turn_id:
                events.append(event)
    events.sort(key=lambda item: item.seq)
    return events


def _iter_events_for_turn_from_workspace(
    workspace_dir: Path | str,
    turn_id: str,
    *,
    candidate_is_start: bool = False,
) -> list[ChatExperienceEvent]:
    events: list[ChatExperienceEvent] = []
    for active_path in (
        events_path_for_workspace(workspace_dir),
        legacy_events_path_for_workspace(workspace_dir),
    ):
        events.extend(
            _iter_events_for_turn_from_active_path(
                active_path,
                turn_id,
                candidate_is_start=candidate_is_start,
            )
        )
    events.sort(key=lambda item: item.seq)
    return events


def _event_index_after_append(
    index: _EventLedgerIndex,
    *,
    event: ChatExperienceEvent,
    offset: int,
    ledger_size: int,
) -> _EventLedgerIndex:
    offset_by_seq = dict(index.offset_by_seq)
    offset_by_seq.setdefault(event.seq, offset)
    offsets_by_turn_id = {
        turn_id: list(offsets)
        for turn_id, offsets in index.offsets_by_turn_id.items()
    }
    turn_id = _turn_id_for_event(event)
    if turn_id:
        offsets_by_turn_id.setdefault(turn_id, []).append(offset)
    return _EventLedgerIndex(
        ledger_size=ledger_size,
        line_count=index.line_count + 1,
        latest_seq=max(index.latest_seq, event.seq),
        next_seq=max(index.next_seq, event.seq + 1),
        offset_by_seq=offset_by_seq,
        offsets_by_turn_id=offsets_by_turn_id,
    )


def _iter_events_from_file_after_seq(
    path: Path,
    after_seq: int,
) -> list[ChatExperienceEvent]:
    return list(_stream_events_from_file_after_seq(path, after_seq))


def _stream_events_from_file_after_seq(
    path: Path,
    after_seq: int,
) -> Iterator[ChatExperienceEvent]:
    if not path.exists():
        return

    index = _load_or_rebuild_event_index(path)
    normalized_after_seq = int(after_seq or 0)
    if normalized_after_seq >= index.latest_seq:
        return

    first_offset = min(
        (
            offset
            for seq, offset in index.offset_by_seq.items()
            if seq > normalized_after_seq
        ),
        default=None,
    )
    if first_offset is None:
        return

    for event in _stream_events_from_file_from_offset(
        path, offset=max(0, first_offset),
    ):
        if event.seq > normalized_after_seq:
            yield event


def _next_seq_from_file(path: Path) -> int:
    """Return the next monotonic sequence for a ledger file."""

    return _load_or_rebuild_event_index(path).next_seq


def _next_seq_for_workspace_append(workspace_dir: Path | str) -> int:
    next_seq = 1
    for path in _event_ledger_paths_for_workspace(workspace_dir):
        try:
            next_seq = max(next_seq, _load_or_rebuild_event_index(path).latest_seq + 1)
        except Exception:
            continue
    return next_seq


def _normalized_event_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    return max(1, int(limit))


def _runtime_event_stream_metadata(
    workspace_dir: Path | str,
) -> tuple[str, int, int]:
    """Return a stable ledger generation plus its available sequence bounds."""

    earliest_event: ChatExperienceEvent | None = None
    earliest_seq = 0
    latest_seq = 0
    for path in _event_ledger_paths_for_workspace(workspace_dir):
        try:
            index = _load_or_rebuild_event_index(path)
        except Exception:
            continue
        latest_seq = max(latest_seq, index.latest_seq)
        if not index.offset_by_seq:
            continue
        candidate_seq = min(index.offset_by_seq)
        candidate = _event_from_file_at_offset(
            path,
            index.offset_by_seq[candidate_seq],
        )
        if candidate is None:
            continue
        if earliest_event is None or (candidate.seq, candidate.id) < (
            earliest_event.seq,
            earliest_event.id,
        ):
            earliest_event = candidate

    if earliest_event is not None:
        earliest_seq = earliest_event.seq
        anchor = f"{earliest_event.seq}:{earliest_event.id}"
    else:
        resolved = Path(workspace_dir).expanduser().resolve(strict=False)
        anchor = f"empty:{resolved}"
    epoch = hashlib.sha256(
        f"lantai-runtime-event-stream-v1:{anchor}".encode("utf-8")
    ).hexdigest()[:24]
    return epoch, earliest_seq, latest_seq


def _attempt_id_for_event(event: ChatExperienceEvent) -> str:
    payload = event.payload or {}
    for key in (
        "attempt_id",
        "requested_attempt_id",
        "source_attempt_id",
        "target_attempt_id",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _event_matches_attempt(
    event: ChatExperienceEvent,
    attempt_id: str | None,
) -> bool:
    if not attempt_id:
        return True
    return _attempt_id_for_event(event) == attempt_id


def build_runtime_events_response(
    workspace_dir: Path | str,
    chat_id: str,
    *,
    after_seq: int = 0,
    limit: int | None = None,
    attempt_id: str | None = None,
    event_types: set[str] | None = None,
    expected_stream_epoch: str | None = None,
) -> RuntimeEventsResponse:
    """Build RuntimeEvent v1 rows for one chat ledger."""

    requested_after_seq = int(after_seq or 0)
    stream_epoch, earliest_seq, ledger_latest_seq = _runtime_event_stream_metadata(
        workspace_dir
    )
    normalized_expected_epoch = str(expected_stream_epoch or "").strip()
    resync_reason: str | None = None
    if normalized_expected_epoch and normalized_expected_epoch != stream_epoch:
        resync_reason = "stream_epoch_mismatch"
    elif requested_after_seq > ledger_latest_seq:
        resync_reason = "cursor_ahead_of_ledger"
    elif (
        requested_after_seq > 0
        and earliest_seq > 0
        and requested_after_seq < earliest_seq - 1
    ):
        resync_reason = "history_gap"
    effective_after_seq = 0 if resync_reason else requested_after_seq

    matching_events: list[ChatExperienceEvent] = []
    matching_count = 0
    latest_seq = effective_after_seq
    normalized_limit = _normalized_event_limit(limit)
    normalized_event_types = {
        str(event_type).strip()
        for event_type in (event_types or set())
        if str(event_type).strip()
    }
    for path in _event_ledger_paths_for_workspace_after_seq(
        workspace_dir,
        effective_after_seq,
    ):
        for event in _stream_events_from_file_after_seq(path, effective_after_seq):
            if event.chat_id != chat_id:
                continue
            if not _event_matches_attempt(event, attempt_id):
                continue
            if normalized_event_types and event.type not in normalized_event_types:
                continue
            matching_count += 1
            if normalized_limit is None:
                matching_events.append(event)
                continue
            # Canonical and legacy files may overlap or contain out-of-order
            # rows. Retain the earliest page while scanning, preserving every
            # row at its boundary seq so reconnect never skips an equal seq.
            if (
                len(matching_events) >= normalized_limit
                and event.seq > matching_events[-1].seq
            ):
                continue
            position = bisect_right(
                matching_events, event.seq, key=lambda row: row.seq,
            )
            matching_events.insert(position, event)
            if len(matching_events) > normalized_limit:
                boundary = matching_events[normalized_limit - 1].seq
                end = bisect_right(
                    matching_events, boundary, key=lambda row: row.seq,
                )
                del matching_events[end:]

    matching_events.sort(key=lambda item: item.seq)
    selected_events = matching_events
    has_more = matching_count > len(selected_events)
    for event in selected_events:
        latest_seq = max(latest_seq, event.seq)
    return RuntimeEventsResponse(
        chat_id=chat_id,
        workspace_dir=str(Path(workspace_dir).expanduser()),
        stream_epoch=stream_epoch,
        cursor=f"{stream_epoch}:{latest_seq}",
        after_seq=requested_after_seq,
        earliest_seq=earliest_seq,
        ledger_latest_seq=ledger_latest_seq,
        latest_seq=latest_seq,
        limit=normalized_limit,
        next_after_seq=latest_seq,
        has_more=has_more,
        resync_required=resync_reason is not None,
        resync_reason=resync_reason,
        events=[_event_to_runtime(event) for event in selected_events],
    )


def iter_runtime_events_through_seq(
    workspace_dir: Path | str,
    chat_id: str,
    through_seq: int,
) -> Iterator[RuntimeEvent]:
    """Replay a prefix in stable seq order without retaining event bodies.

    Only (seq, file order, byte offset, line length) references are sorted.
    Each row is parsed at most twice, including disordered legacy ledgers.
    Pin the two mutable active files across rotation; immutable segments use
    one reusable reader, avoiding one open descriptor per historical segment.
    The caller must close this iterator if it stops consuming early.
    """
    if through_seq <= 0:
        return
    active_paths = {
        events_path_for_workspace(workspace_dir),
        legacy_events_path_for_workspace(workspace_dir),
    }
    paths = _event_ledger_paths_for_workspace_after_seq(workspace_dir, 0)
    references: list[tuple[int, int, int, int]] = []
    signatures: dict[int, tuple[int, int, int, int]] = {}
    pinned = {}
    segment_reader = None
    segment_index = None
    with ExitStack() as stack:
        try:
            for file_index, path in enumerate(paths):
                try:
                    handle = os.fdopen(_open_ledger_read_fd(path), "rb")
                except FileNotFoundError:
                    # Same discovery/rotation race as the normal event reader.
                    continue
                if path in active_paths:
                    pinned[file_index] = stack.enter_context(handle)
                with nullcontext(handle) if path in active_paths else handle:
                    stat = os.fstat(handle.fileno())
                    signatures[file_index] = (
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_size,
                        stat.st_mtime_ns,
                    )
                    while (offset := handle.tell()) < stat.st_size:
                        line = handle.readline(stat.st_size - offset)
                        if not line:
                            break
                        if not line.strip():
                            continue
                        try:
                            event = ChatExperienceEvent.model_validate(
                                json.loads(line),
                            )
                        except Exception:
                            logger.warning(
                                "Failed to parse experience event line"
                            )
                            continue
                        if (
                            event.chat_id == chat_id
                            and 0 < event.seq <= through_seq
                        ):
                            references.append(
                                (event.seq, file_index, offset, len(line))
                            )
            references.sort()
            for _seq, file_index, offset, length in references:
                handle = pinned.get(file_index)
                if handle is None:
                    if segment_index != file_index:
                        if segment_reader is not None:
                            segment_reader.close()
                        segment_reader = os.fdopen(
                            _open_ledger_read_fd(paths[file_index]),
                            "rb",
                        )
                        segment_index = file_index
                        stat = os.fstat(segment_reader.fileno())
                        if (
                            stat.st_dev,
                            stat.st_ino,
                            stat.st_size,
                            stat.st_mtime_ns,
                        ) != signatures[file_index]:
                            raise RuntimeError(
                                "Immutable event segment changed during replay"
                            )
                    handle = segment_reader
                handle.seek(offset)
                event = ChatExperienceEvent.model_validate(
                    json.loads(handle.read(length)),
                )
                yield _event_to_runtime(event)
        finally:
            if segment_reader is not None:
                segment_reader.close()


def _parse_event_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _run_key_for_event(event: ChatExperienceEvent) -> str:
    for value in (event.run_id, event.chat_id, event.session_id, event.id):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return event.id


def _parent_run_id_for_event(event: ChatExperienceEvent) -> str | None:
    payload = event.payload or {}
    for key in ("parent_run_id", "source_run_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    sub_run = payload.get("sub_run")
    if isinstance(sub_run, dict):
        value = sub_run.get("parent_run_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _group_run_key_for_event(event: ChatExperienceEvent) -> str:
    """Return the run bucket key, folding child agent events into parents."""

    return _parent_run_id_for_event(event) or _run_key_for_event(event)


def _run_source_for_events(events: list[ChatExperienceEvent]) -> str:
    sources: set[str] = set()
    for event in events:
        kind = event.kind
        if kind.startswith("cron."):
            sources.add("cron")
        elif kind.startswith("heartbeat."):
            sources.add("heartbeat")
        elif kind.startswith("image."):
            sources.add("image")
        elif kind == "skills.changed":
            sources.add("skills")
        elif kind.startswith("chat.approval."):
            sources.add("approval")
        elif kind.startswith("chat.tool"):
            sources.add("tool")
        elif kind.startswith("chat."):
            sources.add("chat")
    for source in (
        "chat",
        "image",
        "cron",
        "heartbeat",
        "skills",
        "approval",
        "tool",
    ):
        if source in sources:
            return source
    return "system"


_RUN_STATUS_PRIORITY = {
    "unknown": 0,
    "idle": 1,
    "running": 2,
    "completed": 3,
    "interrupted": 4,
    "stalled": 5,
    "failed": 6,
}


def _run_status_candidate_for_event(event: ChatExperienceEvent) -> str:
    if event.kind == "chat.turn.stop_requested":
        return "running"
    if event.status == "failed":
        return "failed"
    if event.status == "stalled":
        return "stalled"
    if event.status == "interrupted":
        return "interrupted"
    if event.status in {"finished", "completed"}:
        return "completed"
    if event.status == "running":
        return "running"
    if event.status == "info":
        return "idle"
    return "unknown"


def _run_status_for_events(events: list[ChatExperienceEvent]) -> str:
    if not events:
        return "unknown"
    status = "unknown"
    for event in events:
        candidate = _run_status_candidate_for_event(event)
        if _RUN_STATUS_PRIORITY[candidate] > _RUN_STATUS_PRIORITY[status]:
            status = candidate
    return status


def _runtime_status_for_contract(contract: RuntimeRunContract) -> str:
    return {
        "queued": "running",
        "claimed": "running",
        "running": "running",
        "waiting_approval": "running",
        "paused": "stalled",
        "stop_requested": "interrupted",
        "succeeded": "completed",
        "failed": "failed",
        "cancelled": "interrupted",
        "stalled": "stalled",
    }.get(contract.status, "unknown")


def _contract_datetime(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _contract_budget_summary(contract: RuntimeRunContract) -> dict[str, Any]:
    return {
        key: value
        for key, value in contract.budget.model_dump(mode="json").items()
        if value is not None
    }


def _contract_step_summary(contract: RuntimeRunContract) -> dict[str, Any] | None:
    if contract.current_step is None:
        return None
    return contract.current_step.model_dump(mode="json")


def _contract_checkpoint_summary(
    contract: RuntimeRunContract,
) -> dict[str, Any] | None:
    if contract.checkpoint is None:
        return None
    return contract.checkpoint.model_dump(mode="json")


def _contract_reason_summary(contract: RuntimeRunContract) -> dict[str, Any] | None:
    reason = contract.error or contract.stop_reason
    if reason is None:
        return None
    return reason.model_dump(mode="json")


def _contract_recovery_summary(contract: RuntimeRunContract) -> dict[str, Any]:
    reason = _contract_reason_summary(contract)
    recoverable = contract.status in {"paused", "stalled", "waiting_approval"}
    can_resume = (
        contract.source == "browser"
        and contract.status in {"paused", "waiting_approval"}
    )
    return {
        "can_pause": contract.source == "browser"
        and contract.status in {"running", "waiting_approval"},
        "can_resume": can_resume,
        "can_takeover": recoverable,
        "resume_requires_durable_lease": recoverable and not can_resume,
        "reason_code": reason.get("code") if reason else None,
        "reason_message": reason.get("message") if reason else None,
    }


def _conservative_effect_recovery(
    effect: RuntimeEffectRecord,
) -> RuntimeEffectRecoveryCapability:
    """Return the safest known recovery declaration for a persisted effect."""

    if effect.source == "browser" and effect.tool_name.startswith("browser."):
        action_name = effect.tool_name.split(".", 1)[1] or "status"
        try:
            from ..browser_workbench.effects import browser_action_effect_recovery
            from ..browser_workbench.models import BrowserAction

            return browser_action_effect_recovery(BrowserAction(action=action_name))
        except Exception:  # pragma: no cover - defensive projection fallback
            pass
    return RuntimeEffectRecoveryCapability(
        external_side_effect=True,
        idempotency="not_idempotent",
        probe="none",
        compensation="none",
        on_ambiguity="manual_disambiguation",
    )


def _latest_effects_by_id(
    effects: list[RuntimeEffectRecord],
) -> list[RuntimeEffectRecord]:
    latest: dict[str, RuntimeEffectRecord] = {}
    for effect in effects:
        previous = latest.get(effect.effect_id)
        if previous is None or effect.revision > previous.revision:
            latest[effect.effect_id] = effect
    return sorted(latest.values(), key=lambda item: (item.step_id, item.effect_id))


def _effect_recovery_item(
    effect: RuntimeEffectRecord,
    contract: RuntimeRunContract,
) -> dict[str, Any] | None:
    recovery = _conservative_effect_recovery(effect)
    assessment = assess_runtime_effect_replay(effect, recovery)
    external_replay_forbidden = (
        recovery.external_side_effect
        and not assessment.automatic_external_call_allowed
        and effect.status in {"running", "ambiguous", "committed", "probed_committed"}
    )
    ambiguous = (
        effect.status in {"running", "ambiguous"}
        and recovery.external_side_effect
        and not assessment.automatic_external_call_allowed
    )
    checkpoint_rebuild_required = (
        effect.status in {"committed", "probed_committed"}
        and contract.status not in TERMINAL_RUNTIME_RUN_STATUSES
        and contract.checkpoint is None
    )
    terminal_rewrite_required = (
        effect.status in {"committed", "probed_committed"}
        and contract.status not in TERMINAL_RUNTIME_RUN_STATUSES
        and contract.checkpoint is not None
    )
    if not (
        ambiguous
        or checkpoint_rebuild_required
        or terminal_rewrite_required
        or effect.status == "waiting_approval"
    ):
        return None
    return {
        "effect_id": effect.effect_id,
        "step_id": effect.step_id,
        "tool_name": effect.tool_name,
        "source": effect.source,
        "status": effect.status,
        "revision": effect.revision,
        "crash_window": assessment.crash_window,
        "external_side_effect": recovery.external_side_effect,
        "idempotency": recovery.idempotency,
        "probe": recovery.probe,
        "compensation": recovery.compensation,
        "automatic_external_call_allowed": assessment.automatic_external_call_allowed,
        "external_call_must_not_be_replayed": external_replay_forbidden,
        "requires_probe": assessment.requires_probe,
        "requires_human": assessment.requires_human,
        "checkpoint_rebuild_required": checkpoint_rebuild_required,
        "terminal_rewrite_required": terminal_rewrite_required,
        "recommended_reducer_actions": list(assessment.recommended_reducer_actions),
        "reason": assessment.reason,
    }


def _effect_recovery_overlay(
    contract: RuntimeRunContract,
    workspace_dir: str,
) -> dict[str, Any]:
    try:
        effects = RuntimeEffectJournal(workspace_dir).list_effects(
            contract.run_id,
            contract.attempt_id,
        )
    except Exception as exc:  # pragma: no cover - corruption path is defensive
        return {
            "ambiguous_effect": True,
            "recovery": {
                "effect_journal_error": type(exc).__name__,
                "effect_journal_error_message": str(exc),
                "can_takeover": True,
            },
            "debug_refs": {"effect_journal_error": type(exc).__name__},
        }

    items = [
        item
        for effect in _latest_effects_by_id(effects)
        if (item := _effect_recovery_item(effect, contract)) is not None
    ]
    if not items:
        return {}

    recovery = {
        "effects": items,
        "effect_count": len(items),
        "effect_replay_forbidden": any(
            item.get("external_call_must_not_be_replayed") for item in items
        ),
        "effect_requires_probe": any(item.get("requires_probe") for item in items),
        "effect_requires_human": any(item.get("requires_human") for item in items),
        "effect_checkpoint_rebuild_required": any(
            item.get("checkpoint_rebuild_required") for item in items
        ),
        "effect_terminal_rewrite_required": any(
            item.get("terminal_rewrite_required") for item in items
        ),
        "can_takeover": True,
    }
    ambiguous_effect = any(
        item.get("status") in {"running", "ambiguous"}
        and item.get("external_call_must_not_be_replayed")
        for item in items
    )
    return {
        "ambiguous_effect": ambiguous_effect,
        "recovery": recovery,
        "debug_refs": {
            "effect_journal_workspace_dir": workspace_dir,
            "effect_count": len(effects),
            "effect_recovery_count": len(items),
        },
    }


def _runtime_summary_with_effect_recovery(
    summary: RuntimeRunSummary,
    contract: RuntimeRunContract,
    workspace_dir: str,
) -> RuntimeRunSummary:
    overlay = _effect_recovery_overlay(contract, workspace_dir)
    if not overlay:
        return summary
    recovery = {
        **dict(summary.recovery or {}),
        **dict(overlay.get("recovery") or {}),
    }
    debug_refs = {
        **dict(summary.debug_refs or {}),
        **dict(overlay.get("debug_refs") or {}),
    }
    return summary.model_copy(
        update={
            "ambiguous_effect": bool(summary.ambiguous_effect)
            or bool(overlay.get("ambiguous_effect")),
            "recovery": recovery,
            "debug_refs": debug_refs,
        }
    )


def _artifact_count_for_events(events: list[ChatExperienceEvent]) -> int:
    return sum(
        1
        for event in events
        if event.kind in {"chat.artifact.created", "image.task.settled"}
    )


def _evaluation_summary_for_events(
    events: list[ChatExperienceEvent],
) -> dict[str, Any] | None:
    for event in reversed(events):
        payload = event.payload or {}
        summary = payload.get("evaluation_summary")
        if isinstance(summary, dict):
            return dict(summary)
        report = payload.get("evaluation_report")
        if isinstance(report, dict):
            return {
                key: value
                for key, value in report.items()
                if key in {"evaluator", "conclusion", "summary", "created_at"}
            }
    return None


def _runtime_summary_from_contract(
    contract: RuntimeRunContract,
    workspace_dir: str,
) -> RuntimeRunSummary:
    status = _runtime_status_for_contract(contract)
    chat_id = (
        contract.owner.session_id
        if contract.source == "chat" and contract.owner.session_id
        else None
    )
    summary = contract.error.message if contract.error else contract.status
    started_at = contract.started_at or contract.created_at
    ended_at = contract.settled_at
    duration_ms = None
    if started_at is not None and ended_at is not None:
        duration_ms = max(0, int((ended_at - started_at).total_seconds() * 1000))
    can_retry = contract.source == "chat" and status in {
        "completed",
        "failed",
        "interrupted",
        "stalled",
    }
    return RuntimeRunSummary(
        id=contract.run_id,
        display_id=contract.run_id[:8],
        source=contract.source,  # type: ignore[arg-type]
        attempt_id=contract.attempt_id,
        title=f"{contract.source} run",
        summary=summary,
        status=status,  # type: ignore[arg-type]
        authority_status=contract.status,
        transport_status="unknown",
        revision=contract.revision,
        started_at=_contract_datetime(started_at),
        ended_at=_contract_datetime(ended_at),
        duration_ms=duration_ms,
        chat_id=chat_id,
        session_id=contract.owner.session_id,
        workspace_dir=workspace_dir,
        lease_owner=contract.lease_owner,
        lease_expires_at=_contract_datetime(contract.lease_expires_at),
        heartbeat_at=_contract_datetime(contract.heartbeat_at),
        release_edition=contract.release_edition,
        current_step=_contract_step_summary(contract),
        budget=_contract_budget_summary(contract),
        checkpoint=_contract_checkpoint_summary(contract),
        pending_approval=contract.status == "waiting_approval",
        recovery=_contract_recovery_summary(contract),
        can_interrupt=False,
        can_retry=can_retry,
        last_event_summary="",
        event_count=0,
        projection_source="runtime_store",
        debug_refs={
            "run_id": contract.run_id,
            "attempt_id": contract.attempt_id,
            "revision": contract.revision,
            "contract_hash": contract.contract_hash,
            "policy_snapshot_hash": contract.policy_snapshot_hash,
            "runtime_store_workspace_dir": workspace_dir,
        },
    )


def _merge_debug_refs(
    event_refs: dict[str, Any],
    contract_refs: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(event_refs)
    for key, value in contract_refs.items():
        if value is not None:
            merged[key] = value
    return merged


def _merge_runtime_summary_with_contract(
    event_summary: RuntimeRunSummary,
    contract_summary: RuntimeRunSummary,
) -> RuntimeRunSummary:
    title = event_summary.title or contract_summary.title
    summary = event_summary.summary or contract_summary.summary
    recovery = dict(contract_summary.recovery or {})
    recovery["can_interrupt"] = event_summary.can_interrupt
    recovery["can_retry"] = event_summary.can_retry or contract_summary.can_retry
    return event_summary.model_copy(
        update={
            "source": contract_summary.source,
            "attempt_id": contract_summary.attempt_id,
            "title": title,
            "summary": summary,
            "status": contract_summary.status,
            "authority_status": contract_summary.authority_status,
            "transport_status": event_summary.status,
            "revision": contract_summary.revision,
            "ended_at": contract_summary.ended_at or event_summary.ended_at,
            "duration_ms": contract_summary.duration_ms
            if contract_summary.duration_ms is not None
            else event_summary.duration_ms,
            "lease_owner": contract_summary.lease_owner,
            "lease_expires_at": contract_summary.lease_expires_at,
            "heartbeat_at": contract_summary.heartbeat_at,
            "release_edition": contract_summary.release_edition,
            "current_step": contract_summary.current_step,
            "budget": contract_summary.budget,
            "checkpoint": contract_summary.checkpoint,
            "pending_approval": contract_summary.pending_approval,
            "ambiguous_effect": event_summary.ambiguous_effect
            or contract_summary.ambiguous_effect,
            "recovery": recovery,
            "can_retry": event_summary.can_retry or contract_summary.can_retry,
            "projection_source": "merged",
            "debug_refs": _merge_debug_refs(
                event_summary.debug_refs,
                contract_summary.debug_refs,
            ),
        }
    )


def _event_order_rank(event: ChatExperienceEvent) -> int:
    if event.kind.endswith(".started"):
        return 0
    if event.status in {"info", "running"}:
        return 1
    if event.kind == "chat.turn.stop_requested":
        return 2
    if event.status in {"finished", "completed", "interrupted", "stalled", "failed"}:
        return 3
    return 2


def _runtime_event_row_sort_key(
    row: tuple[ChatExperienceEvent, str],
) -> tuple[str, int, int, str]:
    event, workspace_dir = row
    return (
        event.ts,
        event.seq,
        _event_order_rank(event),
        workspace_dir,
    )


def _runtime_event_representative_key(
    row: tuple[ChatExperienceEvent, str],
) -> tuple[int, str, int, int, str, str]:
    event, workspace_dir = row
    candidate = _run_status_candidate_for_event(event)
    return (
        _RUN_STATUS_PRIORITY[candidate],
        event.ts,
        event.seq,
        _event_order_rank(event),
        workspace_dir,
        event.id,
    )


def _projection_warning_for_rows(
    rows: list[tuple[ChatExperienceEvent, str]],
) -> str | None:
    seen: dict[tuple[str, int], str] = {}
    for event, workspace_dir in rows:
        key = (event.ts, event.seq)
        previous = seen.get(key)
        if previous is not None and previous != workspace_dir:
            return "cross_workspace_local_seq_tie"
        seen[key] = workspace_dir
    return None


def _usage_summary_for_events(events: list[ChatExperienceEvent]) -> RuntimeUsageSummary:
    totals = RuntimeUsageSummary()
    keyed_tool_calls: set[str] = set()
    unkeyed_tool_starts = 0
    unkeyed_tool_finishes = 0
    for event in events:
        usage = _usage_for_payload(event.payload, event.usage) or {}
        totals.input_tokens += usage.get("input_tokens", 0) or 0
        totals.output_tokens += usage.get("output_tokens", 0) or 0
        totals.cache_tokens += usage.get("cache_tokens", 0) or 0
        totals.cost_points += usage.get("cost_points", 0) or 0
        totals.context_tokens = max(
            totals.context_tokens,
            usage.get("context_tokens", 0) or 0,
        )
        if event.kind in {
            "chat.tool_call.started",
            "chat.tool_call.finished",
            "chat.tool_result",
        }:
            tool_key = _tool_call_identity_for_event(event)
            if tool_key:
                keyed_tool_calls.add(tool_key)
            elif event.kind == "chat.tool_call.started":
                unkeyed_tool_starts += 1
            else:
                unkeyed_tool_finishes += 1
        totals.artifact_bytes += _artifact_bytes_for_event(event)
    totals.tool_calls = (
        len(keyed_tool_calls)
        + unkeyed_tool_starts
        + (unkeyed_tool_finishes if not keyed_tool_calls and not unkeyed_tool_starts else 0)
    )
    return totals


def _tool_call_identity_for_event(event: ChatExperienceEvent) -> str | None:
    payload = event.payload or {}
    for value in (
        event.item_id,
        payload.get("tool_call_id"),
        payload.get("call_id"),
        payload.get("id"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool_call = payload.get("tool_call")
    if isinstance(tool_call, dict):
        value = tool_call.get("id") or tool_call.get("tool_call_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _payload_number(payload: dict[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number < 0:
            continue
        if number.is_integer():
            return int(number)
        return number
    return None


def _artifact_bytes_for_event(event: ChatExperienceEvent) -> int:
    if event.kind not in {"chat.artifact.created", "image.task.settled"}:
        return 0
    payload = event.payload or {}
    value = _payload_number(
        payload,
        "artifact_bytes",
        "artifact_size_bytes",
        "size_bytes",
        "bytes",
        "size",
    )
    if value is not None:
        return int(value)
    artifact = payload.get("artifact")
    if isinstance(artifact, dict):
        nested = _payload_number(
            artifact,
            "artifact_bytes",
            "artifact_size_bytes",
            "size_bytes",
            "bytes",
            "size",
        )
        if nested is not None:
            return int(nested)
    return 0


def _event_attempt_id(event: ChatExperienceEvent) -> str | None:
    payload = event.payload or {}
    value = payload.get("attempt_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def runtime_usage_summary_for_run(
    workspace_dir: Path | str,
    run_id: str,
    *,
    attempt_id: str | None = None,
) -> RuntimeUsageSummary:
    """Aggregate normalized usage from ledger events for one runtime run."""

    run_key = str(run_id or "").strip()
    if not run_key:
        return RuntimeUsageSummary()
    attempt_key = str(attempt_id or "").strip() or None
    events: list[ChatExperienceEvent] = []
    for event in _iter_events_for_workspace(workspace_dir):
        if _group_run_key_for_event(event) != run_key:
            continue
        event_attempt_id = _event_attempt_id(event)
        if (
            attempt_key is not None
            and event_attempt_id is not None
            and event_attempt_id != attempt_key
        ):
            continue
        events.append(event)
    return _usage_summary_for_events(events)


def _run_title_for_events(events: list[ChatExperienceEvent], run_id: str) -> str:
    for event in events:
        payload = event.payload or {}
        if event.kind.startswith("cron."):
            return str(payload.get("job_name") or "定时任务")
        if event.kind.startswith("heartbeat."):
            return "心跳跟进"
        if event.kind.startswith("image."):
            prompt_preview = _short_text(str(payload.get("prompt_preview") or ""), 24)
            model = str(payload.get("model") or "").strip()
            if prompt_preview:
                return f"已退役图片任务：{prompt_preview}"
            if model:
                return f"已退役图片任务 · {model}"
            return "已退役图片任务"
        if event.kind == "skills.changed":
            return str(payload.get("skill_name") or "技能库更新")
        if event.kind == "chat.turn.started":
            preview = _short_text(str(payload.get("text_preview") or ""), 36)
            return preview or "聊天运行"
    return run_id[:8]


def _model_for_events(events: list[ChatExperienceEvent]) -> str | None:
    for event in reversed(events):
        payload = event.payload or {}
        model = (
            payload.get("selected_model")
            or payload.get("model")
            or payload.get("model_name")
        )
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None


def collect_runtime_runs(
    workspace_dirs: list[Path | str],
    *,
    source: str | None = None,
    status: str | None = None,
    requested_session_id: str | None = None,
    limit: int = 100,
) -> RuntimeRunsResponse:
    """Collect grouped Runtime runs across workspace ledgers."""

    unique_dirs: dict[str, Path] = {}
    for workspace_dir in workspace_dirs:
        path = Path(workspace_dir).expanduser()
        unique_dirs[str(path)] = path

    grouped: dict[str, list[tuple[ChatExperienceEvent, str]]] = {}
    for workspace_dir in unique_dirs.values():
        for event in _iter_events_for_workspace(workspace_dir):
            grouped.setdefault(_group_run_key_for_event(event), []).append(
                (event, str(workspace_dir)),
            )

    summaries_by_id: dict[str, RuntimeRunSummary] = {}
    for run_id, rows in grouped.items():
        rows.sort(key=_runtime_event_row_sort_key)
        events = [event for event, _workspace_dir in rows]
        run_source = _run_source_for_events(events)
        run_status = _run_status_for_events(events)

        first, _first_workspace_dir = rows[0]
        last, workspace_dir = max(rows, key=_runtime_event_representative_key)
        started = _parse_event_ts(first.ts)
        ended = _parse_event_ts(last.ts)
        duration_ms = None
        if started is not None and ended is not None and run_status != "running":
            duration_ms = max(0, int((ended - started).total_seconds() * 1000))
        chat_id = last.chat_id if isinstance(last.chat_id, str) else None
        session_id = (
            last.session_id if isinstance(last.session_id, str) else None
        )
        if requested_session_id and session_id != requested_session_id:
            continue
        can_interrupt = run_status == "running" and bool(chat_id)
        can_retry = (
            run_source == "chat"
            and run_status in {"completed", "failed", "interrupted", "stalled"}
            and bool(chat_id)
        )
        debug_refs = {
            "run_id": run_id,
            "chat_id": last.chat_id,
            "session_id": last.session_id,
            "workspace_dir": workspace_dir,
            "latest_seq": last.seq,
        }
        projection_warning = _projection_warning_for_rows(rows)
        if projection_warning:
            debug_refs["projection_warning"] = projection_warning
        summaries_by_id[run_id] = RuntimeRunSummary(
            id=run_id,
            display_id=run_id[:8],
            source=run_source,  # type: ignore[arg-type]
            title=_run_title_for_events(events, run_id),
            summary=_detail_for_event(last),
            status=run_status,  # type: ignore[arg-type]
            authority_status=run_status,
            transport_status=run_status,
            started_at=first.ts,
            ended_at=None if run_status == "running" else last.ts,
            duration_ms=duration_ms,
            model=_model_for_events(events),
            chat_id=chat_id,
            session_id=session_id,
            workspace_dir=workspace_dir,
            can_interrupt=can_interrupt,
            can_retry=can_retry,
            usage=_usage_summary_for_events(events),
            last_event_summary=last.summary,
            event_count=len(events),
            artifact_count=_artifact_count_for_events(events),
            evaluation_summary=_evaluation_summary_for_events(events),
            recovery={
                "can_interrupt": can_interrupt,
                "can_retry": can_retry,
                "can_resume": False,
                "can_takeover": run_status == "stalled",
                "resume_requires_durable_lease": run_status == "stalled",
            },
            projection_source="event_ledger",
            debug_refs=debug_refs,
        )

    for workspace_dir in unique_dirs.values():
        for contract in RuntimeRunStore(workspace_dir).list_contracts():
            contract_summary = _runtime_summary_from_contract(
                contract,
                str(workspace_dir),
            )
            contract_summary = _runtime_summary_with_effect_recovery(
                contract_summary,
                contract,
                str(workspace_dir),
            )
            existing = summaries_by_id.get(contract.run_id)
            summaries_by_id[contract.run_id] = (
                _merge_runtime_summary_with_contract(existing, contract_summary)
                if existing is not None
                else contract_summary
            )

    summaries = [
        summary
        for summary in summaries_by_id.values()
        if (not source or summary.source == source)
        and (not status or summary.status == status)
    ]

    summaries.sort(key=lambda item: item.started_at or "", reverse=True)
    return RuntimeRunsResponse(
        total=len(summaries),
        items=summaries[: max(1, limit)],
    )


def build_runtime_run_events_response(
    workspace_dirs: list[Path | str],
    run_id: str,
    *,
    after_seq: int = 0,
    limit: int | None = None,
    attempt_id: str | None = None,
) -> RuntimeRunEventsResponse:
    """Return RuntimeEvent rows for one grouped runtime run."""

    rows: list[tuple[ChatExperienceEvent, RuntimeEvent, str]] = []
    latest_seq = int(after_seq or 0)
    normalized_limit = _normalized_event_limit(limit)
    unique_workspace_dirs = {
        str(Path(p).expanduser()): Path(p).expanduser() for p in workspace_dirs
    }
    for workspace_dir in unique_workspace_dirs.values():
        for path in _event_ledger_paths_for_workspace_after_seq(
            workspace_dir,
            after_seq,
        ):
            for event in _iter_events_from_file_after_seq(path, after_seq):
                if (
                    _run_key_for_event(event) != run_id
                    and _parent_run_id_for_event(event) != run_id
                ):
                    continue
                if not _event_matches_attempt(event, attempt_id):
                    continue
                rows.append((event, _event_to_runtime(event), str(workspace_dir)))

    rows.sort(key=lambda item: _runtime_event_row_sort_key((item[0], item[2])))
    if normalized_limit is None or len(rows) <= normalized_limit:
        selected_rows = rows
        has_more = False
    else:
        selected_rows = list(rows[:normalized_limit])
        boundary_seq = selected_rows[-1][0].seq
        cursor = normalized_limit
        while cursor < len(rows) and rows[cursor][0].seq == boundary_seq:
            selected_rows.append(rows[cursor])
            cursor += 1
        has_more = cursor < len(rows)
    for raw_event, _event, _workspace_dir in selected_rows:
        latest_seq = max(latest_seq, raw_event.seq)
    contract_workspace_dir: str | None = None
    contract_attempt_id: str | None = None
    if not selected_rows and after_seq == 0:
        for candidate_workspace_dir in unique_workspace_dirs.values():
            for contract in RuntimeRunStore(candidate_workspace_dir).list_contracts():
                if contract.run_id != run_id:
                    continue
                if attempt_id is not None and contract.attempt_id != attempt_id:
                    continue
                contract_workspace_dir = str(candidate_workspace_dir)
                contract_attempt_id = contract.attempt_id
                break
            if contract_workspace_dir is not None:
                break
    workspace_dir = selected_rows[-1][2] if selected_rows else contract_workspace_dir
    return RuntimeRunEventsResponse(
        run_id=run_id,
        attempt_id=attempt_id or contract_attempt_id,
        workspace_dir=workspace_dir,
        after_seq=after_seq,
        latest_seq=latest_seq,
        limit=normalized_limit,
        next_after_seq=latest_seq,
        has_more=has_more,
        events=[event for _raw_event, event, _workspace_dir in selected_rows],
    )


def collect_recent_turn_events(
    workspace_dirs: list[Path | str],
    *,
    limit: int = 50,
) -> DebugTurnEventsResponse:
    """Collect recent chat events across multiple workspace event ledgers."""

    unique_dirs: dict[str, Path] = {}
    for workspace_dir in workspace_dirs:
        path = Path(workspace_dir).expanduser()
        unique_dirs[str(path)] = path

    paths: list[str] = []
    items: list[tuple[ChatExperienceEvent, str]] = []
    updated_at: float | None = None
    total_size = 0

    for workspace_dir in unique_dirs.values():
        ledger_paths = _event_ledger_paths_for_workspace(workspace_dir)
        if not ledger_paths:
            paths.append(str(events_path_for_workspace(workspace_dir)))
            continue
        for path in ledger_paths:
            paths.append(str(path))
            stat = path.stat()
            total_size += stat.st_size
            updated_at = (
                stat.st_mtime
                if updated_at is None
                else max(updated_at, stat.st_mtime)
            )
        for event in _iter_events_for_workspace(workspace_dir):
            items.append((event, str(workspace_dir)))

    items.sort(key=lambda item: item[0].ts, reverse=True)
    sliced = items[: max(1, limit)]

    return DebugTurnEventsResponse(
        paths=paths,
        exists=bool(items),
        updated_at=updated_at,
        size=total_size,
        event_count=len(items),
        items=[
            DebugTurnEventRecord(
                id=event.id,
                seq=event.seq,
                ts=event.ts,
                chat_id=event.chat_id,
                session_id=event.session_id,
                run_id=event.run_id,
                scope=event.scope,
                kind=event.kind,
                type=event.type or _runtime_type_for_kind(event.kind, event.status),
                status=event.status,
                summary=event.summary,
                label=_label_for_kind(event.kind),
                detail=_detail_for_event(event),
                workspace_dir=workspace_dir,
                usage=_usage_for_payload(event.payload, event.usage),
            )
            for event, workspace_dir in sliced
        ],
    )


def build_turn_event_preview(
    workspace_dir: Path | str,
    chat_id: str,
    *,
    limit: int = MAX_PREVIEW_EVENTS,
) -> TurnEventPreview | None:
    """Build a compact recent event preview for one chat."""

    if not _event_ledger_paths_for_workspace(workspace_dir):
        return None

    recent: deque[ChatExperienceEvent] = deque(maxlen=max(1, limit))
    for event in _iter_events_for_workspace(workspace_dir):
        if event.chat_id != chat_id or event.scope != "chat":
            continue
        # Token deltas and per-chunk usage are diagnostic telemetry, not
        # execution stages. Letting them consume the bounded preview hid the
        # active tool and terminal rows in real long-running turns.
        if event.type == "message.delta":
            continue
        if event.type == "runtime.warning" and str(
            (event.payload or {}).get("kind") or "",
        ) == "model_usage":
            continue
        presentation = tool_presentation_for_event(
            event,
            dict(event.payload or {}),
        )
        if presentation is not None and (
            presentation.visibility == "hidden"
            or presentation.surface == "debug"
        ):
            continue
        recent.append(event)

    if not recent:
        return None

    latest = recent[-1]
    summary_line = {
        "chat.turn.started": "本轮已开始",
        "chat.turn.stop_requested": "本轮已请求停止",
        "chat.turn.settled": "本轮已结束",
        "chat.topic_workspace.bound": "已绑定当前工作区",
    }.get(latest.kind, latest.summary)

    return TurnEventPreview(
        workspace_dir=str(Path(workspace_dir).expanduser()),
        summary_line=summary_line,
        events=[
            TurnEventPreviewEvent(
                id=event.id,
                ts=event.ts,
                session_id=event.session_id,
                run_id=event.run_id,
                client_turn_id=(
                    str((event.payload or {}).get("client_turn_id")).strip()
                    if (event.payload or {}).get("client_turn_id") is not None
                    and str((event.payload or {}).get("client_turn_id")).strip()
                    else None
                ),
                turn_id=_turn_id_for_event(event),
                item_id=event.item_id,
                seq=event.seq,
                type=event.type or _runtime_type_for_kind(event.kind, event.status),
                kind=event.kind,
                status=event.status,
                label=_label_for_kind(event.kind),
                detail=_detail_for_event(event),
                usage=_usage_for_payload(event.payload, event.usage),
                phase=_trace_phase_for_event(event),
                trace_title=str(
                    (event.payload or {}).get("trace_title")
                    or _label_for_kind(event.kind),
                ),
                trace_detail=str(
                    (event.payload or {}).get("trace_detail")
                    or _detail_for_event(event),
                ),
                duration_ms=(
                    int((event.payload or {}).get("duration_ms"))
                    if isinstance((event.payload or {}).get("duration_ms"), int)
                    else None
                ),
                payload=_preview_payload_for_event(event),
            )
            for event in recent
        ],
    )
