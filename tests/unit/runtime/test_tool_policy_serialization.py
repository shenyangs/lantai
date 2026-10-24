# -*- coding: utf-8 -*-
"""Regression locks for Runtime tool-policy payload serialization."""

from lantai.runtime.tool_policy import _json_safe
from lantai.security.tool_guard.models import (
    GuardFinding,
    GuardSeverity,
    GuardThreatCategory,
    ToolGuardResult,
)


def test_tool_guard_result_remains_a_structured_payload() -> None:
    result = ToolGuardResult(
        tool_name="execute_shell_command",
        params={"command": "rm test.js"},
        findings=[
            GuardFinding(
                id="finding-1",
                rule_id="review-delete",
                category=GuardThreatCategory.CODE_EXECUTION,
                severity=GuardSeverity.MEDIUM,
                title="Review deletion",
                description="Deletion should be reviewed.",
                tool_name="execute_shell_command",
                param_name="command",
            )
        ],
    )

    payload = _json_safe(result)

    assert isinstance(payload, dict)
    assert payload["tool_name"] == "execute_shell_command"
    assert payload["max_severity"] == "MEDIUM"
    assert payload["findings"][0]["rule_id"] == "review-delete"
    assert not isinstance(payload, str)
