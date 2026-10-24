# -*- coding: utf-8 -*-
"""Browser action effect recovery capability classification."""

from __future__ import annotations

from lantai.runtime.effects import RuntimeEffectRecoveryCapability

from .models import READ_ACTIONS, SENSITIVE_ACTIONS, WRITE_ACTIONS, BrowserAction


_READ_ONLY_RECOVERY = RuntimeEffectRecoveryCapability()
_BROWSER_WRITE_RECOVERY = RuntimeEffectRecoveryCapability(
    external_side_effect=True,
    idempotency="not_idempotent",
    probe="browser_observation",
    compensation="manual_or_domain_specific",
    on_ambiguity="manual_disambiguation",
)


def browser_action_effect_recovery(
    action: BrowserAction,
) -> RuntimeEffectRecoveryCapability:
    """Return the recovery declaration for a Browser Workbench action."""

    raw_override = action.metadata.get("effect_recovery")
    if isinstance(raw_override, dict):
        base = (
            _BROWSER_WRITE_RECOVERY
            if _is_write_action(action)
            else _READ_ONLY_RECOVERY
        )
        payload = {
            **base.model_dump(mode="json"),
            **raw_override,
        }
        return RuntimeEffectRecoveryCapability.model_validate(payload)

    if _is_write_action(action):
        return _BROWSER_WRITE_RECOVERY
    return _READ_ONLY_RECOVERY


def _is_write_action(action: BrowserAction) -> bool:
    if action.access == "read" and action.metadata.get("browser_read_task_token"):
        return False
    if action.access in {"write", "sensitive"}:
        return True
    name = str(action.action or "").strip().lower()
    return name in WRITE_ACTIONS or name in SENSITIVE_ACTIONS or action.access == "sensitive"


def browser_action_is_read_only(action: BrowserAction) -> bool:
    """Return whether the action is declared as read-only by default."""

    return (
        action.access == "read"
        or (
            action.access is None
            and str(action.action or "").strip().lower() in READ_ACTIONS
            and not _is_write_action(action)
        )
    )
