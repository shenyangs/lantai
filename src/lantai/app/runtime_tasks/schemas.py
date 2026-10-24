# -*- coding: utf-8 -*-
"""Public schemas for the Agent task board."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


RuntimeTaskLane = Literal[
    "pending",
    "active",
    "needs_user",
    "done",
    "needs_action",
]
RuntimeTaskOutcome = Literal[
    "queued",
    "in_progress",
    "needs_input",
    "delivered",
    "incomplete",
    "blocked",
    "unknown",
]


class CreateRuntimeTaskRequest(BaseModel):
    """Persist one user-authored task before execution begins."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=20_000)
    title: str | None = Field(default=None, max_length=160)
    acceptance: list[str] = Field(default_factory=list, max_length=20)
    work_method_id: str = Field(default="standard", min_length=1, max_length=80)
    client_task_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    auto_start: bool = False

    @field_validator("objective", "work_method_id", "client_task_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value cannot be empty")
        return text

    @field_validator("title")
    @classmethod
    def strip_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("acceptance")
    @classmethod
    def normalize_acceptance(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in normalized:
                normalized.append(text[:500])
        return normalized


class RuntimeTaskBoardItem(BaseModel):
    """One task card projected from the authoritative RuntimeRun."""

    id: str
    display_id: str = ""
    title: str
    summary: str = ""
    source: str = "system"
    lane: RuntimeTaskLane
    outcome_status: RuntimeTaskOutcome = "unknown"
    authority_status: str = ""
    transport_status: str = "unknown"
    delivery_status: str = "pending"
    is_history: bool = False
    pending_approval: bool = False
    ambiguous_effect: bool = False
    next_action: str | None = None
    agent_id: str | None = None
    chat_id: str | None = None
    session_id: str | None = None
    workspace_dir: str | None = None
    attempt_id: str | None = None
    revision: int | None = None
    created_at: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    ended_at: str | None = None
    current_step: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    recovery: dict[str, Any] = Field(default_factory=dict)
    autonomy_grant: dict[str, Any] | None = None
    progress_report: dict[str, Any] | None = None
    completion_evidence: dict[str, Any] | None = None
    can_start: bool = False
    can_interrupt: bool = False
    can_retry: bool = False
    can_resume: bool = False
    can_open_chat: bool = False
    event_count: int = 0
    last_event_summary: str = ""
    debug_refs: dict[str, Any] = Field(default_factory=dict)


class RuntimeTaskBoardCounts(BaseModel):
    pending: int = 0
    active: int = 0
    needs_user: int = 0
    done: int = 0
    needs_action: int = 0
    history: int = 0


class RuntimeTaskBoardResponse(BaseModel):
    total: int = 0
    visible_total: int = 0
    generated_at: str
    counts: RuntimeTaskBoardCounts = Field(default_factory=RuntimeTaskBoardCounts)
    items: list[RuntimeTaskBoardItem] = Field(default_factory=list)


class RuntimeTaskStartResponse(BaseModel):
    run_id: str
    chat_id: str
    session_id: str
    status: str
    already_running: bool = False
    accepted: bool = True
