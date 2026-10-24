# -*- coding: utf-8 -*-
"""Shared routing helpers for the in-chat video generation feature."""
from __future__ import annotations

from typing import Any

from .config.config import Config, ModelSlotConfig, VideoGenerationConfig
from .providers.provider import ModelInfo


def provider_video_models(provider: Any) -> list[ModelInfo]:
    if provider is None:
        return []
    return [
        model
        for model in [
            *list(getattr(provider, "models", []) or []),
            *list(getattr(provider, "extra_models", []) or []),
        ]
        if model.supports_text_to_video
    ]


def find_video_model(provider: Any, model_id: str) -> ModelInfo | None:
    return next(
        (
            model
            for model in provider_video_models(provider)
            if model.id == model_id
        ),
        None,
    )


def effective_video_generation_config(
    config: Config,
    manager: Any,
) -> VideoGenerationConfig:
    """Return central routing, migrating the former TokenDance switches.

    The derived legacy configuration stays marked as not migrated until the
    user saves the new section. That lets unrelated config saves preserve the
    old behavior without making the hidden switches authoritative forever.
    """

    configured = config.video_generation
    if configured.legacy_model_flags_migrated or configured.models:
        return configured.model_copy(deep=True)

    provider = manager.get_provider("tokendance")
    legacy_slots = [
        ModelSlotConfig(provider_id="tokendance", model=model.id)
        for model in provider_video_models(provider)
        if model.use_for_video
    ]
    if not legacy_slots:
        return configured.model_copy(deep=True)
    return configured.model_copy(
        deep=True,
        update={"enabled": True, "models": legacy_slots},
    )


__all__ = [
    "effective_video_generation_config",
    "find_video_model",
    "provider_video_models",
]
