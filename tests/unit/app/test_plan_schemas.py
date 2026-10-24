# -*- coding: utf-8 -*-
"""Tests for plan API response conversion."""

from types import SimpleNamespace

from lantai.plan.schemas import plan_to_response


def test_plan_to_response_accepts_persisted_dict():
    response = plan_to_response(
        {
            "id": "plan-1",
            "name": "Build",
            "description": "Ship it",
            "expected_outcome": "Done",
            "state": "in_progress",
            "subtasks": [
                {
                    "name": "Test",
                    "description": "Run tests",
                    "expected_outcome": "Green",
                    "state": "todo",
                },
            ],
        },
    )

    assert response.id == "plan-1"
    assert response.subtasks[0].name == "Test"


def test_plan_to_response_accepts_plan_like_object():
    subtask = SimpleNamespace(
        name="Patch",
        description="Apply patch",
        expected_outcome="Merged",
        outcome=None,
        state="done",
        created_at=None,
        finished_at=None,
    )
    plan = SimpleNamespace(
        id="plan-2",
        name="Fix",
        description="Fix issue",
        expected_outcome="Stable",
        state="done",
        subtasks=[subtask],
        created_at=None,
        finished_at=None,
        outcome="Fixed",
    )

    response = plan_to_response(plan)

    assert response.id == "plan-2"
    assert response.outcome == "Fixed"
