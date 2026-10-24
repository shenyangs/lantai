# -*- coding: utf-8 -*-
"""TokenDance Authorization Code + S256 PKCE API-key acquisition."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import secrets
import time
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel, Field

TOKENDANCE_APP_URL = "https://lantai.guanlan.xin"
TOKENDANCE_AUTHORIZE_URL = "https://tokendance.space/auth"
TOKENDANCE_EXCHANGE_URL = "https://tokendance.space/portal/api/v1/auth/keys"
TOKENDANCE_CALLBACK_PATH = "/api/models/tokendance/oauth/callback"


def resolve_tokendance_callback_base_url(
    request_url: str,
    configured_callback_url: str | None = None,
) -> str:
    """Use loopback on desktop, or an explicitly configured HTTPS origin."""

    request = urlsplit(request_url)
    if not configured_callback_url:
        if request.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("A cloud OAuth callback must be configured")
        return f"http://127.0.0.1:{request.port or 8088}{TOKENDANCE_CALLBACK_PATH}"

    callback_url = configured_callback_url.strip()
    callback = urlsplit(callback_url)
    hostname = callback.hostname or ""
    is_loopback = hostname.rstrip(".") == "localhost" or hostname.rstrip(
        ".",
    ).endswith(".localhost")
    try:
        address = ipaddress.ip_address(hostname)
        is_loopback = is_loopback or address.is_loopback or (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
            and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        pass
    if (
        callback.scheme != "https"
        or not hostname
        or is_loopback
        or callback.username is not None
        or callback.password is not None
        or callback.path != TOKENDANCE_CALLBACK_PATH
        or "?" in callback_url
        or "#" in callback_url
        or "\\" in callback_url
        or any(char.isspace() for char in callback_url)
    ):
        raise ValueError("Invalid cloud OAuth callback")
    if (request.scheme, request.hostname, request.port or 443) != (
        callback.scheme,
        callback.hostname,
        callback.port or 443,
    ):
        raise ValueError("OAuth callback origin does not match this instance")
    return callback_url


class TokenDanceOAuthStart(BaseModel):
    flow_id: str
    authorization_url: str
    expires_in: int = 600


class TokenDanceOAuthStatus(BaseModel):
    status: str = Field(pattern="^(pending|exchanging|succeeded|failed|expired)$")
    message: str = ""


@dataclass
class _PendingFlow:
    code_verifier: str
    created_at: float
    owner_subject_id: str
    account_epoch: int
    use_kinds: tuple[str, ...]
    callback_base_url: str
    status: str = "pending"
    message: str = ""


class TokenDanceOAuthRegistry:
    """Process-local single-use PKCE state; never persists the verifier."""

    def __init__(self, ttl_seconds: int = 600) -> None:
        self.ttl_seconds = ttl_seconds
        self._flows: dict[str, _PendingFlow] = {}
        self._lock = Lock()

    @staticmethod
    def _challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def create(
        self,
        *,
        callback_base_url: str,
        owner_subject_id: str,
        account_epoch: int,
        use_kinds: tuple[str, ...] = (
            "balance",
            "model",
            "payment",
            "usage",
        ),
    ) -> TokenDanceOAuthStart:
        if not owner_subject_id or account_epoch < 1:
            raise ValueError("A current Watcha account context is required")
        flow_id = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        callback_url = f"{callback_base_url}?{urlencode({'flow': flow_id})}"
        with self._lock:
            self._flows[flow_id] = _PendingFlow(
                code_verifier=verifier,
                created_at=time.monotonic(),
                owner_subject_id=owner_subject_id,
                account_epoch=account_epoch,
                use_kinds=use_kinds,
                callback_base_url=callback_base_url,
            )
        authorize_params = {
            "callback_url": callback_url,
            "code_challenge": self._challenge(verifier),
            "code_challenge_method": "S256",
            "app_url": TOKENDANCE_APP_URL,
            "key_name": "澜台",
        }
        authorization_url = f"{TOKENDANCE_AUTHORIZE_URL}?{urlencode(authorize_params)}"
        return TokenDanceOAuthStart(
            flow_id=flow_id,
            authorization_url=authorization_url,
            expires_in=self.ttl_seconds,
        )

    def _get(self, flow_id: str) -> _PendingFlow:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise ValueError("OAuth flow not found or expired")
        if time.monotonic() - flow.created_at > self.ttl_seconds:
            flow.status = "expired"
            flow.code_verifier = ""
            raise ValueError("OAuth flow expired")
        return flow

    def begin_exchange(
        self,
        flow_id: str,
        *,
        callback_base_url: str,
    ) -> _PendingFlow:
        with self._lock:
            flow = self._get(flow_id)
            if flow.callback_base_url != callback_base_url:
                raise ValueError("OAuth callback changed after authorization started")
            if flow.status != "pending":
                raise ValueError("OAuth flow is already being completed")
            flow.status = "exchanging"
            return _PendingFlow(**flow.__dict__)

    def complete(self, flow_id: str) -> None:
        with self._lock:
            flow = self._get(flow_id)
            flow.status = "succeeded"
            flow.code_verifier = ""
            flow.message = "TokenDance authorization completed"

    def fail(self, flow_id: str, message: str) -> None:
        with self._lock:
            try:
                flow = self._get(flow_id)
            except ValueError:
                return
            flow.status = "failed"
            flow.code_verifier = ""
            flow.message = message

    def status(
        self,
        flow_id: str,
        *,
        owner_subject_id: str | None = None,
        account_epoch: int | None = None,
    ) -> TokenDanceOAuthStatus:
        with self._lock:
            flow = self._flows.get(flow_id)
            if flow is not None and owner_subject_id is not None:
                if (
                    flow.owner_subject_id != owner_subject_id
                    or flow.account_epoch != account_epoch
                ):
                    raise ValueError("OAuth flow account context changed")
            try:
                flow = self._get(flow_id)
            except ValueError as exc:
                message = str(exc)
                status = "expired" if "expired" in message else "failed"
                return TokenDanceOAuthStatus(status=status, message=message)
            return TokenDanceOAuthStatus(
                status=flow.status,
                message=flow.message,
            )


async def exchange_authorization_code(
    *,
    code: str,
    code_verifier: str,
    timeout: float = 15,
) -> str:
    """Exchange a one-time code and return the new key without logging it."""

    if not code.strip() or not code_verifier:
        raise ValueError("OAuth code or verifier is missing")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                TOKENDANCE_EXCHANGE_URL,
                headers={"Content-Type": "application/json"},
                json={
                    "code": code.strip(),
                    "code_verifier": code_verifier,
                    "code_challenge_method": "S256",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("TokenDance authorization exchange failed") from exc
    key = str(payload.get("key") or "").strip() if isinstance(payload, dict) else ""
    if not key:
        raise ValueError("TokenDance authorization returned no API key")
    return key


tokendance_oauth_registry = TokenDanceOAuthRegistry()
