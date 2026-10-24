# -*- coding: utf-8 -*-
import asyncio
import pytest

from lantai.exceptions import AgentStateError
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lantai.app.runner import runner as runner_module
from lantai.agents.context.agent_context import AgentContext
from lantai.app.runner.runner import AgentRunner
from lantai.runtime.converters import legacy_message
from lantai.runtime.messages import RuntimeMessage


async def test_automatic_compaction_marker_is_persisted_but_not_model_visible():
    memory = AgentContext(
        token_counter=SimpleNamespace(count=AsyncMock(return_value=1)),
    )
    marker = legacy_message(
        name="Friday",
        role="assistant",
        content="上下文已整理。",
        metadata={
            "system_action": {
                "kind": "context_compaction",
                "status": "completed",
                "automatic": True,
                "title": "上下文已整理",
            },
        },
    )
    answer = legacy_message(
        name="Friday",
        role="assistant",
        content="继续回答。",
    )
    await memory.add([marker, answer])

    visible = await memory.get_memory(prepend_summary=False)
    state = memory.state_dict()

    assert [msg.id for msg in visible] == [answer.id]
    assert [row[0]["id"] for row in state["content"]] == [marker.id, answer.id]


async def test_native_memory_assembly_includes_summary_but_hides_ui_marker():
    memory = AgentContext(
        token_counter=SimpleNamespace(count=AsyncMock(return_value=1)),
    )
    await memory.update_compressed_summary("## 当前目标\n继续当前任务。")
    marker = legacy_message(
        name="Friday",
        role="assistant",
        content="上下文已自动压缩。",
        metadata={
            "system_action": {
                "kind": "context_compaction",
                "status": "completed",
                "automatic": True,
                "title": "上下文已自动压缩",
            },
        },
    )
    answer = legacy_message(
        name="Friday",
        role="assistant",
        content="最近的回答。",
    )
    await memory.add([marker, answer])

    messages = await AgentRunner._runtime_messages_from_agent_memory(
        SimpleNamespace(memory=memory),
    )

    assert len(messages) == 2
    assert "## 当前目标" in messages[0].text_content()
    assert messages[1].text_content() == "最近的回答。"
    assert all("上下文已自动压缩" not in msg.text_content() for msg in messages)


async def test_next_native_turn_excludes_presentation_only_progress_mirror():
    memory = AgentContext(
        token_counter=SimpleNamespace(count=AsyncMock(return_value=1)),
    )
    public_preamble = legacy_message(
        name="Friday",
        role="assistant",
        content=[
            {
                "type": "thinking",
                "thinking": "我先核对原始记录，再把差异逐项列出来。",
            }
        ],
        metadata={
            "runtime_presentation_only": True,
            "public_progress": {
                "schema_version": 2,
                "id": "public-progress:run-1:client-1:provider-1",
                "revision": 1,
                "source": "runtime_facts",
                "text_origin": "assistant_public_text",
            },
        },
    )
    answer = legacy_message(
        name="Friday",
        role="assistant",
        content="核对完成。",
    )
    await memory.add([public_preamble, answer])

    visible = await memory.get_memory(prepend_summary=False)
    state = memory.state_dict()
    next_turn_messages = await AgentRunner._runtime_messages_from_agent_memory(
        SimpleNamespace(memory=memory),
    )

    assert [message.id for message in visible] == [answer.id]
    assert [row[0]["id"] for row in state["content"]] == [
        public_preamble.id,
        answer.id,
    ]
    assert [message.text_content() for message in next_turn_messages] == [
        "核对完成。"
    ]


async def test_native_memory_ingest_filters_presentation_only_for_generic_store():
    public_preamble = legacy_message(
        name="Friday",
        role="assistant",
        content="不会进入下一轮模型请求。",
        metadata={"runtime_presentation_only": True},
    )
    answer = legacy_message(
        name="Friday",
        role="assistant",
        content="保留的真实回答。",
    )

    class GenericMemory:
        async def get_memory(self, **_kwargs):
            return [public_preamble, answer]

    messages = await AgentRunner._runtime_messages_from_agent_memory(
        SimpleNamespace(memory=GenericMemory()),
    )

    assert [message.text_content() for message in messages] == [
        "保留的真实回答。"
    ]


async def test_native_recall_does_not_reintroduce_presentation_only_progress():
    public_preamble = legacy_message(
        name="Friday",
        role="assistant",
        content="不会经召回重新进入下一轮模型请求。",
        metadata={"runtime_presentation_only": True},
    )

    class FakeContextManager:
        async def pre_reply(self, _agent, kwargs):
            return {"msg": [*kwargs["msg"], public_preamble]}

    recalled = await AgentRunner._native_memory_recall_messages(
        SimpleNamespace(context_manager=FakeContextManager()),
        [RuntimeMessage.text("user", "继续")],
    )

    assert recalled == []


async def test_native_memory_collapses_adjacent_submit_mirrors_only():
    memory = AgentContext(
        token_counter=SimpleNamespace(count=AsyncMock(return_value=1)),
    )
    first = legacy_message(
        name="user",
        role="user",
        content="帮我整理这个问题",
        metadata={"client_turn_id": "optimistic-turn"},
    )
    first.timestamp = "2026-09-04 07:19:00.000"
    mirror = legacy_message(
        name="user",
        role="user",
        content="帮我整理这个问题",
        metadata={"client_turn_id": "persisted-row"},
    )
    mirror.timestamp = "2026-09-04 07:19:00.420"
    answer = legacy_message(
        name="Friday",
        role="assistant",
        content="第一轮完成",
    )
    answer.timestamp = "2026-09-04 07:19:30.000"
    repeated_later = legacy_message(
        name="user",
        role="user",
        content="帮我整理这个问题",
        metadata={"client_turn_id": "later-turn"},
    )
    repeated_later.timestamp = "2026-09-04 07:23:00.000"
    await memory.add([first, mirror, answer, repeated_later])

    messages = await AgentRunner._runtime_messages_from_agent_memory(
        SimpleNamespace(memory=memory),
    )

    assert [
        message.metadata.get("metadata", {}).get("client_turn_id")
        for message in messages
        if message.role == "user"
    ] == ["optimistic-turn", "later-turn"]


async def test_native_context_preflight_streams_status_and_current_input():
    captured: dict[str, object] = {}
    status = legacy_message(
        name="Friday",
        role="assistant",
        content="正在自动压缩上下文。",
    )

    class FakeContextManager:
        async def pre_reasoning(self, agent, kwargs):
            captured["agent"] = agent
            captured["additional"] = kwargs["_additional_context_messages"]
            await kwargs["_context_status_sink"](status, False)

    agent = SimpleNamespace(context_manager=FakeContextManager())

    rows = [
        row
        async for row in AgentRunner._stream_native_context_preflight(
            agent,
            [RuntimeMessage.text("user", "当前问题", id="current-user")],
        )
    ]

    assert rows == [(status, False)]
    assert captured["agent"] is agent
    additional = captured["additional"]
    assert isinstance(additional, list)
    assert additional[0].id == "current-user"
    assert additional[0].get_text_content() == "当前问题"


async def test_native_context_preflight_yields_running_before_completion():
    started = asyncio.Event()
    release = asyncio.Event()
    status = legacy_message(
        name="Friday",
        role="assistant",
        content="正在自动压缩上下文。",
    )

    class FakeContextManager:
        async def pre_reasoning(self, _agent, kwargs):
            await kwargs["_context_status_sink"](status, False)
            started.set()
            await release.wait()

    stream = AgentRunner._stream_native_context_preflight(
        SimpleNamespace(context_manager=FakeContextManager()),
        [RuntimeMessage.text("user", "当前问题")],
    )

    first_row = await anext(stream)
    assert first_row == (status, False)
    assert started.is_set()
    release.set()
    try:
        await anext(stream)
    except StopAsyncIteration:
        pass
    else:
        raise AssertionError("preflight stream should be exhausted")


async def test_native_context_preflight_silent_hang_fails_open(monkeypatch):
    release = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeContextManager:
        async def pre_reasoning(self, _agent, _kwargs):
            try:
                await release.wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(
        runner_module,
        "_FOREGROUND_CONTEXT_PREFLIGHT_SILENT_TIMEOUT_SECONDS",
        0.01,
    )
    rows = [
        row
        async for row in AgentRunner._stream_native_context_preflight(
            SimpleNamespace(context_manager=FakeContextManager()),
            [RuntimeMessage.text("user", "当前问题")],
        )
    ]

    assert rows == []
    assert cancelled.is_set()


async def test_native_memory_load_timeout_fails_closed_and_settles_cancellation(monkeypatch):
    release = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowMemory:
        async def get_memory(self, **_kwargs):
            try:
                await release.wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(
        runner_module,
        "_FOREGROUND_MEMORY_LOAD_TIMEOUT_SECONDS",
        0.01,
    )

    with pytest.raises(AgentStateError, match="历史记录读取超时"):
        await AgentRunner._runtime_messages_from_agent_memory(
            SimpleNamespace(memory=SlowMemory()),
        )
    assert cancelled.is_set()


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("timeout", [0.01, None])
async def test_native_memory_timeout_survives_suppressed_cancellation(
    monkeypatch, cleanup_fails, timeout,
):
    settled = asyncio.Event()

    class Memory:
        async def get_memory(self, **_kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                settled.set()
                if cleanup_fails:
                    raise RuntimeError("cleanup failed")
                return []

    if timeout is not None:
        monkeypatch.setattr(runner_module, "_FOREGROUND_MEMORY_LOAD_TIMEOUT_SECONDS", timeout)
    with pytest.raises(AgentStateError) as error:
        await AgentRunner._runtime_messages_from_agent_memory(SimpleNamespace(memory=Memory()))
    assert error.value.details["reason"] == "native_memory_load_timeout"
    assert settled.is_set()


@pytest.mark.parametrize("timeout_first", [False, True])
async def test_native_memory_repeated_stop_waits_for_cleanup(monkeypatch, timeout_first):
    started, cleaning, release, settled = (asyncio.Event() for _ in range(4))

    class Memory:
        async def get_memory(self, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                settled.set()
                return []

    monkeypatch.setattr(
        runner_module, "_FOREGROUND_MEMORY_LOAD_TIMEOUT_SECONDS",
        0.01 if timeout_first else 10,
    )
    task = asyncio.create_task(AgentRunner._runtime_messages_from_agent_memory(SimpleNamespace(memory=Memory())))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if not timeout_first:
            task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        outcome = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError)
    assert settled.is_set()


@pytest.mark.parametrize("has_history", [False, True])
async def test_native_memory_success_retains_empty_or_existing_history(has_history):
    rows = [legacy_message(name="user", role="user", content="earlier")] if has_history else []
    memory = SimpleNamespace(get_memory=AsyncMock(return_value=rows))
    result = await AgentRunner._runtime_messages_from_agent_memory(SimpleNamespace(memory=memory))
    assert len(result) == len(rows)
    if has_history:
        assert result[0].text_content() == "earlier"


async def test_unconvertible_history_row_aborts_instead_of_returning_partial_history():
    before = legacy_message(name="user", role="user", content="earlier")
    after = legacy_message(name="assistant", role="assistant", content="later")
    memory = SimpleNamespace(get_memory=lambda **kwargs: [before, object(), after])
    with pytest.raises(AgentStateError, match="历史记录.*本轮已停止") as error:
        await AgentRunner._runtime_messages_from_agent_memory(SimpleNamespace(memory=memory))
    assert error.value.details["reason"] == "native_memory_conversion_failed"
    assert error.value.details["row_index"] == 1
