# -*- coding: utf-8 -*-
"""Behavior locks for bounded diagnostic model clients."""

from lantai.agents import model_factory
from lantai.config.config import ModelSlotConfig
from lantai.providers.model_endpoint import ProviderModelEndpoint
from lantai.runtime.adapters.openai_model_client import (
    OpenAICompatibleModelClient,
)
from lantai.runtime.model_wrappers import RetryingModelClient


def test_create_model_diagnostic_client_skips_runtime_wrappers(
    monkeypatch,
) -> None:
    class FakeProvider:
        def resolve_model_endpoint(self, model_id: str):
            return ProviderModelEndpoint(
                provider_id="probe-provider",
                model_id=model_id,
                base_url="https://probe.example/v1",
                api_key="sk-test",
                native_kind="openai_compatible",
            )

    class FakeManager:
        def get_provider(self, provider_id: str):
            assert provider_id == "probe-provider"
            return FakeProvider()

    def get_manager() -> FakeManager:
        return FakeManager()

    monkeypatch.setattr(
        model_factory.ProviderManager,
        "get_instance",
        get_manager,
    )

    client = model_factory.create_model_diagnostic_client(
        model_slot=ModelSlotConfig(
            provider_id="probe-provider",
            model="probe-model",
        ),
    )

    assert isinstance(client, OpenAICompatibleModelClient)
    assert not isinstance(client, RetryingModelClient)
    assert client.provider_id == "probe-provider"
    assert client.model_name == "probe-model"
