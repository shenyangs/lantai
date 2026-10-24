# -*- coding: utf-8 -*-
"""Browser action effect recovery capability locks."""

from __future__ import annotations

from lantai.app.browser_workbench.effects import browser_action_effect_recovery
from lantai.app.browser_workbench.models import BrowserAction


def test_browser_read_action_is_safe_to_retry_and_has_no_external_effect() -> None:
    capability = browser_action_effect_recovery(BrowserAction(action="snapshot"))

    assert capability.external_side_effect is False
    assert capability.idempotency == "read_only"
    assert capability.probe == "not_required"
    assert capability.compensation == "not_required"
    assert capability.on_ambiguity == "safe_to_retry"


def test_browser_write_action_requires_probe_or_manual_disambiguation() -> None:
    capability = browser_action_effect_recovery(
        BrowserAction(action="click", selector="#publish", access="write"),
    )

    assert capability.external_side_effect is True
    assert capability.idempotency == "not_idempotent"
    assert capability.probe == "browser_observation"
    assert capability.compensation == "manual_or_domain_specific"
    assert capability.on_ambiguity == "manual_disambiguation"


def test_browser_action_metadata_can_override_recovery_capability() -> None:
    capability = browser_action_effect_recovery(
        BrowserAction(
            action="click",
            selector="#save-draft",
            access="write",
            metadata={
                "effect_recovery": {
                    "idempotency": "natural_key",
                    "probe": "browser_observation",
                    "compensation": "manual_or_domain_specific",
                    "on_ambiguity": "probe_required",
                    "notes": "Draft save can be checked by page state.",
                },
            },
        ),
    )

    assert capability.external_side_effect is True
    assert capability.idempotency == "natural_key"
    assert capability.on_ambiguity == "probe_required"
    assert capability.notes == "Draft save can be checked by page state."
