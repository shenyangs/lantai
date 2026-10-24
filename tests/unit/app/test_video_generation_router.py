# -*- coding: utf-8 -*-
"""Behavior locks for in-chat video generation settings."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from lantai.app.routers import video_generation
from lantai.config.config import Config, ModelSlotConfig, VideoGenerationConfig
from lantai.providers.provider import ModelInfo


class _VideoProvider:
    id = "tokendance"
    models = [
        ModelInfo(
            id="seedance-2.0",
            name="Seedance 2.0",
            supported_protocols=["seedance:generations"],
        ),
    ]
    extra_models = []

    async def generate_video(self, **_kwargs):
        return {}


class _Manager:
    def __init__(self, provider=None) -> None:
        self.provider = provider if provider is not None else _VideoProvider()

    def is_model_slot_available(self, slot) -> bool:
        return slot.provider_id == "tokendance" and slot.model == "seedance-2.0"

    def get_provider(self, _provider_id: str):
        return self.provider


async def test_put_video_generation_config_persists_order_and_migration(
    monkeypatch,
) -> None:
    root = Config()
    save = Mock()
    monkeypatch.setattr(video_generation, "load_config", lambda: root)
    monkeypatch.setattr(video_generation, "save_config", save)
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: _Manager(),
    )
    body = VideoGenerationConfig(
        enabled=True,
        models=[
            ModelSlotConfig(
                provider_id="tokendance",
                model="seedance-2.0",
            ),
        ],
        timeout_seconds=900,
    )

    response = await video_generation.put_video_generation_config(body)

    assert response.legacy_model_flags_migrated is True
    assert root.video_generation == response
    save.assert_called_once_with(root)


async def test_get_video_generation_config_migrates_legacy_model_flags(
    monkeypatch,
) -> None:
    root = Config()
    provider = _VideoProvider()
    provider.models[0].use_for_video = True
    monkeypatch.setattr(video_generation, "load_config", lambda: root)
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: _Manager(provider),
    )

    response = await video_generation.get_video_generation_config()

    assert response.enabled is True
    assert response.models == [
        ModelSlotConfig(provider_id="tokendance", model="seedance-2.0"),
    ]
    assert response.legacy_model_flags_migrated is False


@pytest.mark.parametrize(
    ("body", "manager", "message"),
    [
        (
            VideoGenerationConfig(enabled=True, models=[]),
            _Manager(),
            "At least one video generation model",
        ),
        (
            VideoGenerationConfig(
                enabled=True,
                models=[
                    ModelSlotConfig(provider_id="missing", model="video")
                ],
            ),
            _Manager(),
            "is not configured or available",
        ),
        (
            VideoGenerationConfig(
                enabled=True,
                models=[
                    ModelSlotConfig(
                        provider_id="tokendance",
                        model="seedance-2.0",
                    )
                ],
            ),
            _Manager(provider=SimpleNamespace()),
            "does not expose",
        ),
    ],
)
async def test_put_video_generation_config_rejects_invalid_routes(
    monkeypatch,
    body,
    manager,
    message,
) -> None:
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )

    with pytest.raises(HTTPException, match=message):
        await video_generation.put_video_generation_config(body)
