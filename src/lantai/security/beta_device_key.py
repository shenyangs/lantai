"""Keychain-backed Ed25519 installation identity for beta device proofs."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEYCHAIN_SERVICE = "xin.guanlan.lantai.beta-device-key.v1"
DEFAULT_KEY_ACCOUNT = "installation-v1"
DEVICE_PROOF_TYPE = "lantai-device-proof+jwt"
DEVICE_PROOF_ALGORITHM = "Ed25519"
DEFAULT_PROOF_AUDIENCE = "https://auth.lantai.guanlan.xin/api/beta"
_ALLOWED_PURPOSES = frozenset({"activate", "issue", "refresh"})


class KeyringBackend(Protocol):
    def get_password(self, service: str, account: str) -> str | None: ...

    def set_password(self, service: str, account: str, password: str) -> None: ...


class DeviceKeyUnavailableError(RuntimeError):
    """The protected key store is locked, unavailable, or corrupt."""


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _parse_expiry(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("challenge expiry is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("challenge expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("challenge expiry must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DeviceIdentity:
    public_jwk: dict[str, str]
    jkt: str
    _private_key: Ed25519PrivateKey = field(repr=False, compare=False)

    def prove_local_possession(self) -> bool:
        message = os.urandom(32)
        signature = self._private_key.sign(message)
        try:
            self._private_key.public_key().verify(signature, message)
        except InvalidSignature:
            return False
        return True


class DeviceKeyStore:
    """Persist the device private key only through an OS keyring backend."""

    def __init__(
        self,
        *,
        backend: KeyringBackend | None = None,
        key_account: str = DEFAULT_KEY_ACCOUNT,
        allowed_audiences: frozenset[str] | None = None,
    ) -> None:
        if backend is None:
            try:
                import keyring
            except Exception as exc:  # pragma: no cover - dependency is packaged
                raise DeviceKeyUnavailableError("OS keyring is unavailable") from exc
            backend = keyring
        self._backend = backend
        self._key_account = key_account
        self._allowed_audiences = allowed_audiences or frozenset({DEFAULT_PROOF_AUDIENCE})
        self._identity: DeviceIdentity | None = None

    def load_or_create(self) -> DeviceIdentity:
        if self._identity is not None:
            return self._identity
        try:
            stored = self._backend.get_password(KEYCHAIN_SERVICE, self._key_account)
        except Exception as exc:
            raise DeviceKeyUnavailableError("OS keyring could not be read") from exc
        if stored:
            identity = self._decode(stored)
        else:
            private_key = Ed25519PrivateKey.generate()
            encoded = self._encode(private_key)
            try:
                self._backend.set_password(KEYCHAIN_SERVICE, self._key_account, encoded)
            except Exception as exc:
                # No plaintext-file fallback: failure keeps activation pending.
                raise DeviceKeyUnavailableError("OS keyring could not store the device key") from exc
            identity = self._identity_from_key(private_key)
        self._identity = identity
        return identity

    def sign_server_challenge(self, challenge: dict[str, Any], *, now: datetime | None = None) -> str:
        identity = self.load_or_create()
        purpose = challenge.get("purpose")
        if purpose not in _ALLOWED_PURPOSES:
            raise ValueError("device proof purpose is not allowed")
        audience = challenge.get("audience")
        if audience not in self._allowed_audiences:
            raise ValueError("device proof audience is not trusted")
        subject = challenge.get("subject")
        account_subject = False
        try:
            UUID(str(subject))
            account_subject = True
        except (TypeError, ValueError):
            pass
        installation_subject = f"installation:{identity.jkt}"
        if not account_subject and subject != installation_subject:
            raise ValueError("device proof subject must match the account or installation key")
        expiry = _parse_expiry(challenge.get("expiresAt"))
        instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if expiry <= instant or (expiry - instant).total_seconds() > 600:
            raise ValueError("device proof expiry is outside the trusted window")
        commitment = challenge.get("intentCommitment")
        if not isinstance(commitment, str) or len(commitment) != 64 or any(c not in "0123456789abcdef" for c in commitment):
            raise ValueError("device proof intent commitment is invalid")
        if challenge.get("deviceJkt") != identity.jkt:
            raise ValueError("device proof key does not match the challenge")
        for key in ("challengeId", "nonce", "requestId"):
            if not isinstance(challenge.get(key), str) or not challenge[key]:
                raise ValueError(f"device proof {key} is required")
        header = {"alg": DEVICE_PROOF_ALGORITHM, "typ": DEVICE_PROOF_TYPE}
        payload = {
            "aud": audience,
            "challenge_id": challenge["challengeId"],
            "device_jkt": identity.jkt,
            "exp": int(expiry.timestamp()),
            "intent_commitment": commitment,
            "nonce": challenge["nonce"],
            "purpose": purpose,
            "request_id": challenge["requestId"],
            "sub": str(subject),
        }
        signing_input = f"{_b64(_canonical(header))}.{_b64(_canonical(payload))}"
        return f"{signing_input}.{_b64(identity._private_key.sign(signing_input.encode('ascii')))}"

    @staticmethod
    def _identity_from_key(private_key: Ed25519PrivateKey) -> DeviceIdentity:
        public_raw = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        public_jwk = {"crv": "Ed25519", "kty": "OKP", "x": _b64(public_raw)}
        thumbprint = _b64(hashlib.sha256(_canonical(public_jwk)).digest())
        return DeviceIdentity(public_jwk=public_jwk, jkt=thumbprint, _private_key=private_key)

    @classmethod
    def _encode(cls, private_key: Ed25519PrivateKey) -> str:
        private_der = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return json.dumps({"v": 1, "alg": "Ed25519", "pkcs8": _b64(private_der)}, separators=(",", ":"))

    @classmethod
    def _decode(cls, stored: str) -> DeviceIdentity:
        try:
            payload = json.loads(stored)
            if payload.get("v") != 1 or payload.get("alg") != "Ed25519":
                raise ValueError("unsupported device key record")
            encoded = payload["pkcs8"]
            private_der = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            key = serialization.load_der_private_key(private_der, password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError("device key is not Ed25519")
            return cls._identity_from_key(key)
        except Exception as exc:
            raise DeviceKeyUnavailableError("OS keyring device key is invalid") from exc
