# -*- coding: utf-8 -*-
"""Authority and permission invariants for the Coherence Harness."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from lantai.runtime.coherence import (
    AuthorityGrant,
    AuthorityProvenance,
    PermissionEnvelope,
    derive_authority,
    merge_permission_decisions,
)


def _user_authority(*, delegable: bool = True) -> AuthorityProvenance:
    return AuthorityProvenance(
        authority_id="authority-user-1",
        source_kind="direct_user_input",
        actor_kind="user",
        actor_id="user-1",
        source_ref="turn:client-1",
        scopes=("workspace:alpha", "run:one"),
        delegable=delegable,
    )


def test_permission_lattice_is_monotonic_and_fails_closed() -> None:
    assert merge_permission_decisions([]) == "deny"
    assert merge_permission_decisions(["allow"]) == "allow"
    assert (
        merge_permission_decisions(["allow", "approval_required"])
        == "approval_required"
    )
    assert (
        merge_permission_decisions(["allow", "approval_required", "deny"])
        == "deny"
    )


def test_permission_envelope_requires_matching_live_provenance() -> None:
    authority = _user_authority()
    envelope = PermissionEnvelope(
        grants=(
            AuthorityGrant(
                capability="files.write",
                decision="approval_required",
                scope="workspace:alpha",
                authority=authority,
            ),
            AuthorityGrant(
                capability="files.write",
                decision="deny",
                scope="workspace:alpha",
                authority=authority,
            ),
        ),
    )

    assert (
        envelope.resolve(
            capability="files.write",
            scope="workspace:alpha",
        )
        == "deny"
    )
    assert (
        envelope.resolve(
            capability="network.send",
            scope="workspace:alpha",
        )
        == "deny"
    )


def test_delegation_is_one_hop_and_scope_narrowing_only() -> None:
    parent = _user_authority()
    child = derive_authority(
        parent,
        authority_id="authority-agent-1",
        actor_kind="agent",
        actor_id="agent-1",
        source_ref="delegation:one",
        scopes=("run:one",),
    )

    assert child.delegation_depth == 1
    assert child.delegable is False
    assert child.parent_authority_ids == (parent.authority_id,)

    with pytest.raises(ValueError, match="not delegable"):
        derive_authority(
            child,
            authority_id="authority-tool-1",
            actor_kind="tool",
            actor_id="tool-1",
            source_ref="delegation:two",
        )

    with pytest.raises(ValueError, match="subset"):
        derive_authority(
            parent,
            authority_id="authority-agent-wide",
            actor_kind="agent",
            actor_id="agent-1",
            source_ref="delegation:wide",
            scopes=("workspace:other",),
        )


def test_expired_authority_does_not_resolve_to_allow() -> None:
    issued_at = datetime.now(timezone.utc) - timedelta(hours=2)
    expired = AuthorityProvenance(
        authority_id="authority-expired",
        source_kind="user_approval",
        actor_kind="user",
        actor_id="user-1",
        source_ref="approval:old",
        scopes=("run:one",),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=10),
    )
    envelope = PermissionEnvelope(
        grants=(
            AuthorityGrant(
                capability="tool.execute",
                decision="allow",
                scope="run:one",
                authority=expired,
            ),
        ),
    )

    assert (
        envelope.resolve(
            capability="tool.execute",
            scope="run:one",
        )
        == "deny"
    )


def test_delegated_authority_cannot_claim_to_be_delegable() -> None:
    with pytest.raises(ValidationError):
        AuthorityProvenance(
            authority_id="bad-child",
            source_kind="delegation",
            actor_kind="agent",
            actor_id="agent-1",
            source_ref="delegation:bad",
            scopes=("run:one",),
            parent_authority_ids=("parent",),
            delegation_depth=1,
            delegable=True,
        )
