# -*- coding: utf-8 -*-
"""Pydantic response schemas for plan API endpoints."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SubTaskResponse(BaseModel):
    """A single subtask in the plan response."""

    name: str
    description: str
    expected_outcome: str
    outcome: str | None = None
    state: Literal["todo", "in_progress", "done", "abandoned"] = "todo"
    created_at: str | None = None
    finished_at: str | None = None


class PlanStateResponse(BaseModel):
    """Top-level plan state returned by ``GET /plan/current``."""

    id: str
    name: str
    description: str
    expected_outcome: str
    state: Literal["todo", "in_progress", "done", "abandoned"] = "todo"
    subtasks: list[SubTaskResponse] = Field(default_factory=list)
    created_at: str | None = None
    finished_at: str | None = None
    outcome: str | None = None


class PlanConfigResponse(BaseModel):
    """Plan configuration returned/accepted by config endpoints."""

    enabled: bool = False


def _get_plan_field(plan: Any, name: str, default: Any = None) -> Any:
    if isinstance(plan, dict):
        return plan.get(name, default)
    return getattr(plan, name, default)


def plan_to_response(plan) -> PlanStateResponse:
    """Convert a plan object or persisted plan dict to a response."""
    subtasks = _get_plan_field(plan, "subtasks", []) or []
    subtasks = [
        SubTaskResponse(
            name=_get_plan_field(st, "name", ""),
            description=_get_plan_field(st, "description", ""),
            expected_outcome=_get_plan_field(st, "expected_outcome", ""),
            outcome=_get_plan_field(st, "outcome"),
            state=_get_plan_field(st, "state", "todo"),
            created_at=_get_plan_field(st, "created_at"),
            finished_at=_get_plan_field(st, "finished_at"),
        )
        for st in subtasks
    ]
    return PlanStateResponse(
        id=_get_plan_field(plan, "id", ""),
        name=_get_plan_field(plan, "name", ""),
        description=_get_plan_field(plan, "description", ""),
        expected_outcome=_get_plan_field(plan, "expected_outcome", ""),
        state=_get_plan_field(plan, "state", "todo"),
        subtasks=subtasks,
        created_at=_get_plan_field(plan, "created_at"),
        finished_at=_get_plan_field(plan, "finished_at"),
        outcome=_get_plan_field(plan, "outcome"),
    )
