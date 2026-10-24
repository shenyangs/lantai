# -*- coding: utf-8 -*-
"""Authentication module: password hashing, JWT tokens, and FastAPI middleware.

Login is disabled by default and only enabled when the environment
variable ``LANTAI_AUTH_ENABLED`` is set to a truthy value (``true``,
``1``, ``yes``).  Credentials are created through a web-based
registration flow rather than environment variables, so that agents
running inside the process cannot read plaintext passwords.

Single-user design: only one account can be registered. Use the auth CLI
to reset a forgotten password. Storage failures preserve the account file
for diagnosis and recovery from its last-good backup.

Uses only Python stdlib (hashlib, hmac, secrets) to avoid adding new
dependencies. Passwords are stored with scrypt in ``auth.json`` under
``SECRET_DIR``; legacy salted SHA-256 records are upgraded after a successful
login.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import tempfile
import time
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..constant import SECRET_DIR, EnvVarLoader
from ..security.secret_store import (
    AUTH_SECRET_FIELDS,
    SecretDecryptionError,
    decrypt_dict_fields,
    encrypt_dict_fields,
    is_encrypted,
)

logger = logging.getLogger(__name__)

AUTH_FILE = SECRET_DIR / "auth.json"


class AuthStorageError(RuntimeError):
    """The existing auth store is unavailable and must not be reset."""


# Token validity: 7 days (default)
TOKEN_EXPIRY_SECONDS = 7 * 24 * 3600

# Maximum token validity: 100 years (for "permanent" tokens)
TOKEN_EXPIRY_MAX = 100 * 365 * 24 * 3600

# Paths that do NOT require authentication
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/api/auth/login",
        "/api/auth/status",
        "/api/auth/register",
        "/api/version",
        "/api/settings/language",
        "/api/plugins",
        "/api/browser-pairing/status",
        "/api/browser-pairing/complete",
        "/api/browser-pairing/logout",
        "/api/mcp/oauth/callback",
        "/api/cloud-account/watcha/callback",
        "/api/models/tokendance/oauth/callback",
    },
)

# Prefixes that do NOT require authentication (static assets)
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/assets/",
    "/logo.png",
    "/lantai-symbol.svg",
    "/api/plugins/",  # plugin JS bundles served to unauthenticated login page
)

_FIRST_RUN_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/api/auth/login",
        "/api/auth/status",
        "/api/auth/register",
        "/api/version",
        "/api/settings/language",
        "/api/plugins",
    },
)


# ---------------------------------------------------------------------------
# Helpers (reuse SECRET_DIR patterns from envs/store.py)
# ---------------------------------------------------------------------------


def _chmod_best_effort(path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _prepare_secret_parent(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path.parent, 0o700)


# ---------------------------------------------------------------------------
# Password hashing (stdlib scrypt, with legacy salted-SHA-256 migration)
# ---------------------------------------------------------------------------


_PASSWORD_SCHEME_SCRYPT = "scrypt"
_PASSWORD_SCHEME_LEGACY_SHA256 = "sha256"
_SCRYPT_PARAMS: dict[str, int] = {
    "n": 2**14,
    "r": 8,
    "p": 1,
    "dklen": 32,
}


def _normalized_scrypt_params(
    params: Optional[dict[str, int]],
) -> dict[str, int]:
    effective = {**_SCRYPT_PARAMS, **(params or {})}
    try:
        normalized = {
            key: int(effective[key]) for key in ("n", "r", "p", "dklen")
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid scrypt password parameters") from exc
    n = normalized["n"]
    if n < 2**14 or n > 2**18 or n & (n - 1):
        raise ValueError("Invalid scrypt cost parameter")
    if not 1 <= normalized["r"] <= 16:
        raise ValueError("Invalid scrypt block-size parameter")
    if not 1 <= normalized["p"] <= 4:
        raise ValueError("Invalid scrypt parallelization parameter")
    if not 16 <= normalized["dklen"] <= 64:
        raise ValueError("Invalid scrypt output length")
    return normalized


def _hash_password(
    password: str,
    salt: Optional[str] = None,
    *,
    scheme: str = _PASSWORD_SCHEME_SCRYPT,
    params: Optional[dict[str, int]] = None,
) -> tuple[str, str]:
    """Hash *password* with *salt*.  Returns ``(hash_hex, salt_hex)``."""
    if salt is None:
        salt = secrets.token_hex(16)
    if scheme == _PASSWORD_SCHEME_LEGACY_SHA256:
        digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return digest, salt
    if scheme != _PASSWORD_SCHEME_SCRYPT:
        raise ValueError(f"Unsupported password hash scheme: {scheme}")

    effective = _normalized_scrypt_params(params)
    try:
        salt_bytes = bytes.fromhex(salt)
    except ValueError:
        salt_bytes = salt.encode("utf-8")
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=int(effective["n"]),
        r=int(effective["r"]),
        p=int(effective["p"]),
        dklen=int(effective["dklen"]),
    ).hex()
    return digest, salt


def verify_password(
    password: str,
    stored_hash: str,
    salt: str,
    *,
    scheme: str = _PASSWORD_SCHEME_LEGACY_SHA256,
    params: Optional[dict[str, int]] = None,
) -> bool:
    """Verify *password* against a stored hash."""
    try:
        candidate, _ = _hash_password(
            password,
            salt,
            scheme=scheme,
            params=params,
        )
    except (MemoryError, OSError, OverflowError, TypeError, ValueError):
        return False
    return hmac.compare_digest(candidate, stored_hash)


def _set_user_password(user: dict, password: str) -> None:
    password_hash, salt = _hash_password(password)
    user["password_hash"] = password_hash
    user["password_salt"] = salt
    user["password_hash_scheme"] = _PASSWORD_SCHEME_SCRYPT
    user["password_hash_params"] = dict(_SCRYPT_PARAMS)


def _verify_user_password(user: dict, password: str) -> bool:
    scheme = str(
        user.get("password_hash_scheme") or _PASSWORD_SCHEME_LEGACY_SHA256,
    )
    raw_params = user.get("password_hash_params")
    params = raw_params if isinstance(raw_params, dict) else None
    return verify_password(
        password,
        str(user.get("password_hash") or ""),
        str(user.get("password_salt") or ""),
        scheme=scheme,
        params=params,
    )


# ---------------------------------------------------------------------------
# Token generation / verification (HMAC-SHA256, no PyJWT needed)
# ---------------------------------------------------------------------------


def _get_jwt_secret() -> str:
    """Return the signing secret, creating one if absent."""
    data = _load_auth_data()
    secret = data.get("jwt_secret", "")
    if not secret:
        secret = secrets.token_hex(32)
        data["jwt_secret"] = secret
        _save_auth_data(data)
    return secret


def create_token(username: str, expiry_seconds: Optional[int] = None) -> str:
    """Create an HMAC-signed token: ``base64(payload).signature``.

    Args:
        username: The username to encode in the token.
        expiry_seconds: Custom expiry time in seconds.
            Use -1 or 0 for permanent tokens.
            Defaults to TOKEN_EXPIRY_SECONDS (7 days).
    """
    import base64

    if expiry_seconds is None:
        expiry_seconds = TOKEN_EXPIRY_SECONDS
    elif expiry_seconds <= 0:
        # Permanent token: 100 years
        expiry_seconds = TOKEN_EXPIRY_MAX
    else:
        # Cap at maximum allowed expiry
        expiry_seconds = min(expiry_seconds, TOKEN_EXPIRY_MAX)

    secret = _get_jwt_secret()
    # Generate unique token ID (jti) for revocation support
    token_id = secrets.token_hex(16)
    payload = json.dumps(
        {
            "sub": username,
            "exp": int(time.time()) + expiry_seconds,
            "iat": int(time.time()),
            "jti": token_id,  # JWT ID for individual revocation
        },
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(
        secret.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_token(token: str) -> Optional[str]:
    """Verify *token*, return username if valid, ``None`` otherwise.

    Also checks if the token has been revoked (appears in the revocation list).
    """
    import base64

    try:
        parts = token.split(".", 1)
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        secret = _get_jwt_secret()
        expected_sig = hmac.new(
            secret.encode(),
            payload_b64.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None

        # Check if token is revoked
        jti = payload.get("jti")
        if jti and _is_token_revoked(jti):
            return None

        return payload.get("sub")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.debug("Token verification failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Auth data persistence (auth.json in SECRET_DIR)
# ---------------------------------------------------------------------------


def _validate_auth_structure(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Authentication data must be an object")
    user = data.get("user")
    if not isinstance(user, dict) or any(
        not isinstance(user.get(field), str) or not user[field]
        for field in ("username", "password_hash", "password_salt")
    ):
        raise ValueError("Authentication user record is incompatible")
    if "jwt_secret" in data and not isinstance(data["jwt_secret"], str):
        raise ValueError("Authentication signing secret is incompatible")
    return data


def _read_auth_snapshot() -> tuple[dict, bytes | None, bool]:
    """Distinguish a missing first-run file from a damaged existing file."""
    try:
        file_stat = AUTH_FILE.lstat()
    except FileNotFoundError as missing_error:
        try:
            AUTH_FILE.with_name(f"{AUTH_FILE.name}.bak").lstat()
        except FileNotFoundError:
            return {}, None, False
        except OSError as exc:
            raise AuthStorageError(
                "Authentication backup cannot be inspected",
            ) from exc
        raise AuthStorageError(
            "Authentication file is missing but backup exists",
        ) from missing_error
    except OSError as exc:
        raise AuthStorageError(
            "Authentication file cannot be inspected",
        ) from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise AuthStorageError("Authentication file is not a regular file")
    try:
        with open(AUTH_FILE, "rb") as fh:
            raw = fh.read()
        stored = _validate_auth_structure(json.loads(raw))
        needs_rewrite = any(
            isinstance(stored.get(field), str)
            and stored.get(field)
            and not is_encrypted(stored[field])
            for field in AUTH_SECRET_FIELDS
        )
        data = decrypt_dict_fields(stored, AUTH_SECRET_FIELDS)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        SecretDecryptionError,
    ) as exc:
        logger.error("Failed to load auth file %s: %s", AUTH_FILE, exc)
        raise AuthStorageError("Authentication data is unavailable") from exc
    return data, raw, needs_rewrite


def _load_auth_data() -> dict:
    """Load auth data without treating storage failures as first run."""
    data, _raw, needs_rewrite = _read_auth_snapshot()
    if needs_rewrite:
        try:
            _save_auth_data(data)
        except (AuthStorageError, OSError, ValueError) as exc:
            logger.debug("Deferred auth secret encryption migration: %s", exc)
    return data


def _write_auth_temp(raw: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix=".auth-", dir=AUTH_FILE.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        return path
    except BaseException:
        os.unlink(path)
        raise


def _fsync_auth_parent() -> None:
    fd = os.open(AUTH_FILE.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save_auth_data(data: dict) -> None:
    """Atomically save auth data and retain the previous verified file."""
    _validate_auth_structure(data)
    _existing_data, previous_bytes, _needs_rewrite = _read_auth_snapshot()
    _prepare_secret_parent(AUTH_FILE)
    encrypted_data = encrypt_dict_fields(data, AUTH_SECRET_FIELDS)
    fd, pending = tempfile.mkstemp(prefix=".auth-next-", dir=AUTH_FILE.parent)
    backup_pending: str | None = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(encrypted_data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        if previous_bytes is None:
            with open(pending, "rb") as pending_fh:
                backup_bytes = pending_fh.read()
        else:
            backup_bytes = previous_bytes
        backup_pending = _write_auth_temp(backup_bytes)
        with open(backup_pending, "rb") as backup_fh:
            if backup_fh.read() != backup_bytes:
                raise AuthStorageError(
                    "Authentication backup verification failed",
                )
        os.replace(
            backup_pending,
            AUTH_FILE.with_name(f"{AUTH_FILE.name}.bak"),
        )
        backup_pending = None
        _fsync_auth_parent()
        os.replace(pending, AUTH_FILE)
        pending = ""
        _fsync_auth_parent()
    finally:
        if pending and os.path.exists(pending):
            os.unlink(pending)
        if backup_pending and os.path.exists(backup_pending):
            os.unlink(backup_pending)


# ---------------------------------------------------------------------------
# Token revocation (blacklist management)
# ---------------------------------------------------------------------------


def _is_token_revoked(jti: str) -> bool:
    """Check if a token ID (jti) is in the revocation list.

    Uses O(1) dict lookup via revoked_tokens_meta for performance.
    """
    data = _load_auth_data()
    meta = data.get("revoked_tokens_meta", {})
    return jti in meta


def _add_to_revocation_list(jti: str, exp: int) -> None:
    """Add a token ID to the revocation list with its expiry time.

    Uses revoked_tokens_meta dict for O(1) lookups. The revoked_tokens list
    is kept for backwards compatibility but not used for access checks.
    """
    data = _load_auth_data()
    # Initialize revoked_tokens_meta if not present
    if "revoked_tokens_meta" not in data:
        data["revoked_tokens_meta"] = {}

    # O(1) check using dict
    if jti not in data["revoked_tokens_meta"]:
        data["revoked_tokens_meta"][jti] = exp

        # Also add to list for backwards compatibility
        if "revoked_tokens" not in data:
            data["revoked_tokens"] = []
        data["revoked_tokens"].append(jti)

    _save_auth_data(data)


def _clean_expired_revocations() -> None:
    """
    Remove expired tokens from the revocation list to prevent unbounded growth.
    """
    data = _load_auth_data()
    revoked = data.get("revoked_tokens", [])
    meta = data.get("revoked_tokens_meta", {})
    current_time = int(time.time())

    # Remove expired tokens
    cleaned_revoked = []
    cleaned_meta = {}

    for jti in revoked:
        exp = meta.get(jti, 0)
        if exp > current_time:
            cleaned_revoked.append(jti)
            cleaned_meta[jti] = exp

    if len(cleaned_revoked) < len(revoked):
        data["revoked_tokens"] = cleaned_revoked
        data["revoked_tokens_meta"] = cleaned_meta
        _save_auth_data(data)
        logger.info(
            "Cleaned %d expired tokens from revocation list",
            len(revoked) - len(cleaned_revoked),
        )


def is_auth_enabled() -> bool:
    """Check whether authentication is enabled via environment variable.

    Returns ``True`` when ``LANTAI_AUTH_ENABLED`` is set to a truthy
    value (``true``, ``1``, ``yes``).  The presence of a registered
    user is checked separately by the middleware so that the first
    user can still reach the registration page.
    """
    env_flag = EnvVarLoader.get_str("LANTAI_AUTH_ENABLED", "").strip().lower()
    return env_flag in ("true", "1", "yes")


def has_registered_users() -> bool:
    """Return ``True`` if a user has been registered."""
    data = _load_auth_data()
    return bool(data.get("user"))


# ---------------------------------------------------------------------------
# Registration (single-user)
# ---------------------------------------------------------------------------


def register_user(
    username: str,
    password: str,
    expiry_seconds: Optional[int] = None,
) -> Optional[str]:
    """Register the single user account.

    Args:
        username: The username to register.
        password: The password to register.
        expiry_seconds: Custom token expiry time in seconds.

    Returns a token on success, ``None`` if a user already exists.
    """
    data = _load_auth_data()

    # Only one user allowed
    if data.get("user"):
        return None

    user = {"username": username}
    _set_user_password(user, password)
    data["user"] = user

    # Ensure jwt_secret exists
    if not data.get("jwt_secret"):
        data["jwt_secret"] = secrets.token_hex(32)

    _save_auth_data(data)
    logger.info("User '%s' registered", username)
    return create_token(username, expiry_seconds)


def auto_register_from_env() -> None:
    """Auto-register admin user from environment variables.

    Called once during application startup.  If ``LANTAI_AUTH_ENABLED``
    is truthy and both ``LANTAI_AUTH_USERNAME`` and ``LANTAI_AUTH_PASSWORD``
    are set, the admin account is created automatically — useful for
    Docker, Kubernetes, server-panel, and other automated deployments
    where interactive web registration is not practical.

    Skips silently when:
    - authentication is not enabled
    - a user has already been registered
    - either env var is missing or empty
    """
    if not is_auth_enabled():
        return
    if has_registered_users():
        return

    username = EnvVarLoader.get_str("LANTAI_AUTH_USERNAME", "").strip()
    password = EnvVarLoader.get_str("LANTAI_AUTH_PASSWORD", "").strip()
    if not username or not password:
        return

    token = register_user(username, password)
    if token:
        logger.info(
            "Auto-registered user '%s' from environment variables",
            username,
        )


def update_credentials(
    current_password: str,
    new_username: Optional[str] = None,
    new_password: Optional[str] = None,
    expiry_seconds: Optional[int] = None,
) -> Optional[str]:
    """Update the registered user's username and/or password.

    Requires the current password for verification.  Returns a new
    token on success (because the username may have changed), or
    ``None`` if verification fails.

    Args:
        current_password: The current password for verification.
        new_username: The new username (optional).
        new_password: The new password (optional).
        expiry_seconds: Custom token expiry time in seconds.
    """
    data = _load_auth_data()
    user = data.get("user")
    if not user:
        return None

    if not _verify_user_password(user, current_password):
        return None

    if new_username and new_username.strip():
        user["username"] = new_username.strip()

    if new_password:
        _set_user_password(user, new_password)
        # Rotate JWT secret to invalidate all existing sessions
        data["jwt_secret"] = secrets.token_hex(32)

    data["user"] = user
    _save_auth_data(data)
    logger.info("Credentials updated for user '%s'", user["username"])
    return create_token(user["username"], expiry_seconds)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def authenticate(
    username: str,
    password: str,
    expiry_seconds: Optional[int] = None,
) -> Optional[str]:
    """Authenticate *username* / *password*.  Returns a token if valid.

    Args:
        username: The username to authenticate.
        password: The password to verify.
        expiry_seconds: Custom token expiry time in seconds.
    """
    data = _load_auth_data()
    user = data.get("user")
    if not user:
        return None
    if user.get("username") != username:
        return None
    if not _verify_user_password(user, password):
        return None
    if user.get("password_hash_scheme") != _PASSWORD_SCHEME_SCRYPT:
        _set_user_password(user, password)
        data["user"] = user
        _save_auth_data(data)
    return create_token(username, expiry_seconds)


def revoke_token(token: str) -> bool:
    """Revoke a single token by adding its jti to the blacklist.

    Args:
        token: The token string to revoke.

    Returns True on success, False on failure.
    """
    import base64

    try:
        # Extract jti and exp from token
        parts = token.split(".", 1)
        if len(parts) != 2:
            return False

        payload_b64 = parts[0]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        jti = payload.get("jti")
        exp = payload.get("exp", 0)

        if not jti:
            logger.warning("Token has no jti, cannot revoke individually")
            return False

        _add_to_revocation_list(jti, exp)
        logger.info("Token %s revoked", jti[:8])

        # Clean up expired tokens periodically
        _clean_expired_revocations()

        return True
    except Exception as exc:
        logger.error("Failed to revoke token: %s", exc)
        return False


def revoke_all_tokens() -> bool:
    """Revoke all existing tokens by rotating the JWT secret.

    This will invalidate all tokens that were issued before this call.
    Also clears the revocation list since all tokens are invalid anyway.
    Returns True on success, False on failure.
    """
    try:
        data = _load_auth_data()
        # Rotate JWT secret to invalidate all existing tokens
        data["jwt_secret"] = secrets.token_hex(32)

        # Clear revocation list since all tokens are now invalid
        data["revoked_tokens"] = []
        data["revoked_tokens_meta"] = {}

        _save_auth_data(data)
        logger.info("All tokens revoked (JWT secret rotated)")
        return True
    except Exception as exc:
        logger.error("Failed to revoke tokens: %s", exc)
        return False


# ---------------------------------------------------------------------------
# FastAPI middleware
# ---------------------------------------------------------------------------


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that checks Bearer token on protected routes."""

    @staticmethod
    def _is_managed_desktop_readiness_request(request: Request) -> bool:
        """Authenticate Electron's startup probe before account login."""
        if (
            request.method != "GET"
            or request.url.path != "/api/desktop/readiness"
        ):
            return False
        expected_token = os.environ.get("LANTAI_DESKTOP_TOKEN", "")
        if not expected_token or not os.environ.get(
            "LANTAI_DESKTOP_INSTANCE_NONCE",
            "",
        ):
            return False
        supplied_token = request.headers.get("X-Lantai-Desktop-Token", "")
        return bool(supplied_token) and hmac.compare_digest(
            supplied_token.encode("utf-8"),
            expected_token.encode("utf-8"),
        )

    @staticmethod
    def _storage_unavailable_response() -> Response:
        return Response(
            content=json.dumps(
                {
                    "detail": (
                        "Authentication data unavailable; restore "
                        "the verified auth.json.bak "
                        "or repair the original file"
                    ),
                },
            ),
            status_code=503,
            media_type="application/json",
        )

    async def dispatch(
        self,
        request: Request,
        call_next,
    ) -> Response:
        """Check Bearer token on protected API routes; skip public paths."""
        # Electron establishes backend ownership before the login UI opens.
        try:
            skip_auth = self._is_managed_desktop_readiness_request(
                request,
            ) or self._should_skip_auth(request)
            registration_required = (
                not skip_auth and not has_registered_users()
            )
        except AuthStorageError:
            return self._storage_unavailable_response()
        if skip_auth:
            return await call_next(request)
        if registration_required:
            return Response(
                content=json.dumps({"detail": "Registration required"}),
                status_code=403,
                media_type="application/json",
            )

        token = self._extract_token(request)
        user = None
        if token:
            try:
                user = verify_token(token)
            except AuthStorageError:
                return self._storage_unavailable_response()
        if user is None:
            detail = (
                "Invalid or expired token" if token else "Not authenticated"
            )
            return Response(
                content=json.dumps({"detail": detail}),
                status_code=401,
                media_type="application/json",
            )

        request.state.user = user
        return await call_next(request)

    @staticmethod
    def _should_skip_auth(request: Request) -> bool:
        """Return ``True`` when the request does not require auth."""
        path = request.url.path
        # Keep the login shell accessible when account data needs recovery.
        if not is_auth_enabled() or (
            request.method in {"GET", "HEAD"} and not path.startswith("/api/")
        ):
            return True
        registered = has_registered_users()

        if request.method == "OPTIONS":
            return True

        if not registered:
            return (
                path in _FIRST_RUN_PUBLIC_PATHS
                or any(path.startswith(p) for p in _PUBLIC_PREFIXES)
                or not path.startswith("/api/")
            )

        if path in _PUBLIC_PATHS or any(
            path.startswith(p) for p in _PUBLIC_PREFIXES
        ):
            return True

        # Only protect /api/ routes
        if not path.startswith("/api/"):
            return True

        # Check if client host is in allow_no_auth_hosts whitelist
        from ..config import load_config

        client_host = request.client.host if request.client else ""
        config = load_config()
        allowed_hosts = config.security.allow_no_auth_hosts
        return client_host in allowed_hosts

    @staticmethod
    def _extract_token(request: Request) -> Optional[str]:
        """Extract Bearer token from header or WebSocket query param."""
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        if "upgrade" in request.headers.get("connection", "").lower():
            return request.query_params.get("token")

        token = request.query_params.get("token")
        if token:
            return token
        return None
