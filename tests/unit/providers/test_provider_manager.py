# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument,protected-access
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import lantai.providers.provider_manager as provider_manager_module
import lantai.providers.tokendance_authorization as tokendance_authorization_module
from lantai.config.config import ModelSlotConfig
from lantai.exceptions import ModelNotFoundException, ProviderError
from lantai.local_models.llamacpp import LlamaCppServerSetupResult
from lantai.providers.anthropic_provider import AnthropicProvider
from lantai.providers.lmstudio_provider import LMStudioProvider
from lantai.providers.ollama_provider import OllamaProvider
from lantai.providers.openai_provider import OpenAIProvider
from lantai.providers.provider import ModelInfo
from lantai.providers.provider_manager import ProviderManager
from lantai.providers.tokendance_provider import TokenDanceProvider
from lantai.providers.tokendance_authorization import (
    TokenDanceAuthorization,
    TokenDanceAuthorizationError,
)

LEGACY_PROVIDER = {
    "providers": {
        "modelscope": {
            "base_url": "https://api-inference.modelscope.cn/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "dashscope": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-test-legacy-secret",
            "extra_models": [{"id": "qwen-plus", "name": "Qwen Plus"}],
            "chat_model": "",
        },
        "aliyun-codingplan": {
            "base_url": "https://coding.dashscope.aliyuncs.com/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "azure-openai": {
            "base_url": "",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "ollama": {
            "base_url": "http://myhost:11434/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
    },
    "custom_providers": {
        "mydash": {
            "id": "mydash",
            "name": "MyDash",
            "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",  # noqa: E501
            "api_key_prefix": "sk-",
            "models": [{"id": "qwen3-max", "name": "qwen3-max"}],
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-test-legacy-custom-secret",
            "chat_model": "OpenAIChatModel",
        },
    },
    "active_llm": {"provider_id": "dashscope", "model": "qwen3-max"},
}


@pytest.fixture
def isolated_secret_dir(monkeypatch, tmp_path):
    secret_dir = tmp_path / ".lantai.secret"
    monkeypatch.setattr(provider_manager_module, "SECRET_DIR", secret_dir)
    return secret_dir


def test_builtin_zhipu_providers_registered(isolated_secret_dir) -> None:
    manager = ProviderManager()

    expected_configs = {
        "zhipu-cn": {
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "support_connection_check": True,
        },
        "zhipu-cn-codingplan": {
            "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
            "support_connection_check": False,
        },
        "zhipu-intl": {
            "base_url": "https://api.z.ai/api/paas/v4",
            "support_connection_check": True,
        },
        "zhipu-intl-codingplan": {
            "base_url": "https://api.z.ai/api/coding/paas/v4",
            "support_connection_check": False,
        },
    }

    for provider_id, expected in expected_configs.items():
        provider = manager.get_provider(provider_id)

        assert provider is not None
        assert isinstance(provider, OpenAIProvider)
        assert provider.base_url == expected["base_url"]
        assert provider.freeze_url is True
        assert (
            provider.support_connection_check
            == expected["support_connection_check"]
        )
        assert [model.id for model in provider.models] == [
            "glm-5",
            "glm-5.1",
            "glm-5-turbo",
            "glm-5v-turbo",
        ]


def test_builtin_minimax_providers_support_connection_check(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    expected_configs = {
        "minimax": "https://api.minimax.io/anthropic",
        "minimax-cn": "https://api.minimaxi.com/anthropic",
    }

    for provider_id, expected_base_url in expected_configs.items():
        provider = manager.get_provider(provider_id)

        assert provider is not None
        assert isinstance(provider, AnthropicProvider)
        assert provider.base_url == expected_base_url
        assert provider.chat_model == "AnthropicChatModel"
        assert provider.freeze_url is True
        assert provider.support_connection_check is True
        assert [model.id for model in provider.models] == [
            "MiniMax-M3",
            "MiniMax-M2.5",
            "MiniMax-M2.5-highspeed",
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
        ]


def test_builtin_tokendance_provider_registered(isolated_secret_dir) -> None:
    manager = ProviderManager()

    provider = manager.get_provider("tokendance")

    assert provider is not None
    assert isinstance(provider, OpenAIProvider)
    assert provider.name == "TokenDance"
    assert provider.base_url == "https://tokendance.space/gateway/v1"
    assert provider.api_key_prefix == ""
    assert provider.freeze_url is True
    assert provider.require_api_key is True
    assert provider.support_model_discovery is True
    assert provider.chat_model == "OpenAIChatModel"
    assert provider.models == []
    assert (
        provider.meta["docs_url"] == "https://tokendance.space/docs/quickstart"
    )
    assert provider.meta["api_key_url"] == "https://tokendance.space/keys"
    assert provider.meta["oauth_mode"] == "api_key_pkce"
    assert provider.meta["oauth_app_url"] == "https://lantai.guanlan.xin"


async def test_builtin_tokendance_provider_info_exposes_setup_links(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    provider = manager.get_provider("tokendance")

    assert provider is not None
    info = await provider.get_info()

    assert info.meta["docs_url"] == "https://tokendance.space/docs/quickstart"
    assert info.meta["api_key_url"] == "https://tokendance.space/keys"
    assert info.meta["oauth_mode"] == "api_key_pkce"


async def test_tokendance_provider_info_hides_binding_identity(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    provider = manager.get_provider("tokendance")
    assert provider is not None
    provider.api_key = "synthetic-bound-key"
    provider.meta["tokendance_credential_binding"] = {
        "schema_version": 1,
        "status": "active",
        "owner_subject_id": "watcha:user-a",
        "credential_ref": "credential-secret-ref",
        "payment_binding_id": "payment-secret-ref",
        "binding_revision": 1,
        "scopes": ["model"],
    }
    monkeypatch.setattr(
        provider_manager_module,
        "resolve_tokendance_authorization",
        lambda _manager, *, use_kind: SimpleNamespace(use_kind=use_kind),
        raising=False,
    )

    info = await manager.get_provider_info("tokendance")

    assert info is not None
    assert "tokendance_credential_binding" not in info.meta
    assert info.meta["credential_status"] == "active"
    serialized = json.dumps(info.model_dump())
    assert "watcha:user-a" not in serialized
    assert "credential-secret-ref" not in serialized
    assert "payment-secret-ref" not in serialized


async def test_tokendance_provider_info_marks_legacy_key_for_reauthorization(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    provider = manager.get_provider("tokendance")
    assert provider is not None
    provider.api_key = "legacy-unbound-key"

    def reject_unbound(_manager, *, use_kind):
        raise TokenDanceAuthorizationError(
            "tokendance_credential_reauthorization_required",
            "reauthorize",
        )

    monkeypatch.setattr(
        provider_manager_module,
        "resolve_tokendance_authorization",
        reject_unbound,
        raising=False,
    )

    [listed] = [
        info
        for info in await manager.list_provider_info()
        if info.id == "tokendance"
    ]

    assert listed.api_key
    assert listed.meta["credential_status"] == "reauthorization_required"


async def test_tokendance_rediscovery_preserves_chat_visibility(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    context = SimpleNamespace(
        subject_id="watcha:user-a",
        account_epoch=4,
    )
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: context,
    )
    monkeypatch.setattr(
        tokendance_authorization_module,
        "load_watcha_account_context",
        lambda: context,
    )
    provider = manager.get_provider("tokendance")
    assert isinstance(provider, TokenDanceProvider)
    provider.extra_models = [
        ModelInfo(
            id="qwen3.8-flash",
            name="Qwen old",
            supported_protocols=["openai:responses"],
            show_in_chat=True,
            use_for_video=True,
            generate_kwargs={"temperature": 0.4},
            user_context_window=32000,
        ),
    ]
    manager.bind_tokendance_credential(
        "synthetic-bound-key",
        expected_owner_subject_id="watcha:user-a",
        expected_account_epoch=4,
        ownership_evidence="oauth_pkce",
    )

    async def fetch_models(_self, _timeout=5):
        return [
            ModelInfo(
                id="qwen3.8-flash",
                name="Qwen current",
                supported_protocols=["openai:responses"],
                show_in_chat=False,
                use_for_video=False,
            ),
        ]

    monkeypatch.setattr(TokenDanceProvider, "fetch_models", fetch_models)

    discovered = await manager.fetch_provider_models("tokendance", save=True)

    assert discovered[0].name == "Qwen current"
    assert provider.extra_models[0].show_in_chat is True
    assert provider.extra_models[0].use_for_video is True
    assert provider.extra_models[0].generate_kwargs == {"temperature": 0.4}
    assert provider.extra_models[0].user_context_window == 32000
    reloaded = ProviderManager().get_provider("tokendance")
    assert reloaded.extra_models[0].generate_kwargs == {"temperature": 0.4}
    assert reloaded.extra_models[0].user_context_window == 32000


async def test_tokendance_rediscovery_discards_stale_result_before_save(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    context = SimpleNamespace(
        subject_id="watcha:user-a",
        account_epoch=4,
    )
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: context,
    )
    monkeypatch.setattr(
        tokendance_authorization_module,
        "load_watcha_account_context",
        lambda: context,
    )
    provider = manager.get_provider("tokendance")
    provider.extra_models = [ModelInfo(id="old-model", name="Old")]
    manager.bind_tokendance_credential(
        "synthetic-bound-key",
        expected_owner_subject_id="watcha:user-a",
        expected_account_epoch=4,
        ownership_evidence="oauth_pkce",
    )

    async def fetch_models(_self, _timeout=5):
        context.subject_id = "watcha:user-b"
        context.account_epoch = 5
        return [ModelInfo(id="new-model", name="New")]

    monkeypatch.setattr(TokenDanceProvider, "fetch_models", fetch_models)

    with pytest.raises(
        TokenDanceAuthorizationError,
        match="授权已变化",
    ):
        await manager.fetch_provider_models(
            "tokendance",
            save=True,
            raise_on_error=True,
        )

    assert [model.id for model in provider.extra_models] == ["old-model"]


async def test_tokendance_multimodal_probe_discards_stale_result_before_save(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    context = SimpleNamespace(
        subject_id="watcha:user-a",
        account_epoch=4,
    )
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: context,
    )
    monkeypatch.setattr(
        tokendance_authorization_module,
        "load_watcha_account_context",
        lambda: context,
    )
    provider = manager.get_provider("tokendance")
    model = ModelInfo(id="vision-model", name="Vision")
    provider.extra_models = [model]
    manager.bind_tokendance_credential(
        "synthetic-bound-key",
        expected_owner_subject_id="watcha:user-a",
        expected_account_epoch=4,
        ownership_evidence="oauth_pkce",
    )

    async def probe_model_multimodal(_self, _model_id, image_only=False):
        del image_only
        context.subject_id = "watcha:user-b"
        context.account_epoch = 5
        return SimpleNamespace(
            supports_image=True,
            supports_video=True,
            supports_multimodal=True,
            image_message="ok",
            video_message="ok",
        )

    monkeypatch.setattr(
        TokenDanceProvider,
        "probe_model_multimodal",
        probe_model_multimodal,
    )

    with pytest.raises(
        TokenDanceAuthorizationError,
        match="授权已变化",
    ):
        await manager.probe_model_multimodal("tokendance", "vision-model")

    assert model.supports_image is None
    assert model.supports_video is None
    assert model.probe_source is None


def test_tokendance_encrypted_upgrade_survives_restart(isolated_secret_dir):
    from lantai.providers.provider_state import STATE_VERSION, VERSION_KEY

    manager = ProviderManager()
    provider = manager.get_provider("tokendance")
    provider.api_key = "synthetic-upgrade-test-key"
    provider.extra_models = [
        ModelInfo(id="qwen3.8-flash", name="Qwen", show_in_chat=True),
    ]
    manager._save_provider(provider, is_builtin=True)
    path = manager.builtin_path / "tokendance.json"
    raw = json.loads(path.read_text())
    cipher = raw["api_key"]
    assert cipher != provider.api_key
    raw["unknown-user-field"] = {"preserve": True}
    raw["meta"].pop(VERSION_KEY, None)
    path.write_text(json.dumps(raw))
    reloaded = ProviderManager().get_provider("tokendance")
    assert reloaded.api_key == provider.api_key
    assert reloaded.extra_models[0].show_in_chat is True
    assert reloaded.meta[VERSION_KEY] == STATE_VERSION
    migrated = json.loads(path.read_text())
    assert migrated["api_key"] == cipher
    assert migrated["unknown-user-field"] == {"preserve": True}
    original = path.read_bytes()
    ProviderManager()
    assert path.read_bytes() == original


def test_tokendance_binding_persists_key_and_owner_in_one_write(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: SimpleNamespace(
            subject_id="watcha:user-a",
            account_epoch=4,
        ),
    )

    binding = manager.bind_tokendance_credential(
        "synthetic-bound-key",
        expected_owner_subject_id="watcha:user-a",
        expected_account_epoch=4,
        ownership_evidence="oauth_pkce",
    )

    provider = manager.get_provider("tokendance")
    assert provider.api_key == "synthetic-bound-key"
    assert provider.meta["tokendance_credential_binding"] == binding
    assert binding["owner_subject_id"] == "watcha:user-a"
    assert binding["account_epoch_at_binding"] == 4
    assert binding["binding_revision"] == 1
    assert binding["scopes"] == ["balance", "model", "payment", "usage"]
    raw = json.loads(
        (manager.builtin_path / "tokendance.json").read_text(),
    )
    assert raw["api_key"] != "synthetic-bound-key"
    assert raw["meta"]["tokendance_credential_binding"] == binding


def test_tokendance_binding_rejects_changed_watcha_context(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: SimpleNamespace(
            subject_id="watcha:user-b",
            account_epoch=5,
        ),
    )

    with pytest.raises(ValueError, match="account context changed"):
        manager.bind_tokendance_credential(
            "must-not-be-saved",
            expected_owner_subject_id="watcha:user-a",
            expected_account_epoch=4,
            ownership_evidence="oauth_pkce",
        )

    assert manager.get_provider("tokendance").api_key == ""
    assert not (manager.builtin_path / "tokendance.json").exists()


def test_tokendance_payment_session_owner_persists_and_is_binding_scoped(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    context = SimpleNamespace(
        subject_id="watcha:user-a",
        account_epoch=4,
    )
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: context,
    )
    monkeypatch.setattr(
        tokendance_authorization_module,
        "load_watcha_account_context",
        lambda: context,
    )
    binding = manager.bind_tokendance_credential(
        "synthetic-bound-key",
        expected_owner_subject_id="watcha:user-a",
        expected_account_epoch=4,
        ownership_evidence="oauth_pkce",
    )
    authorization = TokenDanceAuthorization(
        owner_subject_id="watcha:user-a",
        account_epoch=4,
        credential_ref=binding["credential_ref"],
        payment_binding_id=binding["payment_binding_id"],
        binding_revision=binding["binding_revision"],
        use_kind="payment",
        api_key="synthetic-bound-key",
    )

    manager.record_tokendance_payment_session("pay_1", authorization)
    reloaded = ProviderManager()
    reloaded.assert_tokendance_payment_session_owner("pay_1", authorization)

    changed_binding = TokenDanceAuthorization(
        owner_subject_id="watcha:user-a",
        account_epoch=4,
        credential_ref="replacement-credential",
        payment_binding_id="replacement-payment-binding",
        binding_revision=2,
        use_kind="payment",
        api_key="replacement-key",
    )
    with pytest.raises(
        TokenDanceAuthorizationError,
        match="授权已变化",
    ):
        reloaded.assert_tokendance_payment_session_owner(
            "pay_1",
            changed_binding,
        )


def test_tokendance_payment_intent_is_durable_and_idempotent(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    context = SimpleNamespace(subject_id="watcha:user-a", account_epoch=4)
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: context,
    )
    monkeypatch.setattr(
        tokendance_authorization_module,
        "load_watcha_account_context",
        lambda: context,
    )
    binding = manager.bind_tokendance_credential(
        "synthetic-bound-key",
        expected_owner_subject_id="watcha:user-a",
        expected_account_epoch=4,
        ownership_evidence="oauth_pkce",
    )
    authorization = TokenDanceAuthorization(
        owner_subject_id="watcha:user-a",
        account_epoch=4,
        credential_ref=binding["credential_ref"],
        payment_binding_id=binding["payment_binding_id"],
        binding_revision=binding["binding_revision"],
        use_kind="payment",
        api_key="synthetic-bound-key",
    )

    intent, created = manager.prepare_tokendance_payment_intent(
        "tdpi_0123456789abcdef",
        199,
        authorization,
    )
    assert created is True
    assert intent["status"] == "prepared"
    same, created_again = ProviderManager().prepare_tokendance_payment_intent(
        "tdpi_0123456789abcdef",
        199,
        authorization,
    )
    assert created_again is False
    assert same["status"] == "prepared"

    manager.complete_tokendance_payment_intent(
        "tdpi_0123456789abcdef",
        {
            "id": "pay_1",
            "amount": 199,
            "status": "pending",
            "payment_url": "https://tokendance.space/pay/pay_1",
            "expired_at": 1_789_300_800,
            "created_at": 1_789_299_900,
        },
        authorization,
    )
    completed, created_after_complete = (
        ProviderManager().prepare_tokendance_payment_intent(
            "tdpi_0123456789abcdef",
            199,
            authorization,
        )
    )
    assert created_after_complete is False
    assert completed["status"] == "submitted"
    assert completed["session"]["id"] == "pay_1"

    with pytest.raises(
        TokenDanceAuthorizationError,
        match="充值金额",
    ):
        manager.prepare_tokendance_payment_intent(
            "tdpi_0123456789abcdef",
            499,
            authorization,
        )


def test_tokendance_payment_session_rejects_stale_account_context(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    context = SimpleNamespace(
        subject_id="watcha:user-a",
        account_epoch=4,
    )
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: context,
    )
    monkeypatch.setattr(
        tokendance_authorization_module,
        "load_watcha_account_context",
        lambda: context,
    )
    binding = manager.bind_tokendance_credential(
        "synthetic-bound-key",
        expected_owner_subject_id="watcha:user-a",
        expected_account_epoch=4,
        ownership_evidence="oauth_pkce",
    )
    authorization = TokenDanceAuthorization(
        owner_subject_id="watcha:user-a",
        account_epoch=4,
        credential_ref=binding["credential_ref"],
        payment_binding_id=binding["payment_binding_id"],
        binding_revision=binding["binding_revision"],
        use_kind="payment",
        api_key="synthetic-bound-key",
    )
    manager.record_tokendance_payment_session("pay_1", authorization)

    context.subject_id = "watcha:user-b"
    context.account_epoch = 5

    with pytest.raises(
        TokenDanceAuthorizationError,
        match="授权已变化",
    ):
        manager.assert_tokendance_payment_session_owner(
            "pay_1",
            authorization,
        )


def test_tokendance_revoke_clears_binding_and_payment_sessions(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    context = SimpleNamespace(
        subject_id="watcha:user-a",
        account_epoch=4,
    )
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: context,
    )
    monkeypatch.setattr(
        tokendance_authorization_module,
        "load_watcha_account_context",
        lambda: context,
    )
    binding = manager.bind_tokendance_credential(
        "synthetic-bound-key",
        expected_owner_subject_id="watcha:user-a",
        expected_account_epoch=4,
        ownership_evidence="oauth_pkce",
    )
    authorization = TokenDanceAuthorization(
        owner_subject_id="watcha:user-a",
        account_epoch=4,
        credential_ref=binding["credential_ref"],
        payment_binding_id=binding["payment_binding_id"],
        binding_revision=binding["binding_revision"],
        use_kind="payment",
        api_key="synthetic-bound-key",
    )
    manager.record_tokendance_payment_session("pay_1", authorization)

    assert manager.update_provider("tokendance", {"api_key": ""}) is True

    provider = manager.get_provider("tokendance")
    assert provider.api_key == ""
    assert "tokendance_credential_binding" not in provider.meta
    assert "tokendance_payment_sessions" not in provider.meta

    reloaded = ProviderManager().get_provider("tokendance")
    assert reloaded.api_key == ""
    assert "tokendance_credential_binding" not in reloaded.meta
    assert "tokendance_payment_sessions" not in reloaded.meta


def test_tokendance_key_cannot_bypass_owner_binding(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    with pytest.raises(ValueError, match="bind_tokendance_credential"):
        manager.update_provider(
            "tokendance",
            {"api_key": "unbound-key"},
        )

    assert manager.get_provider("tokendance").api_key == ""


def test_tokendance_disk_failure_does_not_hide_config(
    isolated_secret_dir,
    monkeypatch,
):
    import lantai.providers.provider_state as state

    manager = ProviderManager()
    provider = manager.get_provider("tokendance")
    provider.api_key = "synthetic-upgrade-test-key"
    provider.extra_models = [ModelInfo(id="qwen3.8-flash", name="Qwen")]
    manager._save_provider(provider, is_builtin=True)
    path = manager.builtin_path / "tokendance.json"
    original = path.read_bytes()

    def fail(*args):
        raise OSError("simulated full disk")

    monkeypatch.setattr(state.os, "replace", fail)
    loaded = manager.load_provider("tokendance", is_builtin=True)
    assert loaded.api_key == provider.api_key
    assert (
        loaded.resolve_model_endpoint("qwen3.8-flash").native_kind
        == "openai_compatible"
    )
    assert path.read_bytes() == original


async def test_model_chat_visibility_can_be_updated(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    provider = manager.get_provider("tokendance")
    assert isinstance(provider, TokenDanceProvider)
    provider.extra_models = [
        ModelInfo(
            id="qwen3.8-flash",
            name="Qwen",
            supported_protocols=["openai:responses"],
            show_in_chat=False,
        ),
    ]

    await manager.update_model_config(
        "tokendance",
        "qwen3.8-flash",
        {"show_in_chat": True},
    )

    assert provider.extra_models[0].show_in_chat is True


async def test_provider_verification_receipt_is_revision_bound_and_persisted(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    provider = manager.get_provider("openai")
    assert provider is not None

    manager.update_provider("openai", {"api_key": "sk-first"})
    revision = provider.configuration_revision
    receipt = await manager.record_provider_verification(
        "openai",
        "gpt-5-mini",
    )

    assert receipt.connection_verified_revision == revision
    assert receipt.model_verified_revision == revision
    assert receipt.model_verified_id == "gpt-5-mini"

    manager.update_provider("openai", {"api_key": "sk-first"})
    assert provider.configuration_revision == revision
    manager.update_provider("openai", {"api_key": "sk-second"})
    assert provider.configuration_revision == revision + 1
    assert provider.model_verified_revision == revision

    reloaded = ProviderManager().get_provider("openai")
    assert reloaded is not None
    assert reloaded.configuration_revision == revision + 1
    assert reloaded.model_verified_revision == revision
    assert reloaded.model_verified_id == "gpt-5-mini"


async def test_add_custom_provider_and_reload_from_storage(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    custom = OpenAIProvider(
        id="custom-openai",
        name="Custom OpenAI",
        base_url="https://custom.example/v1",
        api_key="sk-custom",
        models=[ModelInfo(id="custom-model", name="Custom Model")],
    )

    created = await manager.add_custom_provider(custom)
    builtin_conflict = await manager.add_custom_provider(
        OpenAIProvider(
            id="openai",
            name="Conflict OpenAI",
        ),
    )
    duplicate = await manager.add_custom_provider(custom)

    reloaded = ProviderManager()
    loaded = reloaded.get_provider("custom-openai")
    loaded_builtin_conflict = reloaded.get_provider("openai-custom")
    loaded_duplicate = reloaded.get_provider("custom-openai-new")

    assert created.id == "custom-openai"
    assert builtin_conflict.id == "openai-custom"
    assert duplicate.id == "custom-openai-new"
    assert loaded is not None
    assert isinstance(loaded, OpenAIProvider)
    assert loaded.is_custom is True
    assert loaded.base_url == "https://custom.example/v1"
    assert loaded.api_key == "sk-custom"
    assert [m.id for m in loaded.models] == ["custom-model"]
    assert loaded_builtin_conflict is not None
    assert isinstance(loaded_builtin_conflict, OpenAIProvider)
    assert loaded_duplicate is not None
    assert isinstance(loaded_duplicate, OpenAIProvider)


async def test_activate_provider_persists_active_model(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(id="ok", request=kwargs)

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
    )

    monkeypatch.setattr(
        OpenAIProvider,
        "_client",
        lambda self, timeout=5: fake_client,
    )

    manager.update_provider("openai", {"api_key": "sk-test"})
    await manager.activate_model("openai", "gpt-5")

    assert manager.active_model is not None
    assert manager.active_model.provider_id == "openai"
    assert manager.active_model.model == "gpt-5"

    reloaded = ProviderManager()
    assert reloaded.active_model is not None
    assert reloaded.active_model.provider_id == "openai"
    assert reloaded.active_model.model == "gpt-5"


async def test_activate_provider_rejects_unconfigured_required_key(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    manager.update_provider("openai", {"api_key": ""})

    with pytest.raises(ProviderError, match="not configured or available"):
        await manager.activate_model("openai", "gpt-5")

    assert manager.active_model is None


def test_does_not_guess_between_kimi_plan_model_routes(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    manager.update_provider("openai", {"api_key": ""})
    manager.update_provider("kimi-codingplan", {"api_key": "sk-kimi"})

    reloaded = ProviderManager()

    assert reloaded.active_model is None
    assert not (
        isolated_secret_dir / "providers" / "active_model.json"
    ).exists()


def test_does_not_auto_select_when_multiple_model_slots_are_available(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    manager.update_provider("kimi-codingplan", {"api_key": ""})
    manager.update_provider("openai", {"api_key": "sk-openai"})

    reloaded = ProviderManager()

    assert reloaded.active_model is None
    assert not (
        isolated_secret_dir / "providers" / "active_model.json"
    ).exists()


def test_stale_model_is_cleared_when_multiple_kimi_routes_are_available(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    manager.update_provider("openai", {"api_key": ""})
    manager.update_provider("kimi-codingplan", {"api_key": "sk-kimi"})

    active_path = isolated_secret_dir / "providers" / "active_model.json"
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_text(
        json.dumps(
            {
                "provider_id": "openai",
                "model": "gpt-5",
            },
        ),
        encoding="utf-8",
    )

    reloaded = ProviderManager()

    assert reloaded.active_model is None


def test_model_slot_availability_tracks_removed_provider_key(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    manager.update_provider("openai", {"api_key": ""})
    slot = ModelSlotConfig(provider_id="openai", model="gpt-5")

    assert manager.is_model_slot_available(slot) is False

    manager.update_provider("openai", {"api_key": "sk-test"})
    assert manager.is_model_slot_available(slot) is True

    manager.update_provider("openai", {"api_key": ""})
    assert manager.is_model_slot_available(slot) is False


def test_tokendance_model_slot_requires_current_identity_binding(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    provider = manager.get_provider("tokendance")
    assert provider is not None
    provider.api_key = "legacy-unbound-key"
    provider.extra_models = [ModelInfo(id="qwen3.8-flash", name="Qwen")]
    slot = ModelSlotConfig(
        provider_id="tokendance",
        model="qwen3.8-flash",
    )

    def reject_unbound(_manager, *, use_kind):
        assert use_kind == "model"
        raise TokenDanceAuthorizationError(
            "tokendance_credential_reauthorization_required",
            "reauthorize",
        )

    monkeypatch.setattr(
        provider_manager_module,
        "resolve_tokendance_authorization",
        reject_unbound,
        raising=False,
    )

    assert manager.is_model_slot_available(slot) is False

    monkeypatch.setattr(
        provider_manager_module,
        "resolve_tokendance_authorization",
        lambda _manager, *, use_kind: SimpleNamespace(use_kind=use_kind),
        raising=False,
    )

    assert manager.is_model_slot_available(slot) is True


def test_corrupted_encrypted_provider_key_is_not_exposed_to_runtime(
    isolated_secret_dir,
) -> None:
    provider_path = (
        isolated_secret_dir / "providers" / "builtin" / "openai.json"
    )
    provider_path.parent.mkdir(parents=True, exist_ok=True)
    provider_path.write_text(
        json.dumps(
            {
                "id": "openai",
                "api_key": "ENC:corrupted-provider-key",
            },
        ),
        encoding="utf-8",
    )

    manager = ProviderManager()
    provider = manager.get_provider("openai")
    slot = ModelSlotConfig(provider_id="openai", model="gpt-5")

    assert provider is not None
    assert provider.api_key == ""
    assert manager.is_model_slot_available(slot) is False


async def test_resume_local_model_restores_server_and_runtime_state(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    model_id = f"{'Agent' + 'Scope'}/Lantai-Flash-2B-Q4_K_M"
    manager.update_provider(
        "lantai-local",
        {
            "base_url": "http://127.0.0.1:9000/v1",
            "extra_models": [
                {
                    "id": model_id,
                    "name": model_id,
                },
            ],
        },
    )
    manager.active_model = ModelSlotConfig(
        provider_id="lantai-local",
        model=model_id,
    )
    manager.save_active_model(manager.active_model)

    class FakeLocalManager:
        def __init__(self) -> None:
            self.restored_model_id = None

        def check_llamacpp_installation(self) -> tuple[bool, str]:
            return True, ""

        def is_model_downloaded(self, requested_model_id: str) -> bool:
            return requested_model_id == model_id

        async def setup_server(
            self,
            requested_model_id: str,
        ) -> LlamaCppServerSetupResult:
            self.restored_model_id = requested_model_id
            return LlamaCppServerSetupResult(
                port=43111,
                model_info=ModelInfo(
                    id=requested_model_id,
                    name=requested_model_id,
                    supports_multimodal=True,
                    supports_image=True,
                    supports_video=True,
                    probe_source="documentation",
                ),
            )

    local_manager = FakeLocalManager()

    await manager._resume_local_model(local_manager)

    provider = manager.get_provider("lantai-local")

    assert local_manager.restored_model_id == model_id
    assert provider is not None
    assert provider.base_url == "http://127.0.0.1:43111/v1"
    assert [model.id for model in provider.extra_models] == [model_id]
    assert provider.extra_models[0].supports_multimodal is True
    assert provider.extra_models[0].supports_image is True
    assert provider.extra_models[0].supports_video is True
    assert provider.extra_models[0].probe_source == "documentation"


async def test_remove_custom_provider_missing_file_is_safe(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    custom = OpenAIProvider(
        id="custom-to-remove",
        name="Custom To Remove",
        base_url="https://remove.example/v1",
        api_key="sk-remove",
    )
    await manager.add_custom_provider(custom)

    custom_path = manager.custom_path / "custom-to-remove.json"
    custom_path.unlink()

    manager.remove_custom_provider("custom-to-remove")

    assert manager.get_provider("custom-to-remove") is None


def test_load_provider_invalid_json_returns_none(isolated_secret_dir) -> None:
    manager = ProviderManager()
    bad_file = manager.custom_path / "bad-provider.json"
    bad_file.write_text("{invalid-json", encoding="utf-8")

    loaded = manager.load_provider("bad-provider", is_builtin=False)

    assert loaded is None


def test_migrate_legacy_file_and_persist_active_model(
    isolated_secret_dir,
) -> None:
    isolated_secret_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = isolated_secret_dir / "providers.json"
    legacy_file.write_text(
        json.dumps(
            LEGACY_PROVIDER,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    manager = ProviderManager()

    assert legacy_file.exists() is False
    assert manager.active_model is not None
    assert manager.active_model.provider_id == "dashscope"
    assert manager.active_model.model == "qwen3-max"

    dashscope_provider = manager.get_provider("dashscope")
    assert dashscope_provider is not None
    assert dashscope_provider.api_key == "sk-test-legacy-secret"

    legacy_custom = manager.get_provider("mydash")
    assert legacy_custom is not None
    assert isinstance(legacy_custom, OpenAIProvider)
    assert len(legacy_custom.extra_models) == 1
    assert legacy_custom.extra_models[0].id == "qwen3-max"
    assert legacy_custom.api_key == "sk-test-legacy-custom-secret"

    legacy_ollama = manager.get_provider("ollama")
    assert legacy_ollama.base_url == "http://myhost:11434"

    active_model_file = isolated_secret_dir / "providers" / "active_model.json"
    assert active_model_file.exists()


def test_local_provider_deserialization_preserves_native_implementations(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    ollama = manager._provider_from_data(
        {
            "id": "ollama",
            "name": "Ollama",
            "base_url": "http://localhost:11434/v1",
        },
    )
    lmstudio = manager._provider_from_data(
        {
            "id": "lmstudio",
            "name": "LM Studio",
            "base_url": "http://localhost:1234",
        },
    )

    assert isinstance(ollama, OllamaProvider)
    assert ollama.base_url == "http://localhost:11434"
    assert isinstance(lmstudio, LMStudioProvider)
    assert lmstudio.base_url == "http://localhost:1234/v1"


async def test_add_custom_provider_conflict_resolution_loops_until_unique(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    conflict = OpenAIProvider(
        id="openai",
        name="Conflict OpenAI",
    )

    first = await manager.add_custom_provider(conflict)
    second = await manager.add_custom_provider(conflict)
    third = await manager.add_custom_provider(conflict)

    assert first.id == "openai-custom"
    assert second.id == "openai-custom-new"
    assert third.id == "openai-custom-new-new"

    assert manager.get_provider("openai-custom") is not None
    assert manager.get_provider("openai-custom-new") is not None
    assert manager.get_provider("openai-custom-new-new") is not None


def test_update_provider_for_builtin_persists_to_builtin_path(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    ok = manager.update_provider(
        "openai",
        {
            "base_url": "https://updated.example/v1",  # not taken effect
            "api_key": "sk-updated",
        },
    )

    assert ok is True
    persisted = manager.load_provider("openai", is_builtin=True)
    assert persisted is not None
    assert isinstance(persisted, OpenAIProvider)
    assert persisted.base_url == "https://api.openai.com/v1"
    assert persisted.api_key == "sk-updated"

    ok = manager.update_provider(
        "azure-openai",
        {
            "base_url": "https://azure-updated.example/v1",
            "api_key": "sk-azure-updated",
        },
    )
    assert ok is True
    persisted_azure = manager.load_provider("azure-openai", is_builtin=True)
    assert persisted_azure is not None
    assert isinstance(persisted_azure, OpenAIProvider)
    assert persisted_azure.base_url == "https://azure-updated.example/v1"
    assert persisted_azure.api_key == "sk-azure-updated"


def test_update_provider_for_unknown_returns_false(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    ok = manager.update_provider("unknown-provider", {"api_key": "sk-x"})

    assert ok is False


async def test_activate_provider_invalid_provider_raises(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    with pytest.raises(ProviderError, match="Provider 'missing' not found"):
        await manager.activate_model("missing", "gpt-5")


async def test_activate_provider_invalid_model_raises(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    with pytest.raises(ModelNotFoundException, match="not-exists"):
        await manager.activate_model("openai", "not-exists")


async def test_activate_provider_rejects_tokendance_media_only_model(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    provider = manager.get_provider("tokendance")
    assert isinstance(provider, TokenDanceProvider)
    provider.api_key = "configured"
    provider.extra_models = [
        ModelInfo(
            id="seedream-5.0-pro",
            name="Seedream",
            supported_protocols=["ark:image-generations"],
        ),
    ]

    with pytest.raises(ProviderError, match="not a chat model"):
        await manager.activate_model("tokendance", "seedream-5.0-pro")


async def test_add_model_to_provider_duplicate_id_raises(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    model_info = ModelInfo(id="custom-duplicate", name="Custom Duplicate")

    provider = await manager.add_model_to_provider("openai", model_info)

    assert [m.id for m in provider.extra_models].count("custom-duplicate") == 1

    with pytest.raises(ProviderError, match="already exists"):
        await manager.add_model_to_provider("openai", model_info)

    reloaded = ProviderManager()
    reloaded_provider = reloaded.get_provider("openai")

    assert reloaded_provider is not None
    assert reloaded_provider.extra_models is not None
    assert [m.id for m in reloaded_provider.extra_models].count(
        "custom-duplicate",
    ) == 1


def test_save_provider_skip_if_exists_does_not_overwrite(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    provider = OpenAIProvider(
        id="custom-skip",
        name="Original",
        api_key="sk-original",
    )
    manager._save_provider(provider, is_builtin=False)

    provider.name = "Changed"
    provider.api_key = "sk-changed"
    manager._save_provider(provider, is_builtin=False, skip_if_exists=True)

    loaded = manager.load_provider("custom-skip", is_builtin=False)
    assert loaded is not None
    assert loaded.name == "Original"
    assert loaded.api_key == "sk-original"


def test_load_provider_missing_returns_none(isolated_secret_dir) -> None:
    manager = ProviderManager()

    loaded = manager.load_provider("not-found", is_builtin=False)

    assert loaded is None


def test_model_fallback_prefers_distinct_active_selection(monkeypatch) -> None:
    manager = object.__new__(ProviderManager)
    primary = ModelSlotConfig(provider_id="tokendance", model="expired-model")
    active = ModelSlotConfig(provider_id="deepseek", model="deepseek-chat")
    manager.active_model = active
    monkeypatch.setattr(manager, "is_chat_model_slot_selectable", lambda slot: True)

    assert manager.get_model_fallback_slot(primary) == active


def test_model_fallback_prefers_stable_model_when_active_is_primary(
    monkeypatch,
) -> None:
    manager = object.__new__(ProviderManager)
    primary = ModelSlotConfig(provider_id="tokendance", model="primary")
    preview = ModelSlotConfig(provider_id="tokendance", model="next-preview")
    stable = ModelSlotConfig(provider_id="deepseek", model="deepseek-chat")
    manager.active_model = primary
    monkeypatch.setattr(manager, "is_chat_model_slot_selectable", lambda slot: True)
    monkeypatch.setattr(
        manager,
        "_iter_available_model_slots",
        lambda: iter((primary, preview, stable)),
    )

    assert manager.get_model_fallback_slot(primary) == stable


def test_provider_from_data_dispatch_to_anthropic(isolated_secret_dir) -> None:
    manager = ProviderManager()

    provider = manager._provider_from_data(
        {
            "id": "custom-anthropic",
            "name": "Custom Anthropic",
            "chat_model": "AnthropicChatModel",
            "api_key": "sk-ant-x",
        },
    )

    assert isinstance(provider, AnthropicProvider)


def test_provider_from_data_fallback_to_openai(isolated_secret_dir) -> None:
    manager = ProviderManager()

    provider = manager._provider_from_data(
        {
            "id": "custom-openai-like",
            "name": "OpenAI Like",
            "base_url": "https://custom.example/v1",
        },
    )

    assert isinstance(provider, OpenAIProvider)


def test_init_from_storage_migrates_with_different_provider(
    isolated_secret_dir,
) -> None:
    builtin_path = isolated_secret_dir / "providers" / "builtin"
    builtin_path.mkdir(parents=True, exist_ok=True)

    legacy_minimax_provider = {
        "id": "minimax",
        "name": "MiniMax",
        "base_url": "https://api.minimax.io/v1",
        "api_key": "sk-legacy-minimax",
        "chat_model": "OpenAIChatModel",
        "models": [{"id": "MiniMax-M2.5", "name": "MiniMax M2.5"}],
        "generate_kwargs": {"temperature": 1.0},
    }
    (builtin_path / "minimax.json").write_text(
        json.dumps(legacy_minimax_provider, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    manager = ProviderManager()

    provider = manager.get_provider("minimax")

    assert provider is not None
    assert isinstance(provider, AnthropicProvider)
    # url / name / chatmodel should be updated
    assert provider.base_url == "https://api.minimax.io/anthropic"
    assert provider.chat_model == "AnthropicChatModel"
    assert provider.name == "MiniMax (International)"
    # api key should be preserved
    assert provider.api_key == "sk-legacy-minimax"

    from lantai.agents.legacy_model_components import AnthropicChatModel

    assert provider.get_chat_model_cls() == AnthropicChatModel

    legacy_ollama_provider = {
        "id": "ollama",
        "name": "Ollama New",
        "base_url": "http://legacy-ollama:11434",
        "api_key": "sk-legacy-ollama",
        "chat_model": "OpenAIChatModel",
        "models": [],
    }
    (builtin_path / "ollama.json").write_text(
        json.dumps(legacy_ollama_provider, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manager = ProviderManager()
    assert manager.get_provider("ollama") is not None
    assert (
        manager.get_provider("ollama").base_url == "http://legacy-ollama:11434"
    )


@pytest.mark.asyncio
async def test_builtin_model_user_context_override_round_trips(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    await manager.update_model_config(
        "openai",
        "gpt-5",
        {"user_context_window": 300_000},
    )

    reloaded = ProviderManager()
    model = next(
        item
        for item in reloaded.get_provider("openai").models
        if item.id == "gpt-5"
    )
    assert model.user_context_window == 300_000
