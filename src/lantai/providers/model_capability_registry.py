"""Versioned, source-aware model context capability registry.

Only capabilities backed by an authoritative source belong here.  Unknown
models intentionally stay unknown and are handled by the conservative product
fallback in ``ModelContextProfile``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

CapabilityConfidence = Literal["verified", "reported", "configured"]


@dataclass(frozen=True)
class ModelCapabilityRecord:
    provider_id: str
    model_id: str
    context_window: int
    max_output_tokens: int | None = None
    recommended_output_reserve: int | None = None
    tokenizer: str | None = None
    source: str = "official-documentation"
    confidence: CapabilityConfidence = "reported"
    verified_at: str = ""
    documentation_url: str = ""

    def as_metadata(self) -> dict[str, object]:
        metadata = {
            key: value for key, value in asdict(self).items() if value is not None
        }
        metadata["_context_source"] = f"registry:{self.source}"
        metadata["_context_confidence"] = self.confidence
        return metadata


_VERIFIED_AT = "2026-07-28"


def _record(
    provider_id: str,
    model_id: str,
    context_window: int,
    *,
    max_output_tokens: int | None = None,
    recommended_output_reserve: int | None = None,
    source: str,
    documentation_url: str,
) -> ModelCapabilityRecord:
    return ModelCapabilityRecord(
        provider_id=provider_id,
        model_id=model_id,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        recommended_output_reserve=recommended_output_reserve,
        source=source,
        confidence="verified",
        verified_at=_VERIFIED_AT,
        documentation_url=documentation_url,
    )


def _records_by_model(
    provider_id: str,
    rows: tuple[tuple[str, int, int | None], ...],
    *,
    source: str,
    documentation_url: str,
) -> dict[tuple[str, str], ModelCapabilityRecord]:
    return {
        (provider_id, model_id): _record(
            provider_id,
            model_id,
            context_window,
            max_output_tokens=max_output_tokens,
            source=source,
            documentation_url=documentation_url,
        )
        for model_id, context_window, max_output_tokens in rows
    }


# Exact model aliases are safe across OpenAI-compatible gateways. A
# provider-specific record below always wins when a subscription route exposes
# a smaller limit than the upstream model.
_GLOBAL_REGISTRY: dict[str, ModelCapabilityRecord] = {
    record.model_id: record
    for record in (
        *_records_by_model(
            "*",
            (
                ("gpt-5.2", 400_000, 128_000),
                ("gpt-5", 400_000, 128_000),
                ("gpt-5-chat", 128_000, 16_384),
                ("gpt-5-chat-latest", 128_000, 16_384),
                ("gpt-5-mini", 400_000, 128_000),
                ("gpt-5-nano", 400_000, 128_000),
                ("gpt-4.1", 1_047_576, 32_768),
                ("gpt-4.1-mini", 1_047_576, 32_768),
                ("gpt-4.1-nano", 1_047_576, 32_768),
                ("o3", 200_000, 100_000),
                ("o4-mini", 200_000, 100_000),
                ("gpt-4o", 128_000, 16_384),
                ("gpt-4o-mini", 128_000, 16_384),
            ),
            source="official:openai-model-catalog",
            documentation_url="https://developers.openai.com/api/docs/models",
        ).values(),
        *_records_by_model(
            "*",
            (
                ("gemini-3.1-pro-preview", 1_048_576, 65_536),
                ("gemini-3-flash-preview", 1_048_576, 65_536),
                ("gemini-3.1-flash-lite", 1_048_576, 65_536),
                ("gemini-3.1-flash-lite-preview", 1_048_576, 65_536),
                ("gemini-2.5-pro", 1_048_576, 65_536),
                ("gemini-2.5-flash", 1_048_576, 65_536),
                ("gemini-2.5-flash-lite", 1_048_576, 65_536),
                ("gemini-2.0-flash", 1_048_576, 8_192),
            ),
            source="official:google-gemini-models",
            documentation_url="https://ai.google.dev/gemini-api/docs/models",
        ).values(),
        *_records_by_model(
            "*",
            (
                ("deepseek-v4-pro", 1_000_000, 384_000),
                ("deepseek-v4-flash", 1_000_000, 384_000),
                ("deepseek-chat", 1_000_000, 384_000),
                ("deepseek-reasoner", 1_000_000, 384_000),
            ),
            source="official:deepseek-api",
            documentation_url="https://api-docs.deepseek.com/quick_start/pricing/",
        ).values(),
        *_records_by_model(
            "*",
            (
                ("MiniMax-M3", 1_000_000, None),
                ("MiniMax-M2.7", 204_800, None),
                ("MiniMax-M2.7-highspeed", 204_800, None),
                ("MiniMax-M2.5", 204_800, None),
                ("MiniMax-M2.5-highspeed", 204_800, None),
            ),
            source="official:minimax-text-generation",
            documentation_url="https://platform.minimaxi.com/docs/guides/text-generation",
        ).values(),
        *_records_by_model(
            "*",
            (
                ("glm-5", 200_000, 128_000),
                ("glm-5.1", 200_000, None),
                ("glm-5-turbo", 200_000, None),
                ("glm-5v-turbo", 200_000, None),
                ("glm-5.2", 1_000_000, None),
            ),
            source="official:zhipu-model-docs",
            documentation_url="https://docs.bigmodel.cn/cn/guide/models/text/glm-5",
        ).values(),
        *_records_by_model(
            "*",
            (
                ("qwen3-max", 262_144, 65_536),
                ("qwen3-max-2026-01-23", 262_144, 65_536),
                ("qwen3.5-plus", 1_000_000, None),
                ("qwen3.6-plus", 1_000_000, None),
                ("qwen3-coder-next", 262_144, None),
                ("qwen3-coder-plus", 1_000_000, None),
                ("kimi-k2.5", 262_144, None),
            ),
            source="official:aliyun-model-studio",
            documentation_url="https://help.aliyun.com/en/model-studio/text-generation-model/",
        ).values(),
        *_records_by_model(
            "*",
            (
                ("claude-opus-4-6", 1_000_000, None),
                ("claude-sonnet-4-6", 1_000_000, None),
                ("claude-sonnet-4-5-20250929", 200_000, None),
            ),
            source="official:anthropic-context-windows",
            documentation_url="https://platform.claude.com/docs/en/build-with-claude/context-windows",
        ).values(),
    )
}


_REGISTRY: dict[tuple[str, str], ModelCapabilityRecord] = {}
for _provider_id in ("openai", "azure-openai"):
    _REGISTRY.update(
        {
            (_provider_id, model_id): ModelCapabilityRecord(
                **{
                    **asdict(record),
                    "provider_id": _provider_id,
                },
            )
            for model_id, record in _GLOBAL_REGISTRY.items()
            if model_id.startswith(("gpt-", "o3", "o4-"))
        },
    )
for _provider_id in ("gemini",):
    _REGISTRY.update(
        {
            (_provider_id, model_id): ModelCapabilityRecord(
                **{
                    **asdict(record),
                    "provider_id": _provider_id,
                },
            )
            for model_id, record in _GLOBAL_REGISTRY.items()
            if model_id.startswith("gemini-")
        },
    )
for _provider_id in ("deepseek",):
    _REGISTRY.update(
        {
            (_provider_id, model_id): ModelCapabilityRecord(
                **{
                    **asdict(record),
                    "provider_id": _provider_id,
                },
            )
            for model_id, record in _GLOBAL_REGISTRY.items()
            if model_id.startswith("deepseek-")
        },
    )
for _provider_id in ("minimax", "minimax-cn"):
    _REGISTRY.update(
        {
            (_provider_id, model_id): ModelCapabilityRecord(
                **{
                    **asdict(record),
                    "provider_id": _provider_id,
                },
            )
            for model_id, record in _GLOBAL_REGISTRY.items()
            if model_id.startswith(("MiniMax-M2", "MiniMax-M3"))
        },
    )
for _provider_id in (
    "zhipu-cn",
    "zhipu-cn-codingplan",
    "zhipu-intl",
    "zhipu-intl-codingplan",
):
    _REGISTRY.update(
        {
            (_provider_id, model_id): ModelCapabilityRecord(
                **{
                    **asdict(record),
                    "provider_id": _provider_id,
                },
            )
            for model_id, record in _GLOBAL_REGISTRY.items()
            if model_id.startswith("glm-")
        },
    )

_CODING_PLAN_URL = "https://help.aliyun.com/en/model-studio/coding-plan-faq"
for _provider_id in (
    "aliyun-tokenplan",
    "aliyun-codingplan",
    "aliyun-codingplan-intl",
):
    _REGISTRY.update(
        _records_by_model(
            _provider_id,
            (
                ("qwen3.6-plus", 1_000_000, None),
                ("qwen3.5-plus", 1_000_000, None),
                ("glm-5", 202_752, None),
                ("glm-4.7", 202_752, None),
                ("MiniMax-M2.5", 196_608, None),
                ("kimi-k2.5", 262_144, None),
                ("qwen3-max-2026-01-23", 262_144, None),
                ("qwen3-coder-next", 262_144, None),
                ("qwen3-coder-plus", 1_000_000, None),
            ),
            source="official:aliyun-coding-plan",
            documentation_url=_CODING_PLAN_URL,
        ),
    )

_REGISTRY.update(
    _records_by_model(
        "dashscope",
        (("qwen3-235b-a22b-thinking-2507", 131_072, 32_768),),
        source="official:aliyun-model-studio",
        documentation_url=(
            "https://help.aliyun.com/zh/model-studio/qwen3-235b-a22b-thinking-2507"
        ),
    ),
)
_REGISTRY[("dashscope", "deepseek-v3.2")] = _record(
    "dashscope",
    "deepseek-v3.2",
    131_072,
    max_output_tokens=65_536,
    source="official:aliyun-model-studio",
    documentation_url=("https://help.aliyun.com/zh/model-studio/deepseek-v3-3"),
)
_REGISTRY[("aliyun-tokenplan", "deepseek-v3.2")] = _record(
    "aliyun-tokenplan",
    "deepseek-v3.2",
    163_840,
    max_output_tokens=16_384,
    source="official:aliyun-token-plan",
    documentation_url="https://help.aliyun.com/en/model-studio/openclaw",
)

_REGISTRY.update(
    _records_by_model(
        "kimi-codingplan",
        (
            ("kimi-for-coding", 256 * 1024, None),
            ("kimi-for-coding-highspeed", 256 * 1024, None),
            ("k3-256k", 256 * 1024, None),
            ("k3", 1_048_576, None),
        ),
        source="official:kimi-code-models",
        documentation_url="https://www.kimi.com/code/docs/en/kimi-code/models.html",
    ),
)
_REGISTRY[("kimi-codingplan", "kimi-for-coding")] = _record(
    "kimi-codingplan",
    "kimi-for-coding",
    256 * 1024,
    recommended_output_reserve=50_000,
    source="official:kimi-code-models",
    documentation_url="https://www.kimi.com/code/docs/en/kimi-code/models.html",
)
_REGISTRY[("modelscope", "Qwen/Qwen3.5-122B-A10B")] = _record(
    "modelscope",
    "Qwen/Qwen3.5-122B-A10B",
    32_768,
    source="official:aliyun-qwen3.5",
    documentation_url="https://help.aliyun.com/en/model-studio/vision-model/",
)


def resolve_model_capability(
    provider_id: str,
    model_id: str,
) -> ModelCapabilityRecord | None:
    """Return the exact registered capability; never apply fuzzy matching."""

    exact = _REGISTRY.get((provider_id.strip(), model_id.strip()))
    if exact is not None:
        return exact
    return _GLOBAL_REGISTRY.get(model_id.strip())


def registered_model_capabilities() -> tuple[ModelCapabilityRecord, ...]:
    """Expose an immutable snapshot for diagnostics and contract tests."""

    seen: set[tuple[str, str]] = set()
    records: list[ModelCapabilityRecord] = []
    for record in (*_REGISTRY.values(), *_GLOBAL_REGISTRY.values()):
        key = (record.provider_id, record.model_id)
        if key not in seen:
            records.append(record)
            seen.add(key)
    return tuple(records)
