# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .profile_ids import canonicalize_profile_id


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


BrowserAccess = Literal["read", "write", "sensitive"]
BrowserRuntime = Literal[
    "electron_bridge",
    "browser_skill",
    "playwright_fallback",
]
BrowserRisk = Literal["low", "medium", "high", "critical"]
BrowserDecision = Literal["allow", "prompt", "block"]
BrowserAdapterStrategy = Literal[
    "PUBLIC",
    "LOGIN_UI",
    "COOKIE_READ_LIMITED",
    "NETWORK_INTERCEPT",
    "LOCAL_BROWSER",
]
BrowserConfirmPolicy = Literal["never", "write", "always"]
BrowserTaskStatus = Literal[
    "queued",
    "running_background",
    "waiting_for_user_confirmation",
    "manual_takeover_required",
    "paused",
    "completed",
    "failed",
    "cancelled",
]
BrowserTaskMode = Literal["background_observable", "foreground_observable"]
ComputerActionType = Literal[
    "screenshot_screen",
    "list_windows",
    "focus_window",
    "click_at",
    "type_text",
    "press_key",
    "drag",
]
ComputerDecision = Literal["allow", "prompt", "block"]


READ_ACTIONS: frozenset[str] = frozenset(
    {
        "status",
        "navigate",
        "open",
        "new_tab",
        "select_tab",
        "close_tab",
        "close_task_group",
        "tabs",
        "find",
        "snapshot",
        "snapshot_ax",
        "snapshot_dom",
        "screenshot",
        "take_screenshot",
        "network_list",
        "network_get",
        "network_requests",
        "console_list",
        "console_messages",
        "evaluate",
        "extract_table",
        "extract_tables",
        "extract_list",
        "extract_items",
        "performance_trace",
        "screencast_start",
        "screencast_stop",
        "download_wait",
        "pdf",
        "wait_for",
    }
)

WRITE_ACTIONS: frozenset[str] = frozenset(
    {
        "click",
        "click_at",
        "type",
        "fill_form",
        "select",
        "select_option",
        "hover",
        "drag",
        "upload_file",
        "press_key",
    }
)

SENSITIVE_ACTIONS: frozenset[str] = frozenset(
    {
        "click",
        "click_at",
        "type",
        "fill_form",
        "upload_file",
        "press_key",
        "select",
        "select_option",
    }
)


class BrowserProfile(BaseModel):
    id: str
    name: str
    description: str = ""
    allowed_domains: list[str] = Field(default_factory=list)
    agent_control_enabled: bool = False
    risk_policy: BrowserConfirmPolicy = "write"
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    last_used_at: str | None = None
    user_data_dir: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        normalized = canonicalize_profile_id(value)
        if not normalized:
            raise ValueError("profile id cannot be empty")
        if not all(
            ch.isascii() and (ch.isalnum() or ch in {"-", "_"})
            for ch in normalized
        ):
            raise ValueError("profile id must be alphanumeric, dash, or underscore")
        return normalized

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            domain = str(item or "").strip().lower()
            if not domain:
                continue
            domain = domain.removeprefix("http://").removeprefix("https://")
            domain = domain.split("/", 1)[0]
            if domain not in out:
                out.append(domain)
        return out


class BrowserTab(BaseModel):
    id: str
    profile_id: str = "default"
    title: str = ""
    url: str = "about:blank"
    loading: bool = False
    can_go_back: bool = False
    can_go_forward: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    last_active_at: str | None = None


class BrowserProfilePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    allowed_domains: list[str] | None = None
    agent_control_enabled: bool | None = None
    risk_policy: BrowserConfirmPolicy | None = None
    metadata: dict[str, Any] | None = None


class BrowserAction(BaseModel):
    action: str
    profile_id: str = "default"
    url: str = ""
    page_id: str = "default"
    selector: str = ""
    ref: str = ""
    text: str = ""
    code: str = ""
    path: str = ""
    access: BrowserAccess | None = None
    reason: str = ""
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    confirmed: bool = False
    runtime: BrowserRuntime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        action = value.strip().lower()
        if not action:
            raise ValueError("action required")
        return action


class BrowserActionConfirmation(BaseModel):
    action: BrowserAction
    approved: bool = False
    reason: str = ""


class BrowserTaskRequest(BaseModel):
    profile_id: str = "default"
    target_domains: list[str] = Field(default_factory=list)
    access: BrowserAccess = "read"
    mode: BrowserTaskMode = "background_observable"
    instruction: str = ""
    url: str = ""
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    actions: list[BrowserAction] = Field(default_factory=list)
    requires_foreground_confirmation: bool = True
    max_actions: int = 200
    max_duration_seconds: float = 1800.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_domains")
    @classmethod
    def normalize_task_domains(cls, value: list[str]) -> list[str]:
        return BrowserProfile.normalize_domains(value)

    @field_validator("max_actions")
    @classmethod
    def normalize_max_actions(cls, value: int) -> int:
        return max(1, min(int(value or 200), 1000))

    @field_validator("max_duration_seconds")
    @classmethod
    def normalize_max_duration_seconds(cls, value: float) -> float:
        return max(1.0, min(float(value or 1800.0), 24 * 60 * 60.0))


class BrowserTaskRun(BaseModel):
    run_id: str
    profile_id: str = "default"
    target_domains: list[str] = Field(default_factory=list)
    access: BrowserAccess = "read"
    mode: BrowserTaskMode = "background_observable"
    instruction: str = ""
    status: BrowserTaskStatus = "queued"
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    current_action: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str = ""
    replay_run_id: str = ""
    planned_action_count: int = 0
    completed_action_count: int = 0
    requires_foreground_confirmation: bool = True
    pending_confirmation: BrowserAction | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserTaskEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    ts: str = Field(default_factory=utc_now_iso)
    run_id: str
    type: str
    status: BrowserTaskStatus
    message: str = ""
    action: str = ""
    observation_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserTaskConfirmation(BaseModel):
    approved: bool = False
    reason: str = ""


class BrowserTakeoverReceipt(BaseModel):
    ready: bool
    run: BrowserTaskRun
    error_code: str = ""
    message: str = ""


class ComputerAction(BaseModel):
    action: ComputerActionType
    app_allowlist: list[str] = Field(default_factory=list)
    app_bundle_id: str = ""
    window_id: str = ""
    process_id: int | None = Field(default=None, ge=1)
    window_title: str = ""
    x: int | None = None
    y: int | None = None
    text: str = ""
    key: str = ""
    confirmed: bool = False
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("app_allowlist")
    @classmethod
    def normalize_app_allowlist(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            app = str(item or "").strip()
            if app and app not in out:
                out.append(app)
        return out


class ComputerPolicyDecision(BaseModel):
    decision: ComputerDecision
    reason: str
    risk: BrowserRisk = "low"
    requires_confirmation: bool = False
    matched_rule_id: str | None = None


class ComputerObservation(BaseModel):
    ok: bool
    action: ComputerActionType
    run_id: str
    message: str = ""
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    decision: ComputerPolicyDecision | None = None
    replay_event_id: str = Field(default_factory=lambda: uuid4().hex)


class BrowserExportApproval(BaseModel):
    approved: bool = False
    reason: str = ""
    requested_by: str = ""
    scope: str = "single_export"
    created_at: str = Field(default_factory=utc_now_iso)

    @field_validator("reason", "requested_by", "scope")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value or "").strip()


class BrowserPolicyDecision(BaseModel):
    decision: BrowserDecision
    reason: str
    risk: BrowserRisk = "low"
    access: BrowserAccess = "read"
    requires_confirmation: bool = False
    matched_rule_id: str | None = None


class BrowserObservation(BaseModel):
    ok: bool
    action: str
    runtime: BrowserRuntime
    profile_id: str
    page_id: str = "default"
    url: str = ""
    message: str = ""
    error: str | None = None
    screenshot_path: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    decision: BrowserPolicyDecision | None = None
    replay_event_id: str = Field(default_factory=lambda: uuid4().hex)


class BrowserActionResult(BaseModel):
    ok: bool
    action: str
    runtime: BrowserRuntime
    profile_id: str
    page_id: str = "default"
    url: str = ""
    message: str = ""
    error: str | None = None
    screenshot_path: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class BrowserReplayEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    ts: str = Field(default_factory=utc_now_iso)
    run_id: str
    profile_id: str
    action: str
    runtime: BrowserRuntime | None = None
    access: BrowserAccess = "read"
    risk: BrowserRisk = "low"
    decision: BrowserDecision = "allow"
    reason: str = ""
    ok: bool | None = None
    url: str = ""
    page_id: str = "default"
    error: str | None = None
    screenshot_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LantaiCliAdapterManifest(BaseModel):
    id: str
    domain: str
    strategy: BrowserAdapterStrategy = "LOGIN_UI"
    access: BrowserAccess = "read"
    required_profile: bool = True
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    confirm_policy: BrowserConfirmPolicy = "write"
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def normalize_adapter_id(cls, value: str) -> str:
        adapter_id = value.strip().lower()
        if not adapter_id:
            raise ValueError("adapter id cannot be empty")
        if len(adapter_id) > 96:
            raise ValueError("adapter id must be 96 characters or fewer")
        if not all(ch.isalnum() or ch in {".", "-", "_"} for ch in adapter_id):
            raise ValueError("adapter id must use letters, numbers, dot, dash, or underscore")
        if ".." in adapter_id or adapter_id.startswith(".") or adapter_id.endswith("."):
            raise ValueError("adapter id has invalid dot separators")
        return adapter_id

    @field_validator("domain")
    @classmethod
    def normalize_adapter_domain(cls, value: str) -> str:
        domain = value.strip().lower()
        if not domain:
            raise ValueError("adapter domain cannot be empty")
        if len(domain) > 120:
            raise ValueError("adapter domain must be 120 characters or fewer")
        if domain == "*":
            return domain
        domain = domain.removeprefix("http://").removeprefix("https://")
        domain = domain.split("/", 1)[0]
        if not all(ch.isalnum() or ch in {".", "-"} for ch in domain):
            raise ValueError("adapter domain must be a hostname or wildcard")
        return domain

    @field_validator("inputs", "outputs")
    @classmethod
    def normalize_adapter_fields(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value[:20]:
            field = str(item or "").strip().lower()
            if not field:
                continue
            if len(field) > 64:
                raise ValueError("adapter input/output field names must be 64 characters or fewer")
            if not all(ch.isalnum() or ch in {"_", "-"} for ch in field):
                raise ValueError("adapter input/output field names must be simple identifiers")
            if field not in out:
                out.append(field)
        return out

    @field_validator("description")
    @classmethod
    def normalize_adapter_description(cls, value: str) -> str:
        return value.strip()[:240]


class AdapterRunRequest(BaseModel):
    profile_id: str = "default"
    inputs: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False
    run_id: str = Field(default_factory=lambda: uuid4().hex)


class DomainSkillDraft(BaseModel):
    domain: str
    title: str
    summary: str = ""
    url_patterns: list[str] = Field(default_factory=list)
    selectors: list[str] = Field(default_factory=list)
    wait_conditions: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


class DomainSkillDraftRecord(BaseModel):
    id: str
    path: str
    draft: DomainSkillDraft
    enabled: bool = False
    updated_at: str = ""
    source_run_id: str = ""
    recommended_adapter_id: str = ""


class DomainSkillDraftPatch(BaseModel):
    title: str | None = None
    summary: str | None = None
    url_patterns: list[str] | None = None
    selectors: list[str] | None = None
    wait_conditions: list[str] | None = None
    failure_modes: list[str] | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class ContentSource(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    source_type: Literal[
        "web_article",
        "social_post",
        "video_page",
        "comment_thread",
        "search_result",
        "manual_note",
    ] = "web_article"
    title: str = ""
    url: str = ""
    domain: str = ""
    author: str = ""
    published_at: str | None = None
    captured_at: str = Field(default_factory=utc_now_iso)
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    evidence_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentDossier(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    topic: str
    brief: str = ""
    sources: list[ContentSource] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    timeline: list[str] = Field(default_factory=list)
    angles: list[str] = Field(default_factory=list)
    outline: list[str] = Field(default_factory=list)
    draft: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentWorkflowRun(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    workflow: Literal[
        "browser_research_pack",
        "journalist_dossier",
        "editor_topic_planning",
        "self_media_series",
        "marketing_campaign_watch",
        "comment_insight",
        "wechat_official_account_series",
    ]
    topic: str
    status: Literal["drafting", "ready", "blocked", "failed"] = "drafting"
    profile_id: str = "default"
    source_count: int = 0
    dossier_id: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentSeriesPlan(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    title: str
    audience: str = ""
    positioning: str = ""
    cadence: str = ""
    article_count: int = 0
    topics: list[str] = Field(default_factory=list)
    calendar: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublishingDraft(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    platform: Literal["wechat_official_account", "local_markdown", "other"] = (
        "local_markdown"
    )
    title: str
    body: str = ""
    summary: str = ""
    status: Literal[
        "local_draft",
        "ready_for_review",
        "write_requested",
        "saved_to_platform",
        "blocked",
    ] = "local_draft"
    source_dossier_id: str | None = None
    platform_url: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublishingQueueItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    draft_id: str
    platform: Literal["wechat_official_account", "local_markdown", "other"] = (
        "wechat_official_account"
    )
    scheduled_for: str | None = None
    status: Literal[
        "queued",
        "scheduled",
        "needs_confirmation",
        "ready_to_write",
        "write_requested",
        "saved",
        "blocked",
        "archived",
    ] = "queued"
    requires_confirmation: bool = True
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)
