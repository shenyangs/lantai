# -*- coding: utf-8 -*-
"""Cross-provider behavior locks for reasoning + tool continuation state."""

import pytest

from lantai.providers.reasoning_continuity import (
    plan_reasoning_turn,
    resolve_reasoning_continuation_policy,
)
from lantai.providers.reasoning import get_reasoning_capability


def test_reasoning_continuation_protocol_matrix_is_explicit() -> None:
    cases = [
        (
            ("deepseek", "deepseek-v4-flash", "https://api.deepseek.com/v1", ""),
            "openai_reasoning_content",
            True,
        ),
        (
            ("kimi-codingplan", "kimi-for-coding", "https://api.kimi.com/coding/v1", ""),
            "openai_reasoning_content",
            True,
        ),
        (
            ("anthropic", "claude-sonnet-4", "https://api.anthropic.com", ""),
            "anthropic_thinking_blocks",
            True,
        ),
        (
            ("gemini", "gemini-2.5-pro", "https://generativelanguage.googleapis.com", ""),
            "gemini_thought_signature",
            True,
        ),
        (
            ("openai", "gpt-5", "https://api.openai.com/v1", ""),
            "none",
            False,
        ),
        (
            (
                "openai",
                "gpt-5",
                "https://api.openai.com/v1",
                "OpenAIResponsesModel",
            ),
            "openai_reasoning_items",
            True,
        ),
        (
            ("openai", "gpt-4.1-mini", "https://api.openai.com/v1", ""),
            "none",
            False,
        ),
    ]

    for arguments, protocol, requires_exact_replay in cases:
        provider_id, model_id, base_url, chat_model = arguments
        policy = resolve_reasoning_continuation_policy(
            provider_id,
            model_id,
            base_url=base_url,
            chat_model=chat_model,
        )
        assert policy.protocol == protocol
        assert policy.requires_exact_replay_with_tools is requires_exact_replay


def test_fixed_kimi_code_keeps_thinking_for_required_tools() -> None:
    capability = get_reasoning_capability("tokendance", "kimi-k2.7-code")
    policy = resolve_reasoning_continuation_policy(
        "tokendance", "kimi-k2.7-code"
    )

    assert (
        capability.supported
        and capability.fixed
        and capability.default_enabled
    )
    assert policy.required_tool_choice_incompatible
    plan = plan_reasoning_turn(
        capability=capability,
        policy=policy,
        requested_enabled=False,
        tool_choice="required",
        has_tools=True,
        messages=[],
    )
    assert plan.effective_enabled
    assert plan.forced_disabled_reason == ""
    assert plan.effective_tool_choice == "auto"
    assert plan.status_metadata(returned=True)[
        "tool_choice_adjustment_reason"
    ] == ("fixed_reasoning_required_tool_choice")


@pytest.mark.parametrize(
    "model_id", ["deepseek-reasoner", "MiniMax-M2", "kimi-k2-thinking"]
)
def test_other_fixed_reasoning_tool_contracts_stay_unchanged(model_id) -> None:
    plan = plan_reasoning_turn(
        capability=get_reasoning_capability("custom", model_id),
        policy=resolve_reasoning_continuation_policy("custom", model_id),
        requested_enabled=True,
        tool_choice="required",
        has_tools=True,
        messages=[],
    )
    assert plan.effective_enabled
    assert plan.effective_tool_choice == "required"
    assert plan.tool_choice_adjustment_reason == ""


@pytest.mark.parametrize("tool_choice", ["auto", "none", None])
def test_fixed_kimi_explicit_nonrequired_choices_stay_unchanged(
    tool_choice,
) -> None:
    plan = plan_reasoning_turn(
        capability=get_reasoning_capability("tokendance", "kimi-k2.7-code"),
        policy=resolve_reasoning_continuation_policy(
            "tokendance", "kimi-k2.7-code"
        ),
        requested_enabled=True,
        tool_choice=tool_choice,
        has_tools=True,
        messages=[],
    )
    assert plan.effective_tool_choice == tool_choice
    assert plan.tool_choice_adjustment_reason == ""
