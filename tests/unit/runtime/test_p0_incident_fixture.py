# -*- coding: utf-8 -*-
"""Locks the anonymized 2/10 false-completion incident as a release fixture."""

from __future__ import annotations

import json
from pathlib import Path

from lantai.runtime.runs import build_chat_task_contract


_FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "runtime"
    / "incidents"
    / "bazi_light_false_completion_v1.json"
)


def test_bazi_false_completion_fixture_is_not_forced_into_light_budget() -> None:
    incident = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    task = incident["task"]
    contract = build_chat_task_contract(
        objective=task["objective"],
        client_turn_id="fixture-client-turn",
        complexity=task["complexity"],
        work_method_id="standard",
        acceptance=tuple(task["acceptance"]),
    )

    assert task["difficulty_score"] == 2
    assert contract.complexity == "standard"
    assert contract.budget.max_model_rounds is None
    assert contract.budget.max_tool_calls is None
    assert contract.budget.max_active_seconds is None
    assert contract.budget.max_tokens is None
    assert contract.budget.max_same_failure_count == 3
    assert incident["observed"]["tool_calls"] > 0
    assert incident["observed"]["tokens"] > 0
    assert len(incident["required_regressions"]) >= 9
