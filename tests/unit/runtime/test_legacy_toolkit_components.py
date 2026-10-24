# -*- coding: utf-8 -*-
"""Behavior locks for the Lantai-owned legacy toolkit facade."""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from lantai.runtime.adapters.legacy_toolkit_components import LegacyToolkit
from lantai.runtime.tool_response import TextBlock, ToolResponse


async def _collect_tool_call(toolkit: LegacyToolkit, name: str, input_: dict):
    stream = await toolkit.call_tool_function(
        {
            "id": "call-1",
            "name": name,
            "input": input_,
        },
    )
    return [chunk async for chunk in stream]


@pytest.mark.asyncio
async def test_legacy_toolkit_registers_schema_and_executes_tool() -> None:
    async def echo_tool(text: str) -> ToolResponse:
        """Echo input text."""

        return ToolResponse(content=[TextBlock(text=text)])

    toolkit = LegacyToolkit()
    toolkit.register_tool_function(echo_tool)

    schemas = toolkit.get_json_schemas()
    assert schemas[0]["function"]["name"] == "echo_tool"
    assert schemas[0]["function"]["parameters"]["required"] == ["text"]

    chunks = await _collect_tool_call(toolkit, "echo_tool", {"text": "ok"})
    assert chunks[0].content == [TextBlock(text="ok")]


@pytest.mark.asyncio
async def test_legacy_toolkit_async_task_helpers() -> None:
    async def slow_tool() -> ToolResponse:
        await asyncio.sleep(0.01)
        return ToolResponse(content=[TextBlock(text="done")])

    toolkit = LegacyToolkit()
    toolkit.register_tool_function(slow_tool, async_execution=True)
    toolkit.register_tool_function(toolkit.wait_task)

    first_chunks = await _collect_tool_call(toolkit, "slow_tool", {})
    reminder = first_chunks[0].content[0]["text"]
    task_id = reminder.split("Task ID: ", 1)[1].split(".", 1)[0]

    result = await toolkit.wait_task(task_id, timeout=1)

    assert result.content == [TextBlock(text="done")]


@pytest.mark.asyncio
async def test_legacy_toolkit_cancelled_sync_background_tool_has_no_late_result() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_sync_tool() -> ToolResponse:
        started.set()
        release.wait(timeout=1)
        return ToolResponse(content=[TextBlock(text="late side effect result")])

    toolkit = LegacyToolkit()
    toolkit.register_tool_function(slow_sync_tool, async_execution=True)

    first_chunks = await _collect_tool_call(toolkit, "slow_sync_tool", {})
    reminder = first_chunks[0].content[0]["text"]
    task_id = reminder.split("Task ID: ", 1)[1].split(".", 1)[0]
    await asyncio.sleep(0.05)
    assert started.is_set()

    cancel_result = await toolkit.cancel_task(task_id)
    release.set()
    await asyncio.sleep(0.05)

    assert "has been cancelled" in cancel_result.content[0]["text"]
    assert task_id not in toolkit._async_results  # pylint: disable=protected-access
    assert "InvalidTaskIdError" in (await toolkit.view_task(task_id)).content[0]["text"]


@pytest.mark.asyncio
async def test_legacy_toolkit_sync_background_tool_gets_context_snapshot() -> None:
    request_id_var = contextvars.ContextVar("request_id", default="missing")
    seen: list[str] = []

    def sync_context_tool() -> ToolResponse:
        seen.append(request_id_var.get())
        return ToolResponse(content=[TextBlock(text="done")])

    toolkit = LegacyToolkit()
    toolkit.register_tool_function(sync_context_tool, async_execution=True)

    token = request_id_var.set("turn-ctx")
    try:
        first_chunks = await _collect_tool_call(toolkit, "sync_context_tool", {})
    finally:
        request_id_var.reset(token)
    reminder = first_chunks[0].content[0]["text"]
    task_id = reminder.split("Task ID: ", 1)[1].split(".", 1)[0]

    result = await toolkit.wait_task(task_id, timeout=1)

    assert result.content == [TextBlock(text="done")]
    assert seen == ["turn-ctx"]


def test_legacy_toolkit_skill_prompt_uses_read_skill_without_exposing_path(
    tmp_path,
) -> None:
    skill_dir = tmp_path / "pptx"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create PowerPoint presentations.\n"
        "---\n"
        "# PPTX\n",
        encoding="utf-8",
    )
    toolkit = LegacyToolkit()
    toolkit.register_agent_skill(str(skill_dir))

    prompt = toolkit.get_agent_skill_prompt()

    assert prompt is not None
    assert "read_skill" in prompt
    assert 'skill_name="pptx"' in prompt
    assert str(skill_dir) not in prompt


def test_legacy_toolkit_clears_transient_groups_between_user_turns() -> None:
    toolkit = LegacyToolkit()
    toolkit.create_tool_group(
        "browser",
        "Browser automation",
        transient=True,
    )
    toolkit.create_tool_group(
        "persistent",
        "Explicitly persistent tools",
        active=True,
    )
    toolkit.update_tool_groups(["browser"], active=True)

    toolkit.deactivate_transient_groups()

    assert toolkit.groups["browser"].active is False
    assert toolkit.groups["persistent"].active is True


def test_legacy_progressive_loader_lists_concrete_available_tools() -> None:
    toolkit = LegacyToolkit()
    toolkit.create_tool_group(
        "research",
        "Search public sources.",
        transient=True,
    )
    toolkit.register_tool_function(
        toolkit.equip_lantai_tools,
        func_name="equip_lantai_tools",
    )
    toolkit.register_tool_function(
        lambda query: query,
        group_name="research",
        func_name="web_search",
    )
    toolkit.register_tool_function(
        lambda query: query,
        group_name="research",
        func_name="guanlan_search",
    )

    loader = next(
        schema
        for schema in toolkit.get_json_schemas()
        if schema["function"]["name"] == "equip_lantai_tools"
    )
    description = loader["function"]["parameters"]["properties"]["research"][
        "description"
    ]

    assert "web_search" in description
    assert "guanlan_search" in description


def test_legacy_progressive_loader_accumulates_and_explicitly_clears() -> None:
    toolkit = LegacyToolkit()
    toolkit.create_tool_group(
        "collaboration",
        "Delegate to specialist agents.",
        transient=True,
    )
    toolkit.create_tool_group(
        "research",
        "Search public sources.",
        transient=True,
    )

    toolkit.equip_lantai_tools(collaboration=True)
    toolkit.equip_lantai_tools(collaboration=False, research=True)

    assert toolkit.groups["collaboration"].active is True
    assert toolkit.groups["research"].active is True

    toolkit.equip_lantai_tools(collaboration=False, research=False)

    assert toolkit.groups["collaboration"].active is False
    assert toolkit.groups["research"].active is False
