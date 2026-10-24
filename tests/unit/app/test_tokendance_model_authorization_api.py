"""Identity fencing for TokenDance model-side account requests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app.routers import providers as providers_module
from lantai.providers import tokendance_authorization


def _binding() -> dict:
    return {
        "tokendance_credential_binding": {
            "schema_version": 1,
            "status": "active",
            "owner_subject_id": "watcha:test-user",
            "credential_ref": "cred-1",
            "payment_binding_id": "payment-1",
            "binding_revision": 1,
            "account_epoch_at_binding": 1,
            "ownership_evidence": "oauth_pkce",
            "scopes": ["model"],
        },
    }


class _Provider:
    id = "tokendance"
    api_key = "bound-key"
    base_url = "https://api.tokendance.space/v1"
    chat_model = "OpenAIChatModel"
    generate_kwargs: dict = {}
    support_connection_check = True
    models: list = []
    extra_models: list = []

    def __init__(self) -> None:
        self.meta = _binding()
        self.connection_calls = 0
        self.model_connection_hook = None

    async def check_connection(self):
        self.connection_calls += 1
        return False, "synthetic failure"

    async def check_model_connection(self, _model_id, timeout=None):
        del timeout
        if self.model_connection_hook is not None:
            self.model_connection_hook()
        return True, "ok"

    def has_model(self, _model_id: str) -> bool:
        return True

    def get_model_info(self, _model_id: str):
        return SimpleNamespace(show_in_chat=True)


class _Manager:
    def __init__(self) -> None:
        self.provider = _Provider()
        self.update_calls = 0
        self.fetch_hook = None
        self.fetch_save_values: list[bool] = []
        self.persist_calls = 0
        self.multimodal_probe_hook = None

    def get_provider(self, provider_id: str):
        return self.provider if provider_id == "tokendance" else None

    def update_provider(self, _provider_id: str, _config: dict) -> bool:
        self.update_calls += 1
        return True

    async def fetch_provider_models(self, *_args, **_kwargs):
        self.fetch_save_values.append(bool(_kwargs.get("save")))
        if self.fetch_hook is not None:
            self.fetch_hook()
        return []

    def save_discovered_models(self, _provider_id: str, _models: list):
        self.persist_calls += 1

    def is_model_slot_available(self, _slot) -> bool:
        return True

    async def probe_model_multimodal(self, _provider_id: str, _model_id: str):
        if self.multimodal_probe_hook is not None:
            self.multimodal_probe_hook()
        return {
            "supports_image": True,
            "supports_video": False,
            "supports_multimodal": True,
            "image_message": "ok",
            "video_message": "not supported",
        }


@pytest.fixture
def watcha_context(monkeypatch):
    context = SimpleNamespace(
        subject_id="watcha:test-user",
        account_epoch=1,
    )
    monkeypatch.setattr(
        tokendance_authorization,
        "load_watcha_account_context",
        lambda: context,
    )
    monkeypatch.setattr(
        providers_module,
        "is_watcha_account_connected",
        lambda: context.subject_id is not None,
    )
    return context


@pytest.mark.asyncio
async def test_connection_rejects_unbound_candidate_key_before_request(
    watcha_context,
) -> None:
    del watcha_context
    manager = _Manager()

    with pytest.raises(HTTPException) as raised:
        await providers_module.test_provider(
            manager=manager,
            provider_id="tokendance",
            body=providers_module.TestProviderRequest(api_key="candidate-key"),
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "tokendance_key_not_bound"
    assert manager.provider.connection_calls == 0


@pytest.mark.asyncio
async def test_discovery_discards_result_after_watcha_context_changes(
    watcha_context,
) -> None:
    manager = _Manager()

    def switch_account() -> None:
        watcha_context.subject_id = "watcha:other-user"
        watcha_context.account_epoch = 2

    manager.fetch_hook = switch_account

    with pytest.raises(HTTPException) as raised:
        await providers_module.discover_models(
            manager=manager,
            provider_id="tokendance",
            body=None,
            save=False,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "authorization_context_stale"
    assert manager.update_calls == 0
    assert manager.fetch_save_values == [False]
    assert manager.persist_calls == 0


@pytest.mark.asyncio
async def test_discovery_preserves_authorization_error_from_manager(
    watcha_context,
) -> None:
    del watcha_context
    manager = _Manager()

    async def stale_fetch(*_args, **_kwargs):
        raise tokendance_authorization.TokenDanceAuthorizationError(
            "authorization_context_stale",
            "stale",
            status_code=409,
        )

    manager.fetch_provider_models = stale_fetch

    with pytest.raises(HTTPException) as raised:
        await providers_module.discover_models(
            manager=manager,
            provider_id="tokendance",
            body=None,
            save=False,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "authorization_context_stale"
    assert manager.persist_calls == 0


@pytest.mark.asyncio
async def test_discovery_persists_only_after_authorization_recheck(
    watcha_context,
) -> None:
    del watcha_context
    manager = _Manager()

    response = await providers_module.discover_models(
        manager=manager,
        provider_id="tokendance",
        body=None,
        save=True,
    )

    assert response.success is True
    assert manager.fetch_save_values == [False]
    assert manager.persist_calls == 1


@pytest.mark.asyncio
async def test_model_activation_probe_discards_stale_authorization(
    watcha_context,
) -> None:
    manager = _Manager()

    def switch_account() -> None:
        watcha_context.subject_id = "watcha:other-user"
        watcha_context.account_epoch = 2

    manager.provider.model_connection_hook = switch_account

    with pytest.raises(HTTPException) as raised:
        await providers_module._validate_model_access(
            manager,
            "tokendance",
            "qwen3.8-flash",
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "authorization_context_stale"


@pytest.mark.asyncio
async def test_compatibility_probe_discards_stale_authorization(
    watcha_context,
    monkeypatch,
) -> None:
    manager = _Manager()
    monkeypatch.setattr(
        providers_module,
        "_create_compatibility_model_client",
        lambda *_args: object(),
    )

    async def stale_probe(**_kwargs):
        watcha_context.subject_id = "watcha:other-user"
        watcha_context.account_epoch = 2
        return object()

    monkeypatch.setattr(
        providers_module,
        "run_model_compatibility_probe",
        stale_probe,
    )

    with pytest.raises(HTTPException) as raised:
        await providers_module.test_model_compatibility(
            manager=manager,
            provider_id="tokendance",
            body=providers_module.TestModelRequest(model_id="qwen3.8-flash"),
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "authorization_context_stale"


@pytest.mark.asyncio
async def test_multimodal_probe_discards_stale_authorization(
    watcha_context,
) -> None:
    manager = _Manager()

    def switch_account() -> None:
        watcha_context.subject_id = "watcha:other-user"
        watcha_context.account_epoch = 2

    manager.multimodal_probe_hook = switch_account

    with pytest.raises(HTTPException) as raised:
        await providers_module.probe_model_multimodal(
            manager=manager,
            provider_id="tokendance",
            model_id="qwen3.8-flash",
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "authorization_context_stale"
