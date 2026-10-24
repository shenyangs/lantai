# -*- coding: utf-8 -*-
"""Behavior locks for in-chat image generation settings."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from lantai.app.routers import image_generation
from lantai.config.config import (
    Config,
    ImageGenerationConfig,
    ModelSlotConfig,
)


class _ImageProvider:
    async def generate_image(self, **_kwargs):
        return {}


class _ProtocolAwareImageProvider(_ImageProvider):
    def supports_image_generation_model(self, model_id: str) -> bool:
        return model_id == "image-model"


class _Manager:
    def __init__(self, *, available: bool = True, provider=None) -> None:
        self.available = available
        self.provider = provider if provider is not None else _ImageProvider()

    def is_model_slot_available(self, _slot) -> bool:
        return self.available

    def get_provider(self, _provider_id: str):
        return self.provider


async def test_put_image_generation_config_persists_valid_order(
    monkeypatch,
) -> None:
    root = Config()
    save = Mock()
    monkeypatch.setattr(image_generation, "load_config", lambda: root)
    monkeypatch.setattr(image_generation, "save_config", save)
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: _Manager(),
    )
    body = ImageGenerationConfig(
        enabled=True,
        models=[
            ModelSlotConfig(provider_id="first", model="image-a"),
            ModelSlotConfig(provider_id="second", model="image-b"),
        ],
        timeout_seconds=80,
    )

    response = await image_generation.put_image_generation_config(body)

    assert response == body
    assert root.image_generation == body
    save.assert_called_once_with(root)


async def test_works_cover_requires_an_explicit_configured_model(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: _Manager(),
    )
    body = ImageGenerationConfig(
        models=[ModelSlotConfig(provider_id="image", model="model-a")],
        works_cover_enabled=True,
    )

    with pytest.raises(HTTPException, match="Select one configured image model"):
        await image_generation.put_image_generation_config(body)


@pytest.mark.parametrize(
    ("body", "manager", "message"),
    [
        (
            ImageGenerationConfig(enabled=True, models=[]),
            _Manager(),
            "At least one image generation model",
        ),
        (
            ImageGenerationConfig(
                enabled=True,
                models=[ModelSlotConfig(provider_id="missing", model="image")],
            ),
            _Manager(available=False),
            "is not configured or available",
        ),
        (
            ImageGenerationConfig(
                enabled=True,
                models=[ModelSlotConfig(provider_id="chat-only", model="image")],
            ),
            _Manager(provider=SimpleNamespace()),
            "does not expose",
        ),
    ],
)
async def test_put_image_generation_config_rejects_invalid_routes(
    monkeypatch,
    body,
    manager,
    message,
) -> None:
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )

    with pytest.raises(HTTPException, match=message):
        await image_generation.put_image_generation_config(body)


async def test_put_image_generation_config_rejects_chat_only_catalog_model(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: _Manager(provider=_ProtocolAwareImageProvider()),
    )

    with pytest.raises(HTTPException, match="not an image generation model"):
        await image_generation.put_image_generation_config(
            ImageGenerationConfig(
                enabled=True,
                models=[
                    ModelSlotConfig(
                        provider_id="tokendance",
                        model="chat-model",
                    ),
                ],
            ),
        )
