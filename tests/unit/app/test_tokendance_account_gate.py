"""TokenDance is a cloud-account benefit without gating user APIs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app.routers import providers as providers_module
from lantai.config.config import ModelSlotConfig
from lantai.providers.provider import ProviderInfo


class _Manager:
    def __init__(self) -> None:
        self.tokendance = SimpleNamespace(
            id="tokendance",
            meta={"oauth_mode": "api_key_pkce"},
        )

    async def list_provider_info(self):
        return [
            ProviderInfo(id="tokendance", name="TokenDance"),
            ProviderInfo(
                id="my-api",
                name="My API",
                is_custom=True,
                base_url="https://api.example/v1",
            ),
        ]

    def get_provider(self, provider_id: str):
        return self.tokendance if provider_id == "tokendance" else None

    def is_model_slot_available(self, _slot) -> bool:
        return True


@pytest.mark.asyncio
async def test_provider_list_hides_only_tokendance_when_logged_out(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        providers_module,
        "is_watcha_account_connected",
        lambda: False,
    )

    providers = await providers_module.list_all_providers(_Manager())

    assert [provider.id for provider in providers] == ["my-api"]


@pytest.mark.asyncio
async def test_provider_list_exposes_tokendance_after_watcha_login(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        providers_module,
        "is_watcha_account_connected",
        lambda: True,
    )

    providers = await providers_module.list_all_providers(_Manager())

    assert [provider.id for provider in providers] == ["tokendance", "my-api"]


def test_logged_out_active_tokendance_is_not_projected(monkeypatch) -> None:
    monkeypatch.setattr(
        providers_module,
        "is_watcha_account_connected",
        lambda: False,
    )

    assert (
        providers_module._available_model_slot(
            _Manager(),
            ModelSlotConfig(provider_id="tokendance", model="qwen3.8-flash"),
        )
        is None
    )


def test_tokendance_oauth_requires_watcha_but_custom_api_does_not(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        providers_module,
        "is_watcha_account_connected",
        lambda: False,
    )

    with pytest.raises(HTTPException, match="Watcha account"):
        providers_module._require_tokendance_oauth_provider(
            _Manager(),
            "tokendance",
        )
