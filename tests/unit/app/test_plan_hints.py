# -*- coding: utf-8 -*-
"""Tests for Lantai-owned plan hint helpers."""

from dataclasses import dataclass

from lantai.plan.hints import (
    SimplePlanToHint,
    check_plan_tool_gate,
    set_plan_gate,
)


class FakeNotebook:
    def __init__(self, current_plan=None) -> None:
        self.current_plan = current_plan


@dataclass
class FakeSubtask:
    name: str
    description: str
    expected_outcome: str
    state: str

    def to_markdown(self, detailed: bool = False) -> str:
        if detailed:
            return f"## {self.name}\n{self.description}\n{self.expected_outcome}"
        return f"## {self.name}"


@dataclass
class FakePlan:
    name: str
    description: str
    state: str
    subtasks: list[FakeSubtask]

    def to_markdown(self) -> str:
        items = "\n".join(f"- {subtask.name}" for subtask in self.subtasks)
        return f"# {self.name}\n{self.description}\n{items}"


def test_simple_plan_hint_only_emits_no_plan_when_gate_is_active():
    notebook = FakeNotebook()
    hints = SimplePlanToHint()
    hints.bind_notebook(notebook)

    assert hints(None) is None

    set_plan_gate(notebook, True)
    hint = hints(None)

    assert hint is not None
    assert hint.startswith("<system-hint>")
    assert "There is no active plan yet." in hint
    assert hint.endswith("</system-hint>")


def test_simple_plan_hint_uses_local_plan_contract():
    notebook = FakeNotebook()
    plan = FakePlan(
        name="Ship fix",
        description="Fix the regression",
        state="in_progress",
        subtasks=[
            FakeSubtask(
                name="Patch",
                description="Apply code changes",
                expected_outcome="Patch lands",
                state="in_progress",
            ),
        ],
    )
    hints = SimplePlanToHint()
    hints.bind_notebook(notebook)

    hint = hints(plan)

    assert hint is not None
    assert "Subtask 0 ('Patch') is in_progress." in hint
    assert "Fix the regression" in hint


def test_check_plan_tool_gate_blocks_until_create_plan_then_clears():
    notebook = FakeNotebook()

    set_plan_gate(notebook, True)

    assert check_plan_tool_gate(notebook, "create_plan") is None
    blocked = check_plan_tool_gate(notebook, "finish_plan")
    assert blocked is not None
    assert "create_plan" in blocked

    notebook.current_plan = object()
    assert check_plan_tool_gate(notebook, "finish_plan") is None
    assert notebook._plan_tool_gate is False
