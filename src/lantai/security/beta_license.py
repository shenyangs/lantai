"""Strict local verification for signed private-beta access licenses.

The verifier is deliberately network-free.  It accepts only the dedicated
Ed25519 license profile and locally pinned public keys; member-session tokens
and their HS256 secret never participate in desktop verification.
"""
from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

LICENSE_ALGORITHM = "Ed25519"
LICENSE_TYPE = "lantai-beta-license+jwt"
LICENSE_AUDIENCE = "lantai-desktop-beta"
LICENSE_VERSION = 1
MAX_LICENSE_BYTES = 65_536
_HEADER_FIELDS = frozenset({"alg", "typ", "kid"})
_REQUIRED_CLAIMS = frozenset(
    {
        "ver",
        "iss",
        "aud",
        "sub",
        "jti",
        "license_id",
        "entitlement_id",
        "device_id",
        "cnf",
        "iat",
        "nbf",
        "refresh_after",
        "renew_by",
        "exp",
        "decision_seq",
        "entitlement_epoch",
        "device_epoch",
        "edition",
        "features",
        "policy_version",
    }
)


class LicenseVerificationError(ValueError):
    """Raised when an offline license cannot be trusted or is not usable."""


class AccessDisposition(str, Enum):
    ALLOW = "allow"
    GRACE = "grace"


@dataclass(frozen=True)
class VerifiedBetaLicense:
    header: Mapping[str, Any]
    claims: Mapping[str, Any]
    disposition: AccessDisposition
    refresh_due: bool

    @property
    def decision_seq(self) -> int:
        return int(self.claims["decision_seq"])


def _decode_base64url(value: str, label: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise LicenseVerificationError(f"invalid {label} base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise LicenseVerificationError(f"invalid {label} base64url") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value:
        raise LicenseVerificationError(f"invalid {label} base64url")
    return decoded


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LicenseVerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(value: bytes, label: str) -> dict[str, Any]:
    try:
        text = value.decode("utf-8", errors="strict")
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                LicenseVerificationError(f"invalid {label} numeric constant: {constant}")
            ),
        )
    except LicenseVerificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise LicenseVerificationError(f"invalid {label} JSON") from exc
    if not isinstance(decoded, dict):
        raise LicenseVerificationError(f"invalid {label} JSON object")
    return decoded


def _integer_claim(claims: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = claims.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise LicenseVerificationError(f"invalid license claim: {key}")
    return value


def _text_claim(claims: Mapping[str, Any], key: str) -> str:
    value = claims.get(key)
    if not isinstance(value, str) or not value:
        raise LicenseVerificationError(f"invalid license claim: {key}")
    return value


class BetaLicenseVerifier:
    """Verify a compact beta license against a pinned Ed25519 key ring."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str = LICENSE_AUDIENCE,
        key_ring: Mapping[str, Ed25519PublicKey],
        nbf_clock_skew_seconds: int = 300,
    ) -> None:
        if not issuer or not audience:
            raise ValueError("issuer and audience are required")
        if nbf_clock_skew_seconds < 0:
            raise ValueError("clock skew cannot be negative")
        self._issuer = issuer
        self._audience = audience
        self._key_ring = dict(key_ring)
        self._nbf_clock_skew_seconds = nbf_clock_skew_seconds

    def verify(
        self,
        token: str,
        *,
        now: datetime | None = None,
        expected_subject: str,
        expected_jkt: str,
    ) -> VerifiedBetaLicense:
        header, claims = self._verify_signed_identity(
            token,
            expected_subject=expected_subject,
            expected_jkt=expected_jkt,
        )
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise LicenseVerificationError("verification time must be timezone-aware")
        now_seconds = math.floor(instant.timestamp())
        not_before = _integer_claim(claims, "nbf")
        renew_by = _integer_claim(claims, "renew_by")
        expires_at = _integer_claim(claims, "exp")
        refresh_after = _integer_claim(claims, "refresh_after")
        if now_seconds + self._nbf_clock_skew_seconds < not_before:
            raise LicenseVerificationError("license is not active yet")
        # exp already includes the bounded offline window.  Equality is an
        # expiry, and no extra grace is added locally.
        if now_seconds >= expires_at:
            raise LicenseVerificationError("license expired")
        disposition = AccessDisposition.GRACE if now_seconds >= renew_by else AccessDisposition.ALLOW
        return VerifiedBetaLicense(
            header=header,
            claims=claims,
            disposition=disposition,
            refresh_due=now_seconds >= refresh_after,
        )

    def verify_historical_identity(
        self,
        token: str,
        *,
        expected_subject: str,
        expected_jkt: str,
    ) -> Mapping[str, Any]:
        """Verify identity/signature without granting access after ``exp``.

        This exists only to bind online refresh and signed-status processing to
        a previously issued credential. Callers must never use it for local
        admission.
        """

        _header, claims = self._verify_signed_identity(
            token,
            expected_subject=expected_subject,
            expected_jkt=expected_jkt,
        )
        return claims

    def _verify_signed_identity(
        self,
        token: str,
        *,
        expected_subject: str,
        expected_jkt: str,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > MAX_LICENSE_BYTES:
            raise LicenseVerificationError("invalid compact license size")
        parts = token.split(".")
        if len(parts) != 3:
            raise LicenseVerificationError("invalid compact license")
        encoded_header, encoded_payload, encoded_signature = parts
        header = _strict_json(_decode_base64url(encoded_header, "header"), "header")
        if set(header) != _HEADER_FIELDS:
            raise LicenseVerificationError("untrusted license header fields")
        if header.get("alg") != LICENSE_ALGORITHM or header.get("typ") != LICENSE_TYPE:
            raise LicenseVerificationError("invalid license header profile")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise LicenseVerificationError("invalid license header key id")
        public_key = self._key_ring.get(kid)
        if not isinstance(public_key, Ed25519PublicKey):
            raise LicenseVerificationError("unknown pinned license key")

        claims = _strict_json(_decode_base64url(encoded_payload, "payload"), "payload")
        missing = _REQUIRED_CLAIMS.difference(claims)
        if missing:
            raise LicenseVerificationError(f"missing license claims: {', '.join(sorted(missing))}")
        try:
            public_key.verify(
                _decode_base64url(encoded_signature, "signature"),
                f"{encoded_header}.{encoded_payload}".encode("ascii"),
            )
        except InvalidSignature as exc:
            raise LicenseVerificationError("invalid license signature") from exc

        self._validate_claims(claims, expected_subject=expected_subject, expected_jkt=expected_jkt)
        return header, claims

    def _validate_claims(
        self,
        claims: Mapping[str, Any],
        *,
        expected_subject: str,
        expected_jkt: str,
    ) -> None:
        if claims.get("ver") != LICENSE_VERSION:
            raise LicenseVerificationError("unsupported license version")
        if claims.get("iss") != self._issuer or claims.get("aud") != self._audience:
            raise LicenseVerificationError("license issuer or audience mismatch")
        if _text_claim(claims, "sub") != expected_subject:
            raise LicenseVerificationError("license account binding mismatch")
        for key in ("jti", "license_id", "entitlement_id", "device_id", "edition"):
            _text_claim(claims, key)
        confirmation = claims.get("cnf")
        if not isinstance(confirmation, dict) or confirmation.get("jkt") != expected_jkt:
            raise LicenseVerificationError("license device binding mismatch")
        features = claims.get("features")
        if not isinstance(features, list) or not features or any(not isinstance(item, str) or not item for item in features):
            raise LicenseVerificationError("invalid license features")
        issued_at = _integer_claim(claims, "iat")
        not_before = _integer_claim(claims, "nbf")
        refresh_after = _integer_claim(claims, "refresh_after")
        renew_by = _integer_claim(claims, "renew_by")
        expires_at = _integer_claim(claims, "exp")
        for key in ("decision_seq", "entitlement_epoch", "device_epoch", "policy_version"):
            _integer_claim(claims, key, minimum=1)
        if not (issued_at <= not_before <= refresh_after <= renew_by <= expires_at):
            raise LicenseVerificationError("invalid license time window")
