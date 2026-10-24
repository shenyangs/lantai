# -*- coding: utf-8 -*-
"""Per-request reasoning/thinking controls for supported model families."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import logging
from typing import Any

from .request_profiles import is_kimi_coding_base_url


@dataclass(frozen=True)
class ReasoningRequestOptions:
    """Runtime options attached to one user request."""

    enabled: bool
    effort: str = "high"


@dataclass(frozen=True)
class ReasoningCapability:
    """A model family's supported reasoning control surface."""

    supported: bool
    mode: str = ""
    fixed: bool = False
    default_enabled: bool = False
    note: str = ""


_current_options: ContextVar[ReasoningRequestOptions | None] = ContextVar(
    "lantai_reasoning_options",
    default=None,
)


def set_reasoning_options(options: ReasoningRequestOptions | None):
    """Set reasoning options for the current async context."""

    return _current_options.set(options)


def reset_reasoning_options(token: Any) -> None:
    """Reset reasoning options for the current async context."""
    try:
        _current_options.reset(token)
    except ValueError:
        logger.debug(
            "Ignored reasoning context reset from a different async context.",
            exc_info=True,
        )


def get_reasoning_options() -> ReasoningRequestOptions | None:
    """Return current request reasoning options, if any."""

    return _current_options.get()


def _model_leaf(model_id: str) -> str:
    return (model_id or "").strip().split("/")[-1].lower()


def _model_full(model_id: str) -> str:
    return (model_id or "").strip().lower()


def _provider(provider_id: str) -> str:
    return (provider_id or "").strip().lower()


def _is_openai_reasoning_model(model_id: str) -> bool:
    model = _model_leaf(model_id)
    if model.startswith("gpt-5-chat"):
        return False
    return model.startswith(("gpt-5", "o1", "o3", "o4", "codex-mini"))


def _is_qwen_thinking_model(model_id: str) -> bool:
    model = _model_full(model_id)
    return "qwen3" in model or "qwq" in model


def _is_kimi_thinking_model(model_id: str) -> bool:
    model = _model_full(model_id)
    return "kimi" in model and any(
        part in model
        for part in (
            "k2.5",
            "k2-5",
            "k2-thinking",
            "k2.6",
            "for-coding",
        )
    )


def _is_minimax_thinking_model(model_id: str) -> bool:
    model = _model_full(model_id)
    return "minimax" in model and any(
        marker in model
        for marker in ("m2", "m-2", "m_2")
    )


def _is_minimax_m3_model(model_id: str) -> bool:
    model = _model_full(model_id)
    return "minimax" in model and any(
        marker in model
        for marker in ("m3", "m-3", "m_3")
    )


def _is_glm_thinking_model(model_id: str) -> bool:
    model = _model_leaf(model_id)
    return model.startswith(
        (
            "glm-5",
            "glm-4.7",
            "glm-4.6",
            "glm-4.5",
        )
    )


def _is_deepseek_switchable_model(model_id: str) -> bool:
    model = _model_full(model_id)
    return "deepseek" in model and any(
        part in model
        for part in (
            "v3.2",
            "v3-2",
            "v4",
        )
    )


def _is_fixed_reasoning_model(model_id: str) -> bool:
    model = _model_full(model_id)
    return _is_minimax_thinking_model(model_id) or any(
        part in model
        for part in (
            "deepseek-reasoner",
            "deepseek-r1",
            "kimi-k2-thinking",
            "kimi-k2.7-code",
            "kimi-k2-7-code",
            "thinking-turbo",
        )
    )


def _is_gemini_thinking_model(model_id: str) -> bool:
    model = _model_leaf(model_id)
    return model.startswith("gemini-3") or model.startswith("gemini-2.5")


def _is_anthropic_thinking_model(model_id: str) -> bool:
    model = _model_leaf(model_id)
    return model.startswith("claude-") and (
        "3-7" in model or "sonnet-4" in model or "opus-4" in model
    )


def _is_openrouter_reasoning_model(model_id: str) -> bool:
    model = _model_full(model_id)
    return any(
        part in model
        for part in (
            "gpt-5",
            "/o3",
            "/o4",
            "claude-3.7",
            "claude-3-7",
            "claude-sonnet-4",
            "claude-opus-4",
            "gemini-2.5",
            "gemini-3",
            "grok-4",
            "deepseek-r1",
            "deepseek-reasoner",
            "deepseek-v3.2",
            "qwen3",
        )
    )


def get_reasoning_capability(
    provider_id: str,
    model_id: str,
    *,
    base_url: str = "",
    chat_model: str = "",
) -> ReasoningCapability:
    """Return the supported reasoning control for a provider/model pair."""

    provider = _provider(provider_id)
    base = (base_url or "").lower()
    chat_model_name = (chat_model or "").lower()

    if _is_fixed_reasoning_model(model_id):
        return ReasoningCapability(
            supported=True,
            mode="fixed_reasoning",
            fixed=True,
            default_enabled=True,
            note="model always uses reasoning",
        )

    if _is_minimax_m3_model(model_id):
        return ReasoningCapability(
            supported=True,
            mode="minimax_adaptive_thinking",
            default_enabled=True,
            note="MiniMax-M3 supports adaptive thinking",
        )

    if provider in {"openai", "azure-openai"} and _is_openai_reasoning_model(
        model_id,
    ):
        return ReasoningCapability(
            supported=True,
            mode="openai_reasoning_effort",
        )

    if provider == "openrouter" and _is_openrouter_reasoning_model(model_id):
        return ReasoningCapability(
            supported=True,
            mode="openrouter_reasoning",
        )

    if provider == "gemini" and _is_gemini_thinking_model(model_id):
        return ReasoningCapability(
            supported=True,
            mode="gemini_thinking_config",
            default_enabled=True,
        )

    if provider == "anthropic" and _is_anthropic_thinking_model(model_id):
        return ReasoningCapability(
            supported=True,
            mode="anthropic_extended_thinking",
        )

    if _is_qwen_thinking_model(model_id):
        return ReasoningCapability(
            supported=True,
            mode="dashscope_enable_thinking",
        )

    if _is_glm_thinking_model(model_id):
        return ReasoningCapability(
            supported=True,
            mode="openai_extra_thinking",
            default_enabled=True,
        )

    if _is_deepseek_switchable_model(model_id):
        return ReasoningCapability(
            supported=True,
            mode="deepseek_thinking",
            default_enabled=True,
        )

    if _is_kimi_thinking_model(model_id) or "api.kimi.com" in base:
        return ReasoningCapability(
            supported=True,
            mode="openai_extra_thinking",
            default_enabled=True,
        )

    if provider in {"zhipu-cn", "zhipu-intl"} and _is_glm_thinking_model(
        model_id,
    ):
        return ReasoningCapability(
            supported=True,
            mode="openai_extra_thinking",
            default_enabled=True,
        )

    if "anthropic" in chat_model_name and _is_anthropic_thinking_model(
        model_id,
    ):
        return ReasoningCapability(
            supported=True,
            mode="anthropic_extended_thinking",
        )

    return ReasoningCapability(supported=False)


def _merge_extra_body(
    kwargs: dict[str, Any],
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(kwargs)
    existing = merged.get("extra_body")
    if isinstance(existing, dict):
        merged["extra_body"] = _deep_merge(existing, extra_body)
    else:
        merged["extra_body"] = extra_body
    return merged


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result


def _normalized_effort(effort: str) -> str:
    return effort if effort in {"low", "medium", "high"} else "high"


def _enabled_type(enabled: bool) -> dict[str, str]:
    return {"type": "enabled" if enabled else "disabled"}


def build_reasoning_call_kwargs(
    provider_id: str,
    model_id: str,
    *,
    enabled: bool,
    effort: str = "high",
    base_url: str = "",
    chat_model: str = "",
) -> dict[str, Any]:
    """Build per-call generation kwargs for the detected reasoning mode."""

    capability = get_reasoning_capability(
        provider_id,
        model_id,
        base_url=base_url,
        chat_model=chat_model,
    )
    if not capability.supported or capability.fixed:
        return {}

    mode = capability.mode
    normalized_effort = _normalized_effort(effort)

    if mode == "openai_reasoning_effort":
        return {"reasoning_effort": normalized_effort} if enabled else {}

    if mode == "openrouter_reasoning":
        if not enabled:
            return {}
        return {"extra_body": {"reasoning": {"effort": normalized_effort}}}

    if mode == "dashscope_enable_thinking":
        return {"extra_body": {"enable_thinking": bool(enabled)}}

    if mode == "minimax_adaptive_thinking":
        thinking = {"type": "adaptive" if enabled else "disabled"}
        if (
            _provider(provider_id) in {"minimax", "minimax-cn"}
            or "anthropic" in (chat_model or "").lower()
            or "/anthropic" in (base_url or "").lower()
        ):
            return {"thinking": thinking}
        return {"extra_body": {"thinking": thinking}}

    if mode == "openai_extra_thinking":
        thinking = _enabled_type(enabled)
        if enabled and is_kimi_coding_base_url(base_url):
            thinking["keep"] = "all"
        return {"extra_body": {"thinking": thinking}}

    if mode == "deepseek_thinking":
        payload: dict[str, Any] = {
            "extra_body": {"thinking": _enabled_type(enabled)},
        }
        if enabled:
            payload["reasoning_effort"] = "high"
        return payload

    if mode == "anthropic_extended_thinking":
        if not enabled:
            return {}
        return {"thinking": {"type": "enabled", "budget_tokens": 4096}}

    if mode == "gemini_thinking_config":
        if not enabled:
            return {}
        model = _model_leaf(model_id)
        if model.startswith("gemini-3"):
            return {
                "thinking_config": {
                    "include_thoughts": True,
                    "thinking_level": "high",
                },
            }
        return {
            "thinking_config": {
                "include_thoughts": True,
                "thinking_budget": 4096,
            },
        }

    return {}


def apply_current_reasoning_options(
    kwargs: dict[str, Any],
    *,
    provider_id: str,
    model_id: str,
    base_url: str = "",
    chat_model: str = "",
) -> dict[str, Any]:
    """Merge current request reasoning options into generation kwargs."""

    options = get_reasoning_options()
    if options is None:
        return kwargs

    reasoning_kwargs = build_reasoning_call_kwargs(
        provider_id,
        model_id,
        enabled=options.enabled,
        effort=options.effort,
        base_url=base_url,
        chat_model=chat_model,
    )
    if not reasoning_kwargs:
        return kwargs

    merged = dict(kwargs)
    extra_body = reasoning_kwargs.pop("extra_body", None)
    if isinstance(extra_body, dict):
        merged = _merge_extra_body(merged, extra_body)
    return _deep_merge(merged, reasoning_kwargs)
logger = logging.getLogger(__name__)
