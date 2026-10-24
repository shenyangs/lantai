# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


ReminderStatus = Literal["new", "read", "snoozed", "dismissed", "done"]
ReminderSource = Literal["manual", "chat", "cron", "capability", "watch_topic"]
ReminderPriority = Literal["low", "normal", "high"]
NativeNotificationPolicy = Literal["off", "explicit_only"]
ReminderTriggerStatus = Literal["claimed", "deduped"]
ReminderDeliveryStatus = Literal["delivered", "deduped", "failed"]
WatchTopicBriefStatus = Literal[
    "unconfigured",
    "proposed",
    "confirmed",
    "paused",
    "archived",
]


class ReminderItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    summary: str = ""
    reason: str = ""
    source: ReminderSource = "manual"
    priority: ReminderPriority = "normal"
    status: ReminderStatus = "new"
    due_at: datetime | None = None
    snooze_until: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title cannot be empty")
        return title


class ReminderCreateRequest(BaseModel):
    title: str
    summary: str = ""
    reason: str = ""
    source: ReminderSource = "manual"
    priority: ReminderPriority = "normal"
    due_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReminderUpdateRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    reason: str | None = None
    source: ReminderSource | None = None
    priority: ReminderPriority | None = None
    status: ReminderStatus | None = None
    due_at: datetime | None = None
    snooze_until: datetime | None = None
    metadata: dict[str, Any] | None = None


class SnoozeReminderRequest(BaseModel):
    until: datetime


class ReminderTriggerResult(BaseModel):
    reminder_id: str
    status: ReminderTriggerStatus
    run_id: str | None = None
    reason: str = ""
    triggered_at: datetime
    dedupe_until: datetime | None = None
    attempt_id: str | None = None
    agent_id: str | None = None
    receipt_token: str | None = None


class ReminderDeliveryResult(BaseModel):
    reminder_id: str
    status: ReminderDeliveryStatus
    run_id: str | None = None
    reason: str = ""
    error: str = ""
    triggered_at: datetime
    dedupe_until: datetime | None = None


class ReminderDeliveryTickResult(BaseModel):
    checked_at: datetime
    agent_id: str
    claimed_count: int = 0
    delivered_count: int = 0
    deduped_count: int = 0
    failed_count: int = 0
    results: list[ReminderDeliveryResult] = Field(default_factory=list)


class WatchTopic(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    description: str = ""
    origin: str = "manual"
    enabled: bool = True
    frequency: str = "daily"
    revision: int = Field(default=1, ge=1)
    brief_status: WatchTopicBriefStatus = "unconfigured"
    brief_purpose: str = ""
    brief_scope_refs: list[str] = Field(default_factory=list)
    brief_evidence_refs: list[str] = Field(default_factory=list)
    brief_exclusions: list[str] = Field(default_factory=list)
    brief_valid_until: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_checked_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title cannot be empty")
        return title


class WatchTopicCreateRequest(BaseModel):
    title: str
    description: str = ""
    origin: str = "manual"
    enabled: bool = True
    frequency: str = "daily"
    metadata: dict[str, Any] = Field(default_factory=dict)


class WatchTopicUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    origin: str | None = None
    enabled: bool | None = None
    frequency: str | None = None
    last_checked_at: datetime | None = None
    metadata: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class WatchTopicBriefUseRequest(BaseModel):
    """An explicit, version-fenced decision about using a topic for briefs."""

    expected_revision: int = Field(ge=1)
    explicit_confirmation: bool = False
    status: Literal["confirmed", "paused", "archived"]
    purpose: str = ""
    scope_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    valid_until: datetime | None = None


class ReminderSettings(BaseModel):
    quiet_mode: bool = True
    daily_brief_enabled: bool = True
    native_notification_policy: NativeNotificationPolicy = "off"


class ReminderDeliveryClaim(BaseModel):
    run_id: str
    attempt_id: str
    agent_id: str
    reminder: ReminderItem
    owner_id: str
    owner_pid: int
    owner_birth: float
    receipt_token: str
    status: Literal["pending", "attempting", "submitted", "failed", "cancelled", "unknown"] = "pending"
    settled: bool = False
    native_ready: bool = False
    caller_id: str = ""
    receipt: dict[str, Any] | None = None
    error: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class NativeReminderClaimRequest(BaseModel):
    caller_id: str = Field(min_length=1, max_length=128)


class NativeReminderResolveRequest(BaseModel):
    run_id: str
    attempt_id: str
    agent_id: str
    reminder_id: str
    caller_id: str
    receipt_token: str
    ok: bool = Field(strict=True)
    shown: bool = Field(default=False, strict=True)
    id: str = ""
    status: Literal["shown", "failed", "unknown"]
    reason: str = ""
    error: str = ""


class RemindersFile(BaseModel):
    version: int = 1
    reminders: list[ReminderItem] = Field(default_factory=list)
    watch_topics: list[WatchTopic] = Field(default_factory=list)
    settings: ReminderSettings = Field(default_factory=ReminderSettings)
    delivery_claims: dict[str, ReminderDeliveryClaim] = Field(default_factory=dict)
    native_adapter_agent_id: str = ""
    native_adapter_ready_until: datetime | None = None


class TodayBrief(BaseModel):
    date: str
    items: list[ReminderItem] = Field(default_factory=list)
    new_count: int = 0
    due_count: int = 0
    high_value_count: int = 0
    quiet_mode: bool = True
