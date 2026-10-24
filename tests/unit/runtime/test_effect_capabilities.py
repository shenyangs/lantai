# -*- coding: utf-8 -*-
"""Behavior locks for effect recovery declarations."""

from __future__ import annotations

from lantai.runtime.effects import RuntimeEffectRecoveryCapability
from lantai.runtime.tools import ToolSpec, tool_effect_recovery_capability


def test_tool_spec_defaults_to_fail_closed_mutating_recovery_capability() -> None:
    spec = ToolSpec(name="lookup")

    capability = tool_effect_recovery_capability(spec)

    assert isinstance(capability, RuntimeEffectRecoveryCapability)
    assert capability.external_side_effect is True
    assert capability.idempotency == "not_idempotent"
    assert capability.probe == "none"
    assert capability.compensation == "none"
    assert capability.on_ambiguity == "manual_disambiguation"


def test_read_only_tool_spec_keeps_safe_retry_recovery_capability() -> None:
    capability = tool_effect_recovery_capability(
        ToolSpec(name="lookup", read_only=True)
    )

    assert capability.external_side_effect is False
    assert capability.idempotency == "read_only"
    assert capability.on_ambiguity == "safe_to_retry"


def test_tool_spec_accepts_explicit_side_effect_recovery_capability() -> None:
    spec = ToolSpec(
        name="send_email",
        effect_recovery=RuntimeEffectRecoveryCapability(
            external_side_effect=True,
            idempotency="idempotency_key",
            probe="tool_specific",
            compensation="tool_specific",
            on_ambiguity="probe_required",
            notes="Email provider supports message-id lookup.",
        ),
    )

    capability = tool_effect_recovery_capability(spec)

    assert capability.external_side_effect is True
    assert capability.idempotency == "idempotency_key"
    assert capability.probe == "tool_specific"
    assert capability.compensation == "tool_specific"
    assert capability.on_ambiguity == "probe_required"
    assert capability.notes == "Email provider supports message-id lookup."
