# -*- coding: utf-8 -*-
"""Native Runtime plan-module contract tests."""

from __future__ import annotations

from lantai.plan.runtime import PlanRuntimeModule, split_explicit_plan_request
from lantai.runtime import RuntimeMessage


class _Session:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state or {}
        self.updates: list[tuple[str, dict]] = []

    async def get_session_state_dict(self, **_kwargs):
        return self.state

    async def update_session_state(self, *, key, value, **_kwargs):
        self.updates.append((key, value))
        self.state.setdefault("agent", {})["plan_notebook"] = value


def test_split_explicit_plan_request_strips_only_latest_user_prefix():
    messages = [
        RuntimeMessage.text("user", "older"),
        RuntimeMessage.text("assistant", "reply"),
        RuntimeMessage.text("user", "/plan 设计一个发布流程"),
    ]

    explicit, prepared = split_explicit_plan_request(messages)

    assert explicit is True
    assert prepared[-1].text_content() == "设计一个发布流程"
    assert prepared[0].text_content() == "older"
    assert messages[-1].text_content().startswith("/plan")


async def test_ordinary_turn_has_no_plan_tools_or_hint_without_active_plan():
    module = await PlanRuntimeModule.load(
        session=_Session(),
        agent_id="agent",
        session_id="session",
        user_id="user",
        channel="console",
        explicit_entry=False,
    )

    assert module.is_active is False
    assert module.trusted_hint_message() is None
    assert module.tool_registry().tool_items() == []


async def test_explicit_turn_exposes_plan_controls_and_persists_updates():
    session = _Session()
    broadcasts: list[dict] = []
    module = await PlanRuntimeModule.load(
        session=session,
        agent_id="agent",
        session_id="session",
        user_id="user",
        channel="console",
        explicit_entry=True,
        broadcast=lambda payload: broadcasts.append(payload),
    )

    registry = module.tool_registry()
    names = [spec.name for spec, _ in registry.tool_items()]
    assert names == [
        "create_plan",
        "revise_current_plan",
        "update_subtask_state",
        "finish_subtask",
        "finish_plan",
    ]
    assert all(spec.read_only for spec, _ in registry.tool_items())
    assert "create_plan" in module.trusted_hint_message().text_content()

    _, create_plan = registry.get_tool("create_plan")
    result = await create_plan(
        name="发布",
        description="准备发布",
        expected_outcome="发布完成",
        subtasks=[
            {
                "name": "验证",
                "description": "运行检查",
                "expected_outcome": "检查通过",
            }
        ],
    )

    assert "created successfully" in result.content[0]["text"]
    assert session.updates[-1][0] == "agent.plan_notebook"
    assert session.updates[-1][1]["current_plan"]["name"] == "发布"
    assert broadcasts[-1]["plan"]["name"] == "发布"

    _, update_state = registry.get_tool("update_subtask_state")
    blocked = await update_state(subtask_idx=0, state="in_progress")
    assert blocked.is_error is True
    assert "confirmation" in blocked.content[0]["text"].lower()


async def test_active_plan_reloads_for_confirmation_turn():
    first_session = _Session()
    first = await PlanRuntimeModule.load(
        session=first_session,
        agent_id="agent",
        session_id="session",
        user_id="user",
        channel="console",
        explicit_entry=True,
    )
    _, create_plan = first.tool_registry().get_tool("create_plan")
    await create_plan(
        name="发布",
        description="准备发布",
        expected_outcome="发布完成",
        subtasks=[
            {
                "name": "验证",
                "description": "运行检查",
                "expected_outcome": "检查通过",
            }
        ],
    )

    reloaded = await PlanRuntimeModule.load(
        session=_Session(first_session.state),
        agent_id="agent",
        session_id="session",
        user_id="user",
        channel="console",
        explicit_entry=False,
    )

    assert reloaded.is_active is True
    assert "LATEST message" in reloaded.trusted_hint_message().text_content()
    _, update_state = reloaded.tool_registry().get_tool("update_subtask_state")
    result = await update_state(subtask_idx=0, state="in_progress")
    assert result.is_error is False
