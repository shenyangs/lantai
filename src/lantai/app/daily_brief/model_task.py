# -*- coding: utf-8 -*-
"""Translate known model failures at the brief domain boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..structured_utility import StructuredTaskValidationError
from ..utility_model import UtilityModelPolicyError, UtilityTaskTimeoutError
from .service import BriefModelPolicyRejected, BriefModelUnavailable
from .models import BriefModelCapability


def brief_model_capability(agent_id: str, *, allow_configured_model: bool = False) -> BriefModelCapability:
    """Read configuration only: capability checks never invoke a provider."""
    from .. import utility_model
    config = utility_model.load_config().utility_intelligence
    if not bool(getattr(config, "background_processing_enabled", True)):
        return BriefModelCapability(ready=False, code="background_disabled",
            message="请在「设置 → 模型 → 回答后的后台整理」开启自动整理后重试。")
    try:
        resolved = utility_model.resolve_utility_model(config, agent_id)
        if resolved is None and not allow_configured_model:
            resolved = utility_model.resolve_active_model(agent_id)
    except Exception:
        resolved = None
    if resolved is None:
        return BriefModelCapability(ready=False, code="model_unavailable",
            message="简报模型暂不可用，请在「设置 → 模型 → 回答后的后台整理」检查模型连接后重试。")
    provider = utility_model.ProviderManager.get_instance().get_provider(resolved.provider_id)
    info = provider.get_model_info(resolved.model_id) if provider is not None else None
    if provider is None or (not allow_configured_model and not (
        bool(getattr(provider, "is_local", False)) or bool(getattr(info, "is_free", False)))):
        return BriefModelCapability(ready=False, code="model_policy",
            message="简报目前仅使用本地或免费模型。请在「设置 → 模型 → 回答后的后台整理」调整模型后重试。")
    return BriefModelCapability(ready=True, code="ready", model_label=resolved.label,
        message=(f"将使用 {resolved.label}，会消耗该模型的额度或产生 API 费用。"
            if allow_configured_model else "当前模型支持简报后台整理"))


def require_brief_model_capability(agent_id: str, *, allow_configured_model: bool = False) -> None:
    capability = brief_model_capability(agent_id, allow_configured_model=allow_configured_model)
    if not capability.ready:
        error = BriefModelPolicyRejected if capability.code == "model_policy" else BriefModelUnavailable
        raise error(capability.message)


async def run_brief_model_task(
    task: Callable[..., Awaitable[Any]],
    **kwargs: Any,
) -> Any:
    try:
        result = await task(**kwargs)
    except UtilityModelPolicyError as exc:
        if not kwargs.get("require_no_api_cost", True):
            raise BriefModelPolicyRejected(
                "简报模型与授权配置不一致。请在「设置 → 模型 → 回答后的后台整理」检查模型后重试。"
            ) from exc
        raise BriefModelPolicyRejected(
            "简报目前仅使用本地或免费模型。"
            "请在「设置 → 模型 → 回答后的后台整理」调整模型后重试。"
        ) from exc
    except UtilityTaskTimeoutError as exc:
        raise BriefModelUnavailable(
            "简报模型响应超时，请检查模型连接后重试。"
        ) from exc
    except StructuredTaskValidationError as exc:
        raise BriefModelUnavailable(
            "模型返回的简报结果未通过校验。"
            "请重试，或在「设置 → 模型 → 回答后的后台整理」更换模型。"
        ) from exc
    if result is None:
        raise BriefModelUnavailable(
            "简报模型暂不可用。请在「设置 → 模型 → 回答后的后台整理」"
            "检查模型配置和自动整理开关后重试。"
        )
    return result
