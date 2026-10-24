"""Official model aliases must resolve to exact, source-aware context limits."""

import pytest

from lantai.providers.model_capability_registry import resolve_model_capability


@pytest.mark.parametrize(
    ("provider_id", "model_id", "context_window"),
    [
        ("openai", "gpt-5", 400_000),
        ("openai", "gpt-4.1", 1_047_576),
        ("openai", "o3", 200_000),
        ("gemini", "gemini-3.1-pro-preview", 1_048_576),
        ("gemini", "gemini-2.5-flash", 1_048_576),
        ("deepseek", "deepseek-v4-pro", 1_000_000),
        ("minimax-cn", "MiniMax-M2.7", 204_800),
        ("minimax-cn", "MiniMax-M3", 1_000_000),
        ("aliyun-codingplan", "qwen3.6-plus", 1_000_000),
        ("aliyun-codingplan", "kimi-k2.5", 262_144),
        ("kimi-codingplan", "kimi-for-coding", 262_144),
        ("kimi-codingplan", "kimi-for-coding-highspeed", 262_144),
        ("kimi-codingplan", "k3-256k", 262_144),
        ("kimi-codingplan", "k3", 1_048_576),
        ("zhipu-cn", "glm-5", 200_000),
    ],
)
def test_mainstream_model_context_capabilities_are_preconfigured(
    provider_id,
    model_id,
    context_window,
):
    capability = resolve_model_capability(provider_id, model_id)

    assert capability is not None
    assert capability.context_window == context_window
    assert capability.source.startswith("official:")
    assert capability.verified_at


def test_unknown_models_do_not_receive_a_fuzzy_capability_guess():
    assert resolve_model_capability("custom", "gpt-5-maybe") is None
