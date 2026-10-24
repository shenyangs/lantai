# -*- coding: utf-8 -*-
"""Behaviour locks for model-specific context capacity and planning."""

from types import SimpleNamespace

import pytest

from lantai.agents.context.context_budget import (
    ContextBudgetError,
    ContextBudgetPlanner,
    ContextCandidate,
    ContextTooLongRecovery,
    ModelAwareTokenCounter,
)
from lantai.agents.context.model_context_profile import ModelContextProfile
from lantai.providers.provider import ModelInfo


class _ProviderManager:
    def __init__(self) -> None:
        self.providers = {
            "remote-a": SimpleNamespace(
                meta={"max_output_tokens": 4096, "tokenizer": "gateway-tokenizer"},
                models=[ModelInfo(id="alpha", name="alpha", context_window=512 * 1024)],
                extra_models=[],
            ),
            "remote-b": SimpleNamespace(
                meta={},
                models=[ModelInfo(id="beta", name="beta", context_window=1024 * 1024)],
                extra_models=[],
            ),
            "kimi-codingplan": SimpleNamespace(
                meta={},
                models=[ModelInfo(id="kimi-for-coding", name="Kimi for Coding")],
                extra_models=[],
            ),
        }

    def get_provider(self, provider_id):
        return self.providers.get(provider_id)


def test_effective_window_clamps_256k_agent_to_64k_local_runtime():
    profile = ModelContextProfile.from_mapping(
        model_id="local-qwen",
        provider_id="lantai-local",
        user_window=256 * 1024,
        local_runtime_window=64 * 1024,
        local_runtime_verified=True,
    )

    assert profile.effective_window == 64 * 1024
    assert profile.status == "verified"


def test_unknown_provider_window_uses_unverified_256k_floor():
    profile = ModelContextProfile.from_mapping(user_window=256 * 1024)

    assert profile.effective_window == 256 * 1024
    assert profile.status == "unverified"
    assert profile.policy().used_unknown_fallback is True


def test_provider_model_metadata_recalculates_the_effective_window():
    profile = ModelContextProfile.from_model_info(
        ModelInfo(id="remote", name="remote", context_window=512 * 1024,
                  max_output_tokens=16 * 1024),
        user_window=256 * 1024,
        provider_id="provider-a",
        route_metadata={"context_length": 128 * 1024},
    )

    assert profile.effective_window == 128 * 1024
    assert profile.output_reserve(32 * 1024) == 16 * 1024


def test_provider_selection_recalculates_profile_and_keeps_gateway_fallbacks():
    manager = _ProviderManager()

    first = ModelContextProfile.from_provider_selection(
        provider_manager=manager,
        provider_id="remote-a",
        model_id="alpha",
        user_window=1024 * 1024,
    )
    second = ModelContextProfile.from_provider_selection(
        provider_manager=manager,
        provider_id="remote-b",
        model_id="beta",
        user_window=1024 * 1024,
    )

    assert first.effective_window == 512 * 1024
    assert first.output_reserve(8192) == 4096
    assert first.tokenizer == "gateway-tokenizer"
    assert second.effective_window == 1024 * 1024
    assert second.output_reserve(8192) == 8192


def test_registered_model_capability_drives_the_smart_budget_after_switch():
    profile = ModelContextProfile.from_provider_selection(
        provider_manager=_ProviderManager(),
        provider_id="kimi-codingplan",
        model_id="kimi-for-coding",
        user_window=1024 * 1024,
    )

    decision = profile.policy()
    assert profile.effective_window == 256 * 1024
    assert profile.provider_window.source == "registry:official:kimi-code-models"
    assert decision.output_reserve == 50_000
    assert decision.usable_input_tokens < profile.effective_window
    assert decision.target_input_tokens < decision.usable_input_tokens
    assert decision.used_unknown_fallback is False


def test_model_specific_user_context_override_wins_over_agent_default():
    manager = _ProviderManager()
    manager.providers["remote-a"].models[0].user_context_window = 192 * 1024

    profile = ModelContextProfile.from_provider_selection(
        provider_manager=manager,
        provider_id="remote-a",
        model_id="alpha",
        user_window=256 * 1024,
    )

    assert profile.user_window.tokens == 192 * 1024
    assert profile.user_window.source == "model-user-override"
    assert profile.effective_window == 192 * 1024


def test_planner_never_exceeds_effective_window_and_emits_manifest():
    profile = ModelContextProfile.from_mapping(
        user_window=1000,
        provider_metadata={"context_length": 900, "max_output_tokens": 300},
    )
    planner = ContextBudgetPlanner(ModelAwareTokenCounter(estimate_divisor=4))
    selected, manifest = planner.plan(
        profile=profile,
        requested_output_tokens=500,
        required=[
            ContextCandidate("system", "system rules", required=True, identifier="system"),
            ContextCandidate("current_message", "current user request", required=True, identifier="user"),
        ],
        optional=[
            ContextCandidate("wiki_evidence", "x" * 5000, relevance=1, identifier="large"),
            ContextCandidate("dynamic_memory", "small memory", relevance=.8, identifier="small"),
        ],
        unknown_serialization_tokens=20,
        next_turn_reserve_tokens=30,
    )

    assert manifest.effective_window == 900
    assert manifest.total_upper_bound <= 900
    assert "large" in manifest.discarded
    assert "system" in manifest.selected
    assert manifest.payload_hash
    assert any(block.identifier == "small" for block in selected)


def test_smart_planner_reserves_output_and_stops_before_the_physical_limit():
    profile = ModelContextProfile.from_mapping(
        user_window=1024 * 1024,
        provider_metadata={
            "context_window": 256 * 1024,
            "recommended_output_reserve": 50_000,
        },
    )
    planner = ContextBudgetPlanner(
        ModelAwareTokenCounter(estimate_divisor=1, initial_error_ratio=0),
    )

    _selected, manifest = planner.plan(
        profile=profile,
        requested_output_tokens=0,
        required=[
            ContextCandidate("system", "system", required=True),
            ContextCandidate("current_message", "current", required=True),
        ],
        optional=[
            ContextCandidate(
                "history",
                "x" * 170_000,
                relevance=1,
                identifier="too-close-to-limit",
            ),
        ],
    )

    assert manifest.output_reserve == 50_000
    assert manifest.usable_input_budget < manifest.effective_window
    assert manifest.target_input_budget < manifest.usable_input_budget
    assert manifest.discarded["too-close-to-limit"] == "budget"
    assert manifest.total_upper_bound <= manifest.effective_window


def test_planner_rejects_mandatory_overflow_instead_of_silent_truncation():
    planner = ContextBudgetPlanner(ModelAwareTokenCounter(estimate_divisor=1))
    profile = ModelContextProfile.from_mapping(user_window=100)

    with pytest.raises(ContextBudgetError):
        planner.plan(
            profile=profile,
            requested_output_tokens=30,
            required=[ContextCandidate("system", "x" * 100, required=True)],
            unknown_serialization_tokens=10,
            next_turn_reserve_tokens=10,
        )


def test_counter_uses_real_usage_to_raise_the_conservative_upper_bound():
    counter = ModelAwareTokenCounter(estimate_divisor=10, initial_error_ratio=0)
    before = counter.count("abcdefghij")
    counter.observe_usage(serialized_payload="abcdefghij", actual_prompt_tokens=4)
    after = counter.count("abcdefghij")

    assert before.upper_bound == 1
    assert after.upper_bound >= 4


@pytest.mark.parametrize("window", [256 * 1024, 512 * 1024, 1024 * 1024])
def test_context_window_matrix_preserves_required_material(window):
    planner = ContextBudgetPlanner(ModelAwareTokenCounter())
    profile = ModelContextProfile.from_mapping(
        user_window=window,
        provider_metadata={"context_length": window, "max_output_tokens": 8192},
    )
    selected, manifest = planner.plan(
        profile=profile,
        requested_output_tokens=16 * 1024,
        required=[
            ContextCandidate("system", "system", required=True, identifier="system"),
            ContextCandidate("tool_schema", "tools", required=True, identifier="tools"),
            ContextCandidate("current_message", "current", required=True, identifier="current"),
        ],
        optional=[ContextCandidate("wiki_evidence", "evidence " * 400, relevance=1)],
    )

    assert manifest.total_upper_bound <= window
    assert [item.identifier for item in selected[:3]] == ["system", "tools", "current"]


def test_context_too_long_recovery_requires_evidence_and_only_retries_once():
    profile = ModelContextProfile.from_mapping(user_window=256 * 1024)
    recovery = ContextTooLongRecovery()

    assert recovery.downgrade(profile, "context too long") is None
    downgraded = recovery.downgrade(
        profile,
        "maximum context length is 65536 tokens",
    )

    assert downgraded.effective_window == 65536
    assert downgraded.provider_window.confidence == "verified"
    assert recovery.downgrade(
        downgraded,
        "maximum context length is 32768 tokens",
    ) is None


@pytest.mark.parametrize(
    ("name", "user", "provider", "route", "local", "expected"),
    [
        ("remote-256k", 256 * 1024, 512 * 1024, None, None, 256 * 1024),
        ("remote-route-downgrade", 1024 * 1024, 1024 * 1024, 512 * 1024, None, 512 * 1024),
        ("local-64k-clamp", 256 * 1024, None, None, 64 * 1024, 64 * 1024),
        ("unknown-provider", 512 * 1024, None, None, None, 256 * 1024),
        ("large-remote-1m", 1024 * 1024, 1024 * 1024, None, None, 1024 * 1024),
    ],
)
def test_context_matrix_keeps_multimedia_and_tool_pairs_as_hard_constraints(
    name,
    user,
    provider,
    route,
    local,
    expected,
):
    profile = ModelContextProfile.from_mapping(
        model_id=name,
        user_window=user,
        provider_metadata={"context_length": provider} if provider else {},
        route_metadata={"context_length": route} if route else {},
        local_runtime_window=local,
        local_runtime_verified=local is not None,
    )
    planner = ContextBudgetPlanner(ModelAwareTokenCounter())
    selected, manifest = planner.plan(
        profile=profile,
        requested_output_tokens=32 * 1024,
        required=[
            ContextCandidate("system", "system safety", required=True, identifier="system"),
            ContextCandidate("tool_schema", "large schema " * 800, required=True, identifier="tools"),
            ContextCandidate("attachment", {"type": "image", "url": "local://fixture"}, required=True, identifier="attachment"),
            ContextCandidate("tool_result", "tool_use:call-1\ntool_result:call-1", required=True, identifier="tool-pair"),
            ContextCandidate("current_message", "current request", required=True, identifier="current"),
        ],
        optional=[
            ContextCandidate("history", "history " * 2000, relevance=0.8, identifier="history"),
            ContextCandidate("wiki_evidence", "wiki " * 2000, relevance=0.7, citation="wiki:fixture", identifier="wiki"),
        ],
        unknown_serialization_tokens=512,
        next_turn_reserve_tokens=1024,
    )

    assert manifest.effective_window == expected
    assert manifest.total_upper_bound <= expected
    assert {item.identifier for item in selected}.issuperset(
        {"system", "tools", "attachment", "tool-pair", "current"},
    )
    if name == "unknown-provider":
        assert profile.status == "unverified"
