"""Versioned contracts for native macOS application control."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MacUseAction = Literal[
    "status",
    "list_windows",
    "select_target",
    "observe_window",
    "focus_window",
    "click_at",
    "type_text",
    "press_key",
    "set_value",
    "press_element",
    "stop",
    "pause",
    "resume",
    "session_status",
]
MacUsePolicyStatus = Literal["allowed", "denied", "needs_authorization"]
MacUseDispatchStatus = Literal[
    "not_sent",
    "sent",
    "transport_failed",
    "unknown",
]
MacUseEffectStatus = Literal[
    "not_applicable",
    "verified",
    "no_effect_verified",
    "ambiguous",
    "failed",
]

READ_ACTIONS = frozenset(
    {"status", "list_windows", "select_target", "observe_window", "session_status"},
)
WRITE_ACTIONS = frozenset(
    {
        "focus_window",
        "click_at",
        "type_text",
        "press_key",
        "set_value",
        "press_element",
    },
)
AMBIGUOUS_WRITE_ACTIONS = frozenset(
    {"click_at", "type_text", "press_key", "press_element"},
)
SESSION_ACTIONS = frozenset({"stop", "pause", "resume", "session_status"})


class MacUseTarget(BaseModel):
    """A concrete application/window identity, never just display text."""

    model_config = ConfigDict(extra="forbid")

    app_bundle_id: str = ""
    app_path: str = ""
    process_id: int | None = Field(default=None, ge=1)
    process_started_at: float | None = Field(default=None, gt=0)
    window_id: str = ""
    window_title: str = ""

    @field_validator("app_bundle_id", "app_path", "window_id", "window_title")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value or "").strip()

    def is_bound(self) -> bool:
        return bool(
            self.app_bundle_id
            and self.window_id
            and self.process_id
            and self.process_started_at
        )


class MacUseActionRequest(BaseModel):
    """One brokered operation sent to the signed desktop bridge."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = 1
    action: MacUseAction
    run_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    action_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    effect_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    target: MacUseTarget = Field(default_factory=MacUseTarget)
    selection_receipt: str = Field(default="", max_length=128)
    observation_id: str = Field(default="", max_length=128)
    element_id: str = Field(default="", max_length=500)
    element_role: str = Field(default="", max_length=80)
    element_title: str = Field(default="", max_length=500)
    x: float | None = Field(default=None, ge=0.0, le=1.0)
    y: float | None = Field(default=None, ge=0.0, le=1.0)
    text: str = Field(default="", max_length=100_000)
    key: str = Field(default="", max_length=40)
    expected_value: str = Field(default="", max_length=100_000)
    verification_element_id: str = Field(default="", max_length=500)
    resolves_effect_id: str = Field(default="", max_length=128)
    reason: str = Field(default="", max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "run_id",
        "action_id",
        "effect_id",
        "observation_id",
        "selection_receipt",
        "element_id",
        "element_role",
        "element_title",
        "key",
        "reason",
        "resolves_effect_id",
        "verification_element_id",
    )
    @classmethod
    def trim_text(cls, value: str) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def validate_action_shape(self) -> "MacUseActionRequest":
        if self.action in WRITE_ACTIONS and not self.target.is_bound():
            raise ValueError(
                "Mac write actions require app_bundle_id, window_id, and process generation",
            )
        if self.action == "select_target" and not self.target.is_bound():
            raise ValueError(
                "select_target requires app_bundle_id, window_id, and process generation",
            )
        if self.action in WRITE_ACTIONS | {"observe_window"} and not self.selection_receipt:
            raise ValueError("Mac observation and writes require a user selection receipt")
        if self.action in WRITE_ACTIONS and not self.observation_id:
            raise ValueError("Mac write actions require a current observation_id")
        if self.action == "observe_window" and self.metadata.get("verification") is True:
            if not self.observation_id or not self.element_id or not self.resolves_effect_id:
                raise ValueError(
                    "Mac verification requires the original observation_id, "
                    "element_id, and resolves_effect_id",
                )
        if self.action == "click_at" and (self.x is None or self.y is None):
            raise ValueError("click_at requires normalized x and y")
        if self.action in {"type_text", "set_value", "press_element"} and not self.element_id:
            raise ValueError(f"{self.action} requires element_id")
        if self.action == "press_key" and not self.key:
            raise ValueError("press_key requires key")
        if self.action in AMBIGUOUS_WRITE_ACTIONS and not self.verification_element_id:
            raise ValueError(
                f"{self.action} requires a verification_element_id and expected_value",
            )
        return self


class MacUseActionResult(BaseModel):
    """Separates policy, transport, observed change, and verified effect."""

    model_config = ConfigDict(extra="allow")

    protocol_version: Literal[1] = 1
    ok: bool = False
    action: MacUseAction
    run_id: str
    action_id: str
    effect_id: str
    policy_status: MacUsePolicyStatus = "allowed"
    dispatch_status: MacUseDispatchStatus = "not_sent"
    effect_status: MacUseEffectStatus = "not_applicable"
    observed_change: bool | None = None
    observation_id: str = ""
    message: str = ""
    error: str | None = None
    error_code: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    retry_safe: bool = False

    @classmethod
    def unavailable(
        cls,
        request: MacUseActionRequest,
        *,
        error_code: str = "mac_use_bridge_unavailable",
        error: str = "Mac Use desktop bridge is unavailable.",
    ) -> "MacUseActionResult":
        return cls(
            action=request.action,
            run_id=request.run_id,
            action_id=request.action_id,
            effect_id=request.effect_id,
            ok=False,
            dispatch_status="not_sent",
            effect_status="failed",
            error=error,
            error_code=error_code,
            retry_safe=True,
        )
