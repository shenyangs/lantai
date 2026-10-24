# -*- coding: utf-8 -*-
"""Tests for explainable LLM routing policy behavior."""

from __future__ import annotations

from lantai.agents.routing_chat_model import RoutingPolicy
from lantai.config.config import AgentsLLMRoutingConfig


def test_short_continuation_requires_explicit_previous_decision():
    policy = RoutingPolicy(AgentsLLMRoutingConfig(enabled=True, mode="cloud_first"))

    first = policy.decide(text="请先分析这个跨文件修改")

    policy.cfg.mode = "local_first"
    second = policy.decide(text="继续")

    assert second.route == "local"
    assert second.mode == "local_first"
    assert second.previous_route_inherited is False

    inherited = policy.decide(text="继续", previous_decision=first)

    assert inherited.route == "cloud"
    assert inherited.mode == "cloud_first"
    assert inherited.previous_route_inherited is True
    assert inherited.reasons == (
        "short_continuation:inherit_previous_route",
        "mode:cloud_first",
    )
    assert not hasattr(policy, "_last_decision")


def test_short_continuation_inherits_previous_policy_explanation_payload():
    policy = RoutingPolicy(AgentsLLMRoutingConfig(enabled=True, mode="local_first"))
    previous = {
        "route": "cloud",
        "mode": "cloud_first",
        "reasons": ["mode:cloud_first"],
        "estimated_context_tokens": 1200,
        "estimated_cost_points": 4,
        "fallback_reason": "cloud fallback",
    }

    second = policy.decide(text="继续", previous_decision=previous)

    assert second.route == "cloud"
    assert second.mode == "cloud_first"
    assert second.previous_route_inherited is True
    assert second.estimated_context_tokens == 1200
    assert second.estimated_cost_points == 4
    assert second.fallback_reason == "cloud fallback"
    assert second.reasons == (
        "short_continuation:inherit_previous_route",
        "mode:cloud_first",
    )
