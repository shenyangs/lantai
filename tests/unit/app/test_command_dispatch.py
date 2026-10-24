from __future__ import annotations

from types import SimpleNamespace

import pytest

import lantai.app.runner.command_dispatch as command_dispatch
from lantai.app.runner.command_dispatch import run_command_path
from lantai.runtime.converters import legacy_assistant_text_msg


class _RuntimeUserMsg:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text_content(self) -> str:
        return self._text


class _Memory:
    def __init__(self) -> None:
        self.loaded: dict[str, object] | None = None

    def load_state_dict(self, state: dict[str, object], strict: bool = False) -> None:
        self.loaded = {"state": state, "strict": strict}

    def state_dict(self) -> dict[str, object]:
        return {"loaded": self.loaded}


class _ContextManager:
    def __init__(self, memory: _Memory) -> None:
        self.memory = memory

    def get_agent_context(self) -> _Memory:
        return self.memory


class _Session:
    def __init__(self) -> None:
        self.get_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []

    async def get_session_state_dict(self, **kwargs) -> dict[str, object]:
        self.get_calls.append(kwargs)
        return {"agent": {"memory": {"remembered": True}}}

    async def update_session_state(self, **kwargs) -> None:
        self.update_calls.append(kwargs)


def _first_text(msg) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    block = content[0]
    if isinstance(block, dict):
        return str(block.get("text") or "")
    return str(getattr(block, "text", ""))


@pytest.mark.asyncio
async def test_conversation_command_uses_request_channel_string(monkeypatch) -> None:
    class FakeCommandHandler:
        def __init__(self, **_kwargs) -> None:
            pass

        async def handle_conversation_command(self, query: str):
            assert query == "/new"
            return legacy_assistant_text_msg("conversation ok")

    monkeypatch.setattr(command_dispatch, "CommandHandler", FakeCommandHandler)

    memory = _Memory()
    session = _Session()
    runner = SimpleNamespace(
        agent_id="default",
        context_manager=_ContextManager(memory),
        memory_manager=SimpleNamespace(),
        session=session,
    )
    request = SimpleNamespace(
        session_id="s-command",
        user_id="u-1",
        channel="console",
    )

    output = [
        (_first_text(msg), last)
        async for msg, last in run_command_path(
            request,
            [_RuntimeUserMsg("/new")],
            runner,
        )
    ]

    assert output == [("conversation ok", True)]
    assert session.get_calls[0]["channel"] == "console"
    assert session.update_calls[0]["channel"] == "console"
    assert memory.loaded == {"state": {"remembered": True}, "strict": False}


@pytest.mark.asyncio
async def test_conversation_command_empty_runtime_error_is_visible(
    monkeypatch,
) -> None:
    class FakeCommandHandler:
        def __init__(self, **_kwargs) -> None:
            pass

        async def handle_conversation_command(self, _query: str):
            raise RuntimeError()

    monkeypatch.setattr(command_dispatch, "CommandHandler", FakeCommandHandler)

    runner = SimpleNamespace(
        agent_id="default",
        context_manager=_ContextManager(_Memory()),
        memory_manager=SimpleNamespace(),
        session=_Session(),
    )
    request = SimpleNamespace(
        session_id="s-command",
        user_id="u-1",
        channel="console",
    )

    output = [
        (_first_text(msg), last)
        async for msg, last in run_command_path(
            request,
            [_RuntimeUserMsg("/new")],
            runner,
        )
    ]

    assert output == [("Conversation command: RuntimeError", True)]


@pytest.mark.asyncio
async def test_control_command_empty_error_is_visible(monkeypatch) -> None:
    class _ChannelManager:
        async def get_channel(self, channel_id: str):
            return SimpleNamespace(channel=channel_id)

    async def fail_control_command(_query: str, _ctx) -> str:
        raise RuntimeError()

    monkeypatch.setattr(command_dispatch, "_is_control_command", lambda _query: True)
    monkeypatch.setattr(
        command_dispatch.control_commands,
        "handle_control_command",
        fail_control_command,
    )

    runner = SimpleNamespace(
        agent_id="default",
        _workspace=SimpleNamespace(channel_manager=_ChannelManager()),
    )
    request = SimpleNamespace(
        session_id="s-command",
        user_id="u-1",
        channel="console",
    )

    output = [
        (_first_text(msg), last)
        async for msg, last in run_command_path(
            request,
            [_RuntimeUserMsg("/stop")],
            runner,
        )
    ]

    assert output == [("**Command Failed**\n\nControl command: RuntimeError", True)]
