from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lantai.app.beta_access.account import BetaMemberSession
from lantai.app.beta_access.bootstrap import BetaAccessBootstrapError, build_beta_access_runtime
from lantai.app.beta_access.refresh import BetaAccountContext
from lantai.security.beta_access import AccessBoundary, BetaAccessStatus


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _write_profile(tmp_path):
    public = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    path = tmp_path / "beta-access-profile.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "channel": "private_beta",
                "issuer": "https://auth.lantai.guanlan.xin/beta-issuer",
                "audience": "lantai-desktop-beta",
                "proof_audience": "https://auth.lantai.guanlan.xin/api/beta",
                "service_origin": "https://auth.lantai.guanlan.xin",
                "keys": [
                    {
                        "kid": "beta-test-1",
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "x": _b64(public),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class ProbeKeyStore:
    def __init__(self, *, allowed_audiences):
        self.allowed_audiences = allowed_audiences
        self.loads = 0

    def load_or_create(self):
        self.loads += 1
        raise AssertionError("unactivated startup must not touch protected key storage")


def _anonymous_session():
    return BetaMemberSession(BetaAccountContext(None, 0), None)


def test_non_beta_channel_is_unrestricted_without_reading_profile_or_keychain(tmp_path):
    calls = []

    def forbidden_key_store(**kwargs):
        calls.append(kwargs)
        raise AssertionError("ordinary channels must not construct beta key storage")

    runtime = build_beta_access_runtime(
        release_channel="official",
        profile_path=tmp_path / "missing.json",
        device_key_store_factory=forbidden_key_store,
    )

    assert runtime.enabled is False
    assert runtime.router is None
    assert runtime.service is None
    assert calls == []
    decision = runtime.admission_guard.decide(
        boundary=AccessBoundary.WORK_START,
        capability="chat.start",
    )
    assert decision.allowed is True
    assert decision.reason == "channel_not_beta_gated"


def test_private_beta_fails_closed_without_a_valid_local_profile(tmp_path):
    with pytest.raises(BetaAccessBootstrapError, match="profile is required"):
        build_beta_access_runtime(release_channel="private_beta", profile_path="")

    with pytest.raises(BetaAccessBootstrapError, match="profile is invalid"):
        build_beta_access_runtime(
            release_channel="private_beta",
            profile_path=tmp_path / "missing.json",
        )


def test_private_beta_composes_gate_without_touching_keychain_before_activation(tmp_path):
    probes = []

    def key_store_factory(**kwargs):
        probe = ProbeKeyStore(**kwargs)
        probes.append(probe)
        return probe

    runtime = build_beta_access_runtime(
        release_channel=" private_beta ",
        profile_path=_write_profile(tmp_path),
        license_root=tmp_path / "license",
        session_loader=_anonymous_session,
        device_key_store_factory=key_store_factory,
    )

    assert runtime.enabled is True
    assert runtime.profile is not None
    assert runtime.profile.service_origin == "https://auth.lantai.guanlan.xin"
    assert runtime.router is not None
    assert probes[0].allowed_audiences == frozenset(
        {"https://auth.lantai.guanlan.xin/api/beta"}
    )
    snapshot = runtime.snapshot_reader()
    assert snapshot.status is BetaAccessStatus.NOT_ACTIVATED
    assert probes[0].loads == 0
    decision = runtime.admission_guard.decide(
        boundary=AccessBoundary.WORK_START,
        capability="chat.start",
    )
    assert decision.allowed is False
    assert decision.reason == "beta_access_not_activated:work_start"
    assert probes[0].loads == 0
