# -*- coding: utf-8 -*-
"""Chat models for runner with UUID management."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..channels.schema import DEFAULT_CHANNEL
from ..experience.schemas import TurnEventPreview, TurnEventPreviewEvent


class ChatSpec(BaseModel):
    """Chat specification with UUID identifier.

    Stored in Redis and can be persisted in JSON file.
    """

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Chat UUID identifier",
    )
    name: str = Field(default="New Chat", description="Chat name")
    session_id: str = Field(
        ...,
        description="Session identifier (channel:user_id format)",
    )
    user_id: str = Field(..., description="User identifier")
    channel: str = Field(default=DEFAULT_CHANNEL, description="Channel name")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Chat creation timestamp",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Chat last update timestamp",
    )
    meta: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata",
    )
    status: str = Field(
        default="idle",
        description="Conversation status: idle or running",
    )
    pinned: bool = Field(
        default=False,
        description="Whether the chat is pinned to the top",
    )
    archived: bool = Field(
        default=False,
        description="Whether the chat is archived",
    )


class ChatUpdate(BaseModel):
    """Mutable chat fields accepted from external clients.

    Chat identity and system-managed fields stay read-only. The update API is
    currently used for renaming chats, so only externally mutable fields belong
    here.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, description="Chat name")
    pinned: bool | None = Field(
        default=None,
        description="Whether the chat is pinned to the top",
    )
    archived: bool | None = Field(
        default=None,
        description="Whether the chat is archived",
    )


class ChatRuntimeState(BaseModel):
    """Single typed runtime state for restoring one chat turn."""

    status: str = Field(
        default="idle",
        description=(
            "Runtime status: idle, running, completed, failed, "
            "interrupted, or stalled"
        ),
    )
    run_id: str | None = Field(
        default=None,
        description="Recoverable runtime run identifier when available",
    )
    client_turn_id: str | None = Field(
        default=None,
        description="Client turn identifier bound to the recovered run",
    )
    attempt_id: str | None = Field(
        default=None,
        description="Authoritative execution attempt identifier when available",
    )
    outcome_status: str = Field(
        default="unknown",
        description=(
            "Semantic delivery outcome: delivered, incomplete, needs_input, "
            "blocked, or unknown"
        ),
    )
    delivery_status: str = Field(
        default="pending",
        description=(
            "System delivery proof: pending, verified, partial, or "
            "not_delivered"
        ),
    )
    started_at: str | None = Field(
        default=None,
        description="ISO timestamp for the active or latest turn start",
    )
    updated_at: str | None = Field(
        default=None,
        description="ISO timestamp for the latest runtime update",
    )
    summary: str | None = Field(
        default=None,
        description="Short user-facing runtime summary",
    )
    events_preview: list[TurnEventPreviewEvent] = Field(
        default_factory=list,
        description="Recent compact runtime events for the chat",
    )
    workspace_dir: str | None = Field(
        default=None,
        description="Effective workspace directory that owns the runtime ledger",
    )
    can_reconnect: bool = Field(
        default=False,
        description="Whether the frontend can reconnect to the active stream",
    )
    can_stop: bool = Field(
        default=False,
        description="Whether the frontend can request stopping the active run",
    )


class ChatHistory(BaseModel):
    """Complete chat view with spec and state."""

    messages: list[Any] = Field(default_factory=list)
    status: str = Field(
        default="idle",
        description="Conversation status: idle or running",
    )
    chat_id: str | None = Field(
        default=None,
        description="Canonical backend chat UUID",
    )
    session_id: str | None = Field(
        default=None,
        description="Channel session identifier used by the agent runtime",
    )
    name: str | None = Field(default=None, description="Chat display name")
    user_id: str | None = Field(default=None, description="User identifier")
    channel: str | None = Field(default=None, description="Channel name")
    meta: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional chat metadata",
    )
    ququ_continuations: list[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Completed user-requested Ququ child records restored with history"
        ),
    )
    created_at: datetime | None = Field(
        default=None,
        description="Chat creation timestamp",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Chat last update timestamp",
    )
    pinned: bool = Field(
        default=False,
        description="Whether the chat is pinned to the top",
    )
    archived: bool = Field(
        default=False,
        description="Whether the chat is archived",
    )
    pending_user_message: str | None = Field(
        default=None,
        description="Latest user text for an active turn not yet in memory",
    )
    active_run: Dict[str, Any] | None = Field(
        default=None,
        description="Recoverable in-flight run snapshot for the frontend",
    )
    turn_event_preview: TurnEventPreview | None = Field(
        default=None,
        description="Compact recent runtime event preview for the current chat",
    )
    runtime_state: ChatRuntimeState | None = Field(
        default=None,
        description="Single typed runtime state used by chat UI recovery",
    )


class AnalysisCleanupJob(BaseModel):
    """Minimal durable cleanup identity retained after deleting a chat row."""

    model_config = ConfigDict(extra="forbid")

    chat_id: str
    session_state_id: str
    state_workspace_dir: str | None = None


class ChatsFile(BaseModel):
    """Chat registry file for JSON repository.

    Stores chat_id (UUID) -> session_id mappings for persistence.
    """

    version: int = 1
    chats: list[ChatSpec] = Field(default_factory=list)
    analysis_cleanup_jobs: list[AnalysisCleanupJob] = Field(default_factory=list)
