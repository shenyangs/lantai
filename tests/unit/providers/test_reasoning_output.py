from lantai.providers.reasoning_output import (
    resolve_reasoning_output_profile,
)


def test_inline_reasoning_output_profile_covers_known_tag_model_families():
    for model_id in (
        "minimax-m2.7",
        "minimax-m3",
        "qwen3-max",
        "qwq-32b",
        "deepseek-r1",
        "deepseek-reasoner",
        "kimi-k2-thinking",
    ):
        profile = resolve_reasoning_output_profile(
            "custom-provider",
            model_id,
        )
        assert profile.inline_tag_pairs, model_id
        if model_id.startswith("minimax-"):
            assert profile.request_reasoning_split is True


def test_inline_reasoning_output_profile_does_not_scan_plain_or_native_models():
    for model_id in (
        "plain-chat-model",
        "gpt-5.2",
        "claude-sonnet-4",
        "gemini-3-pro",
    ):
        profile = resolve_reasoning_output_profile(
            "custom-provider",
            model_id,
        )
        assert profile.inline_tag_pairs == (), model_id
