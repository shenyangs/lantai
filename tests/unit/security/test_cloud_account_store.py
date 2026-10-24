from __future__ import annotations

import json
import stat

from lantai.security import cloud_account_store as store


def test_cloud_account_tokens_are_protected_and_round_trip(monkeypatch, tmp_path):
    target = tmp_path / "cloud-account.json"
    monkeypatch.setattr(store, "CLOUD_ACCOUNT_FILE", target)
    monkeypatch.setattr(
        store,
        "encrypt_dict_fields",
        lambda data, fields: {
            key: f"ENC:{value}" if key in fields and value else value
            for key, value in data.items()
        },
    )
    monkeypatch.setattr(
        store,
        "decrypt_dict_fields",
        lambda data, fields: {
            key: value.removeprefix("ENC:")
            if key in fields and isinstance(value, str)
            else value
            for key, value in data.items()
        },
    )

    store.save_cloud_account(
        {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "user": {"id": "user-1"},
        },
    )

    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["access_token"] == "ENC:access-secret"
    assert raw["refresh_token"] == "ENC:refresh-secret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert store.load_cloud_account()["refresh_token"] == "refresh-secret"

    store.clear_cloud_account()
    assert target.is_file()
    assert store.load_cloud_account().get("refresh_token") is None


def test_watcha_connected_requires_refresh_token_and_user(
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "cloud-account.json"
    monkeypatch.setattr(store, "CLOUD_ACCOUNT_FILE", target)
    monkeypatch.setattr(
        store,
        "decrypt_dict_fields",
        lambda data, _fields: data,
    )

    assert store.is_watcha_account_connected() is False
    target.write_text(
        json.dumps({"provider": "watcha", "refresh_token": "token"}),
        encoding="utf-8",
    )
    assert store.is_watcha_account_connected() is False
    target.write_text(
        json.dumps(
            {
                "provider": "watcha",
                "refresh_token": "token",
                "user": {"id": "watcha-user"},
            },
        ),
        encoding="utf-8",
    )
    assert store.is_watcha_account_connected() is True


def test_legacy_connected_account_gets_a_stable_initial_epoch(
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "cloud-account.json"
    monkeypatch.setattr(store, "CLOUD_ACCOUNT_FILE", target)
    monkeypatch.setattr(store, "encrypt_dict_fields", lambda data, _fields: data)
    monkeypatch.setattr(store, "decrypt_dict_fields", lambda data, _fields: data)
    target.write_text(
        json.dumps(
            {
                "provider": "watcha",
                "refresh_token": "legacy-token",
                "user": {"id": "legacy-user"},
            },
        ),
        encoding="utf-8",
    )

    first = store.load_watcha_account_context()
    second = store.load_watcha_account_context()

    assert first == store.WatchaAccountContext(
        subject_id="watcha:legacy-user",
        account_epoch=1,
    )
    assert second == first


def test_legacy_account_logout_and_relogin_cannot_reuse_initial_epoch(
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "cloud-account.json"
    monkeypatch.setattr(store, "CLOUD_ACCOUNT_FILE", target)
    monkeypatch.setattr(store, "encrypt_dict_fields", lambda data, _fields: data)
    monkeypatch.setattr(store, "decrypt_dict_fields", lambda data, _fields: data)
    legacy_account = {
        "provider": "watcha",
        "refresh_token": "legacy-token",
        "user": {"id": "legacy-user"},
    }
    target.write_text(json.dumps(legacy_account), encoding="utf-8")

    before_logout = store.load_watcha_account_context()
    store.clear_cloud_account()
    store.save_cloud_account(legacy_account)
    after_relogin = store.load_watcha_account_context()

    assert before_logout.account_epoch == 1
    assert after_relogin.subject_id == before_logout.subject_id
    assert after_relogin.account_epoch > before_logout.account_epoch


def test_broken_cloud_account_never_blocks_local_provider_use(monkeypatch) -> None:
    monkeypatch.setattr(
        store,
        "load_cloud_account",
        lambda: (_ for _ in ()).throw(RuntimeError("broken secret")),
    )

    assert store.is_watcha_account_connected() is False


def test_account_epoch_changes_on_switch_and_logout_but_not_refresh(
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "cloud-account.json"
    monkeypatch.setattr(store, "CLOUD_ACCOUNT_FILE", target)
    monkeypatch.setattr(store, "encrypt_dict_fields", lambda data, _fields: data)
    monkeypatch.setattr(store, "decrypt_dict_fields", lambda data, _fields: data)

    account_a = {
        "provider": "watcha",
        "refresh_token": "refresh-a",
        "user": {"id": "user-a"},
    }
    store.save_cloud_account(account_a)
    first = store.load_watcha_account_context()
    store.save_cloud_account({**account_a, "access_token": "refreshed"})
    refreshed = store.load_watcha_account_context()
    store.save_cloud_account(
        {
            "provider": "watcha",
            "refresh_token": "refresh-b",
            "user": {"id": "user-b"},
        },
    )
    switched = store.load_watcha_account_context()
    store.clear_cloud_account()
    logged_out = store.load_watcha_account_context()

    assert first.subject_id == "watcha:user-a"
    assert first.account_epoch >= 1
    assert refreshed == first
    assert switched.subject_id == "watcha:user-b"
    assert switched.account_epoch == first.account_epoch + 1
    assert logged_out.subject_id is None
    assert logged_out.account_epoch == switched.account_epoch + 1
    assert target.is_file()
    assert "refresh-b" not in target.read_text(encoding="utf-8")
