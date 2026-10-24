"""Strict verification for signed authoritative beta revocation decisions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .beta_license import LicenseVerificationError, _decode_base64url, _integer_claim, _strict_json, _text_claim

STATUS_TYPE = "lantai-beta-status+jwt"
STATUS_ALGORITHM = "Ed25519"
STATUS_AUDIENCE = "lantai-desktop-beta"
_HEADER_FIELDS = frozenset({"alg", "typ", "kid"})
_REQUIRED = frozenset(
    {
        "iss",
        "aud",
        "sub",
        "device_id",
        "license_id",
        "decision",
        "decision_seq",
        "request_id",
        "challenge_id",
        "issued_at",
        "reason_code",
    }
)


@dataclass(frozen=True)
class VerifiedRevocation:
    claims: Mapping[str, Any]

    @property
    def decision_seq(self) -> int:
        return int(self.claims["decision_seq"])


class BetaStatusVerifier:
    def __init__(self, *, issuer: str, key_ring: Mapping[str, Ed25519PublicKey]) -> None:
        self.issuer = issuer
        self.key_ring = dict(key_ring)

    def verify(
        self,
        token: str,
        *,
        expected_subject: str,
        expected_device_id: str,
        expected_license_id: str,
        expected_request_id: str,
        expected_challenge_id: str,
        now: datetime | None = None,
    ) -> VerifiedRevocation:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > 65_536:
            raise LicenseVerificationError("invalid signed status size")
        parts = token.split(".")
        if len(parts) != 3:
            raise LicenseVerificationError("invalid signed status")
        encoded_header, encoded_payload, encoded_signature = parts
        header = _strict_json(_decode_base64url(encoded_header, "status header"), "status header")
        if set(header) != _HEADER_FIELDS or header.get("alg") != STATUS_ALGORITHM or header.get("typ") != STATUS_TYPE:
            raise LicenseVerificationError("invalid signed status profile")
        kid = header.get("kid")
        public_key = self.key_ring.get(kid) if isinstance(kid, str) else None
        if not isinstance(public_key, Ed25519PublicKey):
            raise LicenseVerificationError("unknown signed status key")
        claims = _strict_json(_decode_base64url(encoded_payload, "status payload"), "status payload")
        if _REQUIRED.difference(claims):
            raise LicenseVerificationError("signed status is missing claims")
        try:
            public_key.verify(
                _decode_base64url(encoded_signature, "status signature"),
                f"{encoded_header}.{encoded_payload}".encode("ascii"),
            )
        except InvalidSignature as exc:
            raise LicenseVerificationError("invalid signed status signature") from exc
        expected = {
            "iss": self.issuer,
            "aud": STATUS_AUDIENCE,
            "sub": expected_subject,
            "device_id": expected_device_id,
            "license_id": expected_license_id,
            "decision": "revoked",
            "request_id": expected_request_id,
            "challenge_id": expected_challenge_id,
        }
        if any(claims.get(key) != value for key, value in expected.items()):
            raise LicenseVerificationError("signed status binding mismatch")
        _text_claim(claims, "reason_code")
        _integer_claim(claims, "decision_seq", minimum=1)
        issued_at = _integer_claim(claims, "issued_at", minimum=1)
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise LicenseVerificationError("verification time must be timezone-aware")
        if issued_at > int(instant.timestamp()) + 300:
            raise LicenseVerificationError("signed status is from the future")
        return VerifiedRevocation(claims=claims)
