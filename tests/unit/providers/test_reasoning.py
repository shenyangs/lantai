from lantai.providers.reasoning import (
    apply_current_reasoning_options,
    build_reasoning_call_kwargs,
    get_reasoning_capability,
    reset_reasoning_options,
    set_reasoning_options,
    ReasoningRequestOptions,
)


def test_openai_reasoning_effort_enabled_only():
    assert get_reasoning_capability("openai", "gpt-5.2").supported is True
    assert build_reasoning_call_kwargs(
        "openai",
        "gpt-5.2",
        enabled=True,
    ) == {"reasoning_effort": "high"}
    assert (
        build_reasoning_call_kwargs("openai", "gpt-5.2", enabled=False) == {}
    )


def test_qwen_uses_dashscope_extra_body_toggle():
    assert build_reasoning_call_kwargs(
        "dashscope",
        "qwen3-max",
        enabled=True,
    ) == {"extra_body": {"enable_thinking": True}}
    assert build_reasoning_call_kwargs(
        "dashscope",
        "qwen3-max",
        enabled=False,
    ) == {"extra_body": {"enable_thinking": False}}


def test_kimi_and_glm_use_thinking_type():
    assert (
        get_reasoning_capability("kimi-cn", "kimi-k2.5").default_enabled
        is True
    )
    assert build_reasoning_call_kwargs(
        "kimi-cn",
        "kimi-k2.5",
        enabled=True,
    ) == {"extra_body": {"thinking": {"type": "enabled"}}}
    assert build_reasoning_call_kwargs(
        "zhipu-cn",
        "glm-5",
        enabled=False,
    ) == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_minimax_m2_is_fixed_reasoning_independently_of_provider_name():
    capability = get_reasoning_capability(
        "tokendance",
        "minimax-m2.7",
    )

    assert capability.supported is True
    assert capability.default_enabled is True
    assert capability.fixed is True
    assert capability.mode == "fixed_reasoning"
    assert build_reasoning_call_kwargs(
        "tokendance",
        "minimax-m2.7",
        enabled=True,
    ) == {}


def test_minimax_m3_uses_switchable_adaptive_thinking():
    capability = get_reasoning_capability("minimax-cn", "MiniMax-M3")

    assert capability.supported is True
    assert capability.default_enabled is True
    assert capability.fixed is False
    assert capability.mode == "minimax_adaptive_thinking"
    assert build_reasoning_call_kwargs(
        "minimax-cn",
        "MiniMax-M3",
        enabled=True,
        base_url="https://api.minimaxi.com/anthropic",
        chat_model="AnthropicChatModel",
    ) == {"thinking": {"type": "adaptive"}}
    assert build_reasoning_call_kwargs(
        "custom",
        "MiniMax-M3",
        enabled=False,
        base_url="https://api.minimaxi.com/v1",
        chat_model="OpenAIChatModel",
    ) == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_kimi_coding_keeps_reasoning_across_tool_turns():
    assert build_reasoning_call_kwargs(
        "kimi-codingplan",
        "kimi-for-coding",
        enabled=True,
        base_url="https://api.kimi.com/coding/v1/",
    ) == {
        "extra_body": {
            "thinking": {
                "type": "enabled",
                "keep": "all",
            },
        },
    }


def test_context_options_merge_with_existing_kwargs():
    token = set_reasoning_options(ReasoningRequestOptions(enabled=True))
    try:
        merged = apply_current_reasoning_options(
            {"extra_body": {"foo": "bar"}},
            provider_id="openrouter",
            model_id="openai/gpt-5",
        )
    finally:
        reset_reasoning_options(token)

    assert merged == {
        "extra_body": {
            "foo": "bar",
            "reasoning": {"effort": "high"},
        },
    }
