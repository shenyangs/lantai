# -*- coding: utf-8 -*-
"""Strict contracts for the Browser Agent loop.

These models are deliberately separate from the compatibility BrowserTask
projection.  They describe one attempt's observations and decisions, but do
not create another authoritative long-running state store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models import BrowserAccess, BrowserAction, BrowserRuntime
from ..policy import canonical_action_name


BrowserAgentPhase = Literal[
    "observing",
    "planning",
    "checking_policy",
    "executing",
    "verifying",
    "recovering",
]
BrowserDecisionKind = Literal["action", "complete", "ask_user", "takeover", "fail"]
BrowserVerificationStatus = Literal[
    "verified",
    "not_met",
    "indeterminate",
    "blocked",
]
BrowserExpectedOutcomeKind = Literal[
    "action_ok",
    "url_contains",
    "url_equals",
    "domain_is",
    "title_contains",
    "text_contains",
    "text_absent",
    "element_present",
    "element_absent",
    "element_enabled",
    "element_value_changed",
    "tab_count_delta",
    "page_changed",
    "download_present",
]
BrowserRecoveryKind = Literal[
    "refresh_observation",
    "retry",
    "rediscover",
    "visual_fallback",
    "replan",
    "ask_user",
    "takeover",
    "stop",
]
BrowserAgentStopReason = Literal[
    "goal_complete",
    "waiting_confirmation",
    "manual_takeover",
    "cancelled",
    "paused",
    "action_budget",
    "decision_budget",
    "token_budget",
    "cost_budget",
    "replan_budget",
    "duration_budget",
    "no_progress",
    "repeated_failure",
    "policy_blocked",
    "planner_invalid",
    "failed",
]

_MODEL_BROWSER_ACTIONS = frozenset(
    {
        "navigate",
        "open",
        "new_tab",
        "select_tab",
        "close_tab",
        "tabs",
        "snapshot",
        "screenshot",
        "network_list",
        "console_list",
        "extract_table",
        "extract_tables",
        "extract_list",
        "extract_items",
        "download_wait",
        "pdf",
        "wait_for",
        "click",
        "click_at",
        "type",
        "fill_form",
        "select",
        "hover",
        "drag",
        "press_key",
    }
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrowserAgentBudget(StrictContract):
    profile: Literal["quick", "standard", "long_read", "draft_write"]
    max_actions: int = Field(ge=1, le=1000)
    max_model_decisions: int = Field(ge=1, le=500)
    max_model_tokens: int = Field(default=50_000, ge=1, le=10_000_000)
    max_cost_cny: float = Field(default=20.0, ge=0, le=100_000)
    max_replans: int = Field(ge=0, le=100)
    max_duration_seconds: float = Field(gt=0, le=24 * 60 * 60)
    max_same_failure: int = Field(default=2, ge=1, le=10)
    max_no_progress: int = Field(default=3, ge=1, le=20)
    max_screenshots: int = Field(default=8, ge=0, le=200)
    max_observation_chars: int = Field(default=18_000, ge=1000, le=100_000)
    max_interactive_elements: int = Field(default=250, ge=10, le=2000)


BROWSER_AGENT_BUDGETS: dict[str, BrowserAgentBudget] = {
    "quick": BrowserAgentBudget(
        profile="quick",
        max_actions=15,
        max_model_decisions=8,
        max_model_tokens=12_000,
        max_cost_cny=5.0,
        max_replans=2,
        max_duration_seconds=180,
        max_screenshots=3,
    ),
    "standard": BrowserAgentBudget(
        profile="standard",
        max_actions=50,
        max_model_decisions=25,
        max_model_tokens=50_000,
        max_cost_cny=20.0,
        max_replans=5,
        max_duration_seconds=600,
        max_screenshots=8,
    ),
    "long_read": BrowserAgentBudget(
        profile="long_read",
        max_actions=120,
        max_model_decisions=50,
        max_model_tokens=120_000,
        max_cost_cny=50.0,
        max_replans=8,
        max_duration_seconds=1800,
        max_screenshots=15,
        max_observation_chars=24_000,
    ),
    "draft_write": BrowserAgentBudget(
        profile="draft_write",
        max_actions=30,
        max_model_decisions=20,
        max_model_tokens=40_000,
        max_cost_cny=30.0,
        max_replans=4,
        max_duration_seconds=600,
        max_screenshots=8,
    ),
}


def browser_agent_budget(profile: str) -> BrowserAgentBudget:
    return BROWSER_AGENT_BUDGETS.get(profile, BROWSER_AGENT_BUDGETS["standard"]).model_copy(
        deep=True
    )


class BrowserTabState(StrictContract):
    id: str
    title: str = ""
    url: str = ""
    active: bool = False
    loading: bool = False


class BrowserTextBlock(StrictContract):
    source: Literal["document", "element", "table", "list", "network", "console"]
    text: str
    ref: str = ""
    tainted: bool = True


class BrowserInteractiveElement(StrictContract):
    ref: str
    target_kind: Literal["selector", "runtime_ref"] = "selector"
    selector: str = ""
    role: str = ""
    name: str = ""
    tag: str = ""
    input_type: str = ""
    value_state: Literal["empty", "present", "redacted", "unknown"] = "unknown"
    value_hash: str = ""
    enabled: bool = True
    visible: bool = True
    selected: bool | None = None
    checked: bool | None = None
    frame_selector: str = ""
    shadow_host_selector: str = ""


class BrowserSecuritySignal(StrictContract):
    code: str
    severity: Literal["info", "low", "medium", "high", "critical"] = "medium"
    source: str = "page"
    evidence_excerpt: str = ""
    blocks_write: bool = False
    blocks_cross_domain: bool = False


class BrowserPageState(StrictContract):
    schema_version: Literal[1] = 1
    page_revision: str
    content_hash: str
    progress_hash: str = ""
    observed_at: datetime = Field(default_factory=utc_now)
    runtime: BrowserRuntime
    profile_id: str
    page_id: str = "default"
    url: str = ""
    title: str = ""
    domain: str = ""
    loading: bool = False
    tabs: tuple[BrowserTabState, ...] = ()
    text_blocks: tuple[BrowserTextBlock, ...] = ()
    interactive_elements: tuple[BrowserInteractiveElement, ...] = ()
    screenshot_artifact_ref: str = ""
    screenshot_sha256: str = ""
    downloads: tuple[str, ...] = ()
    network_errors: tuple[str, ...] = ()
    console_errors: tuple[str, ...] = ()
    security_signals: tuple[BrowserSecuritySignal, ...] = ()
    page_context_kind: str = "normal"
    requires_manual_takeover: bool = False
    truncated: bool = False
    raw_observation_hash: str = ""

    @field_validator("domain", mode="before")
    @classmethod
    def derive_domain(cls, value: str, info):
        if str(value or "").strip():
            return str(value).strip().lower()
        url = str(info.data.get("url") or "")
        return (urlparse(url).hostname or "").lower()

    def visible_text(self) -> str:
        return "\n".join(block.text for block in self.text_blocks if block.text)

    def element(self, ref: str) -> BrowserInteractiveElement | None:
        return next((item for item in self.interactive_elements if item.ref == ref), None)


class BrowserExpectedOutcome(StrictContract):
    kind: BrowserExpectedOutcomeKind
    value: str = ""
    ref: str = ""
    delta: int = 0
    description: str = ""

    @model_validator(mode="after")
    def validate_target(self) -> "BrowserExpectedOutcome":
        if self.kind.startswith("element_") and not self.ref:
            raise ValueError(f"{self.kind} requires ref")
        if self.kind in {
            "url_contains",
            "url_equals",
            "domain_is",
            "title_contains",
            "text_contains",
            "text_absent",
            "download_present",
        } and not self.value:
            raise ValueError(f"{self.kind} requires value")
        return self


class BrowserActionDraft(StrictContract):
    action: str
    url: str = ""
    page_id: str = ""
    selector: str = ""
    ref: str = ""
    text: str = ""
    path: str = ""
    access: BrowserAccess | None = None
    reason: str = ""
    metadata: dict[str, str | int | float | bool | list[str]] = Field(
        default_factory=dict
    )

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        action = canonical_action_name(value)
        if not action:
            raise ValueError("action required")
        if action not in _MODEL_BROWSER_ACTIONS:
            raise ValueError(f"planner may not request unsupported action {action}")
        return action

    @model_validator(mode="after")
    def reject_confirmation_escalation(self) -> "BrowserActionDraft":
        forbidden = {
            "browser_agent_element_input_type",
            "browser_agent_element_name",
            "browser_agent_element_role",
            "browser_agent_element_tag",
            "browser_read_task_token",
            "confirmed",
            "browser_confirmation_token",
            "file_grant_id",
            "cookie",
            "authorization",
            "token",
            "password",
        }
        normalized_keys = {
            str(key).strip().lower().replace("-", "_") for key in self.metadata
        }
        overlap = forbidden.intersection(normalized_keys)
        if overlap:
            raise ValueError(
                "planner metadata contains forbidden keys: " + ", ".join(sorted(overlap))
            )
        return self

    def to_action(
        self,
        *,
        profile_id: str,
        run_id: str,
        default_access: BrowserAccess,
        current_page_id: str,
    ) -> BrowserAction:
        return BrowserAction(
            action=self.action,
            profile_id=profile_id,
            run_id=run_id,
            page_id=self.page_id or current_page_id or "default",
            url=self.url,
            selector=self.selector,
            ref=self.ref,
            text=self.text,
            path=self.path,
            access=self.access or default_access,
            reason=self.reason,
            confirmed=False,
            metadata=dict(self.metadata),
        )


class BrowserNextDecision(StrictContract):
    schema_version: Literal[1] = 1
    kind: BrowserDecisionKind
    action: BrowserActionDraft | None = None
    expected_outcomes: tuple[BrowserExpectedOutcome, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    reason: str = Field(default="", max_length=800)
    confidence: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def validate_decision(self) -> "BrowserNextDecision":
        if self.kind == "action":
            if self.action is None:
                raise ValueError("action decision requires action")
            if not self.expected_outcomes:
                raise ValueError("action decision requires expected_outcomes")
        elif self.action is not None:
            raise ValueError(f"{self.kind} decision may not include action")
        return self


class BrowserStepOutcome(StrictContract):
    schema_version: Literal[1] = 1
    status: BrowserVerificationStatus
    matched_expectations: tuple[str, ...] = ()
    failed_expectations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    failure_fingerprint: str = ""
    safe_to_retry: bool = False
    reason: str = ""


class BrowserRecoveryDecision(StrictContract):
    kind: BrowserRecoveryKind
    reason: str
    failure_fingerprint: str = ""
    retry_action: bool = False


class BrowserAgentHistoryItem(StrictContract):
    index: int = Field(ge=0)
    decision: BrowserNextDecision
    before_revision: str
    after_revision: str = ""
    before_progress_hash: str = ""
    after_progress_hash: str = ""
    outcome: BrowserStepOutcome | None = None


class BrowserAgentUsage(StrictContract):
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_cny: float = Field(default=0.0, ge=0)


class BrowserAgentContext(StrictContract):
    run_id: str
    attempt_id: str
    instruction: str
    start_url: str = ""
    profile_id: str
    target_domains: tuple[str, ...] = ()
    access: BrowserAccess = "read"
    page_state: BrowserPageState
    budget: BrowserAgentBudget
    action_count: int = 0
    model_decision_count: int = 0
    replan_count: int = 0
    screenshot_count: int = 0
    failure_counts: dict[str, int] = Field(default_factory=dict)
    no_progress_count: int = 0
    verified_evidence_refs: tuple[str, ...] = ()
    usage: BrowserAgentUsage = Field(default_factory=BrowserAgentUsage)
    history: tuple[BrowserAgentHistoryItem, ...] = ()


class BrowserAgentResult(StrictContract):
    ok: bool
    stop_reason: BrowserAgentStopReason
    message: str
    final_state: BrowserPageState
    action_count: int = 0
    model_decision_count: int = 0
    replan_count: int = 0
    usage: BrowserAgentUsage = Field(default_factory=BrowserAgentUsage)
    pending_action: BrowserAction | None = None
    evidence_refs: tuple[str, ...] = ()
