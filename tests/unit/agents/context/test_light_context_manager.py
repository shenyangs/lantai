# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import lantai.agents.context.light_context_manager as light_context_module
from lantai.agents.context.light_context_manager import LightContextManager
from lantai.providers.provider import ModelInfo
from lantai.runtime.converters import LegacyMsg as Msg


def test_compaction_uses_effective_provider_window_not_legacy_ratio(monkeypatch):
    from lantai.providers.provider_manager import ProviderManager

    provider = SimpleNamespace(
        meta={},
        models=[
            ModelInfo(
                id="remote",
                name="remote",
                context_window=64 * 1024,
            ),
        ],
        extra_models=[],
    )
    provider_manager = SimpleNamespace(
        get_active_model=lambda: None,
        get_provider=lambda provider_id: provider if provider_id == "remote" else None,
    )
    monkeypatch.setattr(
        ProviderManager,
        "get_instance",
        staticmethod(lambda: provider_manager),
    )
    config = SimpleNamespace(
        running=SimpleNamespace(
            max_input_length=256 * 1024,
            context_policy_mode="smart",
        ),
        active_model=SimpleNamespace(provider_id="remote", model="remote"),
    )

    assert LightContextManager._effective_context_window(config) == 64 * 1024
    assert (
        LightContextManager._context_input_budget(config)
        < LightContextManager._effective_context_window(config)
    )
    assert LightContextManager._compaction_source_budget(64 * 1024) == 8192


async def test_pre_reply_memory_recall_is_optional_and_time_bounded(
    monkeypatch,
    tmp_path,
):
    manager = LightContextManager(str(tmp_path), "default")
    release = asyncio.Event()

    class FakeMemoryManager:
        async def retrieve(self, *_args, **_kwargs):
            await release.wait()

    fake_config = SimpleNamespace(
        running=SimpleNamespace(
            reme_light_memory_config=SimpleNamespace(
                auto_memory_search_config=SimpleNamespace(enabled=True),
            ),
        ),
    )
    monkeypatch.setattr(
        light_context_module,
        "load_agent_config",
        lambda _agent_id: fake_config,
    )
    monkeypatch.setattr(
        light_context_module,
        "FOREGROUND_MEMORY_RECALL_TIMEOUT_SECONDS",
        0.01,
    )
    agent = SimpleNamespace(
        command_handler=None,
        memory_manager=FakeMemoryManager(),
        name="agent",
    )

    result = await manager.pre_reply(
        agent,
        {"msg": Msg(name="user", role="user", content="ordinary chat")},
    )

    assert result is None


async def test_post_reply_memory_capture_does_not_block(monkeypatch, tmp_path):
    manager = LightContextManager(str(tmp_path), "default")
    capture_started = asyncio.Event()
    release_capture = asyncio.Event()

    class FakeMemoryManager:
        async def capture_turn(self, messages, session_id, source):
            assert source == "post_reply"
            assert session_id == "session-legacy"
            assert [msg.get_text_content() for msg in messages] == [
                "hello",
                "hi",
            ]
            capture_started.set()
            await release_capture.wait()

        def add_summarize_task(self, messages):
            raise AssertionError("summarize should not run in this test")

    fake_config = SimpleNamespace(
        running=SimpleNamespace(
            reme_light_memory_config=SimpleNamespace(
                auto_memory_interval=1,
            ),
        ),
    )
    monkeypatch.setattr(
        light_context_module,
        "load_agent_config",
        lambda _agent_id: fake_config,
    )

    fake_agent = SimpleNamespace(
        memory_manager=FakeMemoryManager(),
        _request_context={"session_id": "session-legacy"},
        memory=SimpleNamespace(
            content=[
                (Msg("user", "hello", "user"), []),
                (Msg("Friday", "hi", "assistant"), []),
            ],
        ),
    )

    await asyncio.wait_for(
        manager.post_reply(fake_agent, {}, Msg("Friday", "hi", "assistant")),
        timeout=0.05,
    )
    await asyncio.wait_for(capture_started.wait(), timeout=0.2)

    tasks = list(manager._post_reply_tasks)
    release_capture.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=0.2)
    assert not manager._post_reply_tasks


async def test_post_reply_private_editor_does_not_capture_global_memory(tmp_path):
    manager = LightContextManager(str(tmp_path), "default")

    class FakeMemoryManager:
        async def capture_turn(self, **_kwargs):
            raise AssertionError("private editor must not feed global memory")

    fake_agent = SimpleNamespace(
        memory_manager=FakeMemoryManager(),
        _request_context={
            "session_id": "session-private-editor",
            "work_method_id": "private_editor",
        },
        memory=SimpleNamespace(
            content=[(Msg("user", "未发表原稿", "user"), [])],
        ),
    )

    assert await manager.post_reply(fake_agent, {}, None) is None
    assert not manager._post_reply_tasks


async def test_system2_runs_only_when_capture_reports_new_memory_work(tmp_path):
    manager = LightContextManager(str(tmp_path), "default")
    calls: list[str] = []

    class FakeMemoryManager:
        async def capture_turn(self, **_kwargs):
            return {"maintenance_required": True}

        async def run_system2_consolidation(self, reason):
            calls.append(reason)

    await manager._capture_turn_after_reply(FakeMemoryManager(), [])

    assert calls == ["post_reply"]


async def test_close_drains_started_post_reply_memory_capture(tmp_path):
    manager = LightContextManager(str(tmp_path), "default")
    completed: list[str] = []

    async def capture() -> None:
        await asyncio.sleep(0)
        completed.append("saved")

    manager._schedule_post_reply_task(capture(), name="memory-capture-test")

    assert await manager.close() is True
    assert completed == ["saved"]


async def test_context_compaction_builds_a_local_checkpoint_without_model_call(
    monkeypatch,
    tmp_path,
):
    manager = LightContextManager(str(tmp_path), "agent-1")

    fake_counter = SimpleNamespace(
        count=AsyncMock(return_value=8),
    )
    monkeypatch.setattr(
        light_context_module,
        "load_agent_config",
        lambda _agent_id: SimpleNamespace(language="zh"),
    )
    monkeypatch.setattr(
        light_context_module,
        "get_token_counter",
        lambda _config: fake_counter,
    )
    monkeypatch.setattr(
        light_context_module.AsMsgHandler,
        "count_msgs_token",
        AsyncMock(return_value=100),
    )
    monkeypatch.setattr(
        light_context_module.AsMsgHandler,
        "format_msgs_to_str",
        AsyncMock(return_value="[user]\n保留任务"),
    )
    monkeypatch.setattr(
        light_context_module.AsMsgHandler,
        "count_str_token",
        AsyncMock(return_value=20),
    )

    result = await manager._compact_context(
        messages=[Msg("user", "保留任务", "user")],
        previous_summary="## 之前的任务\n不要丢失这条约束。",
        language="zh",
        as_token_counter=fake_counter,
    )

    assert result["success"] is True
    assert result["user_message"] == ""
    assert result["history_compact"].startswith("## 较早对话检查点")
    assert "不要丢失这条约束" in result["history_compact"]
    assert "[user]\n保留任务" in result["history_compact"]


async def test_context_compaction_guard_rejects_overlapping_runs(
    monkeypatch,
    tmp_path,
):
    manager = LightContextManager(str(tmp_path), "agent-1")
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_compaction(**_kwargs):
        started.set()
        await release.wait()
        return {
            "success": True,
            "reason": "",
            "history_compact": "## 当前任务\n完成",
            "is_valid": True,
            "before_tokens": 10,
            "after_tokens": 2,
        }

    monkeypatch.setattr(manager, "_compact_context", slow_compaction)
    first = asyncio.create_task(
        manager._compact_context_safe(messages=[object()]),
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)

    second = await manager._compact_context_safe(messages=[object()])

    assert second["success"] is False
    assert second["reason"] == "context compaction already in progress"
    release.set()
    assert (await first)["success"] is True


async def test_context_compaction_cannot_wait_on_an_auxiliary_model(
    monkeypatch,
    tmp_path,
):
    manager = LightContextManager(str(tmp_path), "agent-1")
    fake_counter = SimpleNamespace(count=AsyncMock(return_value=8))

    monkeypatch.setattr(
        light_context_module,
        "load_agent_config",
        lambda _agent_id: SimpleNamespace(language="zh"),
    )
    monkeypatch.setattr(
        light_context_module.AsMsgHandler,
        "count_msgs_token",
        AsyncMock(return_value=321),
    )
    monkeypatch.setattr(
        light_context_module.AsMsgHandler,
        "format_msgs_to_str",
        AsyncMock(return_value="[user]\n需要保留的上下文"),
    )
    monkeypatch.setattr(
        light_context_module.AsMsgHandler,
        "count_str_token",
        AsyncMock(return_value=40),
    )

    result = await asyncio.wait_for(
        manager._compact_context_safe(
            messages=[Msg("user", "需要保留的上下文", "user")],
            language="zh",
            as_token_counter=fake_counter,
        ),
        timeout=0.1,
    )

    assert result["success"] is True
    assert result["before_tokens"] == 321
    assert result["reason"] == ""


async def test_automatic_compaction_emits_one_structured_lifecycle(
    monkeypatch,
    tmp_path,
):
    manager = LightContextManager(str(tmp_path), "agent-1")
    old_messages = [Msg("user", "较早的对话", "user")]
    recent_messages = [Msg("Friday", "最近的回答", "assistant")]
    printed: list[tuple[Msg, bool]] = []
    summarized: list[list[Msg]] = []

    class FakeMemory:
        def __init__(self) -> None:
            self.summary = ""
            self.marked: list[Msg] = []
            self.added: list[Msg] = []

        def get_compressed_summary(self) -> str:
            return self.summary

        async def get_memory(self, prepend_summary=False):
            assert prepend_summary is False
            return [*old_messages, *recent_messages]

        async def mark_messages_compressed(self, messages):
            self.marked = list(messages)
            return len(messages)

        async def update_compressed_summary(self, summary):
            self.summary = summary

        async def add(self, message):
            self.added.append(message)

    memory = FakeMemory()

    class FakeAgent:
        def __init__(self) -> None:
            self.name = "Friday"
            self.sys_prompt = ""
            self.model = None
            self.formatter = None
            self.memory_manager = SimpleNamespace(
                add_summarize_task=lambda messages: summarized.append(
                    list(messages),
                ),
            )
            self.memory = memory

        async def print(self, msg, last=True, speech=None):
            del speech
            printed.append((msg, last))

    fake_config = SimpleNamespace(
        language="zh",
        running=SimpleNamespace(
            light_context_config=SimpleNamespace(
                context_compact_config=SimpleNamespace(
                    enabled=True,
                    compact_threshold_ratio=0.8,
                    compact_with_thinking_block=True,
                ),
            ),
            reme_light_memory_config=SimpleNamespace(
                summarize_when_compact=True,
            ),
        ),
    )
    counter = SimpleNamespace(count=AsyncMock(return_value=0))
    monkeypatch.setattr(
        light_context_module,
        "load_agent_config",
        lambda _agent_id: fake_config,
    )
    monkeypatch.setattr(
        light_context_module,
        "get_token_counter",
        lambda _config: counter,
    )
    monkeypatch.setattr(manager, "_context_input_budget", lambda _config: 1000)
    monkeypatch.setattr(
        manager,
        "_check_context",
        AsyncMock(
            return_value=(old_messages, recent_messages, 1200, 180),
        ),
    )
    monkeypatch.setattr(
        manager,
        "_compact_context_safe",
        AsyncMock(
            return_value={
                "success": True,
                "reason": "",
                "history_compact": "## 当前目标\n保留任务连续性。",
                "before_tokens": 1020,
                "after_tokens": 120,
            },
        ),
    )

    await manager.pre_reasoning(FakeAgent(), {})

    assert len(printed) == 2
    (started, started_last), (completed, completed_last) = printed
    assert started.id == completed.id
    assert started_last is False
    assert completed_last is True
    started_action = started.metadata["system_action"]
    completed_action = completed.metadata["system_action"]
    assert started_action == {
        "kind": "context_compaction",
        "status": "running",
        "title": "正在自动压缩上下文",
        "detail": "正在将较早的 1 条消息转换为摘要；完成后将继续回答。",
        "automatic": True,
        "message_count": 1,
        "before_tokens": 1200,
    }
    assert completed_action["kind"] == "context_compaction"
    assert completed_action["status"] == "completed"
    assert completed_action["title"] == "上下文已自动压缩"
    assert completed_action["message_count"] == 1
    assert completed_action["before_tokens"] == 1200
    assert completed_action["after_tokens"] == 300
    assert completed_action["saved_percent"] == 75
    assert completed_action["summary"] == "## 当前目标\n保留任务连续性。"
    assert "后续回答可能省略部分原始措辞与细节" in completed_action["detail"]
    assert memory.marked == old_messages
    assert memory.summary == "## 当前目标\n保留任务连续性。"
    assert [msg.id for msg in memory.added] == [completed.id]
    assert summarized == []


async def test_automatic_compaction_failure_explains_context_degradation(
    monkeypatch,
    tmp_path,
):
    manager = LightContextManager(str(tmp_path), "agent-1")
    old_messages = [Msg("user", "较早的对话", "user")]
    recent_messages = [Msg("Friday", "最近的回答", "assistant")]
    printed: list[tuple[Msg, bool]] = []

    class FakeMemory:
        def __init__(self) -> None:
            self.added: list[Msg] = []

        def get_compressed_summary(self) -> str:
            return ""

        async def get_memory(self, prepend_summary=False):
            assert prepend_summary is False
            return [*old_messages, *recent_messages]

        async def mark_messages_compressed(self, messages):
            assert messages == old_messages
            return len(messages)

        async def update_compressed_summary(self, summary):
            assert summary == ""

        async def add(self, message):
            self.added.append(message)

    memory = FakeMemory()

    class FakeAgent:
        def __init__(self) -> None:
            self.name = "Friday"
            self.sys_prompt = ""
            self.model = None
            self.formatter = None
            self.memory_manager = SimpleNamespace(
                add_summarize_task=lambda **_kwargs: None,
            )
            self.memory = memory

        async def print(self, msg, last=True, speech=None):
            del speech
            printed.append((msg, last))

    fake_config = SimpleNamespace(
        language="zh",
        running=SimpleNamespace(
            light_context_config=SimpleNamespace(
                context_compact_config=SimpleNamespace(
                    enabled=True,
                    compact_threshold_ratio=0.8,
                    compact_with_thinking_block=True,
                ),
            ),
            reme_light_memory_config=SimpleNamespace(
                summarize_when_compact=False,
            ),
        ),
    )
    counter = SimpleNamespace(count=AsyncMock(return_value=0))
    monkeypatch.setattr(
        light_context_module,
        "load_agent_config",
        lambda _agent_id: fake_config,
    )
    monkeypatch.setattr(
        light_context_module,
        "get_token_counter",
        lambda _config: counter,
    )
    monkeypatch.setattr(manager, "_context_input_budget", lambda _config: 1000)
    monkeypatch.setattr(
        manager,
        "_check_context",
        AsyncMock(
            side_effect=[
                (old_messages, recent_messages, 1200, 180),
                (old_messages, recent_messages, 1200, 180),
            ],
        ),
    )
    monkeypatch.setattr(
        manager,
        "_compact_context_safe",
        AsyncMock(
            return_value={
                "success": False,
                "reason": "utility model unavailable",
                "history_compact": "",
                "before_tokens": 1020,
                "after_tokens": 0,
            },
        ),
    )

    await manager.pre_reasoning(FakeAgent(), {})

    assert len(printed) == 2
    started, degraded = printed[0][0], printed[1][0]
    assert started.id == degraded.id
    action = degraded.metadata["system_action"]
    assert action["status"] == "degraded"
    assert action["title"] == "上下文已降级整理"
    assert action["technical_reason"] == "utility model unavailable"
    assert "较早的 1 条未进入本轮上下文" in action["detail"]
    assert "请补充关键背景" in action["detail"]
    assert [msg.id for msg in memory.added] == [degraded.id]
