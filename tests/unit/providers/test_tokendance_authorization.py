from __future__ import annotations

from types import SimpleNamespace

import pytest

from lantai.providers.tokendance_authorization import (
    TokenDanceAuthorizationError,
    assert_tokendance_authorization_current,
    resolve_tokendance_authorization,
)
from lantai.security import cloud_account_store


class _Manager:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get_provider(self, provider_id: str):
        return self.provider if provider_id == "tokendance" else None


def test_account_switch_cannot_reuse_previous_users_tokendance_key(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        cloud_account_store,
        "CLOUD_ACCOUNT_FILE",
        tmp_path / "cloud-account.json",
    )
    monkeypatch.setattr(
        cloud_account_store,
        "encrypt_dict_fields",
        lambda data, _fields: data,
    )
    monkeypatch.setattr(
        cloud_account_store,
        "decrypt_dict_fields",
        lambda data, _fields: data,
    )
    cloud_account_store.save_cloud_account(
        {
            "provider": "watcha",
            "refresh_token": "refresh-b",
            "user": {"id": "user-b"},
        },
    )
    provider = SimpleNamespace(
        api_key="key-owned-by-a",
        meta={
            "tokendance_credential_binding": {
                "schema_version": 1,
                "status": "active",
                "owner_subject_id": "watcha:user-a",
                "credential_ref": "cred-a",
                "payment_binding_id": "payment-a",
                "binding_revision": 1,
                "ownership_evidence": "oauth_pkce",
                "scopes": ["balance"],
            },
        },
    )

    with pytest.raises(TokenDanceAuthorizationError) as caught:
        resolve_tokendance_authorization(_Manager(provider), use_kind="balance")

    assert caught.value.code == "credential_owner_mismatch"
    assert "key-owned-by-a" not in str(caught.value)


def test_late_response_is_rejected_after_watcha_account_switch(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        cloud_account_store,
        "CLOUD_ACCOUNT_FILE",
        tmp_path / "cloud-account.json",
    )
    monkeypatch.setattr(
        cloud_account_store,
        "encrypt_dict_fields",
        lambda data, _fields: data,
    )
    monkeypatch.setattr(
        cloud_account_store,
        "decrypt_dict_fields",
        lambda data, _fields: data,
    )
    provider = SimpleNamespace(
        api_key="key-owned-by-a",
        meta={
            "tokendance_credential_binding": {
                "schema_version": 1,
                "status": "active",
                "owner_subject_id": "watcha:user-a",
                "credential_ref": "cred-a",
                "payment_binding_id": "payment-a",
                "binding_revision": 3,
                "account_epoch_at_binding": 1,
                "ownership_evidence": "oauth_pkce",
                "scopes": ["balance"],
            },
        },
    )
    manager = _Manager(provider)
    cloud_account_store.save_cloud_account(
        {
            "provider": "watcha",
            "refresh_token": "refresh-a",
            "user": {"id": "user-a"},
        },
    )
    started = resolve_tokendance_authorization(manager, use_kind="balance")
    assert started.context_id.startswith("tdctx_v1_")

    cloud_account_store.save_cloud_account(
        {
            "provider": "watcha",
            "refresh_token": "refresh-b",
            "user": {"id": "user-b"},
        },
    )

    with pytest.raises(TokenDanceAuthorizationError) as caught:
        assert_tokendance_authorization_current(manager, started)

    assert caught.value.code == "authorization_context_stale"


def test_logout_and_relogin_requires_rebinding_even_for_same_watcha_subject(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        cloud_account_store,
        "CLOUD_ACCOUNT_FILE",
        tmp_path / "cloud-account.json",
    )
    monkeypatch.setattr(
        cloud_account_store,
        "encrypt_dict_fields",
        lambda data, _fields: data,
    )
    monkeypatch.setattr(
        cloud_account_store,
        "decrypt_dict_fields",
        lambda data, _fields: data,
    )
    cloud_account_store.save_cloud_account(
        {
            "provider": "watcha",
            "refresh_token": "refresh-a",
            "user": {"id": "user-a"},
        },
    )
    provider = SimpleNamespace(
        api_key="key-owned-by-a",
        meta={
            "tokendance_credential_binding": {
                "schema_version": 1,
                "status": "active",
                "owner_subject_id": "watcha:user-a",
                "credential_ref": "cred-a",
                "payment_binding_id": "payment-a",
                "binding_revision": 1,
                "account_epoch_at_binding": 1,
                "ownership_evidence": "oauth_pkce",
                "scopes": ["balance"],
            },
        },
    )

    cloud_account_store.clear_cloud_account()
    cloud_account_store.save_cloud_account(
        {
            "provider": "watcha",
            "refresh_token": "refresh-a-again",
            "user": {"id": "user-a"},
        },
    )

    with pytest.raises(TokenDanceAuthorizationError) as caught:
        resolve_tokendance_authorization(_Manager(provider), use_kind="balance")

    assert caught.value.code == "tokendance_credential_reauthorization_required"


def test_context_id_is_shared_across_uses_and_changes_with_binding_revision(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        cloud_account_store,
        "CLOUD_ACCOUNT_FILE",
        tmp_path / "cloud-account.json",
    )
    monkeypatch.setattr(
        cloud_account_store,
        "encrypt_dict_fields",
        lambda data, _fields: data,
    )
    monkeypatch.setattr(
        cloud_account_store,
        "decrypt_dict_fields",
        lambda data, _fields: data,
    )
    cloud_account_store.save_cloud_account(
        {
            "provider": "watcha",
            "refresh_token": "refresh-a",
            "user": {"id": "user-a"},
        },
    )
    binding = {
        "schema_version": 1,
        "status": "active",
        "owner_subject_id": "watcha:user-a",
        "credential_ref": "cred-a",
        "payment_binding_id": "payment-a",
        "binding_revision": 3,
        "account_epoch_at_binding": 1,
        "ownership_evidence": "oauth_pkce",
        "scopes": ["balance", "usage"],
    }
    provider = SimpleNamespace(
        api_key="key-owned-by-a",
        meta={"tokendance_credential_binding": binding},
    )
    manager = _Manager(provider)

    balance = resolve_tokendance_authorization(manager, use_kind="balance")
    usage = resolve_tokendance_authorization(manager, use_kind="usage")
    assert balance.context_id == usage.context_id

    binding["binding_revision"] = 4
    rebound = resolve_tokendance_authorization(manager, use_kind="balance")
    assert rebound.context_id != balance.context_id
