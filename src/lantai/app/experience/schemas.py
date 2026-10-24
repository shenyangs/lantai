# -*- coding: utf-8 -*-
"""Schemas for append-only chat experience events."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from lantai.runtime.events import (
    RUNTIME_EVENT_SCHEMA_VERSION,
    RuntimeEventSource,
    RuntimeEventStatus,
    RuntimeEventType,
)


ChatExperienceScope = Literal["chat", "task"]
ChatExperienceStatus = Literal[
    "info",
    "running",
    "interrupted",
    "finished",
    "completed",
    "failed",
    "stalled",
]
ToolPresentationVisibility = Literal[
    "visible",
    "hidden",
    "quiet",
    "prominent",
]
ToolPresentationSurface = Literal[
    "tool",
    "subagent",
    "approval",
    "service",
    "debug",
]
ToolPresentationRisk = Literal["low", "medium", "high"]
ChatExperienceKind = Literal[
    "chat.topic_workspace.bound",
    "chat.turn.started",
    "chat.turn.stop_requested",
    "chat.turn.continue_requested",
    "chat.turn.settled",
    "chat.tool_call.started",
    "chat.tool_call.finished",
    "chat.tool_result",
    "chat.approval.requested",
    "chat.approval.resolved",
    "chat.artifact.created",
    "chat.model.routing_decision",
    "chat.message.started",
    "chat.message.delta",
    "chat.message.completed",
    "chat.system.event",
    "chat.checkpoint.created",
    "chat.checkpoint.restored",
    "chat.replay.started",
    "chat.replay.settled",
    "chat.final_answer",
    "chat.error",
    "cron.job.started",
    "cron.job.settled",
    "heartbeat.run.started",
    "heartbeat.run.settled",
    "image.task.started",
    "image.task.settled",
    "loop.budget.changed",
    "runtime.notification",
    "skills.changed",
]


class ChatExperienceEvent(BaseModel):
    """One append-only event written for a chat run."""

    id: str
    seq: int = Field(
        default=0,
        description="Monotonic sequence number within one workspace ledger",
    )
    ts: str
    agent_id: str
    chat_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    item_id: str | None = None
    scope: ChatExperienceScope = "chat"
    kind: ChatExperienceKind
    type: str | None = Field(
        default=None,
        description="RuntimeEvent v1 type, e.g. turn.started or tool.completed",
    )
    status: ChatExperienceStatus
    source: str = Field(
        default="system",
        description="RuntimeEvent v1 source: user, assistant, tool, or system",
    )
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] | None = None


class ToolPresentation(BaseModel):
    """Product-layer visibility and renderer hint for tool-like events."""

    visibility: ToolPresentationVisibility = "visible"
    surface: ToolPresentationSurface = "tool"
    risk_level: ToolPresentationRisk = "low"
    display_name: str = ""
    summary: str = ""


class RuntimeEvent(BaseModel):
    """Public RuntimeEvent v1 row returned to chat and task UIs."""

    schema_version: Literal[1] = RUNTIME_EVENT_SCHEMA_VERSION
    id: str
    session_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    seq: int
    type: RuntimeEventType
    status: RuntimeEventStatus | None = None
    source: RuntimeEventSource = "system"
    presentation: ToolPresentation | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] | None = None
    created_at: str


class RuntimeEventsResponse(BaseModel):
    """RuntimeEvent v1 list response for one chat."""

    chat_id: str
    workspace_dir: str | None = None
    stream_epoch: str = ""
    cursor: str | None = None
    after_seq: int = 0
    earliest_seq: int = 0
    ledger_latest_seq: int = 0
    latest_seq: int = 0
    limit: int | None = None
    next_after_seq: int | None = None
    has_more: bool = False
    resync_required: bool = False
    resync_reason: str | None = None
    events: list[RuntimeEvent] = Field(default_factory=list)


class RuntimeUsageSummary(BaseModel):
    """Aggregated token and entitlement usage for one runtime run."""

    input_tokens: int | float = 0
    output_tokens: int | float = 0
    cache_tokens: int | float = 0
    context_tokens: int | float = 0
    cost_points: int | float = 0
    tool_calls: int = 0
    artifact_bytes: int = 0


RuntimeRunStatus = Literal[
    "running",
    "completed",
    "failed",
    "interrupted",
    "stalled",
    "idle",
    "unknown",
]
RuntimeRunSource = Literal[
    "chat",
    "tool",
    "approval",
    "mission",
    "browser",
    "capability",
    "pipeline",
    "cron",
    "reminder",
    "heartbeat",
    "image",
    "skills",
    "system",
]


class RuntimeRunSummary(BaseModel):
    """User-facing summary row for the Runtime runs console."""

    id: str
    display_id: str = ""
    source: RuntimeRunSource = "system"
    attempt_id: str | None = None
    title: str = ""
    summary: str = ""
    status: RuntimeRunStatus = "unknown"
    authority_status: str = ""
    transport_status: str = "unknown"
    revision: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    model: str | None = None
    chat_id: str | None = None
    session_id: str | None = None
    workspace_dir: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None
    release_edition: str | None = None
    current_step: dict[str, Any] | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    pending_approval: bool = False
    ambiguous_effect: bool = False
    evaluation_summary: dict[str, Any] | None = None
    artifact_count: int = 0
    recovery: dict[str, Any] = Field(default_factory=dict)
    can_interrupt: bool = False
    can_retry: bool = False
    usage: RuntimeUsageSummary = Field(default_factory=RuntimeUsageSummary)
    last_event_summary: str = ""
    event_count: int = 0
    projection_source: str = "event_ledger"
    debug_refs: dict[str, Any] = Field(default_factory=dict)


class RuntimeRunsResponse(BaseModel):
    """Runtime run summary list response."""

    total: int = 0
    items: list[RuntimeRunSummary] = Field(default_factory=list)


class RuntimeRunEventsResponse(BaseModel):
    """Runtime events for one grouped run."""

    run_id: str
    attempt_id: str | None = None
    workspace_dir: str | None = None
    after_seq: int = 0
    latest_seq: int = 0
    limit: int | None = None
    next_after_seq: int | None = None
    has_more: bool = False
    events: list[RuntimeEvent] = Field(default_factory=list)


class TurnEventPreviewEvent(BaseModel):
    """Compact event row returned to the chat page."""

    id: str
    ts: str
    session_id: str | None = None
    run_id: str | None = None
    client_turn_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    seq: int | None = None
    type: str | None = None
    kind: str
    status: str
    label: str
    detail: str
    usage: dict[str, Any] | None = None
    phase: str | None = None
    trace_title: str | None = None
    trace_detail: str | None = None
    duration_ms: int | None = None
    payload: dict[str, Any] | None = None


class TurnEventPreview(BaseModel):
    """Recent chat runtime events shown on the chat page."""

    workspace_dir: str | None = None
    summary_line: str | None = None
    events: list[TurnEventPreviewEvent] = Field(default_factory=list)


class DebugTurnEventRecord(BaseModel):
    """One debug view event row."""

    id: str
    ts: str
    chat_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    scope: str = "chat"
    kind: str
    status: str
    summary: str
    label: str
    detail: str
    workspace_dir: str
    seq: int = 0
    type: str | None = None
    usage: dict[str, Any] | None = None


class DebugTurnEventsResponse(BaseModel):
    """Aggregated debug view over chat event ledgers."""

    paths: list[str] = Field(default_factory=list)
    exists: bool = False
    updated_at: float | None = None
    size: int = 0
    event_count: int = 0
    items: list[DebugTurnEventRecord] = Field(default_factory=list)
