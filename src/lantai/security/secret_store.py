# -*- coding: utf-8 -*-
"""Encrypted secret storage layer.

Provides transparent encryption/decryption for sensitive fields (API keys,
tokens, etc.) stored on disk.  Secrets are encrypted with Fernet (AES-128-CBC
+ HMAC-SHA256) using a master key that is:

1. Stored in the OS keychain via the ``keyring`` library (preferred), or
2. Persisted to ``SECRET_DIR/.master_key`` with mode ``0o600`` (fallback).

Encrypted values carry an ``ENC:`` prefix so readers can distinguish them
from legacy plaintext and transparently migrate on first access.

Set ``LANTAI_DISABLE_KEYRING=1`` for ad-hoc signed local desktop builds so
macOS does not prompt for Keychain access on every rebuilt ``lantai-server``.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

from ..constant import EnvVarLoader

logger = logging.getLogger(__name__)

_ENC_PREFIX = "ENC:"
_KEYRING_SERVICE = "lantai"
_KEYRING_SERVICE_LEGACY = "copaw"
_KEYRING_ACCOUNT = "master_key"


class SecretDecryptionError(ValueError):
    """A stored encrypted secret cannot be safely recovered.

    Callers must treat this as a local configuration failure.  Returning an
    ``ENC:`` payload would let it reach an external provider as if it were a
    credential, which both obscures the real problem and leaks unusable
    ciphertext outside the machine.
    """


def _get_secret_dir() -> Path:
    """Lazy import to avoid circular dependency with ``constant.py``."""
    from ..constant import SECRET_DIR

    return SECRET_DIR


# ---------------------------------------------------------------------------
# Master-key management
# ---------------------------------------------------------------------------

_cached_master_key: Optional[bytes] = None
_master_key_lock = threading.Lock()


def _should_skip_keyring() -> bool:
    """Return ``True`` when the OS keyring is unlikely to be available.

    Covers Docker containers, headless Linux servers, and CI
    environments where attempting keyring access could hang on D-Bus.
    """
    import sys

    if "pytest" in sys.modules:
        return True

    if EnvVarLoader.get_bool("LANTAI_DISABLE_KEYRING") or EnvVarLoader.get_bool(
        "LANTAI_SKIP_KEYRING",
    ):
        return True

    if EnvVarLoader.get_bool("LANTAI_RUNNING_IN_CONTAINER"):
        return True

    if sys.platform == "linux" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return True

    if os.environ.get("CI", "").lower() in ("true", "1"):
        return True

    return False


def _try_keyring_get() -> Optional[str]:
    """Read master key from OS keychain. Returns ``None`` on any failure.

    Skipped inside containers, headless Linux, and CI environments.
    """
    if _should_skip_keyring():
        return None
    try:
        import keyring

        value = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        if value:
            return value

        # Backward compatibility: read legacy CoPaw keyring entry.
        return keyring.get_password(
            _KEYRING_SERVICE_LEGACY,
            _KEYRING_ACCOUNT,
        )
    except Exception:
        return None


def _try_keyring_set(key_hex: str) -> bool:
    """Store master key in OS keychain. Returns success flag.

    Skipped inside containers where no desktop keyring service exists.
    """
    if _should_skip_keyring():
        return False
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, key_hex)
        return True
    except Exception:
        logger.debug("keyring unavailable, falling back to file storage")
        return False


def _master_key_file() -> Path:
    return _get_secret_dir() / ".master_key"


def _read_key_file() -> Optional[str]:
    path = _master_key_file()
    if path.is_file():
        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                return None
            bytes.fromhex(content)
            if len(content) != 64:
                logger.warning(
                    "Master key file has unexpected length (%d hex chars,"
                    " expected 64); ignoring",
                    len(content),
                )
                return None
            return content
        except (OSError, ValueError):
            logger.warning(
                "Master key file is corrupt or unreadable; will regenerate",
            )
            return None
    return None


def _write_key_file(key_hex: str) -> None:
    path = _master_key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key_hex, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _generate_master_key() -> str:
    """Generate a 32-byte random master key and return its hex encoding."""
    return secrets.token_hex(32)


def _get_master_key() -> bytes:
    """Return the 32-byte master key, creating one if it does not exist.

    Uses double-checked locking to guarantee that only one thread ever
    generates or loads the key, even when multiple FastAPI worker
    threads start up concurrently.

    Resolution order:
    1. In-process cache (fast path, no lock)
    2. OS keychain (via ``keyring``)
    3. File ``SECRET_DIR/.master_key``
    4. Generate new → store in keychain (preferred) and file (fallback)
    """
    global _cached_master_key
    if _cached_master_key is not None:
        return _cached_master_key

    with _master_key_lock:
        if _cached_master_key is not None:
            return _cached_master_key

        key_hex = _try_keyring_get()

        if not key_hex:
            key_hex = _read_key_file()
            if key_hex:
                _try_keyring_set(key_hex)

        if not key_hex:
            key_hex = _generate_master_key()
            _try_keyring_set(key_hex)
            _write_key_file(key_hex)

        _cached_master_key = bytes.fromhex(key_hex)
        return _cached_master_key


# ---------------------------------------------------------------------------
# Fernet encrypt / decrypt
# ---------------------------------------------------------------------------


_cached_fernet: Optional[object] = None


def _get_fernet():
    """Return a cached Fernet instance backed by the master key."""
    global _cached_fernet
    if _cached_fernet is not None:
        return _cached_fernet

    from cryptography.fernet import Fernet

    raw = _get_master_key()
    fernet_key = base64.urlsafe_b64encode(raw[:32])
    _cached_fernet = Fernet(fernet_key)
    return _cached_fernet


def _fernet_from_master_key(master_key: bytes):
    """Build a Fernet instance for a validated 32-byte master key."""
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(master_key[:32]))


def _recover_from_file_backed_key(token: bytes) -> Optional[str]:
    """Try the file-backed key after a keychain-backed decrypt failure.

    Local desktop builds deliberately use ``.master_key`` to avoid repeated
    Keychain prompts.  A CLI or development launch may instead pick up a stale
    Keychain value first.  Recover only when the file key actually decrypts
    the ciphertext, then synchronize the Keychain for future launches.
    """
    key_hex = _read_key_file()
    if not key_hex:
        return None

    try:
        master_key = bytes.fromhex(key_hex)
        fallback_fernet = _fernet_from_master_key(master_key)
        plaintext = fallback_fernet.decrypt(token).decode("utf-8")
    except Exception:
        return None

    global _cached_master_key, _cached_fernet
    with _master_key_lock:
        _cached_master_key = master_key
        _cached_fernet = fallback_fernet

    _try_keyring_set(key_hex)
    logger.warning(
        "Recovered encrypted secret with the file-backed master key and "
        "synchronized the keychain",
    )
    return plaintext


def encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* and return ``ENC:<base64-ciphertext>``."""
    if not plaintext:
        return plaintext
    f = _get_fernet()
    token = f.encrypt(plaintext.encode("utf-8"))
    return _ENC_PREFIX + token.decode("ascii")


def decrypt(value: str) -> str:
    """Decrypt *value* if it carries the ``ENC:`` prefix; pass through
    otherwise.

    A keychain/file mismatch is recovered from the local file-backed master
    key when possible.  Any remaining failure is explicit and fail-closed so
    encrypted ciphertext is never sent to an external service as a secret.
    """
    if not value or not value.startswith(_ENC_PREFIX):
        return value
    token = value[len(_ENC_PREFIX) :].encode("ascii")
    try:
        f = _get_fernet()
        return f.decrypt(token).decode("utf-8")
    except Exception as exc:
        recovered = _recover_from_file_backed_key(token)
        if recovered is not None:
            return recovered
        logger.warning(
            "Failed to decrypt stored secret; blocking use until credentials "
            "are reconfigured",
        )
        raise SecretDecryptionError(
            "Stored credentials cannot be decrypted. Re-enter the credential."
        ) from exc


def is_encrypted(value: str) -> bool:
    """Return ``True`` when *value* looks like an encrypted token."""
    return bool(value) and value.startswith(_ENC_PREFIX)


def reload_master_key_from_disk() -> None:
    """Invalidate the in-process master-key cache and re-sync the OS keyring.

    Call this after a backup restore has replaced ``SECRET_DIR/.master_key``
    on disk so that the running process and the OS keyring both pick up the
    restored key instead of continuing to use the old one.

    Resolution order after clearing the cache:
    1. Read the new key from ``SECRET_DIR/.master_key``.
    2. If successful and the OS keyring is available, overwrite the keyring
       entry with the restored key so that the next process start does not
       fall back to the old keyring value.
    3. If anything goes wrong, log a warning but never propagate the error
       to the restore caller.
    """
    global _cached_master_key, _cached_fernet
    try:
        with _master_key_lock:
            _cached_master_key = None
            _cached_fernet = None

            key_hex = _read_key_file()
            if not key_hex:
                logger.warning(
                    "reload_master_key_from_disk: .master_key file not found"
                    " or unreadable after restore; cache cleared but keyring"
                    " not updated",
                )
                return

            _try_keyring_set(key_hex)
            logger.info(
                "reload_master_key_from_disk: in-process cache invalidated"
                " and keyring updated with restored master key",
            )
    except Exception:
        logger.warning(
            "reload_master_key_from_disk: unexpected error; master-key cache"
            " has been cleared but keyring sync may be incomplete",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# High-level helpers for dict-based secret fields
# ---------------------------------------------------------------------------

# Fields that should be encrypted when persisting provider JSON.
PROVIDER_SECRET_FIELDS: frozenset[str] = frozenset({"api_key"})

# Fields that should be encrypted when persisting auth.json.
AUTH_SECRET_FIELDS: frozenset[str] = frozenset({"jwt_secret"})


def encrypt_dict_fields(
    data: dict,
    secret_fields: frozenset[str],
) -> dict:
    """Return a shallow copy of *data* with *secret_fields* encrypted."""
    result = dict(data)
    for field in secret_fields:
        if (
            field in result
            and isinstance(result[field], str)
            and result[field]
        ):
            if not is_encrypted(result[field]):
                result[field] = encrypt(result[field])
    return result


def decrypt_dict_fields(
    data: dict,
    secret_fields: frozenset[str],
) -> dict:
    """Return a shallow copy of *data* with *secret_fields* decrypted."""
    result = dict(data)
    for field in secret_fields:
        if (
            field in result
            and isinstance(result[field], str)
            and result[field]
        ):
            result[field] = decrypt(result[field])
    return result
