from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lantai.security.beta_license import (
    AccessDisposition,
    BetaLicenseVerifier,
    LicenseVerificationError,
)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token(private_key: Ed25519PrivateKey, claims: dict, *, header: dict | None = None) -> str:
    protected = header or {
        "alg": "Ed25519",
        "typ": "lantai-beta-license+jwt",
        "kid": "beta-test-01",
    }
    signing_input = f"{_b64(json.dumps(protected, separators=(',', ':')).encode())}.{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    return f"{signing_input}.{_b64(private_key.sign(signing_input.encode('ascii')))}"


@pytest.fixture
def license_fixture():
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    now = datetime(2026, 9, 20, 14, tzinfo=timezone.utc)
    claims = {
        "ver": 1,
        "iss": "https://auth.lantai.guanlan.xin/beta-issuer",
        "aud": "lantai-desktop-beta",
        "sub": "11111111-2222-4333-8444-555555555555",
        "jti": "issuance-1",
        "license_id": "license-1",
        "entitlement_id": "entitlement-1",
        "device_id": "device-1",
        "cnf": {"jkt": "device-jkt"},
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "refresh_after": int((now + timedelta(days=7)).timestamp()),
        "renew_by": int((now + timedelta(days=90)).timestamp()),
        "exp": int((now + timedelta(days=120)).timestamp()),
        "decision_seq": 7,
        "entitlement_epoch": 1,
        "device_epoch": 1,
        "edition": "private_beta",
        "features": ["application.use"],
        "policy_version": 1,
    }
    return private_key, public_key, now, claims


def test_local_verifier_reports_normal_grace_and_hard_expiry(license_fixture):
    private_key, public_key, now, claims = license_fixture
    verifier = BetaLicenseVerifier(
        issuer=claims["iss"],
        audience=claims["aud"],
        key_ring={"beta-test-01": public_key},
    )
    token = _token(private_key, claims)
    normal = verifier.verify(token, now=now + timedelta(days=1), expected_subject=claims["sub"], expected_jkt="device-jkt")
    grace = verifier.verify(token, now=now + timedelta(days=100), expected_subject=claims["sub"], expected_jkt="device-jkt")
    assert normal.disposition is AccessDisposition.ALLOW
    assert grace.disposition is AccessDisposition.GRACE
    with pytest.raises(LicenseVerificationError, match="expired"):
        verifier.verify(token, now=now + timedelta(days=120), expected_subject=claims["sub"], expected_jkt="device-jkt")


def test_local_verifier_rejects_untrusted_headers_and_binding(license_fixture):
    private_key, public_key, now, claims = license_fixture
    verifier = BetaLicenseVerifier(issuer=claims["iss"], audience=claims["aud"], key_ring={"beta-test-01": public_key})
    with pytest.raises(LicenseVerificationError, match="header"):
        verifier.verify(_token(private_key, claims, header={"alg": "Ed25519", "typ": "lantai-beta-license+jwt", "kid": "beta-test-01", "jku": "https://evil.example"}), now=now, expected_subject=claims["sub"], expected_jkt="device-jkt")
    with pytest.raises(LicenseVerificationError, match="device"):
        verifier.verify(_token(private_key, claims), now=now, expected_subject=claims["sub"], expected_jkt="other-jkt")
