# -*- coding: utf-8 -*-
"""Identity and authority contracts for the Coherence Harness.

These types deliberately carry no runtime side effects.  They make authority
and fencing explicit so existing Runtime, policy, and Effect code can share a
single fail-closed vocabulary without creating a second control plane.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


PermissionDecision = Literal["allow", "approval_required", "deny"]
AuthoritySourceKind = Literal[
    "direct_user_input",
    "user_approval",
    "runtime_policy",
    "delegation",
    "recovery",
    "migration",
    "legacy",
]
AuthorityActorKind = Literal["user", "system", "agent", "tool", "policy"]

_PERMISSION_RANK: dict[PermissionDecision, int] = {
    "allow": 0,
    "approval_required": 1,
    "deny": 2,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _non_empty(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    return text


class ExecutionFence(BaseModel):
    """The mutable authority generation that owns one runtime write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str
    generation: int = Field(default=0, ge=0)
    expected_revision: int | None = Field(default=None, ge=0)
    lease_owner: str | None = None

    @field_validator("attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str) -> str:
        return _non_empty(value, "attempt_id")

    @field_validator("lease_owner")
    @classmethod
    def normalize_lease_owner(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_empty(value, "lease_owner")

    def accepts(self, candidate: "ExecutionFence") -> bool:
        """Return whether ``candidate`` still belongs to this authority."""

        if self.attempt_id != candidate.attempt_id:
            return False
        if self.generation != candidate.generation:
            return False
        if (
            self.expected_revision is not None
            and self.expected_revision != candidate.expected_revision
        ):
            return False
        return not (
            self.lease_owner is not None
            and self.lease_owner != candidate.lease_owner
        )


class AuthorityProvenance(BaseModel):
    """Durable source of authority, intentionally non-transitive by default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authority_id: str
    source_kind: AuthoritySourceKind
    actor_kind: AuthorityActorKind
    actor_id: str
    source_ref: str
    scopes: tuple[str, ...] = ()
    issued_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime | None = None
    parent_authority_ids: tuple[str, ...] = ()
    delegation_depth: int = Field(default=0, ge=0, le=1)
    delegable: bool = False

    @field_validator("authority_id", "actor_id", "source_ref")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("scopes", "parent_authority_ids")
    @classmethod
    def normalize_unique_values(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            text = _non_empty(value, "authority value")
            if text not in normalized:
                normalized.append(text)
        return tuple(normalized)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def validate_delegation(self) -> "AuthorityProvenance":
        if self.source_kind == "delegation":
            if not self.parent_authority_ids:
                raise ValueError("delegation requires parent authority")
            if self.delegation_depth != 1:
                raise ValueError("delegated authority must have depth 1")
            if self.delegable:
                raise ValueError(
                    "delegated authority cannot be delegated again"
                )
        elif self.delegation_depth != 0:
            raise ValueError(
                "only delegated authority may have delegation depth"
            )
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise ValueError("authority expiry must be after issue time")
        return self

    def is_active(self, *, at: datetime | None = None) -> bool:
        moment = at or _utc_now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return self.expires_at is None or moment < self.expires_at


def derive_authority(
    parent: AuthorityProvenance,
    *,
    authority_id: str,
    actor_kind: AuthorityActorKind,
    actor_id: str,
    source_ref: str,
    scopes: Sequence[str] | None = None,
    expires_at: datetime | None = None,
) -> AuthorityProvenance:
    """Create a one-hop child grant or fail closed.

    A delegated authority is always non-delegable, which prevents an Agent or
    plugin from silently widening authority through a delegation chain.
    """

    if not parent.delegable or parent.delegation_depth != 0:
        raise ValueError("authority is not delegable")
    requested_scopes = tuple(scopes) if scopes is not None else parent.scopes
    if not set(requested_scopes).issubset(parent.scopes):
        raise ValueError("delegated scopes must be a subset of parent scopes")
    child_expiry = expires_at or parent.expires_at
    if (
        parent.expires_at is not None
        and child_expiry is not None
        and child_expiry > parent.expires_at
    ):
        raise ValueError("delegated authority cannot outlive parent authority")
    return AuthorityProvenance(
        authority_id=authority_id,
        source_kind="delegation",
        actor_kind=actor_kind,
        actor_id=actor_id,
        source_ref=source_ref,
        scopes=requested_scopes,
        expires_at=child_expiry,
        parent_authority_ids=(parent.authority_id,),
        delegation_depth=1,
        delegable=False,
    )


class AuthorityGrant(BaseModel):
    """One capability decision backed by durable authority provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str
    decision: PermissionDecision
    scope: str
    authority: AuthorityProvenance

    @field_validator("capability", "scope")
    @classmethod
    def validate_text(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)

    @model_validator(mode="after")
    def validate_scope(self) -> "AuthorityGrant":
        if self.authority.scopes and self.scope not in self.authority.scopes:
            raise ValueError(
                "grant scope is not covered by authority provenance"
            )
        return self


def merge_permission_decisions(
    decisions: Sequence[PermissionDecision],
) -> PermissionDecision:
    """Return the most restrictive decision in the monotonic lattice."""

    if not decisions:
        return "deny"
    return max(decisions, key=_PERMISSION_RANK.__getitem__)


class PermissionEnvelope(BaseModel):
    """A source-aware, fail-closed view of effective permissions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[AuthorityGrant, ...] = ()

    def resolve(
        self,
        *,
        capability: str,
        scope: str,
        at: datetime | None = None,
    ) -> PermissionDecision:
        decisions = [
            grant.decision
            for grant in self.grants
            if grant.capability == capability
            and grant.scope == scope
            and grant.authority.is_active(at=at)
        ]
        return merge_permission_decisions(decisions)


__all__ = [
    "AuthorityActorKind",
    "AuthorityGrant",
    "AuthorityProvenance",
    "AuthoritySourceKind",
    "ExecutionFence",
    "PermissionDecision",
    "PermissionEnvelope",
    "derive_authority",
    "merge_permission_decisions",
]
