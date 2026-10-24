from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lantai.security.beta_profile import BetaAccessProfile, BetaProfileError, load_beta_access_profile


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _profile_payload() -> dict:
    public = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "version": 1,
        "channel": "private_beta",
        "issuer": "https://auth.lantai.guanlan.xin",
        "audience": "lantai-desktop-beta",
        "proof_audience": "https://auth.lantai.guanlan.xin/api/beta",
        "service_origin": "https://auth.lantai.guanlan.xin",
        "keys": [{"kid": "beta-2026-01", "kty": "OKP", "crv": "Ed25519", "x": _b64(public)}],
    }


def test_profile_accepts_only_pinned_ed25519_keys_and_fixed_https_origin():
    profile = BetaAccessProfile.from_mapping(_profile_payload())

    assert profile.channel == "private_beta"
    assert set(profile.key_ring) == {"beta-2026-01"}
    assert profile.service_origin == "https://auth.lantai.guanlan.xin"


def test_profile_allows_a_fixed_https_issuer_path_used_by_the_signer():
    payload = _profile_payload()
    payload["issuer"] = "https://auth.lantai.guanlan.xin/beta-issuer"

    profile = BetaAccessProfile.from_mapping(payload)

    assert profile.issuer == "https://auth.lantai.guanlan.xin/beta-issuer"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(service_origin="http://auth.lantai.guanlan.xin"),
        lambda value: value["keys"][0].update(crv="P-256"),
        lambda value: value["keys"][0].update(x="not-base64!"),
        lambda value: value.update(audience="other-product"),
    ],
)
def test_profile_fails_closed_instead_of_widening_algorithm_or_audience(mutation):
    payload = _profile_payload()
    mutation(payload)
    with pytest.raises(BetaProfileError):
        BetaAccessProfile.from_mapping(payload)


def test_profile_rejects_unknown_fields_and_exposes_an_immutable_key_ring():
    payload = _profile_payload()
    payload["allow_any_algorithm"] = True
    with pytest.raises(BetaProfileError):
        BetaAccessProfile.from_mapping(payload)

    profile = BetaAccessProfile.from_mapping(_profile_payload())
    with pytest.raises(TypeError):
        profile.key_ring["attacker"] = Ed25519PrivateKey.generate().public_key()


def test_profile_file_loader_rejects_duplicate_json_keys(tmp_path):
    payload = _profile_payload()
    encoded = json.dumps(payload).replace('"version": 1', '"version": 1, "version": 1')
    path = tmp_path / "beta-access-profile.json"
    path.write_text(encoded, encoding="utf-8")

    with pytest.raises(BetaProfileError, match="duplicate"):
        load_beta_access_profile(path)
