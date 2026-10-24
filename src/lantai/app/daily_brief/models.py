# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


DailyBriefHomeState = Literal[
    "not_configured",
    "declined",
    "generating",
    "ready",
    "no_update",
    "partial",
    "failed",
    "not_run",
    "paused",
    "stale",
    "hidden",
]


class DailyBriefHomeCard(StrictModel):
    """Metadata-only projection for the compact new-chat entry card."""

    title: str = "澜台简报"
    status_line: str = Field(min_length=1, max_length=48)
    hint: str = Field(default="", max_length=64)
    route: str = "/daily-brief"
    edition_id: str | None = None
    edition_revision: int | None = Field(default=None, ge=1)
    edition_date: date | None = None
    item_count: int | None = Field(default=None, ge=0)
    has_update: bool = False
    is_stale: bool = False


class DailyBriefHomeSnapshot(StrictModel):
    """Read-only metadata; loading it never consents, researches, or generates."""

    state: DailyBriefHomeState = "not_configured"
    visible: bool = True
    card: DailyBriefHomeCard = Field(
        default_factory=lambda: DailyBriefHomeCard(
            status_line="尚未开启",
            hint="开启后由澜台自动发现并整理",
        )
    )


BriefGrantStatus = Literal["active", "revoked", "expired", "consumed"]
CloudProcessingPolicy = Literal["off", "public_only", "approved_context"]
BriefSubscriptionStatus = Literal["active", "paused", "archived"]
BriefCycleStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
    "invalidated",
]
EditionOutcome = Literal["ready", "no_material_update", "partial"]
EditionStatus = Literal["draft", "published", "withdrawn", "superseded"]
CoverageStatus = Literal["complete", "partial", "failed", "skipped"]
ReadLevel = Literal["search_snippet", "source_page", "primary_document"]
SourceKind = Literal["primary", "republished", "secondary"]
DeliveryState = Literal[
    "pending",
    "submitted",
    "unknown",
    "acknowledged",
    "read",
    "failed",
    "suppressed",
]


class BriefBudget(StrictModel):
    max_searches: int = Field(default=8, ge=0, le=100)
    max_source_reads: int = Field(default=12, ge=0, le=100)
    max_model_tokens: int = Field(default=20_000, ge=0, le=2_000_000)
    max_cost_usd: float = Field(default=1.0, ge=0, le=1_000)
    max_cost_cny: float = Field(default=0, ge=0, le=10_000)


class BriefGrant(StrictModel):
    id: str = Field(default_factory=lambda: new_id("grant"))
    revision: int = Field(default=1, ge=1)
    status: BriefGrantStatus = "active"
    one_time: bool = False
    topic_ids: list[str] = Field(default_factory=list)
    public_source_discovery: bool = False
    public_source_read: bool = False
    cloud_processing: CloudProcessingPolicy = "off"
    allowed_context_refs: list[str] = Field(default_factory=list)
    search_provider: str = ""
    model_provider: str = ""
    model_policy: Literal["free_only", "configured"] = "free_only"
    budget: BriefBudget = Field(default_factory=BriefBudget)
    granted_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    def is_effective(self, now: datetime | None = None) -> bool:
        instant = now or utc_now()
        return self.status == "active" and (
            self.expires_at is None or self.expires_at > instant
        )


class BriefGrantCreateRequest(StrictModel):
    explicit_consent: bool = False
    one_time: bool = False
    topic_ids: list[str] = Field(default_factory=list)
    public_source_discovery: bool = False
    public_source_read: bool = False
    cloud_processing: CloudProcessingPolicy = "off"
    allowed_context_refs: list[str] = Field(default_factory=list)
    search_provider: str = ""
    model_provider: str = ""
    budget: BriefBudget = Field(default_factory=BriefBudget)
    expires_at: datetime | None = None
    action_id: str = Field(min_length=1)


class BriefSubscription(StrictModel):
    id: str = Field(default_factory=lambda: new_id("subscription"))
    revision: int = Field(default=1, ge=1)
    status: BriefSubscriptionStatus = "active"
    grant_id: str
    grant_revision: int = Field(ge=1)
    topic_revisions: dict[str, int] = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    local_time: time = time(hour=8)
    schedule_revision: int = Field(default=1, ge=1)
    home_visible: bool = True
    native_notifications: bool = False
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    last_successful_check_at: datetime | None = None
    last_successful_local_date: date | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


class BriefSubscriptionCreateRequest(StrictModel):
    grant_id: str
    topic_ids: list[str] = Field(min_length=1)
    timezone: str = "Asia/Shanghai"
    local_time: time = time(hour=8)
    home_visible: bool = True
    native_notifications: bool = False
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    action_id: str = Field(min_length=1)


class BriefSubscriptionUpdateRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    status: BriefSubscriptionStatus | None = None
    timezone: str | None = None
    local_time: time | None = None
    home_visible: bool | None = None
    native_notifications: bool | None = None
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    action_id: str = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


BriefActivationMode = Literal["preview", "daily"]


class BriefActivationRequest(StrictModel):
    """One explicit, product-level action; provider details stay internal."""

    explicit_consent: bool = False
    configured_model_consent: bool = False
    mode: BriefActivationMode = "daily"
    topic_ids: list[str] = Field(default_factory=list)
    use_memory_clues: bool = True
    memory_context_processing_consent: bool = False
    timezone: str = "Asia/Shanghai"
    local_time: time = time(hour=8)
    home_visible: bool = True
    native_notifications: bool = False
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    action_id: str = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


class BriefCycle(StrictModel):
    id: str = Field(default_factory=lambda: new_id("cycle"))
    logical_key: str
    subscription_id: str | None = None
    grant_id: str
    grant_revision: int = Field(ge=1)
    topic_revisions: dict[str, int] = Field(default_factory=dict)
    occurrence_id: str
    local_date: date
    timezone: str = "Asia/Shanghai"
    schedule_revision: int = Field(default=1, ge=1)
    status: BriefCycleStatus = "queued"
    attempt_count: int = Field(default=0, ge=0)
    manual: bool = False
    requested_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_reason: str = ""
    edition_id: str | None = None
    supersedes_cycle_id: str | None = None
    execution_token: str = ""
    execution_started_at: datetime | None = None
    lease_expires_at: datetime | None = None
    stage: str = "queued"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


class BriefActivationResult(StrictModel):
    grant: BriefGrant
    topic_ids: list[str]
    discovered_topic_ids: list[str] = Field(default_factory=list)
    subscription: BriefSubscription | None = None
    cycle: BriefCycle | None = None
    execution_state: Literal["scheduled", "queued", "running", "unavailable"] = "scheduled"


class BriefCoverage(StrictModel):
    topic_id: str
    status: CoverageStatus
    checked_at: datetime | None = None
    query_count: int = Field(default=0, ge=0)
    source_read_count: int = Field(default=0, ge=0)
    note: str = ""


class BriefEvidence(StrictModel):
    id: str = Field(default_factory=lambda: new_id("evidence"))
    canonical_url: str
    original_url: str
    origin_group: str = ""
    source_kind: SourceKind = "primary"
    publisher: str = ""
    title: str = ""
    published_at: datetime | None = None
    event_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=utc_now)
    read_level: ReadLevel
    evidence_text: str = Field(min_length=1, max_length=8_000)
    content_hash: str = Field(min_length=16)
    retracted: bool = False

    @field_validator("canonical_url", "original_url")
    @classmethod
    def validate_public_web_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source URL must use http or https")
        if parsed.username or parsed.password:
            raise ValueError("source URL must not contain credentials")
        return value


class BriefItem(StrictModel):
    id: str = Field(default_factory=lambda: new_id("item"))
    topic_id: str
    title: str = Field(min_length=1)
    fact: str = Field(min_length=1)
    relevance: str = Field(min_length=1)
    analysis: str = ""
    limitations: str = ""
    optional_action: str = ""
    evidence_ids: list[str] = Field(min_length=1)
    private_dependency_refs: list[str] = Field(default_factory=list)
    related_work_refs: list[str] = Field(default_factory=list)
    explicit_work_association: bool = False

    @model_validator(mode="after")
    def validate_work_association(self) -> "BriefItem":
        if self.related_work_refs and not self.explicit_work_association:
            raise ValueError("related work requires an explicit user association")
        return self


class BriefTrend(StrictModel):
    id: str = Field(default_factory=lambda: new_id("trend"))
    topic_id: str
    revision: int = Field(default=1, ge=1)
    statement: str = Field(min_length=1)
    support_evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    falsifiers: list[str] = Field(default_factory=list)
    uncertainty: str = ""
    supersedes_id: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_external_support(self) -> "BriefTrend":
        if not self.support_evidence_ids:
            raise ValueError("a trend requires source evidence, not a prior conclusion")
        return self


class BriefEdition(StrictModel):
    id: str = Field(default_factory=lambda: new_id("edition"))
    revision: int = Field(default=1, ge=1)
    cycle_id: str
    subscription_id: str | None = None
    grant_id: str
    grant_revision: int = Field(ge=1)
    topic_revisions: dict[str, int] = Field(default_factory=dict)
    status: EditionStatus = "draft"
    outcome: EditionOutcome
    local_date: date
    timezone: str
    title: str = "澜台简报"
    summary: str = ""
    coverage: list[BriefCoverage] = Field(default_factory=list)
    evidence: list[BriefEvidence] = Field(default_factory=list)
    items: list[BriefItem] = Field(default_factory=list, max_length=12)
    trends: list[BriefTrend] = Field(default_factory=list)
    private_dependency_refs: list[str] = Field(default_factory=list)
    checked_started_at: datetime
    checked_finished_at: datetime
    published_at: datetime | None = None
    supersedes_id: str | None = None
    correction_note: str = ""
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_outcome_evidence(self) -> "BriefEdition":
        if self.checked_finished_at < self.checked_started_at:
            raise ValueError("check window is reversed")
        if self.status == "withdrawn":
            return self
        statuses = {entry.status for entry in self.coverage}
        if self.outcome == "no_material_update":
            if self.items or statuses - {"complete"}:
                raise ValueError(
                    "no_material_update requires zero items and complete checks"
                )
        elif self.outcome == "ready":
            if not self.items or statuses - {"complete"}:
                raise ValueError("ready requires items and complete checks")
        elif not self.items or not statuses.intersection({"partial", "failed"}):
            raise ValueError("partial requires items and an incomplete check")

        evidence_ids = {entry.id for entry in self.evidence if not entry.retracted}
        referenced = {
            evidence_id
            for item in self.items
            for evidence_id in item.evidence_ids
        }
        referenced.update(
            evidence_id
            for trend in self.trends
            for evidence_id in [
                *trend.support_evidence_ids,
                *trend.counter_evidence_ids,
            ]
        )
        if not referenced.issubset(evidence_ids):
            raise ValueError("every claim must reference present, unretracted evidence")
        return self


class BriefPublishRequest(StrictModel):
    cycle_id: str
    execution_token: str = ""
    expected_grant_revision: int = Field(ge=1)
    expected_topic_revisions: dict[str, int]
    outcome: EditionOutcome
    local_date: date
    timezone: str
    title: str = "澜台简报"
    summary: str = ""
    coverage: list[BriefCoverage]
    evidence: list[BriefEvidence] = Field(default_factory=list)
    items: list[BriefItem] = Field(default_factory=list, max_length=12)
    trends: list[BriefTrend] = Field(default_factory=list)
    private_dependency_refs: list[str] = Field(default_factory=list)
    checked_started_at: datetime
    checked_finished_at: datetime
    supersedes_id: str | None = None
    correction_note: str = ""

    @model_validator(mode="after")
    def validate_claimed_outcome(self) -> "BriefPublishRequest":
        statuses = {entry.status for entry in self.coverage}
        if self.outcome == "no_material_update":
            if self.items or statuses - {"complete"}:
                raise ValueError(
                    "no_material_update requires zero items and complete checks"
                )
        elif self.outcome == "ready":
            if not self.items or statuses - {"complete"}:
                raise ValueError("ready requires items and complete checks")
        elif not self.items or not statuses.intersection({"partial", "failed"}):
            raise ValueError("partial requires items and an incomplete check")
        return self


class BriefFeedback(StrictModel):
    id: str = Field(default_factory=lambda: new_id("feedback"))
    edition_id: str
    item_id: str | None = None
    kind: Literal["useful", "not_useful", "incorrect", "too_much", "save"]
    note: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class BriefFeedbackCreateRequest(StrictModel):
    kind: Literal["useful", "not_useful", "incorrect", "too_much", "save"]
    note: str = ""
    action_id: str = Field(min_length=1)


class BriefContextRef(StrictModel):
    edition_id: str
    edition_revision: int = Field(ge=1)
    item_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class BriefDiscussionDraft(StrictModel):
    id: str = Field(default_factory=lambda: new_id("discussion"))
    status: Literal["draft", "sent", "invalidated"] = "draft"
    context_ref: BriefContextRef
    text: str
    private_dependency_refs: list[str] = Field(default_factory=list)
    command_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    invalidated_at: datetime | None = None


class BriefDiscussionDraftView(BriefDiscussionDraft):
    composer_text: str


class BriefDiscussionDraftCreateRequest(StrictModel):
    edition_revision: int = Field(ge=1)
    item_id: str | None = None
    text: str = Field(min_length=1, max_length=4_000)
    action_id: str = Field(min_length=1)


class BriefDeliveryReceipt(StrictModel):
    id: str = Field(default_factory=lambda: new_id("delivery"))
    edition_id: str
    edition_revision: int = Field(default=1, ge=1)
    subscription_id: str
    channel: Literal["home", "native"]
    state: DeliveryState = "pending"
    attempt_count: int = Field(default=0, ge=0)
    adapter_message_id: str | None = None
    last_attempt_at: datetime | None = None
    acknowledged_at: datetime | None = None
    read_at: datetime | None = None
    error: str = ""


class BriefCostRecord(StrictModel):
    id: str = Field(default_factory=lambda: new_id("cost"))
    cycle_id: str
    search_count: int = Field(default=0, ge=0)
    source_read_count: int = Field(default=0, ge=0)
    model_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cost_cny: float = Field(default=0.0, ge=0)
    model_usage_status: Literal["not_used", "recorded", "unknown"] = "not_used"
    recorded_at: datetime = Field(default_factory=utc_now)


class BriefSavedItem(StrictModel):
    id: str = Field(default_factory=lambda: new_id("saved"))
    edition_id: str
    edition_revision: int = Field(ge=1)
    item_id: str
    saved_at: datetime = Field(default_factory=utc_now)


class BriefCycleView(StrictModel):
    id: str
    subscription_id: str | None = None
    status: BriefCycleStatus
    stage: str
    local_date: date
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    supersedes_cycle_id: str | None = None
    failure_code: str = ""
    message: str = ""
    retryable: bool = False
    usage_uncertain: bool = False
    cost_cny: float = Field(default=0.0, ge=0)
    usage: BriefBudget = Field(default_factory=lambda: BriefBudget(
        max_searches=0, max_source_reads=0, max_model_tokens=0, max_cost_usd=0))


class BriefRuntimeStatus(StrictModel):
    enabled: bool = False
    running: bool = False
    message: str = "后台执行器状态尚未确认"
    last_tick_at: datetime | None = None


class BriefModelCapability(StrictModel):
    ready: bool
    code: str
    message: str
    model_label: str = ""


class BriefFeatureFlags(StrictModel):
    homepage_entry: bool = True
    research: bool = True
    native_notification: bool = False


class DailyBriefStore(StrictModel):
    version: int = 2
    feature_flags: BriefFeatureFlags = Field(default_factory=BriefFeatureFlags)
    invitation_hidden: bool = False
    grants: list[BriefGrant] = Field(default_factory=list)
    subscriptions: list[BriefSubscription] = Field(default_factory=list)
    cycles: list[BriefCycle] = Field(default_factory=list)
    editions: list[BriefEdition] = Field(default_factory=list)
    feedback: list[BriefFeedback] = Field(default_factory=list)
    discussion_drafts: list[BriefDiscussionDraft] = Field(default_factory=list)
    delivery_receipts: list[BriefDeliveryReceipt] = Field(default_factory=list)
    costs: list[BriefCostRecord] = Field(default_factory=list)
    processed_action_ids: list[str] = Field(default_factory=list)
    action_results: dict[str, str] = Field(default_factory=dict)
    activation_results: dict[str, BriefActivationResult] = Field(default_factory=dict)
    action_fingerprints: dict[str, str] = Field(default_factory=dict)
    read_revisions: dict[str, int] = Field(default_factory=dict)
    edition_aliases: dict[str, str] = Field(default_factory=dict)
    saved_items: list[BriefSavedItem] = Field(default_factory=list)


class BriefStorageStatus(StrictModel):
    store_bytes: int = 0
    edition_count: int = 0
    cycle_count: int = 0
    cost_count: int = 0
    capacity_warning: bool = False
    warning_threshold_bytes: int = 20 * 1024 * 1024


class BriefWorkspaceSnapshot(StrictModel):
    grants: list[BriefGrant]
    subscriptions: list[BriefSubscription]
    latest_edition: BriefEdition | None
    history: list[BriefEdition]
    topics: list[dict[str, object]]
    feature_flags: BriefFeatureFlags
    invitation_hidden: bool
    cycles: list[BriefCycleView] = Field(default_factory=list)
    runtime: BriefRuntimeStatus = Field(default_factory=BriefRuntimeStatus)
    activation_state: str = "not_configured"
    history_total: int = 0
    saved_items: list[BriefSavedItem] = Field(default_factory=list)
    subscription_issues: dict[str, str] = Field(default_factory=dict)
    storage: BriefStorageStatus = Field(default_factory=BriefStorageStatus)
