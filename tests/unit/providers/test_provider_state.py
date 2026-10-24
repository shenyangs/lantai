"""Upgrade contracts: offline parity, user data, retry and idempotency."""
import json
import stat

import pytest

from lantai.providers.provider_state import (
    STATE_VERSION,
    VERSION_KEY,
    migrate_provider_file,
    upgrade_provider_data,
)
from lantai.providers.tokendance_provider import TokenDanceProvider
from lantai.providers.tokendance_catalog import CATALOG
from lantai.exceptions import ProviderError


def legacy():
    return {
        "id": "tokendance",
        "name": "TokenDance",
        "api_key": "encrypted-opaque",
        "base_url": "https://tokendance.space/gateway/v1",
        "unknown": {"keep": True},
        "extra_models": [
            {
                "id": "qwen3.8-flash",
                "name": "My name",
                "show_in_chat": True,
                "user_context_window": 32000,
                "generate_kwargs": {"temperature": 0.3},
            }
        ],
    }


def test_upgrade_preserves_user_fields_and_is_idempotent(tmp_path):
    data = legacy()
    path = tmp_path / "tokendance.json"
    original = json.dumps(data).encode()
    path.write_bytes(original)
    result = migrate_provider_file(path, data)
    assert result["api_key"] == data["api_key"]
    assert result["unknown"] == data["unknown"]
    for key, value in data["extra_models"][0].items():
        assert result["extra_models"][0][key] == value
    assert result["meta"][VERSION_KEY] == STATE_VERSION
    assert upgrade_provider_data(result) == result
    assert migrate_provider_file(path, result) == result
    backup = path.with_suffix(
        f".json.pre-provider-state-v{STATE_VERSION}.bak",
    )
    assert backup.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_failure_keeps_original_and_retries(tmp_path, monkeypatch):
    import lantai.providers.provider_state as module

    path = tmp_path / "tokendance.json"
    data = legacy()
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    with monkeypatch.context() as patch:

        def fail(*args):
            raise OSError("simulated disk failure")

        patch.setattr(module.os, "replace", fail)
        with pytest.raises(OSError):
            migrate_provider_file(path, data)
    assert path.read_bytes() == original
    assert (
        migrate_provider_file(path, data)["meta"][VERSION_KEY]
        == STATE_VERSION
    )


def test_future_version_is_not_downgraded():
    data = legacy()
    data["meta"] = {VERSION_KEY: 999}
    with pytest.raises(ValueError):
        upgrade_provider_data(data)
    assert data["meta"][VERSION_KEY] == 999


def test_legacy_tokendance_key_is_quarantined_until_owner_reauthorizes():
    data = legacy()

    result = upgrade_provider_data(data)

    assert result["api_key"] == "encrypted-opaque"
    assert result["meta"]["tokendance_credential_binding"] == {
        "schema_version": 1,
        "status": "reauthorization_required",
        "owner_subject_id": None,
        "credential_ref": "legacy-unconfirmed",
        "payment_binding_id": "",
        "binding_revision": 0,
        "account_epoch_at_binding": None,
        "ownership_evidence": "legacy_unconfirmed",
        "scopes": [],
    }
    assert upgrade_provider_data(result) == result


def test_stale_read_does_not_overwrite_new_user_edit(tmp_path):
    path = tmp_path / "tokendance.json"
    old = legacy()
    edited = legacy()
    edited["extra_models"][0]["user_context_window"] = 16000
    path.write_text(json.dumps(edited))
    with pytest.raises(RuntimeError):
        migrate_provider_file(path, old)
    assert json.loads(path.read_text()) == edited


def test_live_protocols_win_while_legacy_rows_use_verified_fallback():
    old = legacy()
    fresh = legacy()
    fresh["extra_models"][0]["supported_protocols"] = ["openai:responses"]
    legacy_endpoint = TokenDanceProvider.model_validate(
        upgrade_provider_data(old),
    ).resolve_model_endpoint("qwen3.8-flash")
    live_endpoint = TokenDanceProvider.model_validate(
        fresh,
    ).resolve_model_endpoint("qwen3.8-flash")

    assert legacy_endpoint.native_kind == "openai_compatible"
    assert legacy_endpoint.metadata["protocol_source"] == (
        "verified_release_fallback"
    )
    assert live_endpoint.native_kind == "openai_responses"
    assert live_endpoint.metadata["protocol_source"] == "live_or_configured"
    assert live_endpoint.generate_kwargs["temperature"] == 0.3


def test_other_provider_and_unknown_model_are_untouched():
    data = legacy()
    data["id"] = "custom"
    assert upgrade_provider_data(data) == data
    data["id"] = "tokendance"
    data["extra_models"][0]["id"] = "custom-model"
    assert upgrade_provider_data(data)["extra_models"] == data["extra_models"]


@pytest.mark.parametrize("model_id", sorted(CATALOG))
def test_entire_release_catalog_preserves_live_evidence_and_legacy_route(model_id):
    old = legacy()
    old["extra_models"] = [{"id": model_id, "name": model_id}]
    fresh = legacy()
    fresh["extra_models"] = [
        {"id": model_id, "name": model_id, **CATALOG[model_id]}
    ]
    upgraded = upgrade_provider_data(old)
    providers = [TokenDanceProvider.model_validate(data) for data in (upgraded, fresh)]
    assert providers[0].extra_models[0].supported_protocols == []
    assert providers[1].extra_models[0].supported_protocols == CATALOG[model_id][
        "supported_protocols"
    ]
    assert upgraded["extra_models"] == old["extra_models"]
    verified = upgraded["meta"]["tokendance_verified_capabilities"]
    assert verified["models"][model_id] == CATALOG[model_id]

    def route(provider):
        try:
            endpoint = provider.resolve_model_endpoint(model_id)
            return endpoint.family, endpoint.native_kind, endpoint.base_url
        except ProviderError:
            return "media-only"

    assert route(providers[0]) == route(providers[1])


@pytest.mark.asyncio
async def test_probe_and_runtime_use_same_chat_route(monkeypatch):
    from lantai.providers.openai_provider import OpenAIProvider

    provider = TokenDanceProvider.model_validate(legacy())
    called = []

    async def check(self, model_id, timeout=5):
        called.append(model_id)
        return True, ""

    monkeypatch.setattr(OpenAIProvider, "check_model_connection", check)
    assert (
        provider.resolve_model_endpoint("qwen3.8-flash").native_kind
        == "openai_compatible"
    )
    assert await provider.check_model_connection("qwen3.8-flash") == (True, "")
    assert called == ["qwen3.8-flash"]
