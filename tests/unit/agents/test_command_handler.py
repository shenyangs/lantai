# -*- coding: utf-8 -*-
from types import SimpleNamespace

import pytest

from lantai.agents.command_handler import CommandHandler


class DummyMemory:
    def __init__(self) -> None:
        self.clear_content_called = 0
        self.clear_summary_called = 0
        self.summary = ""

    async def clear_content(self) -> None:
        self.clear_content_called += 1

    def clear_compressed_summary(self) -> None:
        self.clear_summary_called += 1
        self.summary = ""

    def get_compressed_summary(self) -> str:
        return self.summary

    async def update_compressed_summary(self, summary: str) -> None:
        self.summary = summary

    async def get_memory(self, prepend_summary: bool = False) -> list:
        assert prepend_summary is False
        return []


@pytest.mark.asyncio
async def test_process_clear_returns_clear_history_metadata() -> None:
    memory = DummyMemory()
    handler = CommandHandler(agent_name="Lantai", memory=memory)

    msg = await handler.handle_command("/clear")

    assert memory.clear_content_called == 1
    assert memory.clear_summary_called == 1
    assert msg.metadata["clear_history"] is True
    assert msg.metadata["clear_plan"] is True
    assert msg.metadata["system_command"] == "clear"
    assert msg.metadata["system_action"] == {
        "kind": "history_clear",
        "status": "completed",
        "title": "对话记录已清空",
        "detail": "上下文摘要和任务计划也已重置。",
    }


class DummyMemoryManager:
    def __init__(self) -> None:
        self.tasks: list[list] = []

    def add_summarize_task(self, messages: list) -> None:
        self.tasks.append(messages)


class DummyContextManager:
    async def compact_context(self, **_kwargs) -> dict:
        return {
            "success": True,
            "history_compact": (
                "# 当前目标\n\n保留用户界面优化目标。\n\n"
                "# 已知问题\n\n（无）"
            ),
            "before_tokens": 1000,
            "after_tokens": 250,
        }


class TimeoutContextManager:
    async def compact_context(self, **_kwargs) -> dict:
        return {
            "success": False,
            "reason": (
                "utility model timed out waiting for first model output "
                "after 20s"
            ),
            "history_compact": "",
            "before_tokens": 3200,
            "after_tokens": 0,
        }


@pytest.mark.asyncio
async def test_compact_returns_structured_action_instead_of_debug_markdown() -> None:
    memory = DummyMemory()
    memory.get_memory = lambda prepend_summary=False: _async_value(  # type: ignore[method-assign]
        [SimpleNamespace(content="hello")],
    )
    manager = DummyMemoryManager()
    handler = CommandHandler(
        agent_name="Lantai",
        memory=memory,
        memory_manager=manager,
        context_manager=DummyContextManager(),
    )
    handler._get_agent_config = lambda: SimpleNamespace(  # type: ignore[method-assign]
        running=SimpleNamespace(max_input_length=4000),
    )

    msg = await handler.handle_command("/compact")

    assert msg.get_text_content() == "对话已压缩：1 条消息，1.0k → 250 Token。"
    assert msg.metadata["system_command"] == "compact"
    action = msg.metadata["system_action"]
    assert action["kind"] == "context_compaction"
    assert action["status"] == "completed"
    assert action["message_count"] == 1
    assert action["before_tokens"] == 1000
    assert action["after_tokens"] == 250
    assert action["saved_percent"] == 75
    assert action["summary"].startswith("# 当前目标")
    assert "Compact Complete" not in msg.get_text_content()
    assert "background_memory_task" not in action
    assert manager.tasks == []


@pytest.mark.asyncio
async def test_compact_timeout_preserves_existing_conversation() -> None:
    memory = DummyMemory()
    memory.summary = "# 当前目标\n保留旧摘要"
    messages = [SimpleNamespace(content="hello")]
    memory.get_memory = lambda prepend_summary=False: _async_value(  # type: ignore[method-assign]
        messages,
    )
    manager = DummyMemoryManager()
    handler = CommandHandler(
        agent_name="Lantai",
        memory=memory,
        memory_manager=manager,
        context_manager=TimeoutContextManager(),
    )
    handler._get_agent_config = lambda: SimpleNamespace(  # type: ignore[method-assign]
        running=SimpleNamespace(max_input_length=4000),
    )

    msg = await handler.handle_command("/compact")

    action = msg.metadata["system_action"]
    assert action["status"] == "failed"
    assert action["before_tokens"] == 3200
    assert "first model output" in action["technical_reason"]
    assert memory.summary == "# 当前目标\n保留旧摘要"
    assert memory.clear_content_called == 0
    assert manager.tasks == []


async def _async_value(value):
    return value
