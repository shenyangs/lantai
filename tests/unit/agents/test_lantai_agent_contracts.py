# -*- coding: utf-8 -*-
"""Behavior locks for LegacyLantaiAgent registration boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import lantai.agents.legacy_lantai_agent as legacy_lantai_agent_module
import lantai.agents.tool_registry as tool_registry_module
from lantai.agents.legacy_lantai_agent import LegacyLantaiAgent


class _ToolConfig:
    def __init__(self, enabled: bool = True, async_execution: bool = False) -> None:
        self.enabled = enabled
        self.async_execution = async_execution


class _FakeToolkit:
    def __init__(self) -> None:
        self.registered_tools: list[dict[str, object]] = []
        self.registered_skills: list[str] = []
        self.registered_mcp_clients: list[dict[str, object]] = []
        self.groups: dict[str, SimpleNamespace] = {}

    def create_tool_group(
        self,
        group_name: str,
        description: str,
        *,
        transient: bool = False,
    ) -> None:
        self.groups[group_name] = SimpleNamespace(
            description=description,
            transient=transient,
        )

    def equip_lantai_tools(self) -> None:
        pass

    def view_task(self) -> None:
        pass

    def wait_task(self) -> None:
        pass

    def cancel_task(self) -> None:
        pass

    def register_tool_function(
        self,
        tool_func,
        *,
        group_name: str = "basic",
        func_name: str | None = None,
        func_description: str | None = None,
        namesake_strategy: str = "skip",
        async_execution: bool = False,
    ) -> None:
        del func_description
        self.registered_tools.append(
            {
                "name": func_name or getattr(tool_func, "__name__", repr(tool_func)),
                "group_name": group_name,
                "namesake_strategy": namesake_strategy,
                "async_execution": async_execution,
            },
        )

    def register_agent_skill(self, skill_dir: str) -> None:
        self.registered_skills.append(skill_dir)

    async def register_mcp_client(
        self,
        client,
        *,
        group_name: str = "basic",
        namesake_strategy: str,
        execution_timeout: float,
    ) -> None:
        if getattr(client, "fail_register", False):
            raise RuntimeError("client unavailable")
        self.registered_mcp_clients.append(
            {
                "name": client.name,
                "group_name": group_name,
                "namesake_strategy": namesake_strategy,
                "execution_timeout": execution_timeout,
            },
        )


def _new_agent_stub(*, builtin_tools: dict[str, _ToolConfig] | None = None):
    agent = object.__new__(LegacyLantaiAgent)
    agent._agent_config = SimpleNamespace(
        tools=SimpleNamespace(builtin_tools=builtin_tools or {}),
    )
    agent._workspace_dir = None
    agent._request_context = {}
    agent._mcp_clients = []
    return agent


def test_create_toolkit_registers_hardcoded_tools_and_async_task_helpers(
    monkeypatch,
) -> None:
    monkeypatch.setattr(legacy_lantai_agent_module, "LegacyToolkit", _FakeToolkit)
    monkeypatch.setattr(
        tool_registry_module,
        "_detect_distribution",
        lambda distribution=None: "developer_id",
    )
    agent = _new_agent_stub(
        builtin_tools={
            "execute_shell_command": _ToolConfig(
                enabled=True,
                async_execution=True,
            ),
            "read_file": _ToolConfig(enabled=False),
            # Old configs may still contain the retired tool name.
            "generate_image_with_provider": _ToolConfig(enabled=True),
        },
    )

    toolkit = agent._create_toolkit(namesake_strategy="rename")
    registered = {
        str(item["name"]): item
        for item in toolkit.registered_tools
    }

    assert registered["execute_shell_command"]["async_execution"] is True
    assert registered["execute_shell_command"]["namesake_strategy"] == "rename"
    assert registered["execute_shell_command"]["group_name"] == "advanced"
    assert registered["equip_lantai_tools"]["group_name"] == "basic"
    assert "read_file" not in registered
    assert "write_file" in registered
    assert "prepare_scheduled_task" in registered
    assert "generate_image_with_provider" not in registered
    assert "append_file" not in registered
    assert {"view_task", "wait_task", "cancel_task"} <= set(registered)


def test_progressive_tool_group_keeps_browser_schema_out_of_ordinary_chat(
    monkeypatch,
) -> None:
    from lantai.runtime.adapters.legacy_toolkit_components import LegacyToolkit

    monkeypatch.setattr(
        tool_registry_module,
        "_detect_distribution",
        lambda distribution=None: "developer_id",
    )
    agent = _new_agent_stub(
        builtin_tools={
            "browser_use": _ToolConfig(enabled=True),
            "read_file": _ToolConfig(enabled=True),
        },
    )
    monkeypatch.setattr(legacy_lantai_agent_module, "LegacyToolkit", LegacyToolkit)

    toolkit = agent._create_toolkit()
    ordinary_names = {
        schema["function"]["name"] for schema in toolkit.get_json_schemas()
    }

    assert "equip_lantai_tools" in ordinary_names
    assert "browser_use" not in ordinary_names
    assert "read_file" not in ordinary_names
    assert "browser_use" in toolkit.tools
    assert toolkit.tools["browser_use"].group == "browser"


def test_register_skills_uses_effective_workspace_channel(
    tmp_path,
    monkeypatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    skills_dir = workspace_dir / "skills"
    alpha_dir = skills_dir / "alpha"
    alpha_dir.mkdir(parents=True)
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        tool_registry_module,
        "ensure_skills_initialized",
        lambda path: calls.append(("ensure", path)),
    )
    monkeypatch.setattr(
        tool_registry_module,
        "resolve_effective_skills",
        lambda path, channel: calls.append(("resolve", (path, channel)))
        or ["alpha", "missing"],
    )
    monkeypatch.setattr(
        tool_registry_module,
        "get_workspace_skills_dir",
        lambda path: skills_dir,
    )

    agent = _new_agent_stub()
    agent._workspace_dir = workspace_dir
    agent._request_context = {"channel": "wechat"}
    toolkit = _FakeToolkit()

    agent._register_skills(toolkit)

    assert calls == [
        ("ensure", workspace_dir),
        ("resolve", (workspace_dir, "wechat")),
    ]
    assert toolkit.registered_skills == [str(alpha_dir)]


@pytest.mark.asyncio
async def test_register_mcp_clients_skips_failed_client_and_keeps_timeout(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tool_registry_module,
        "_detect_distribution",
        lambda distribution=None: "developer_id",
    )
    agent = _new_agent_stub()
    agent.toolkit = _FakeToolkit()
    agent._mcp_clients = [
        SimpleNamespace(
            name="broken",
            read_timeout_seconds=3.0,
            fail_register=True,
        ),
        SimpleNamespace(
            name="healthy",
            read_timeout_seconds=7.5,
            fail_register=False,
        ),
    ]

    await agent.register_mcp_clients(namesake_strategy="override")

    assert agent.toolkit.registered_mcp_clients == [
        {
            "name": "healthy",
            "group_name": "connected_services",
            "namesake_strategy": "override",
            "execution_timeout": 7.5,
        },
    ]

    monkeypatch.setattr(
        tool_registry_module,
        "_detect_distribution",
        lambda distribution=None: "app_store",
    )
    mas_agent = _new_agent_stub()
    mas_agent.toolkit = _FakeToolkit()
    mas_agent._mcp_clients = [
        SimpleNamespace(
            name="healthy",
            read_timeout_seconds=7.5,
            fail_register=False,
        ),
    ]

    await mas_agent.register_mcp_clients(namesake_strategy="override")

    assert mas_agent.toolkit.registered_mcp_clients == []


@pytest.mark.asyncio
async def test_official_guanlan_is_registered_as_a_research_capability(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tool_registry_module,
        "_detect_distribution",
        lambda distribution=None: "developer_id",
    )
    agent = _new_agent_stub()
    agent.toolkit = _FakeToolkit()
    agent._mcp_clients = [
        SimpleNamespace(
            name="观澜 / Guanlan",
            _lantai_client_key="guanlan",
            read_timeout_seconds=90.0,
            fail_register=False,
        ),
    ]

    await agent.register_mcp_clients(namesake_strategy="override")

    assert agent.toolkit.registered_mcp_clients == [
        {
            "name": "观澜 / Guanlan",
            "group_name": "research",
            "namesake_strategy": "override",
            "execution_timeout": 90.0,
        },
    ]


def test_reuse_shared_client_reference_updates_original_dict() -> None:
    original = SimpleNamespace(name="old", value=1)
    rebuilt = SimpleNamespace(name="new", value=2, extra=True)

    result = LegacyLantaiAgent._reuse_shared_client_reference(original, rebuilt)

    assert result is original
    assert original.name == "new"
    assert original.value == 2
    assert original.extra is True


def test_system_prompt_always_keeps_web_baseline_and_conditionally_adds_guanlan(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        legacy_lantai_agent_module,
        "build_system_prompt_from_working_dir",
        lambda **_kwargs: "base prompt",
    )
    monkeypatch.setattr(legacy_lantai_agent_module, "ququ_enabled", lambda: False)
    monkeypatch.setattr(
        legacy_lantai_agent_module,
        "build_multimodal_hint",
        lambda: "",
    )
    monkeypatch.setattr(
        legacy_lantai_agent_module,
        "build_coding_mode_prompt",
        lambda **_kwargs: "",
    )

    official_agent = _new_agent_stub()
    official_agent._agent_config.heartbeat = None
    official_agent._language = "zh"
    official_agent.memory_manager = None
    official_agent._runtime_workspace_dir = None
    official_agent._env_context = None
    official_agent._mcp_clients = [
        SimpleNamespace(_lantai_client_key="guanlan"),
    ]
    namesake_agent = _new_agent_stub()
    namesake_agent._agent_config.heartbeat = None
    namesake_agent._language = "zh"
    namesake_agent.memory_manager = None
    namesake_agent._runtime_workspace_dir = None
    namesake_agent._env_context = None
    namesake_agent._mcp_clients = [SimpleNamespace(name="guanlan")]

    official_prompt = official_agent._build_sys_prompt()
    namesake_prompt = namesake_agent._build_sys_prompt()

    assert "澜台基础 Web 能力" in official_prompt
    assert "澜台基础 Web 能力" in namesake_prompt
    assert "不依赖观澜" in namesake_prompt
    assert "先降级到 `web_search`" in official_prompt
    assert "观澜原生外部信息规则" in official_prompt
    assert "观澜原生外部信息规则" not in namesake_prompt


def test_internal_worker_prompt_does_not_compile_delivery_persona(
    monkeypatch,
) -> None:
    def fail_persona_compile(**_kwargs):
        raise AssertionError("internal workers must not compile delivery persona")

    monkeypatch.setattr(
        legacy_lantai_agent_module,
        "build_system_prompt_from_working_dir",
        fail_persona_compile,
    )
    agent = _new_agent_stub()
    agent._internal_worker_execution = True

    prompt = agent._build_sys_prompt()

    assert "internal execution worker" in prompt
    assert "final answer" in prompt
