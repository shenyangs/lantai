# -*- coding: utf-8 -*-
"""Coding Mode prompt tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lantai.agents.coding_mode import (
    build_coding_mode_prompt,
    is_coding_mode_enabled,
)
from lantai.agents import legacy_lantai_agent
from lantai.agents import tool_registry
from lantai.agents.legacy_lantai_agent import LegacyLantaiAgent
from lantai.agents.tools import coding as coding_tools


def test_coding_mode_flag_parses_explicit_truthy_values() -> None:
    assert is_coding_mode_enabled({"coding_mode": True})
    assert is_coding_mode_enabled({"coding_mode": "true"})
    assert is_coding_mode_enabled({"coding_mode": "coding"})
    assert not is_coding_mode_enabled({"coding_mode": "false"})
    assert not is_coding_mode_enabled({})


def test_coding_mode_prompt_is_scoped_and_guarded(tmp_path: Path) -> None:
    prompt = build_coding_mode_prompt(
        request_context={"coding_mode": "true"},
        runtime_workspace_dir=tmp_path,
    )

    assert "Coding Mode" in prompt
    assert str(tmp_path) in prompt
    assert "git reset" in prompt
    assert "git clean" in prompt
    assert "dependency-gated" in prompt
    assert "App Store" not in prompt
    assert "TestFlight" not in prompt


def test_agent_sys_prompt_injects_coding_mode_when_requested(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        legacy_lantai_agent,
        "build_system_prompt_from_working_dir",
        lambda **_: "BASE PROMPT",
    )
    monkeypatch.setattr(legacy_lantai_agent, "build_multimodal_hint", lambda: "")
    monkeypatch.setattr(legacy_lantai_agent, "ququ_enabled", lambda: False)

    agent = object.__new__(LegacyLantaiAgent)
    agent._request_context = {"agent_id": "default", "coding_mode": "true"}
    agent._agent_config = SimpleNamespace(heartbeat=None, enableQuqu=False)
    agent._language = "zh"
    agent._workspace_dir = tmp_path
    agent._runtime_workspace_dir = tmp_path / "runtime-topic"
    agent._env_context = None
    agent.memory_manager = None

    prompt = LegacyLantaiAgent._build_sys_prompt(agent)

    assert prompt.startswith("BASE PROMPT")
    assert "Coding Mode" in prompt
    assert "runtime-topic" in prompt
    assert "【蛐蛐 · 内心独白】" not in prompt


def test_agent_sys_prompt_uses_the_global_ququ_preference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        legacy_lantai_agent,
        "build_system_prompt_from_working_dir",
        lambda **_: "BASE PROMPT",
    )
    monkeypatch.setattr(legacy_lantai_agent, "build_multimodal_hint", lambda: "")
    monkeypatch.setattr(legacy_lantai_agent, "ququ_enabled", lambda: True)
    monkeypatch.setattr(
        legacy_lantai_agent,
        "ququ_turn_selected",
        lambda: True,
        raising=False,
    )
    agent = object.__new__(LegacyLantaiAgent)
    agent._request_context = {"agent_id": "default"}
    agent._agent_config = SimpleNamespace(heartbeat=None, enableQuqu=False)
    agent._language = "zh"
    agent._workspace_dir = tmp_path
    agent._runtime_workspace_dir = tmp_path / "runtime-topic"
    agent._env_context = None
    agent.memory_manager = None

    prompt = LegacyLantaiAgent._build_sys_prompt(agent)

    assert "【蛐蛐 · 内心独白】" in prompt
    assert '<ququ-route baseline_selected="true" hard_required="true" />' in prompt
    assert "【本轮蛐蛐路由：50% 基础硬命中】" in prompt
    assert "本轮必须输出一条蛐蛐" in prompt
    assert "不得以没有新反应为由保持安静" in prompt
    assert "命中只提供一次自然出现的机会" not in prompt
    assert "不是每轮必做步骤" not in prompt
    assert "出现时只写一条" in prompt
    assert "内部推理" in prompt
    assert "可复制、下载、交付" in prompt
    assert "~>[lead]" in prompt
    assert "~>[echo]" in prompt
    assert "拿不准用 `echo`" not in prompt
    assert "明显优于另一位置" in prompt
    assert "同一条回答流" in prompt
    assert "两种格式互斥" in prompt
    assert "不在正文中间插入" in prompt
    assert "作为一个真实的人" not in prompt
    assert "不要用旁白代替回答" in prompt
    assert "不复读正文或上一条旁白" in prompt

    assert agent.ququ_hard_required is True


def test_agent_sys_prompt_allows_discretionary_ququ_for_an_unselected_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        legacy_lantai_agent,
        "build_system_prompt_from_working_dir",
        lambda **_: "BASE PROMPT",
    )
    monkeypatch.setattr(legacy_lantai_agent, "build_multimodal_hint", lambda: "")
    monkeypatch.setattr(legacy_lantai_agent, "ququ_enabled", lambda: True)
    monkeypatch.setattr(legacy_lantai_agent, "ququ_turn_selected", lambda: False)
    agent = object.__new__(LegacyLantaiAgent)
    agent._request_context = {"agent_id": "default"}
    agent._agent_config = SimpleNamespace(heartbeat=None, enableQuqu=False)
    agent._language = "zh"
    agent._workspace_dir = tmp_path
    agent._runtime_workspace_dir = tmp_path / "runtime-topic"
    agent._env_context = None
    agent.memory_manager = None

    prompt = LegacyLantaiAgent._build_sys_prompt(agent)

    assert "【蛐蛐 · 内心独白】" in prompt
    assert "本轮未命中 50% 基础出现槽" in prompt
    assert "默认保持安静" not in prompt
    assert "只有当某个念头因果足够真" in prompt
    assert "沉默会丢掉这一刻的主体性" in prompt


def test_ququ_generation_draws_baseline_and_keeps_emotional_bypass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        legacy_lantai_agent,
        "build_system_prompt_from_working_dir",
        lambda **_: "BASE PROMPT",
    )
    monkeypatch.setattr(legacy_lantai_agent, "build_multimodal_hint", lambda: "")
    monkeypatch.setattr(legacy_lantai_agent, "ququ_enabled", lambda: True)

    draws = 0

    def miss_baseline_bucket() -> bool:
        nonlocal draws
        draws += 1
        return False

    monkeypatch.setattr(
        legacy_lantai_agent,
        "ququ_turn_selected",
        miss_baseline_bucket,
    )
    agent = object.__new__(LegacyLantaiAgent)
    agent._request_context = {"agent_id": "default"}
    agent._agent_config = SimpleNamespace(heartbeat=None, enableQuqu=False)
    agent._language = "zh"
    agent._workspace_dir = tmp_path
    agent._runtime_workspace_dir = tmp_path / "runtime-topic"
    agent._env_context = None
    agent.memory_manager = None

    prompt = LegacyLantaiAgent._build_sys_prompt(agent)

    assert draws == 1
    assert "本轮未命中 50% 基础出现槽" in prompt
    assert "情感金句旁路" in prompt
    assert "默认保持安静" not in prompt


def test_ququ_route_is_drawn_once_when_the_same_agent_rebuilds_its_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        legacy_lantai_agent,
        "build_system_prompt_from_working_dir",
        lambda **_: "BASE PROMPT",
    )
    monkeypatch.setattr(legacy_lantai_agent, "build_multimodal_hint", lambda: "")
    monkeypatch.setattr(legacy_lantai_agent, "ququ_enabled", lambda: True)
    draws = 0

    def alternating_draw() -> bool:
        nonlocal draws
        draws += 1
        return draws % 2 == 1

    monkeypatch.setattr(
        legacy_lantai_agent,
        "ququ_turn_selected",
        alternating_draw,
    )
    agent = object.__new__(LegacyLantaiAgent)
    agent._request_context = {"agent_id": "default"}
    agent._agent_config = SimpleNamespace(heartbeat=None, enableQuqu=False)
    agent._language = "zh"
    agent._workspace_dir = tmp_path
    agent._runtime_workspace_dir = tmp_path / "runtime-topic"
    agent._env_context = None
    agent.memory_manager = None

    first = LegacyLantaiAgent._build_sys_prompt(agent)
    rebuilt = LegacyLantaiAgent._build_sys_prompt(agent)

    assert draws == 1
    assert "本轮蛐蛐路由：50% 基础硬命中" in first
    assert "本轮蛐蛐路由：50% 基础硬命中" in rebuilt


def test_coding_tool_capabilities_are_dependency_and_distribution_gated(
    monkeypatch,
) -> None:
    def fake_which(name: str) -> str | None:
        if name in {"ast-grep", "pyright-langserver"}:
            return f"/usr/local/bin/{name}"
        return None

    monkeypatch.setattr(coding_tools.shutil, "which", fake_which)

    developer_id = coding_tools.detect_coding_tool_capabilities(
        distribution="developer_id",
    )
    if coding_tools._normalise_distribution("developer_id") == "app_store":
        assert not developer_id["ast"].available
        assert not developer_id["lsp"].available
    else:
        assert developer_id["ast"].available
        assert developer_id["lsp"].available

    app_store = coding_tools.detect_coding_tool_capabilities(
        distribution="app_store",
    )
    assert not app_store["ast"].available
    assert not app_store["lsp"].available
    assert "App Store" in (app_store["ast"].reason or "")


def test_create_toolkit_registers_optional_tools_only_in_coding_mode(
    monkeypatch,
) -> None:
    class FakeToolkit:
        def __init__(self) -> None:
            self.registered: list[str] = []

        def create_tool_group(self, *_args, **_kwargs) -> None:
            pass

        def equip_lantai_tools(self) -> None:
            pass

        def register_tool_function(self, func, **kwargs) -> None:
            self.registered.append(kwargs.get("func_name") or func.__name__)

    async def coding_ast_search() -> None:
        return None

    monkeypatch.setattr(legacy_lantai_agent, "LegacyToolkit", FakeToolkit)
    monkeypatch.setattr(
        tool_registry,
        "get_coding_mode_tool_functions",
        lambda _ctx: {"coding_ast_search": coding_ast_search},
    )
    monkeypatch.setattr(
        tool_registry,
        "_detect_distribution",
        lambda distribution=None: "developer_id",
    )

    agent = object.__new__(LegacyLantaiAgent)
    agent._agent_config = SimpleNamespace(
        tools=SimpleNamespace(builtin_tools={}),
    )
    agent._request_context = {}

    normal_toolkit = LegacyLantaiAgent._create_toolkit(agent)
    assert "coding_ast_search" not in normal_toolkit.registered

    agent._request_context = {"coding_mode": "true"}
    coding_toolkit = LegacyLantaiAgent._create_toolkit(agent)
    assert "coding_ast_search" in coding_toolkit.registered

    monkeypatch.setattr(
        tool_registry,
        "_detect_distribution",
        lambda distribution=None: "app_store",
    )
    mas_toolkit = LegacyLantaiAgent._create_toolkit(agent)
    assert "coding_ast_search" not in mas_toolkit.registered
