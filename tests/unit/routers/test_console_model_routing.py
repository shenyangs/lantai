# -*- coding: utf-8 -*-
"""Tests for console model-routing explanation snapshots."""

from __future__ import annotations

import pytest

from lantai.app.routers import console
from lantai.config.config import AgentProfileRef, Config
from lantai.config.config import ModelSlotConfig


class _ActiveModel:
    provider_id = "openai"
    model = "gpt-4.1"


class _ProviderManager:
    def get_active_model(self):
        return _ActiveModel()


def test_short_continuation_inherits_previous_routing_explanation(monkeypatch):
    monkeypatch.setattr(
        console,
        "load_agent_config",
        lambda _agent_id: (_ for _ in ()).throw(RuntimeError("no agent config")),
    )
    monkeypatch.setattr(
        console.ProviderManager,
        "get_instance",
        lambda: _ProviderManager(),
    )

    decision = console._build_model_routing_decision(
        agent_id="default",
        pending_user_text="可以",
        previous_decision={
            "run_id": "run-prev",
            "route": "cloud",
            "mode": "recommended",
            "complexity": "complex",
            "estimated_context_tokens": 2400,
            "estimated_cost_points": 12,
            "reasons": ["上一轮需要跨文件修改"],
        },
    )

    assert decision["previous_route_inherited"] is True
    assert decision["inherited_from_run_id"] == "run-prev"
    assert decision["route"] == "cloud"
    assert decision["mode"] == "recommended"
    assert decision["complexity"] == "standard"
    assert decision["estimated_context_tokens"] == 1
    assert decision["estimated_cost_points"] is None
    assert decision["selected_provider"] == "openai"
    assert decision["selected_model"] == "gpt-4.1"
    assert decision["provider"] == "openai"
    assert decision["model"] == "gpt-4.1"
    assert decision["reason"] == "短续写消息，沿用上一轮模型路由解释"
    assert "上一轮需要跨文件修改" in decision["reasons"]


@pytest.mark.parametrize("text", ["继续", "好", "开始", "执行", "可以", "ok"])
def test_short_continuation_detector_accepts_expected_words(text):
    assert console.SHORT_CONTINUATION_RE.match(text)


def test_model_routing_skips_external_desktop_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = Config()
    config.agents.profiles = {
        "default": AgentProfileRef(
            id="default",
            workspace_dir=str(tmp_path / "external"),
        ),
    }
    monkeypatch.setattr(console, "load_config", lambda: config)
    monkeypatch.setattr(
        console,
        "load_agent_config",
        lambda _agent_id: pytest.fail("external config was opened"),
    )
    monkeypatch.setattr(
        console.ProviderManager,
        "get_instance",
        lambda: _ProviderManager(),
    )

    decision = console._build_model_routing_decision(
        agent_id="default",
        pending_user_text="你好",
    )

    assert decision["selected_provider"] == "openai"
    assert decision["selected_model"] == "gpt-4.1"
    assert "暂用全局当前模型配置" in " ".join(decision["reasons"])


def test_model_routing_replaces_unselectable_agent_model(monkeypatch):
    configured = ModelSlotConfig(provider_id="tokendance", model="expired-model")
    global_slot = ModelSlotConfig(provider_id="deepseek", model="deepseek-chat")

    class ProviderManager:
        def is_chat_model_slot_selectable(self, slot):
            return slot != configured

        def get_active_model(self):
            return global_slot

    monkeypatch.setattr(console, "load_config", Config)
    monkeypatch.setattr(
        console,
        "load_agent_config",
        lambda _agent_id: type(
            "AgentConfig",
            (),
            {"active_model": configured, "llm_routing": None},
        )(),
    )
    monkeypatch.setattr(
        console.ProviderManager,
        "get_instance",
        lambda: ProviderManager(),
    )

    decision = console._build_model_routing_decision(
        agent_id="default",
        pending_user_text="做成PPT",
    )

    assert decision["selected_provider"] == "deepseek"
    assert decision["selected_model"] == "deepseek-chat"
    assert "智能体专用模型当前不可用，已改用可用模型" in decision["reasons"]
