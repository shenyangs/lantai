from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lantai.security.beta_device_key import DeviceKeyStore, DeviceKeyUnavailableError


class MemoryKeyring:
    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}
        self.fail = False

    def get_password(self, service: str, account: str):
        if self.fail:
            raise RuntimeError("locked")
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str):
        if self.fail:
            raise RuntimeError("locked")
        self.values[(service, account)] = value


def test_device_key_is_reused_from_protected_store_and_signing_is_purpose_limited():
    backend = MemoryKeyring()
    first = DeviceKeyStore(backend=backend, key_account="install-test")
    identity = first.load_or_create()
    second = DeviceKeyStore(backend=backend, key_account="install-test")
    assert second.load_or_create().jkt == identity.jkt

    now = datetime.now(timezone.utc)
    challenge = {
        "challengeId": "challenge-1",
        "nonce": "nonce",
        "purpose": "activate",
        "subject": "11111111-2222-4333-8444-555555555555",
        "requestId": "request-1",
        "intentCommitment": "a" * 64,
        "deviceJkt": identity.jkt,
        "audience": "https://auth.lantai.guanlan.xin/api/beta",
        "expiresAt": (now + timedelta(minutes=5)).isoformat(),
    }
    proof = second.sign_server_challenge(challenge)
    assert proof.count(".") == 2
    with pytest.raises(ValueError, match="purpose"):
        second.sign_server_challenge({**challenge, "purpose": "admin"})


def test_device_key_accepts_only_its_own_installation_subject():
    backend = MemoryKeyring()
    store = DeviceKeyStore(backend=backend, key_account="anonymous-install")
    identity = store.load_or_create()
    now = datetime.now(timezone.utc)
    challenge = {
        "challengeId": "challenge-anonymous",
        "nonce": "nonce",
        "purpose": "activate",
        "subject": f"installation:{identity.jkt}",
        "requestId": "request-anonymous",
        "intentCommitment": "b" * 64,
        "deviceJkt": identity.jkt,
        "audience": "https://auth.lantai.guanlan.xin/api/beta",
        "expiresAt": (now + timedelta(minutes=5)).isoformat(),
    }

    assert store.sign_server_challenge(challenge).count(".") == 2
    with pytest.raises(ValueError, match="account or installation key"):
        store.sign_server_challenge({**challenge, "subject": "installation:someone-else"})


def test_keychain_failure_never_creates_plaintext_fallback(tmp_path):
    backend = MemoryKeyring()
    backend.fail = True
    store = DeviceKeyStore(backend=backend, key_account="install-test")
    with pytest.raises(DeviceKeyUnavailableError):
        store.load_or_create()
    assert list(tmp_path.iterdir()) == []
