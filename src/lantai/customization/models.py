"""Stable contracts for the Lantai customization platform."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def prefixed_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


ScopeKind = Literal["global", "project", "agent", "task", "session"]
MergeStrategy = Literal[
    "override",
    "union",
    "intersection",
    "min",
    "max",
    "append_unique",
    "immutable",
]
RiskLevel = Literal["low", "medium", "high", "forbidden"]
CustomizationMode = Literal["normal", "factory", "safe"]


class RegistryModelHints(BaseModel):
    synonyms: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class RegistryItem(BaseModel):
    id: str
    version: int = Field(default=1, ge=1)
    category: str
    title: str
    description: str = ""
    value_type: Literal[
        "boolean",
        "string",
        "number",
        "string_array",
        "object",
    ]
    factory_value: Any
    scopes: list[ScopeKind] = Field(default_factory=lambda: ["global"])
    merge_strategy: MergeStrategy = "override"
    risk: RiskLevel = "low"
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None
    restart: Literal["none", "renderer", "backend", "app"] = "none"
    preview: str = "none"
    health_checks: list[str] = Field(default_factory=lambda: ["schema"])
    export_policy: Literal[
        "shareable",
        "local_reference",
        "redact_machine_paths",
        "private",
    ] = "shareable"
    requires_explicit_grant: bool = False
    model: RegistryModelHints = Field(default_factory=RegistryModelHints)


class ProfileScope(BaseModel):
    kind: ScopeKind = "global"
    ref: str | None = None


class CustomizationProfile(BaseModel):
    schema_version: int = 1
    id: str
    name: str
    description: str = ""
    icon: str = "sparkles"
    color: str = "#4b6581"
    owner: Literal["personal", "project", "organization"] = "personal"
    base_profile_id: str = "factory"
    scope: ProfileScope = Field(default_factory=ProfileScope)
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    overrides: dict[str, Any] = Field(default_factory=dict)
    references: dict[str, list[str]] = Field(default_factory=dict)


class ProfileSlot(BaseModel):
    index: int = Field(ge=1, le=5)
    profile_id: str | None = None
    label: str | None = None
    icon: str | None = None
    color: str | None = None


class CustomizationState(BaseModel):
    schema_version: int = 1
    mode: CustomizationMode = "factory"
    active_profile_id: str | None = None
    active_scoped_profiles: list[str] = Field(default_factory=list)
    last_known_good: dict[str, int] = Field(default_factory=dict)
    pending_boot_validation: dict[str, int] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class ChangeOperation(BaseModel):
    op: Literal["set", "unset"]
    path: str
    value: Any = None


class ChangePlan(BaseModel):
    id: str = Field(default_factory=lambda: prefixed_id("plan"))
    target_profile_id: str
    base_revision: int = Field(ge=0)
    scope: ProfileScope = Field(default_factory=ProfileScope)
    intent: str = ""
    operations: list[ChangeOperation] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class EffectiveValueSource(BaseModel):
    layer: Literal["factory", "profile", "hard_constraint"]
    profile_id: str | None = None
    revision: int | None = None


class EffectiveState(BaseModel):
    values: dict[str, Any]
    sources: dict[str, EffectiveValueSource]
    ignored: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class RevisionRecord(BaseModel):
    profile_id: str
    revision: int = Field(ge=0)
    parent_revision: int | None = None
    actor: str = "user"
    source: str = "customization"
    intent: str = ""
    plan_id: str | None = None
    before_hash: str
    after_hash: str
    risk: RiskLevel = "low"
    before_overrides: dict[str, Any] = Field(default_factory=dict)
    overrides: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    health: dict[str, Any] = Field(default_factory=dict)
    status: Literal["committed", "rolled_back", "migrated"] = "committed"


class TransactionResult(BaseModel):
    id: str = Field(default_factory=lambda: prefixed_id("tx"))
    plan_id: str
    profile_id: str
    before_revision: int
    result_revision: int
    status: Literal["committed", "rolled_back"]
    health: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ChangePreview(BaseModel):
    plan: ChangePlan
    before: dict[str, Any]
    after: dict[str, Any]
    changed: dict[str, dict[str, Any]]
    ignored: dict[str, str] = Field(default_factory=dict)
    risk: RiskLevel = "low"
    requires_confirmation: bool = False
    warnings: list[str] = Field(default_factory=list)


class NaturalLanguageDraft(BaseModel):
    summary: str
    operations: list[ChangeOperation] = Field(min_length=1, max_length=30)
