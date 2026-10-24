# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument,protected-access
"""Tests for the OpenCode built-in provider."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import lantai.providers.provider_manager as provider_manager_module
from lantai.agents import model_factory
from lantai.config.config import ModelSlotConfig
from lantai.providers.capability_baseline import ExpectedCapabilityRegistry
from lantai.providers.openai_provider import OpenAIProvider
from lantai.providers.provider import ModelInfo
from lantai.runtime.adapters.openai_responses_model_client import (
    OpenAIResponsesModelClient,
)
from lantai.providers.provider_manager import (
    PROVIDER_OPENCODE,
    ProviderManager,
)


def test_opencode_provider_is_openai_compatible() -> None:
    """OpenCode provider should be an OpenAIProvider instance."""
    assert isinstance(PROVIDER_OPENCODE, OpenAIProvider)


def test_opencode_provider_config() -> None:
    """Verify OpenCode provider configuration defaults."""
    assert PROVIDER_OPENCODE.id == "opencode"
    assert PROVIDER_OPENCODE.name == "OpenCode"
    assert PROVIDER_OPENCODE.base_url == "https://opencode.ai/zen/v1"
    assert PROVIDER_OPENCODE.freeze_url is True
    assert PROVIDER_OPENCODE.support_model_discovery is True


def test_opencode_keeps_big_pickle_as_builtin_offline_catalog_fallback() -> None:
    """Big Pickle stays selectable before the first live catalog refresh."""
    assert [(model.id, model.is_free) for model in PROVIDER_OPENCODE.models] == [
        ("big-pickle", True),
    ]


def test_opencode_capability_baseline_has_no_removed_models() -> None:
    """Removed OpenCode models should not remain in capability metadata."""
    registry = ExpectedCapabilityRegistry()

    assert [
        capability.model_id
        for capability in registry.get_all_for_provider("opencode")
    ] == ["big-pickle"]


@pytest.mark.parametrize(
    ("model_id", "native_kind", "family", "chat_model"),
    [
        ("big-pickle", "openai_compatible", "openai", "OpenAIChatModel"),
        ("gpt-5.6-sol", "openai_responses", "openai", "OpenAIChatModel"),
        ("claude-sonnet-4.6", "legacy", "anthropic", "AnthropicChatModel"),
        ("gemini-3.1-pro", "legacy", "gemini", "GeminiChatModel"),
    ],
)
def test_opencode_routes_models_to_their_upstream_protocol(
    model_id,
    native_kind,
    family,
    chat_model,
) -> None:
    """Each OpenCode model should resolve to its documented wire protocol."""
    endpoint = PROVIDER_OPENCODE.resolve_model_endpoint(model_id)

    assert endpoint.native_kind == native_kind
    assert endpoint.family == family
    assert endpoint.chat_model == chat_model
    assert endpoint.base_url == "https://opencode.ai/zen/v1"


@pytest.mark.parametrize(
    ("model_id", "model_class"),
    [
        ("big-pickle", "OpenAIChatModelCompat"),
        ("claude-sonnet-4.6", "AnthropicChatModel"),
        ("gemini-3.1-pro", "GeminiChatModel"),
    ],
)
def test_opencode_builds_the_protocol_specific_legacy_client(
    model_id,
    model_class,
    monkeypatch,
) -> None:
    """Legacy-compatible routes should use the matching SDK client."""
    monkeypatch.setattr(PROVIDER_OPENCODE, "api_key", "test-key")
    model = PROVIDER_OPENCODE.get_chat_model_instance(model_id)

    assert model.__class__.__name__ == model_class


def test_opencode_responses_route_uses_native_responses_client(
    monkeypatch,
) -> None:
    """The public model factory must not send Responses models to Chat."""

    class FakeManager:
        def get_provider(self, provider_id: str):
            assert provider_id == "opencode"
            return PROVIDER_OPENCODE

    monkeypatch.setattr(
        model_factory.ProviderManager,
        "get_instance",
        lambda: FakeManager(),
    )
    monkeypatch.setattr(PROVIDER_OPENCODE, "api_key", "test-key")

    client = model_factory.create_model_client(
        agent_id=None,
        model_slot=ModelSlotConfig(
            provider_id="opencode",
            model="gpt-5.6-sol",
        ),
    )

    assert isinstance(client._inner._inner, OpenAIResponsesModelClient)


@pytest.fixture
def isolated_secret_dir(monkeypatch, tmp_path):
    secret_dir = tmp_path / ".lantai.secret"
    monkeypatch.setattr(provider_manager_module, "SECRET_DIR", secret_dir)
    return secret_dir


def test_opencode_registered_in_provider_manager(isolated_secret_dir) -> None:
    """OpenCode provider should be registered as built-in provider."""
    manager = ProviderManager()

    provider = manager.get_provider("opencode")
    assert provider is not None
    assert isinstance(provider, OpenAIProvider)
    assert provider.base_url == "https://opencode.ai/zen/v1"


async def test_opencode_check_connection_success(monkeypatch) -> None:
    """OpenCode check_connection should delegate to OpenAI client."""
    provider = OpenAIProvider(
        id="opencode",
        name="OpenCode",
        base_url="https://opencode.ai/zen/v1",
        api_key="test-key",
    )

    class FakeModels:
        async def list(self, timeout=None):
            return SimpleNamespace(data=[])

    fake_client = SimpleNamespace(models=FakeModels())
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)

    ok, msg = await provider.check_connection(timeout=2)

    assert ok is True
    assert msg == ""


async def test_opencode_fetches_every_upstream_model_with_free_metadata(
    monkeypatch,
) -> None:
    """Discovery should mirror upstream IDs and preserve free-model warnings."""

    class FakeModels:
        async def list(self, timeout=None):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(id="big-pickle"),
                    SimpleNamespace(id="future-model"),
                    SimpleNamespace(id="nemotron-3.5-lightning-free"),
                ],
            )

    monkeypatch.setattr(
        PROVIDER_OPENCODE,
        "_client",
        lambda timeout=5: SimpleNamespace(models=FakeModels()),
    )

    models = await PROVIDER_OPENCODE.fetch_models(timeout=3)

    assert [(model.id, model.is_free) for model in models] == [
        ("big-pickle", True),
        ("future-model", False),
        ("nemotron-3.5-lightning-free", True),
    ]


async def test_opencode_discovery_replaces_stale_catalog_without_duplicates(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    """A successful refresh should mirror upstream and remove stale rows."""
    manager = ProviderManager()
    provider = manager.get_provider("opencode")
    assert provider is not None
    provider.extra_models = [ModelInfo(id="removed-upstream", name="Old")]

    async def fetch_models(self, timeout=5):
        return [
            ModelInfo(id="big-pickle", name="Big Pickle", is_free=True),
            ModelInfo(id="future-model", name="Future Model"),
        ]

    monkeypatch.setattr(type(provider), "fetch_models", fetch_models)

    discovered = await manager.fetch_provider_models("opencode", save=True)

    assert [model.id for model in discovered] == ["big-pickle", "future-model"]
    assert [model.id for model in provider.extra_models] == ["future-model"]
