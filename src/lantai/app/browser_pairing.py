# -*- coding: utf-8 -*-
"""Ephemeral browser pairing for the desktop loopback API.

The Electron app already proves local ownership by injecting the private
desktop token. A regular browser cannot do that, so the desktop client can
mint a short-lived pairing code. Consuming that code grants the browser an
HttpOnly cookie for the same local backend process.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Final

from fastapi import Request

BROWSER_SESSION_COOKIE: Final = "lantai_browser_session"
PAIRING_CODE_TTL_SECONDS: Final = 5 * 60
BROWSER_SESSION_TTL_SECONDS: Final = 7 * 24 * 60 * 60
_PAIRING_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PUBLIC_PAIRING_PATHS: Final = frozenset(
    {
        "/api/browser-pairing/status",
        "/api/browser-pairing/complete",
        "/api/browser-pairing/logout",
    },
)


@dataclass(frozen=True)
class PairingCode:
    code: str
    expires_at: int
    expires_in_seconds: int


@dataclass(frozen=True)
class BrowserSession:
    token: str
    expires_at: int
    expires_in_seconds: int


def is_public_browser_pairing_path(path: str) -> bool:
    return path in _PUBLIC_PAIRING_PATHS


def is_browser_pairing_path(path: str) -> bool:
    return path.startswith("/api/browser-pairing/")


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_pairing_code() -> str:
    chars = [secrets.choice(_PAIRING_ALPHABET) for _ in range(8)]
    return "".join(chars[:4]) + "-" + "".join(chars[4:])


def is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


class BrowserPairingManager:
    """Small in-memory store for pending codes and browser sessions."""

    def __init__(self) -> None:
        self._pending_codes: dict[str, int] = {}
        self._sessions: dict[str, int] = {}

    def create_code(self) -> PairingCode:
        self._cleanup()
        code = _new_pairing_code()
        expires_at = int(time.time()) + PAIRING_CODE_TTL_SECONDS
        self._pending_codes[_hash_secret(_normalize_code(code))] = expires_at
        return PairingCode(
            code=code,
            expires_at=expires_at,
            expires_in_seconds=PAIRING_CODE_TTL_SECONDS,
        )

    def consume_code(self, code: str) -> BrowserSession | None:
        self._cleanup()
        normalized = _normalize_code(code)
        if not normalized:
            return None

        code_hash = _hash_secret(normalized)
        expires_at = self._pending_codes.get(code_hash)
        if not expires_at or expires_at < time.time():
            self._pending_codes.pop(code_hash, None)
            return None

        self._pending_codes.pop(code_hash, None)
        token = secrets.token_urlsafe(32)
        session_expires_at = int(time.time()) + BROWSER_SESSION_TTL_SECONDS
        self._sessions[_hash_secret(token)] = session_expires_at
        return BrowserSession(
            token=token,
            expires_at=session_expires_at,
            expires_in_seconds=BROWSER_SESSION_TTL_SECONDS,
        )

    def is_valid_session(self, token: str | None) -> bool:
        if not token:
            return False
        self._cleanup()
        token_hash = _hash_secret(token)
        expires_at = self._sessions.get(token_hash)
        if not expires_at or expires_at < time.time():
            self._sessions.pop(token_hash, None)
            return False
        return True

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        self._sessions.pop(_hash_secret(token), None)

    def _cleanup(self) -> None:
        now = time.time()
        self._pending_codes = {
            key: expires_at
            for key, expires_at in self._pending_codes.items()
            if expires_at >= now
        }
        self._sessions = {
            key: expires_at
            for key, expires_at in self._sessions.items()
            if expires_at >= now
        }


browser_pairing_manager = BrowserPairingManager()


def get_browser_session_token(request: Request) -> str | None:
    return request.cookies.get(BROWSER_SESSION_COOKIE)


def has_valid_browser_session(request: Request) -> bool:
    if not is_loopback_request(request):
        return False
    return browser_pairing_manager.is_valid_session(
        get_browser_session_token(request),
    )


def compare_desktop_token(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied, expected)
