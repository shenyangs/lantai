# -*- coding: utf-8 -*-
# pylint: disable=no-name-in-module,protected-access,unused-argument
# pylint: disable=redefined-outer-name,reimported
# Pylint infers the SDK Pydantic default None instead of the constructor env.
# pylint: disable=unsubscriptable-object,unsupported-membership-test
import asyncio
from contextlib import suppress
import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

from lantai.app.mcp.manager import MCPClientManager
from lantai.app.mcp.guanlan import GUANLAN_CLIENT_KEY, GUANLAN_MCP_NAME
from lantai.app.mcp.stateful_client import (
    StdIOStatefulClient,
    reconnect_backoff_seconds,
)
import lantai.app.officecli_safe_bridge as office_bridge


def test_mcp_reconnect_uses_bounded_exponential_backoff():
    assert [reconnect_backoff_seconds(attempt) for attempt in range(1, 8)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
        30.0,
        30.0,
    ]


def test_mcp_children_keep_the_parent_process_owner(monkeypatch):
    nonce = "0123456789abcdef" * 4
    monkeypatch.setenv("LANTAI_DESKTOP_INSTANCE_NONCE", nonce)
    monkeypatch.setenv("LANTAI_DESKTOP_TOKEN", "must-not-leak")
    client = StdIOStatefulClient(
        "owned",
        "python",
        env={
            "LANTAI_DESKTOP_INSTANCE_NONCE": "foreign-owner",
            "LANTAI_DESKTOP_TOKEN": "must-not-leak",
        },
    )
    assert client.server_params.env["LANTAI_DESKTOP_INSTANCE_NONCE"] == nonce
    assert "LANTAI_DESKTOP_TOKEN" not in client.server_params.env


def test_stdio_client_uses_sandboxed_environment(monkeypatch):
    monkeypatch.setenv("LANTAI_BROWSER_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("LANTAI_DESKTOP_AUTH_TOKEN", "desktop-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    client = StdIOStatefulClient("test", "python")

    env = client.server_params.env
    assert env is not None
    assert "LANTAI_BROWSER_BRIDGE_TOKEN" not in env
    assert "LANTAI_DESKTOP_AUTH_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "PATH" in env


@pytest.mark.parametrize(
    "flag",
    ["LANTAI_DISABLE_KEYRING", "LANTAI_SKIP_KEYRING"],
)
def test_stdio_client_preserves_parent_keyring_opt_out(monkeypatch, flag):
    monkeypatch.setenv(flag, "1")
    monkeypatch.setenv("LANTAI_BROWSER_BRIDGE_TOKEN", "must-not-leak")
    client = StdIOStatefulClient(
        "guanlan",
        "lantai-server",
        args=["guanlan-mcp"],
        env={flag: "0"},
    )
    assert client.server_params.env[flag] == "1"
    assert "LANTAI_BROWSER_BRIDGE_TOKEN" not in client.server_params.env


def test_stdio_client_cannot_inject_keyring_policy(monkeypatch):
    flags = ("LANTAI_DISABLE_KEYRING", "LANTAI_SKIP_KEYRING")
    for flag in flags:
        monkeypatch.delenv(flag, raising=False)
    client = StdIOStatefulClient(
        "test",
        "python",
        env=dict.fromkeys(flags, "1"),
    )
    assert all(flag not in client.server_params.env for flag in flags)


def test_stdio_client_overlays_explicit_env_without_internal_tokens(
    monkeypatch,
):
    monkeypatch.setenv("LANTAI_BROWSER_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")

    client = StdIOStatefulClient(
        "test",
        "python",
        env={
            "CUSTOM_MCP_TOKEN": "explicit-mcp-secret",
            "LANTAI_BROWSER_BRIDGE_TOKEN": "must-not-pass",
        },
    )

    env = client.server_params.env
    assert env is not None
    assert env["CUSTOM_MCP_TOKEN"] == "explicit-mcp-secret"
    assert "LANTAI_BROWSER_BRIDGE_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


@pytest.mark.asyncio
async def test_stdio_client_close_stops_pending_lifecycle_task():
    client = StdIOStatefulClient("test", "python")
    started = asyncio.Event()

    async def pending_lifecycle():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(pending_lifecycle())
    await started.wait()
    client._lifecycle_task = task
    client.is_connected = False

    await client.close()

    assert client._lifecycle_task is None
    assert task.done()
    assert client.session is None
    assert client.is_connected is False


@pytest.mark.asyncio
async def test_stateful_client_callable_function_uses_lantai_schema():
    client = StdIOStatefulClient("test", "python")
    tool = SimpleNamespace(
        name="search_notes",
        description="Search notes",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    calls = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[tool])

        async def call_tool(
            self,
            name,
            arguments=None,
            read_timeout_seconds=None,
        ):
            calls.append((name, arguments, read_timeout_seconds))
            return {"ok": True}

    client.session = Session()
    client.is_connected = True

    func = await client.get_callable_function(
        "search_notes",
        execution_timeout=1,
    )
    result = await func(query="runtime")

    assert result == {"ok": True}
    assert func.__name__ == "search_notes"
    assert func.json_schema["function"]["parameters"] == tool.inputSchema
    assert calls[0][0] == "search_notes"
    assert calls[0][1] == {"query": "runtime"}
    assert calls[0][2] == 1.0


@pytest.mark.asyncio
async def test_stateful_client_exposes_resources_and_prompts() -> None:
    client = StdIOStatefulClient("test", "python")

    class Session:
        async def list_resources(self):
            return SimpleNamespace(
                resources=[SimpleNamespace(uri="file:///one")],
            )

        async def list_resource_templates(self):
            return SimpleNamespace(
                resource_templates=[
                    SimpleNamespace(uriTemplate="file:///{id}"),
                ],
            )

        async def read_resource(self, uri):
            return SimpleNamespace(contents=[{"uri": uri, "text": "one"}])

        async def list_prompts(self):
            return SimpleNamespace(prompts=[SimpleNamespace(name="review")])

        async def get_prompt(self, name, arguments=None):
            return SimpleNamespace(description=name, messages=[arguments])

    client.session = Session()
    client.is_connected = True

    assert (await client.list_resources())[0].uri == "file:///one"
    assert (await client.list_resource_templates())[
        0
    ].uriTemplate == "file:///{id}"
    assert (await client.read_resource("file:///one")).contents[0][
        "text"
    ] == "one"
    assert (await client.list_prompts())[0].name == "review"
    prompt = await client.get_prompt("review", {"focus": "safety"})
    assert prompt.messages == [{"focus": "safety"}]


@pytest.mark.asyncio
async def test_stateful_client_collects_all_cursor_pages() -> None:
    client = StdIOStatefulClient("test", "python")

    class ProtocolClient:
        async def list_resources(self, cursor=None):
            if cursor is None:
                return SimpleNamespace(
                    resources=[SimpleNamespace(uri="file:///one")],
                    next_cursor="page-2",
                )
            return SimpleNamespace(
                resources=[SimpleNamespace(uri="file:///two")],
                next_cursor=None,
            )

    client._sdk_client = ProtocolClient()
    client.session = SimpleNamespace()
    client.is_connected = True

    resources = await client.list_resources()

    assert [item.uri for item in resources] == ["file:///one", "file:///two"]


@pytest.mark.asyncio
async def test_stateful_client_exposes_fail_closed_mrtr_exchange() -> None:
    client = StdIOStatefulClient("test", "python")
    calls = []

    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, arguments, kwargs))
            return SimpleNamespace(
                result_type="input_required",
                request_state="s1",
            )

    client.session = Session()
    client.is_connected = True

    result = await client.call_tool_exchange("collect", {"topic": "x"})

    assert result.result_type == "input_required"
    assert calls[0][2]["allow_input_required"] is True
    assert calls[0][2]["input_responses"] is None


@pytest.mark.asyncio
async def test_builtin_guanlan_repairs_safe_numeric_arguments_before_call():
    client = StdIOStatefulClient(GUANLAN_MCP_NAME, "python")
    client._lantai_client_key = GUANLAN_CLIENT_KEY
    tool = SimpleNamespace(
        name="guanlan_research",
        description="Build a guarded research packet",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "read_top": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                },
            },
            "required": ["query"],
        },
    )
    calls = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[tool])

        async def call_tool(
            self,
            name,
            arguments=None,
            read_timeout_seconds=None,
        ):
            calls.append((name, arguments, read_timeout_seconds))
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="research packet")],
                isError=False,
            )

    client.session = Session()
    client.is_connected = True

    func = await client.get_callable_function("guanlan_research")
    result = await func(query="topic", read_top=3)

    assert calls[0][1] == {"query": "topic", "read_top": 2}
    assert "read_top" in result.content[0].text
    assert "3" in result.content[0].text
    assert "2" in result.content[0].text
    assert "not a permission or tool-call limit" in result.content[0].text


@pytest.mark.asyncio
async def test_builtin_guanlan_applies_native_defaults_and_tool_timeout():
    client = StdIOStatefulClient(GUANLAN_MCP_NAME, "python")
    client._lantai_client_key = GUANLAN_CLIENT_KEY
    tool = SimpleNamespace(
        name="guanlan_research",
        description="Build a guarded research packet",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "profile": {"type": "string"},
                "format": {"type": "string"},
                "read_top": {"type": "integer", "minimum": 0, "maximum": 5},
                "max_search_jobs": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                },
            },
            "required": ["query"],
        },
    )
    calls = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[tool])

        async def call_tool(
            self,
            name,
            arguments=None,
            read_timeout_seconds=None,
        ):
            calls.append((name, arguments, read_timeout_seconds))
            return SimpleNamespace(content=[], isError=False)

    client.session = Session()
    client.is_connected = True

    func = await client.get_callable_function("guanlan_research")
    await func(query="对比三家车企")

    assert calls[0][1] == {
        "query": "对比三家车企",
        "limit": 80,
        "profile": "china",
        "format": "json",
        "read_top": 0,
        "max_search_jobs": 2,
    }
    assert calls[0][2] == 300.0


@pytest.mark.asyncio
async def test_builtin_guanlan_compacts_search_pool_before_model_context():
    client = StdIOStatefulClient(GUANLAN_MCP_NAME, "python")
    client._lantai_client_key = GUANLAN_CLIENT_KEY
    tool = SimpleNamespace(
        name="guanlan_search",
        description="Search public sources",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "profile": {"type": "string"},
                "format": {"type": "string"},
                "evidence_mode": {"type": "string"},
            },
        },
    )
    raw_payload = {
        "results": [
            {
                "title": f"Result {index}",
                "url": f"https://source{index}.example/item",
                "domain": f"source{index}.example",
                "snippet": f"Summary {index}",
                "topic_key": f"topic-{index}",
                "evidence_role": "primary",
                "trace": {"debug": True},
            }
            for index in range(20)
        ],
        "agent_followup": {"next_decision": "answer"},
    }

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[tool])

        async def call_tool(
            self,
            name,
            arguments=None,
            read_timeout_seconds=None,
        ):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(raw_payload),
                    ),
                ],
                structuredContent=raw_payload,
            )

    client.session = Session()
    client.is_connected = True
    client._cached_tools = [tool]

    result = await client.call_tool("guanlan_search", {"query": "AI policy"})
    model_payload = json.loads(result.content[0].text)

    assert len(model_payload["results"]) == 12
    assert model_payload["lantai_view"]["original_count"] == 20
    assert model_payload["lantai_review"]["decision"] == "repair"
    assert model_payload["lantai_review"]["requested_limit"] == 80
    assert "insufficient_candidate_pool" in (
        model_payload["lantai_review"]["reasons"]
    )
    assert result.structured_content == model_payload
    assert all(
        "trace" not in row for row in result.structured_content["results"]
    )
    assert result.meta["lantai_guanlan"]["raw_response"] == raw_payload


@pytest.mark.asyncio
async def test_builtin_guanlan_direct_call_uses_the_same_argument_repair():
    client = StdIOStatefulClient(GUANLAN_MCP_NAME, "python")
    client._lantai_client_key = GUANLAN_CLIENT_KEY
    tool = SimpleNamespace(
        name="guanlan_research",
        description="Build a guarded research packet",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "read_top": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                },
            },
        },
    )
    calls = []

    class Session:
        async def call_tool(
            self,
            name,
            arguments=None,
            read_timeout_seconds=None,
        ):
            calls.append((name, arguments, read_timeout_seconds))
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="research packet")],
                isError=False,
            )

    client.session = Session()
    client.is_connected = True
    client._cached_tools = [tool]

    result = await client.call_tool(
        "guanlan_research",
        {"query": "topic", "read_top": 3},
    )

    assert calls[0][0] == "guanlan_research"
    assert calls[0][1] == {"query": "topic", "read_top": 2}
    assert calls[0][2] == 300.0
    assert "not a permission or tool-call limit" in result.content[0].text


@pytest.mark.asyncio
async def test_builtin_officecli_writes_via_copy_validate_atomic_receipt(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(office_bridge, "WORKING_DIR", tmp_path / "working")
    source = tmp_path / "report.docx"
    source.write_bytes(b"before")
    calls = []

    class Session:
        async def call_tool(
            self,
            name,
            arguments=None,
            read_timeout_seconds=None,
        ):
            _ = read_timeout_seconds
            calls.append((name, arguments))
            command = arguments["command"]
            if command[0] == "set":
                assert command[1] != str(source)
                office_bridge.Path(command[1]).write_bytes(b"after")
            return CallToolResult(
                content=[TextContent(type="text", text=f"ran {command[0]}")],
                isError=False,
            )

    client = StdIOStatefulClient("Office 文档能力", "python")
    client._lantai_client_key = "officecli"
    client.session = Session()
    client.is_connected = True

    result = await client.call_tool(
        "officecli",
        {"command": ["set", str(source), "/body/p[1]", "--prop", "bold=true"]},
    )

    assert [call[1]["command"][0] for call in calls] == [
        "set",
        "close",
        "validate",
        "close",
    ]
    assert source.read_bytes() == b"after"
    assert "Lantai Office delivery receipt" in result.content[-1].text
    assert "OpenXML validate passed" in result.content[-1].text
    assert "rollback backup" in result.content[-1].text


@pytest.mark.asyncio
async def test_openchatcut_connector_requires_manual_edit_review():
    calls = []

    class Session:
        async def call_tool(
            self,
            name,
            arguments=None,
            read_timeout_seconds=None,
        ):
            calls.append((name, arguments, read_timeout_seconds))
            return CallToolResult(
                content=[TextContent(type="text", text="draft started")],
                isError=False,
            )

    client = StdIOStatefulClient("视频工程 · OpenChatCut", "python")
    client._lantai_client_key = "openchatcut"
    client.session = Session()
    client.is_connected = True

    await client.call_tool("begin_edit_session", {})
    with pytest.raises(PermissionError, match="manual review"):
        await client.call_tool(
            "begin_edit_session",
            {"approvalMode": "auto"},
        )

    assert calls == [("begin_edit_session", {"approvalMode": "manual"}, None)]


@pytest.mark.asyncio
async def test_force_cleanup_client_uses_close_without_stack():
    class PendingClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    client = PendingClient()

    await MCPClientManager._force_cleanup_client(client)

    assert client.closed is True


@pytest.mark.asyncio
async def test_mcp_manager_publishes_replacement_before_closing_old_client(
    monkeypatch,
):
    manager = MCPClientManager()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class OldClient:
        async def close(self):
            close_started.set()
            await allow_close.wait()

    class NewClient:
        async def connect(self, timeout=60.0):
            _ = timeout

    old_client = OldClient()
    new_client = NewClient()
    manager._clients["docs"] = old_client

    async def prepare(_key, config):
        return config

    monkeypatch.setattr(manager, "_prepare_client_config", prepare)
    monkeypatch.setattr(manager, "_build_client", lambda _config: new_client)

    replacement = asyncio.create_task(
        manager.replace_client("docs", SimpleNamespace()),
    )
    await close_started.wait()

    published = await asyncio.wait_for(
        manager.get_client("docs"),
        timeout=0.1,
    )
    assert published is new_client
    assert new_client._lantai_client_key == "docs"

    allow_close.set()
    await replacement


@pytest.mark.asyncio
async def test_mcp_manager_background_init_is_cancelled_on_close(
    monkeypatch,
):
    manager = MCPClientManager()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def pending_init(_config):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(manager, "init_from_config", pending_init)

    task = manager.init_from_config_background(SimpleNamespace(clients={}))
    await started.wait()

    await manager.close_all()

    assert task.cancelled()
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_mcp_manager_marks_clients_with_their_connector_key(monkeypatch):
    manager = MCPClientManager()

    class Client:
        async def connect(self, timeout=60.0):
            _ = timeout

        async def close(self):
            return None

    client = Client()
    monkeypatch.setattr(manager, "_build_client", lambda _config: client)
    config = SimpleNamespace(
        clients={
            GUANLAN_CLIENT_KEY: SimpleNamespace(
                enabled=True,
                oauth=SimpleNamespace(enabled=False),
            ),
        },
    )

    await manager.init_from_config(config)

    assert await manager.get_client(GUANLAN_CLIENT_KEY) is client
    assert client._lantai_client_key == GUANLAN_CLIENT_KEY


@pytest.mark.asyncio
async def test_mcp_manager_does_not_swallow_startup_cancellation(monkeypatch):
    manager = MCPClientManager()

    async def cancelled_add(_key, _config):
        raise asyncio.CancelledError

    monkeypatch.setattr(manager, "_add_client", cancelled_add)
    config = SimpleNamespace(
        clients={
            GUANLAN_CLIENT_KEY: SimpleNamespace(
                enabled=True,
                oauth=SimpleNamespace(enabled=False),
            ),
        },
    )

    with pytest.raises(asyncio.CancelledError):
        await manager.init_from_config(config)


@pytest.mark.asyncio
async def test_mcp_manager_keeps_failed_native_capability_state(monkeypatch):
    manager = MCPClientManager()

    class BrokenClient:
        is_connected = False

        async def connect(self, timeout=60.0):
            _ = timeout
            raise RuntimeError("incompatible MCP runtime")

        async def close(self):
            return None

    monkeypatch.setattr(
        manager,
        "_build_client",
        lambda _config: BrokenClient(),
    )
    config = SimpleNamespace(
        clients={
            GUANLAN_CLIENT_KEY: SimpleNamespace(
                enabled=True,
                oauth=SimpleNamespace(enabled=False),
            ),
        },
    )

    await manager.init_from_config(config)

    state = await manager.get_runtime_state(GUANLAN_CLIENT_KEY)
    assert state is not None
    assert state.status == "error"
    assert state.attempt_count == 1
    assert state.error_code == "RuntimeError"
    assert "incompatible MCP runtime" in state.diagnostic_message


@pytest.mark.asyncio
async def test_mcp_manager_runtime_state_follows_a_published_client_disconnect(
    monkeypatch,
):
    manager = MCPClientManager()

    class Client:
        is_connected = False
        reconnect_attempt = 0
        last_connection_error = ""

        async def connect(self, timeout=60.0):
            _ = timeout
            self.is_connected = True

        async def close(self):
            return None

    client = Client()
    monkeypatch.setattr(manager, "_build_client", lambda _config: client)
    config = SimpleNamespace(
        clients={
            GUANLAN_CLIENT_KEY: SimpleNamespace(
                enabled=True,
                oauth=SimpleNamespace(enabled=False),
            ),
        },
    )

    await manager.init_from_config(config)
    assert (
        await manager.get_runtime_state(GUANLAN_CLIENT_KEY)
    ).status == "ready"

    client.is_connected = False
    client.reconnect_attempt = 2
    client.last_connection_error = "server exited"

    state = await manager.get_runtime_state(GUANLAN_CLIENT_KEY)
    assert state is not None
    assert state.status == "degraded"
    assert state.attempt_count == 2
    assert state.error_code == "ConnectionError"


@pytest.mark.asyncio
async def test_create_mcp_service_attaches_runner_before_background_init():
    from lantai.app.mcp.chrome_devtools import CHROME_DEVTOOLS_CLIENT_KEY
    from lantai.app.mcp.guanlan import GUANLAN_CLIENT_KEY
    from lantai.app.office_documents import OFFICECLI_CLIENT_KEY
    from lantai.app.workspace.service_factories import create_mcp_service
    from lantai.config.config import MCPConfig

    class Runner:
        def __init__(self):
            self.mcp_manager = None

        def set_mcp_manager(self, mcp_manager):
            self.mcp_manager = mcp_manager

    class BackgroundMCP:
        def __init__(self):
            self.config = None
            self.task = None

        def init_from_config_background(self, config):
            self.config = config
            self.task = asyncio.create_task(asyncio.Event().wait())
            return self.task

    runner = Runner()
    ws = SimpleNamespace(
        agent_id="default",
        _config=SimpleNamespace(mcp=MCPConfig(clients={})),
        _service_manager=SimpleNamespace(services={"runner": runner}),
    )
    mcp = BackgroundMCP()

    await asyncio.wait_for(create_mcp_service(ws, mcp), timeout=0.2)

    assert runner.mcp_manager is mcp
    assert mcp.config is not None
    assert GUANLAN_CLIENT_KEY in mcp.config.clients
    assert OFFICECLI_CLIENT_KEY in mcp.config.clients
    assert CHROME_DEVTOOLS_CLIENT_KEY in mcp.config.clients
    assert mcp.config.clients[CHROME_DEVTOOLS_CLIENT_KEY].enabled is False
    assert mcp.config.clients[OFFICECLI_CLIENT_KEY].enabled is True

    mcp.task.cancel()
    with suppress(asyncio.CancelledError):
        await mcp.task
