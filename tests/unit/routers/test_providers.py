# -*- coding: utf-8 -*-
"""Tests for provider router connection checks."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest
from pydantic import Field

import lantai.app.routers.providers as providers_router
from lantai.app.routers.providers import (
    CreateCustomProviderRequest,
    DiscoverModelsRequest,
    ModelSlotRequest,
    TestProviderRequest as ProviderTestRequest,
)
from lantai.providers.provider import ModelInfo, Provider, ProviderInfo


class _StubProvider(Provider):
    provider_result: tuple[bool, str] = (True, "")
    model_result: tuple[bool, str] = (True, "")
    provider_check_calls: int = 0
    checked_model_ids: List[str] = Field(default_factory=list)

    async def check_connection(
        self,
        timeout: float = 5,  # pylint: disable=unused-argument
    ) -> tuple[bool, str]:
        self.provider_check_calls += 1
        return self.provider_result

    async def fetch_models(
        self,
        timeout: float = 5,  # pylint: disable=unused-argument
    ) -> list[ModelInfo]:
        return self.models

    async def check_model_connection(
        self,
        model_id: str,
        timeout: float = 5,  # pylint: disable=unused-argument
    ) -> tuple[bool, str]:
        self.checked_model_ids.append(model_id)
        return self.model_result

    def get_chat_model_instance(self, model_id: str) -> Any:
        raise NotImplementedError


class _StubManager:
    def __init__(self, provider: Provider | None) -> None:
        self.provider = provider
        self.recorded_verifications: list[tuple[str, str | None]] = []

    def get_provider(self, provider_id: str) -> Provider | None:
        return self.provider if provider_id == "target" else None

    def is_model_slot_available(self, _slot) -> bool:
        return self.provider is not None

    async def record_provider_verification(
        self,
        provider_id: str,
        model_id: str | None = None,
    ) -> ProviderInfo:
        assert self.provider is not None
        self.recorded_verifications.append((provider_id, model_id))
        self.provider.connection_verified_revision = (
            self.provider.configuration_revision
        )
        self.provider.connection_verified_at = "2026-09-19T08:00:00Z"
        if model_id is not None:
            self.provider.model_verified_revision = (
                self.provider.configuration_revision
            )
            self.provider.model_verified_id = model_id
            self.provider.model_verified_at = "2026-09-19T08:00:00Z"
        return await self.provider.get_info()


class _CaptureCustomProviderManager:
    def __init__(self) -> None:
        self.created: ProviderInfo | None = None

    async def add_custom_provider(
        self,
        provider_info: ProviderInfo,
    ) -> ProviderInfo:
        self.created = provider_info
        return provider_info.model_copy(update={"is_custom": True})


class _FailingDiscoveryManager:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def get_provider(self, provider_id: str) -> Provider | None:
        return self.provider if provider_id == "target" else None

    def update_provider(self, provider_id: str, _config: dict) -> bool:
        return provider_id == "target"

    async def fetch_provider_models(
        self,
        provider_id: str,
        *,
        save: bool,
        raise_on_error: bool,
    ) -> list[ModelInfo]:
        assert provider_id == "target"
        assert save is True
        assert raise_on_error is True
        raise ValueError("Cannot reach Ollama; start it with `ollama serve`.")


def _model(model_id: str) -> ModelInfo:
    return ModelInfo(id=model_id, name=model_id)


async def test_custom_provider_accepts_openai_compatible_gateway_config():
    manager = _CaptureCustomProviderManager()

    response = await providers_router.create_custom_provider_endpoint(
        manager=manager,
        body=CreateCustomProviderRequest(
            id="company-gateway",
            name="Company gateway",
            default_base_url="https://gateway.example/v1",
            api_key="secret-token",
            chat_model="OpenAIChatModel",
            support_model_discovery=True,
            require_api_key=True,
            meta={
                "provider_kind": "openai_compatible",
                "user_configured_gateway": True,
            },
        ),
    )

    assert response.is_custom is True
    assert manager.created is not None
    assert manager.created.base_url == "https://gateway.example/v1"
    assert manager.created.api_key == "secret-token"
    assert manager.created.chat_model == "OpenAIChatModel"
    assert manager.created.support_model_discovery is True
    assert manager.created.require_api_key is True
    assert manager.created.meta["provider_kind"] == "openai_compatible"


def test_model_context_capability_can_be_configured_for_unknown_gateways():
    provider = _StubProvider(
        id="target",
        name="Target",
        models=[_model("custom-model")],
    )

    updated = provider.update_model_config(
        "custom-model",
        {
            "context_window": 128 * 1024,
            "max_output_tokens": 16 * 1024,
            "recommended_output_reserve": 24 * 1024,
        },
    )

    assert updated is True
    assert provider.models[0].context_window == 128 * 1024
    assert provider.models[0].max_output_tokens == 16 * 1024
    assert provider.models[0].recommended_output_reserve == 24 * 1024


def test_user_context_override_is_independent_from_model_capability_metadata():
    provider = _StubProvider(
        id="target",
        name="Target",
        models=[
            ModelInfo(
                id="known-model",
                name="Known",
                context_window=256 * 1024,
                capability_source="official-documentation",
            ),
        ],
    )

    updated = provider.update_model_config(
        "known-model",
        {"user_context_window": 192 * 1024},
    )

    assert updated is True
    assert provider.models[0].user_context_window == 192 * 1024
    assert provider.models[0].context_window == 256 * 1024
    assert provider.models[0].capability_source == "official-documentation"


async def test_provider_connection_uses_model_fallback_when_required(
    monkeypatch,
) -> None:
    provider = _StubProvider(
        id="target",
        name="Target",
        base_url="https://old.example/v1",
        api_key="old-key",
        models=[_model("first-model")],
        support_connection_check=False,
    )
    monkeypatch.setattr(providers_router, "deepcopy", lambda value: value)

    response = await providers_router.test_provider(
        manager=_StubManager(provider),
        provider_id="target",
        body=ProviderTestRequest(
            api_key="new-key",
            base_url="https://new.example/v1",
            chat_model="OpenAIChatModel",
        ),
    )

    assert response.success is True
    assert provider.provider_check_calls == 0
    assert provider.checked_model_ids == ["first-model"]
    assert provider.api_key == "new-key"
    assert provider.base_url == "https://new.example/v1"


async def test_provider_connection_falls_back_after_provider_check_failure(
    monkeypatch,
) -> None:
    provider = _StubProvider(
        id="target",
        name="Target",
        models=[_model("default-model")],
        support_connection_check=True,
        provider_result=(False, "models endpoint unavailable"),
    )
    monkeypatch.setattr(providers_router, "deepcopy", lambda value: value)

    response = await providers_router.test_provider(
        manager=_StubManager(provider),
        provider_id="target",
        body=ProviderTestRequest(model_id="requested-model"),
    )

    assert response.success is True
    assert provider.provider_check_calls == 1
    assert provider.checked_model_ids == ["requested-model"]


async def test_provider_connection_without_check_or_model_reports_failure(
    monkeypatch,
) -> None:
    provider = _StubProvider(
        id="target",
        name="Target",
        support_connection_check=False,
    )
    monkeypatch.setattr(providers_router, "deepcopy", lambda value: value)

    response = await providers_router.test_provider(
        manager=_StubManager(provider),
        provider_id="target",
        body=ProviderTestRequest(),
    )

    assert response.success is False
    assert response.message == "Connection failed: no model available to test"
    assert provider.provider_check_calls == 0
    assert provider.checked_model_ids == []


async def test_unsaved_provider_test_does_not_certify_stored_configuration(
) -> None:
    provider = _StubProvider(
        id="target",
        name="Target",
        base_url="https://stored.example/v1",
        api_key="stored-key",
        models=[_model("model-a")],
    )
    manager = _StubManager(provider)

    response = await providers_router.test_provider(
        manager=manager,
        provider_id="target",
        body=ProviderTestRequest(
            base_url="https://draft.example/v1",
            api_key="draft-key",
        ),
    )

    assert response.success is True
    assert response.receipt_recorded is False
    assert manager.recorded_verifications == []


async def test_model_test_records_revision_bound_receipt() -> None:
    provider = _StubProvider(
        id="target",
        name="Target",
        models=[_model("model-a")],
        configuration_revision=7,
    )
    manager = _StubManager(provider)

    response = await providers_router.test_model(
        manager=manager,
        provider_id="target",
        body=providers_router.TestModelRequest(model_id="model-a"),
    )

    assert response.success is True
    assert response.receipt_recorded is True
    assert response.configuration_revision == 7
    assert response.verified_model_id == "model-a"
    assert manager.recorded_verifications == [("target", "model-a")]


async def test_successful_unknown_model_probe_does_not_certify_saved_model() -> None:
    provider = _StubProvider(
        id="target",
        name="Target",
        models=[_model("model-a")],
        configuration_revision=7,
    )
    manager = _StubManager(provider)

    response = await providers_router.test_model(
        manager=manager,
        provider_id="target",
        body=providers_router.TestModelRequest(model_id="one-off-model"),
    )

    assert response.success is True
    assert response.receipt_recorded is False
    assert manager.recorded_verifications == []


async def test_discovery_failure_preserves_actionable_provider_message() -> None:
    provider = _StubProvider(id="target", name="Ollama")

    response = await providers_router.discover_models(
        manager=_FailingDiscoveryManager(provider),
        provider_id="target",
        body=DiscoverModelsRequest(),
        save=True,
    )

    assert response.success is False
    assert response.models == []
    assert response.added_count == 0
    assert response.message == (
        "Cannot reach Ollama; start it with `ollama serve`."
    )


async def test_agent_model_switch_returns_the_new_smart_context_policy(
    monkeypatch,
) -> None:
    provider = _StubProvider(
        id="target",
        name="Target",
        models=[
            ModelInfo(
                id="small-model",
                name="Small",
                context_window=64 * 1024,
                max_output_tokens=8 * 1024,
            ),
        ],
    )
    manager = _StubManager(provider)
    manager.maybe_probe_multimodal = lambda *_args: None
    config = SimpleNamespace(
        running=SimpleNamespace(
            max_input_length=256 * 1024,
            context_policy_mode="smart",
        ),
        active_model=None,
    )
    workspace = SimpleNamespace(agent_id="agent-1")
    monkeypatch.setattr(
        providers_router,
        "get_agent_for_request",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        providers_router,
        "load_agent_config",
        lambda _agent_id: config,
    )
    monkeypatch.setattr(
        providers_router,
        "save_agent_config",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        providers_router,
        "schedule_agent_reload",
        lambda *_args, **_kwargs: None,
    )

    response = await providers_router.set_active_model(
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        manager=manager,
        body=ModelSlotRequest(
            provider_id="target",
            model="small-model",
            scope="agent",
            agent_id="agent-1",
        ),
    )

    assert response.active_llm.model == "small-model"
    assert response.context_policy["hard_window"] == 64 * 1024
    assert response.context_policy["target_input_tokens"] < 64 * 1024
    assert response.context_policy["switch_action"] == "adapted"
    assert provider.checked_model_ids == ["small-model"]


async def test_agent_model_switch_rejects_inaccessible_model_before_save(
    monkeypatch,
) -> None:
    provider = _StubProvider(
        id="target",
        name="Target",
        models=[_model("blocked-model")],
        model_result=(False, "HTTP 403 forbidden"),
    )
    manager = _StubManager(provider)
    manager.maybe_probe_multimodal = lambda *_args: None
    touched: list[str] = []
    config = SimpleNamespace(
        running=SimpleNamespace(
            max_input_length=256 * 1024,
            context_policy_mode="smart",
        ),
        active_model=None,
    )

    monkeypatch.setattr(
        providers_router,
        "get_agent_for_request",
        lambda *_args, **_kwargs: _record_call("workspace", touched),
    )
    monkeypatch.setattr(
        providers_router,
        "load_agent_config",
        lambda _agent_id: config,
    )
    monkeypatch.setattr(
        providers_router,
        "save_agent_config",
        lambda *_args, **_kwargs: touched.append("save"),
    )
    monkeypatch.setattr(
        providers_router,
        "schedule_agent_reload",
        lambda *_args, **_kwargs: touched.append("reload"),
    )

    with pytest.raises(providers_router.HTTPException) as exc_info:
        await providers_router.set_active_model(
            request=SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace()),
            ),
            manager=manager,
            body=ModelSlotRequest(
                provider_id="target",
                model="blocked-model",
                scope="agent",
                agent_id="agent-1",
            ),
        )

    assert exc_info.value.status_code == 400
    assert "blocked-model" in str(exc_info.value.detail)
    assert "403" in str(exc_info.value.detail)
    assert provider.checked_model_ids == ["blocked-model"]
    assert touched == []


async def _async_value(value):
    return value


async def _record_call(name: str, target: list[str]):
    target.append(name)
    return SimpleNamespace(agent_id="agent-1")
