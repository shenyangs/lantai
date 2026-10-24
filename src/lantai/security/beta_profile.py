"""Strict, release-pinned profile for the private-beta license service."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .beta_license import LICENSE_AUDIENCE


class BetaProfileError(ValueError):
    """The installed beta profile is not safe to use."""


MAX_PROFILE_BYTES = 65_536
_PROFILE_FIELDS = frozenset(
    {"version", "channel", "issuer", "audience", "proof_audience", "service_origin", "keys"}
)


def _https_origin(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BetaProfileError(f"{label} is required")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise BetaProfileError(f"{label} must be a fixed HTTPS origin")
    return value.rstrip("/")


def _https_endpoint(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BetaProfileError(f"{label} is required")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise BetaProfileError(f"{label} must be a fixed HTTPS endpoint")
    return value.rstrip("/")


def _decode_public_key(value: Any) -> Ed25519PublicKey:
    if not isinstance(value, str) or not value:
        raise BetaProfileError("beta profile key x is required")
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise BetaProfileError("beta profile key x is not canonical base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError) as exc:
        raise BetaProfileError("beta profile key x is not canonical base64url") from exc
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value or len(decoded) != 32:
        raise BetaProfileError("beta profile key x is not an Ed25519 public key")
    try:
        return Ed25519PublicKey.from_public_bytes(decoded)
    except ValueError as exc:
        raise BetaProfileError("beta profile key x is not an Ed25519 public key") from exc


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BetaProfileError(f"duplicate beta profile key: {key}")
        result[key] = value
    return result


def load_beta_access_profile(path: Path | str) -> "BetaAccessProfile":
    """Load the immutable release profile with strict JSON parsing."""

    try:
        with Path(path).open("rb") as handle:
            encoded = handle.read(MAX_PROFILE_BYTES + 1)
    except OSError as exc:
        raise BetaProfileError("beta access profile is unavailable") from exc
    if not encoded or len(encoded) > MAX_PROFILE_BYTES:
        raise BetaProfileError("beta access profile size is invalid")
    try:
        payload = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                BetaProfileError(f"invalid beta profile numeric constant: {value}")
            ),
        )
    except BetaProfileError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BetaProfileError("beta access profile is not valid JSON") from exc
    return BetaAccessProfile.from_mapping(payload)


@dataclass(frozen=True)
class BetaAccessProfile:
    """Trusted release configuration; never populated from a network response."""

    version: int
    channel: str
    issuer: str
    audience: str
    proof_audience: str
    service_origin: str
    key_ring: Mapping[str, Ed25519PublicKey]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BetaAccessProfile":
        if not isinstance(payload, Mapping):
            raise BetaProfileError("beta profile must be an object")
        if set(payload) != _PROFILE_FIELDS:
            raise BetaProfileError("untrusted beta profile fields")
        if payload.get("version") != 1:
            raise BetaProfileError("unsupported beta profile version")
        if payload.get("channel") != "private_beta":
            raise BetaProfileError("unsupported beta channel")
        if payload.get("audience") != LICENSE_AUDIENCE:
            raise BetaProfileError("unexpected beta license audience")

        issuer = _https_endpoint(payload.get("issuer"), "issuer")
        service_origin = _https_origin(payload.get("service_origin"), "service_origin")
        proof_audience = _https_endpoint(payload.get("proof_audience"), "proof_audience")
        if proof_audience != f"{service_origin}/api/beta":
            raise BetaProfileError("proof audience is not pinned to the beta service")

        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys:
            raise BetaProfileError("beta profile requires at least one key")
        key_ring: dict[str, Ed25519PublicKey] = {}
        for entry in keys:
            if not isinstance(entry, Mapping):
                raise BetaProfileError("invalid beta profile key")
            if set(entry) != {"kid", "kty", "crv", "x"}:
                raise BetaProfileError("untrusted beta profile key fields")
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid or kid in key_ring:
                raise BetaProfileError("invalid or duplicate beta profile key id")
            if entry.get("kty") != "OKP" or entry.get("crv") != "Ed25519":
                raise BetaProfileError("beta profile only accepts Ed25519 OKP keys")
            key_ring[kid] = _decode_public_key(entry.get("x"))

        return cls(
            version=1,
            channel="private_beta",
            issuer=issuer,
            audience=LICENSE_AUDIENCE,
            proof_audience=proof_audience,
            service_origin=service_origin,
            key_ring=MappingProxyType(key_ring),
        )
