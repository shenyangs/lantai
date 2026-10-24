# -*- coding: utf-8 -*-
"""Configuration API for video generation inside chat."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from ...config import load_config, save_config
from ...config.config import VideoGenerationConfig
from ...providers.provider_manager import ProviderManager
from ...video_generation_routing import (
    effective_video_generation_config,
    find_video_model,
)

router = APIRouter(
    prefix="/video-generation",
    tags=["video-generation"],
)


def _validate_video_model_configuration(
    body: VideoGenerationConfig,
) -> None:
    if body.enabled and not body.models:
        raise HTTPException(
            status_code=400,
            detail="At least one video generation model is required.",
        )

    manager = ProviderManager.get_instance()
    for slot in body.models:
        if not manager.is_model_slot_available(slot):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Video model {slot.provider_id}/{slot.model} is not "
                    "configured or available."
                ),
            )
        provider = manager.get_provider(slot.provider_id)
        if provider is None or not callable(
            getattr(provider, "generate_video", None),
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Provider {slot.provider_id} does not expose a "
                    "text-to-video generation API."
                ),
            )
        if find_video_model(provider, slot.model) is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model {slot.provider_id}/{slot.model} is not a "
                    "text-to-video model according to its provider catalog."
                ),
            )


@router.get("/config", response_model=VideoGenerationConfig)
async def get_video_generation_config() -> VideoGenerationConfig:
    manager = ProviderManager.get_instance()
    return effective_video_generation_config(load_config(), manager)


@router.put("/config", response_model=VideoGenerationConfig)
async def put_video_generation_config(
    body: VideoGenerationConfig = Body(...),
) -> VideoGenerationConfig:
    _validate_video_model_configuration(body)
    saved = body.model_copy(update={"legacy_model_flags_migrated": True})
    config = load_config()
    config.video_generation = saved
    save_config(config)
    return saved
