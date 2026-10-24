import pytest
from datetime import datetime, timezone
from types import SimpleNamespace

from lantai.agents.memory.proactive.proactive_trigger import (
    enable_proactive_for_session,
    is_last_message_proactive,
    proactive_configs,
    proactive_tasks,
)
from lantai.agents.memory.proactive import proactive_trigger
from lantai.runtime.converters import LegacyInMemoryMemory, legacy_message


class _FakeChatManager:
    def __init__(self, chats):
        self._chats = chats

    async def list_chats(self):
        return self._chats


class _FakeSession:
    def __init__(self, state):
        self._state = state

    async def get_session_state_dict(self, session_id, user_id, channel):
        return self._state


async def _memory_state_with_message(content: str):
    memory = LegacyInMemoryMemory()
    await memory.add(
        legacy_message(name="assistant", role="assistant", content=content),
    )
    return {"agent": {"memory": memory.state_dict()}}


@pytest.mark.asyncio
async def test_is_last_message_proactive_detects_latest_proactive_message():
    state = await _memory_state_with_message("[PROACTIVE] quick nudge")
    chat = SimpleNamespace(
        updated_at=datetime.now(timezone.utc),
        session_id="session-1",
        user_id="user-1",
        channel="console",
    )
    workspace = SimpleNamespace(
        chat_manager=_FakeChatManager([chat]),
        runner=SimpleNamespace(session=_FakeSession(state)),
    )

    assert await is_last_message_proactive(workspace) is True


@pytest.mark.asyncio
async def test_is_last_message_proactive_returns_false_for_regular_message():
    state = await _memory_state_with_message("ordinary assistant reply")
    chat = SimpleNamespace(
        updated_at=datetime.now(timezone.utc),
        session_id="session-1",
        user_id="user-1",
        channel="console",
    )
    workspace = SimpleNamespace(
        chat_manager=_FakeChatManager([chat]),
        runner=SimpleNamespace(session=_FakeSession(state)),
    )

    assert await is_last_message_proactive(workspace) is False


@pytest.mark.asyncio
async def test_enable_proactive_threads_the_guarded_workspace_into_loop(
    monkeypatch,
):
    workspace = object()
    calls = []

    async def fake_loop(session_id, received_workspace):
        calls.append((session_id, received_workspace))

    monkeypatch.setattr(proactive_trigger, "proactive_trigger_loop", fake_loop)
    proactive_configs.pop("guarded-session", None)
    proactive_tasks.pop("guarded-session", None)

    enable_proactive_for_session(
        "guarded-session",
        30,
        workspace=workspace,
    )
    await proactive_tasks["guarded-session"]

    assert calls == [("guarded-session", workspace)]
    proactive_configs.pop("guarded-session", None)
    proactive_tasks.pop("guarded-session", None)
