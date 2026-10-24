# -*- coding: utf-8 -*-
"""Strict data contracts owned by the hosted-collaboration coordinator."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CollaborationPhase = Literal[
    "preparing",
    "staffing",
    "working",
    "reconciling",
    "verifying",
    "delivering",
    "closing",
    "closed",
]
CollaborationOutcome = Literal[
    "ready_for_synthesis",
    "partial",
    "waiting_approval",
    "failed",
    "cancelled",
]
CollaborationMessageKind = Literal[
    "finding",
    "question",
    "challenge",
    "response",
    "handoff",
    "status",
    "host_decision",
]
CollaborationDraftResolution = Literal[
    "pending",
    "revised",
    "sent_as_is",
    "dropped",
    "deferred",
]


class CollaborationRoomMessage(BaseModel):
    """A bounded public room message; full member prompts never enter it."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    member_id: str
    display_name: str
    kind: CollaborationMessageKind
    safe_summary: str = Field(max_length=240)
    base_revision: int = Field(ge=0)
    committed_revision: int = Field(ge=0)


class CollaborationHeldDraft(BaseModel):
    """A stale proposal fenced from the public room until the host resolves it."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    member_id: str
    display_name: str
    kind: CollaborationMessageKind
    safe_summary: str = Field(max_length=240)
    base_revision: int = Field(ge=0)
    current_revision: int = Field(ge=0)
    resolution: CollaborationDraftResolution = "pending"


class CollaborationConflict(BaseModel):
    """One visible disagreement or evidence gap owned by the host."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    safe_summary: str = Field(max_length=240)
    member_ids: list[str] = Field(default_factory=list, max_length=4)
    status: Literal["open", "resolved", "deferred"] = "open"
    resolution_summary: str = Field(default="", max_length=240)


class CollaborationHostDecision(BaseModel):
    """A concise, inspectable decision made by Shen Xingzhi."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    phase: CollaborationPhase
    safe_summary: str = Field(max_length=240)
    reason: str = Field(default="", max_length=240)
    revision: int = Field(ge=0)


class CollaborationClosureAssessment(BaseModel):
    """The mandatory assessment that prevents ambiguous room endings."""

    model_config = ConfigDict(extra="forbid")

    outcome: CollaborationOutcome
    synthesis_ready: bool = False
    deliverable_ready: bool = False
    user_action_required: bool = False
    completed_members: int = Field(ge=0)
    failed_members: int = Field(ge=0)
    cancelled_members: int = Field(ge=0)
    open_conflicts: int = Field(ge=0)
    acceptance_met: list[str] = Field(default_factory=list)
    acceptance_unmet: list[str] = Field(default_factory=list)
    summary: str = Field(max_length=320)
