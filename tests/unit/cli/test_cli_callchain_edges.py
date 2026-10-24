"""Long inputs, desktop auth routing and Runtime terminal truth at CLI edges."""
import json
from types import SimpleNamespace

import pytest

from lantai.cli.http import client
from lantai.cli.mcp_cmd import _json_object
from lantai.cli.task_cmd import _read_instruction, _run_task


def test_long_instruction_is_text_not_a_filesystem_error():
    text = "请整理这些资料并完成分析。" * 300
    assert _read_instruction(text) == text


def test_large_inline_mcp_json_is_parsed_without_stat():
    config = {"name": "demo", "env": {"CUSTOM_DATA": "x" * 8000}}
    assert _json_object(json.dumps(config), "config") == config


@pytest.mark.parametrize(
    "url,has_token",
    [
        ("http://127.0.0.1:8123", True),
        ("http://127.0.0.1:8124", False),
        ("https://example.com", False),
    ],
)
def test_cli_attaches_existing_desktop_token_only_to_current_origin(
    monkeypatch, url, has_token
):
    from lantai.agents.tools import agent_management

    monkeypatch.setenv("LANTAI_DESKTOP_TOKEN", "test-only-token")
    monkeypatch.setattr(
        agent_management, "read_last_api", lambda: ("127.0.0.1", 8123)
    )
    with client(url) as http:
        request = http.build_request("GET", "/mcp")
        assert (
            request.headers.get("X-Lantai-Desktop-Token") == "test-only-token"
        ) is has_token
        assert request.url.path == "/api/mcp"


@pytest.mark.parametrize(
    "terminal,expected",
    [
        ("turn.failed", "error"),
        ("turn.interrupted", "cancelled"),
        (None, "error"),
    ],
)
async def test_headless_cli_does_not_report_failed_or_unsettled_turn_as_success(
    monkeypatch, tmp_path, terminal, expected
):
    class Agent:
        def __init__(self, **kwargs):
            self._mcp_clients = []

        async def register_mcp_clients(self):
            pass

    class Runner:
        def __init__(self, **kwargs):
            pass

        async def _stream_standard_runtime_turn(self, **kwargs):
            yield SimpleNamespace(
                metadata={"runtime_event_type": "message.completed"},
                get_text_content=lambda: "partial work",
            ), False
            if terminal:
                yield SimpleNamespace(
                    metadata={"runtime_event_type": terminal},
                    get_text_content=lambda: "",
                ), True

    monkeypatch.setattr(
        "lantai.agents.legacy_lantai_agent.LegacyLantaiAgent", Agent
    )
    monkeypatch.setattr("lantai.app.runner.runner.AgentRunner", Runner)
    result = await _run_task(
        "test",
        SimpleNamespace(
            id="test",
            workspace_dir=str(tmp_path),
            running=SimpleNamespace(max_iters=3),
        ),
        {},
        3,
        1,
        None,
    )
    assert result["status"] == expected
    assert result["response"] == "partial work"


@pytest.mark.parametrize("outcome", ["success", "error", "timeout"])
async def test_headless_task_connects_and_closes_configured_mcp(
    monkeypatch, tmp_path, outcome
):
    from unittest.mock import AsyncMock

    mcp_config = object()
    mcp_client = object()
    manager = SimpleNamespace(
        init_from_config=AsyncMock(),
        get_clients=AsyncMock(return_value=[mcp_client]),
        close_all=AsyncMock(),
    )
    if outcome == "error":
        manager.init_from_config.side_effect = RuntimeError(
            "initialization failed"
        )
    elif outcome == "timeout":
        import asyncio

        async def pending(*args):
            await asyncio.Event().wait()

        manager.init_from_config.side_effect = pending
    monkeypatch.setattr(
        "lantai.app.mcp.manager.MCPClientManager", lambda **_: manager
    )

    class Agent:
        def __init__(self, **kwargs):
            self._mcp_clients = []

        async def register_mcp_clients(self):
            assert self._mcp_clients == [mcp_client]

    class Runner:
        def __init__(self, **kwargs):
            pass

        async def _stream_standard_runtime_turn(self, **kwargs):
            assert kwargs["mcp_clients"] == [mcp_client]
            yield SimpleNamespace(
                metadata={"runtime_event_type": "turn.completed"},
                get_text_content=lambda: "",
            ), True

    monkeypatch.setattr(
        "lantai.agents.legacy_lantai_agent.LegacyLantaiAgent", Agent
    )
    monkeypatch.setattr("lantai.app.runner.runner.AgentRunner", Runner)
    config = SimpleNamespace(
        id="test",
        workspace_dir=str(tmp_path),
        running=SimpleNamespace(max_iters=3),
        mcp=mcp_config,
    )
    result = await _run_task(
        "test", config, {}, 3, 0.03 if outcome == "timeout" else 1, None
    )
    assert result["status"] == outcome, result
    manager.init_from_config.assert_awaited_once_with(mcp_config)
    manager.close_all.assert_awaited_once()
