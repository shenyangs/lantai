# -*- coding: utf-8 -*-
"""Runtime input contracts owned by Lantai."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .messages import RuntimeMessage


class RuntimeRequestContext(BaseModel):
    """Stable per-turn context passed into runtime adapters."""

    agent_id: str
    session_id: str
    user_id: str = "default"
    channel: str = "console"
    chat_id: str | None = None
    run_id: str | None = None
    root_session_id: str | None = None
    workspace_dir: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeInput(BaseModel):
    """Input for one runtime turn."""

    request: RuntimeRequestContext
    messages: list[RuntimeMessage] = Field(default_factory=list)
    model_slot: str | None = None
    tool_policy_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FixtureRuntimeInput(RuntimeInput):
    """Trusted fixture input for dev-only dual-run comparisons."""


class ReplayRuntimeInput(RuntimeInput):
    """Trusted replay input for dev-only dual-run comparisons."""
