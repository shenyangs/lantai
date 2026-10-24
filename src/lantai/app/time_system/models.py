# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


TimeItemKind = Literal["reminder", "task", "automation"]


class TimeItem(BaseModel):
    """A read-only projection of a time-bearing source object."""

    id: str
    kind: TimeItemKind
    source: str
    source_label: str
    title: str
    summary: str = ""
    start_at: datetime | None = None
    end_at: datetime | None = None
    all_day: bool = False
    blocking: bool = False
    status: str = ""
    action_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TodayTimeline(BaseModel):
    date: str
    timezone: str
    generated_at: datetime = Field(default_factory=utc_now)
    items: list[TimeItem] = Field(default_factory=list)
    counts: dict[TimeItemKind, int] = Field(default_factory=dict)
