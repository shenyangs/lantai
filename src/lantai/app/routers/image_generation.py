# -*- coding: utf-8 -*-
"""Configuration API for image generation inside chat."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from ...config import load_config, save_config
from ...config.config import ImageGenerationConfig
from ...providers.provider_manager import ProviderManager

router = APIRouter(
    prefix="/image-generation",
    tags=["image-generation"],
)


def _validate_image_model_configuration(
    body: ImageGenerationConfig,
) -> None:
    if body.enabled and not body.models:
        raise HTTPException(
            status_code=400,
            detail="At least one image generation model is required.",
        )

    manager = ProviderManager.get_instance()
    if body.works_cover_enabled:
        cover_key = (
            body.works_cover_model.provider_id.strip(),
            body.works_cover_model.model.strip(),
        )
        configured_keys = {
            (slot.provider_id, slot.model) for slot in body.models
        }
        if not all(cover_key) or cover_key not in configured_keys:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Select one configured image model before enabling "
                    "AI-generated Works covers."
                ),
            )

    for slot in body.models:
        if not manager.is_model_slot_available(slot):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Image model {slot.provider_id}/{slot.model} is not "
                    "configured or available."
                ),
            )
        provider = manager.get_provider(slot.provider_id)
        if provider is None or not callable(
            getattr(provider, "generate_image", None),
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Provider {slot.provider_id} does not expose an "
                    "OpenAI-compatible image generation API."
                ),
            )
        supports_image_model = getattr(
            provider,
            "supports_image_generation_model",
            None,
        )
        if callable(supports_image_model) and not supports_image_model(
            slot.model,
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model {slot.provider_id}/{slot.model} is not an image "
                    "generation model according to its provider catalog."
                ),
            )


@router.get("/config", response_model=ImageGenerationConfig)
async def get_image_generation_config() -> ImageGenerationConfig:
    return load_config().image_generation


@router.put("/config", response_model=ImageGenerationConfig)
async def put_image_generation_config(
    body: ImageGenerationConfig = Body(...),
) -> ImageGenerationConfig:
    _validate_image_model_configuration(body)
    config = load_config()
    config.image_generation = body
    save_config(config)
    return body
