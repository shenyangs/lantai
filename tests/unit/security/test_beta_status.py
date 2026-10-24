from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lantai.security.beta_license import LicenseVerificationError
from lantai.security.beta_status import BetaStatusVerifier


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def token(private_key, claims, header=None):
    protected = header or {"alg": "Ed25519", "typ": "lantai-beta-status+jwt", "kid": "kid"}
    value = f"{b64(json.dumps(protected, separators=(',', ':')).encode())}.{b64(json.dumps(claims, separators=(',', ':')).encode())}"
    return f"{value}.{b64(private_key.sign(value.encode('ascii')))}"


def test_signed_revocation_requires_exact_binding_and_signature() -> None:
    private = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    claims = {
        "iss": "issuer",
        "aud": "lantai-desktop-beta",
        "sub": "account",
        "device_id": "device",
        "license_id": "license",
        "decision": "revoked",
        "decision_seq": 9,
        "request_id": "request",
        "challenge_id": "challenge",
        "issued_at": int(now.timestamp()),
        "reason_code": "device_revoked",
    }
    verifier = BetaStatusVerifier(issuer="issuer", key_ring={"kid": private.public_key()})
    verified = verifier.verify(
        token(private, claims),
        expected_subject="account",
        expected_device_id="device",
        expected_license_id="license",
        expected_request_id="request",
        expected_challenge_id="challenge",
        now=now,
    )
    assert verified.decision_seq == 9
    with pytest.raises(LicenseVerificationError, match="binding"):
        verifier.verify(
            token(private, claims),
            expected_subject="other",
            expected_device_id="device",
            expected_license_id="license",
            expected_request_id="request",
            expected_challenge_id="challenge",
            now=now,
        )
