# -*- coding: utf-8 -*-
"""Deep connector lifecycle primitives for MCP transports.

This module owns OAuth/PKCE and diagnostic classification.  It deliberately
does not own MCP connection state; ``MCPClientManager`` remains that source of
truth and calls the small preparation interface defined here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import httpx

from ...config.config import MCPClientConfig, MCPOAuthConfig
from .network_security import (
    create_pinned_mcp_http_client,
    resolve_mcp_endpoint,
)

OAUTH_STATE_TTL_SECONDS = 10 * 60
OAUTH_EXPIRY_SKEW_SECONDS = 60


class ConnectorLifecycleError(RuntimeError):
    """Structured connector error safe for API/UI classification."""

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class PendingOAuthAuthorization:
    state: str
    agent_id: str
    client_key: str
    verifier: str
    redirect_uri: str
    expires_at: int


@dataclass(frozen=True)
class OAuthStart:
    authorization_url: str
    state: str
    expires_at: int


class OAuthPendingRegistry:
    """Small single-use, in-memory capability registry for OAuth callbacks."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingOAuthAuthorization] = {}
        self._lock = Lock()

    def create(
        self,
        *,
        agent_id: str,
        client_key: str,
        oauth: MCPOAuthConfig,
        redirect_uri: str,
        now: int | None = None,
    ) -> OAuthStart:
        current = int(now if now is not None else time.time())
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest(),
        ).rstrip(b"=").decode("ascii")
        expires_at = current + OAUTH_STATE_TTL_SECONDS
        pending = PendingOAuthAuthorization(
            state=state,
            agent_id=agent_id,
            client_key=client_key,
            verifier=verifier,
            redirect_uri=redirect_uri,
            expires_at=expires_at,
        )
        with self._lock:
            self._cleanup_locked(current)
            self._pending[state] = pending

        params = {
            "response_type": "code",
            "client_id": oauth.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **oauth.extra_authorize_params,
        }
        if oauth.scopes:
            params["scope"] = " ".join(oauth.scopes)
        separator = "&" if "?" in oauth.authorization_url else "?"
        return OAuthStart(
            authorization_url=(
                oauth.authorization_url + separator + urlencode(params)
            ),
            state=state,
            expires_at=expires_at,
        )

    def consume(
        self,
        state: str,
        *,
        now: int | None = None,
    ) -> PendingOAuthAuthorization:
        current = int(now if now is not None else time.time())
        with self._lock:
            pending = self._pending.pop(state, None)
            self._cleanup_locked(current)
        if pending is None:
            raise ConnectorLifecycleError(
                "oauth_state_invalid",
                "授权请求不存在、已过期或已经使用。请从澜台重新发起连接。",
                status_code=400,
            )
        if pending.expires_at < current:
            raise ConnectorLifecycleError(
                "oauth_state_expired",
                "授权请求已过期。请从澜台重新发起连接。",
                status_code=400,
            )
        return pending

    def _cleanup_locked(self, now: int) -> None:
        expired = [
            key for key, value in self._pending.items() if value.expires_at < now
        ]
        for key in expired:
            self._pending.pop(key, None)


oauth_pending_registry = OAuthPendingRegistry()
_refresh_locks: dict[tuple[str, str], asyncio.Lock] = {}
_refresh_lock_users: dict[tuple[str, str], int] = {}
_refresh_locks_guard = Lock()


@asynccontextmanager
async def _hold_refresh_lock(
    key: tuple[str, str],
) -> AsyncIterator[None]:
    """Serialize one connector refresh without retaining idle lock keys."""

    with _refresh_locks_guard:
        lock = _refresh_locks.setdefault(key, asyncio.Lock())
        _refresh_lock_users[key] = _refresh_lock_users.get(key, 0) + 1
    try:
        async with lock:
            yield
    finally:
        with _refresh_locks_guard:
            remaining = _refresh_lock_users.get(key, 1) - 1
            if remaining <= 0:
                _refresh_lock_users.pop(key, None)
                if _refresh_locks.get(key) is lock:
                    _refresh_locks.pop(key, None)
            else:
                _refresh_lock_users[key] = remaining


def oauth_access_token_valid(oauth: MCPOAuthConfig, *, now: int | None = None) -> bool:
    """Return whether an access token exists and is outside the expiry skew."""

    if not oauth.access_token:
        return False
    if oauth.expires_at is None:
        return True
    current = int(now if now is not None else time.time())
    return oauth.expires_at > current + OAUTH_EXPIRY_SKEW_SECONDS


def oauth_headers(client_config: MCPClientConfig) -> dict[str, str]:
    """Return effective HTTP headers without mutating the config model."""

    headers = dict(client_config.headers)
    oauth = client_config.oauth
    if oauth.enabled and oauth.access_token:
        token_type = oauth.token_type.strip() or "Bearer"
        headers["Authorization"] = f"{token_type} {oauth.access_token}"
    return headers


async def exchange_oauth_code(
    oauth: MCPOAuthConfig,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    http_client: httpx.AsyncClient | None = None,
    allow_private_network: bool = False,
    now: int | None = None,
) -> MCPOAuthConfig:
    """Exchange one authorization code and return an updated OAuth config."""

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": oauth.client_id,
        "code_verifier": verifier,
        **oauth.extra_token_params,
    }
    if oauth.client_secret:
        form["client_secret"] = oauth.client_secret
    return await _request_tokens(
        oauth,
        form=form,
        http_client=http_client,
        allow_private_network=allow_private_network,
        now=now,
    )


async def refresh_oauth_token(
    oauth: MCPOAuthConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
    allow_private_network: bool = False,
    now: int | None = None,
) -> MCPOAuthConfig:
    """Refresh an OAuth token or raise an authorization-classified error."""

    if not oauth.refresh_token:
        raise ConnectorLifecycleError(
            "authorization_required",
            "授权已失效且没有可用的刷新令牌，请重新连接。",
            status_code=409,
        )
    form = {
        "grant_type": "refresh_token",
        "refresh_token": oauth.refresh_token,
        "client_id": oauth.client_id,
        **oauth.extra_token_params,
    }
    if oauth.client_secret:
        form["client_secret"] = oauth.client_secret
    return await _request_tokens(
        oauth,
        form=form,
        http_client=http_client,
        allow_private_network=allow_private_network,
        now=now,
    )


async def _request_tokens(
    oauth: MCPOAuthConfig,
    *,
    form: dict[str, str],
    http_client: httpx.AsyncClient | None,
    allow_private_network: bool,
    now: int | None,
) -> MCPOAuthConfig:
    owned_client = http_client is None
    if http_client is None:
        endpoint = await resolve_mcp_endpoint(
            oauth.token_url,
            allow_private_network=allow_private_network,
        )
        client = create_pinned_mcp_http_client(
            endpoint,
            timeout=httpx.Timeout(20.0),
        )
    else:
        client = http_client
    try:
        response = await client.post(
            oauth.token_url,
            data=form,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ConnectorLifecycleError(
            "oauth_token_exchange_failed",
            "认证服务未能签发令牌，请检查 OAuth 配置后重试。",
            status_code=502,
        ) from exc
    finally:
        if owned_client:
            await client.aclose()

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise ConnectorLifecycleError(
            "oauth_token_missing",
            "认证服务响应中没有 access_token。",
            status_code=502,
        )
    current = int(now if now is not None else time.time())
    expires_in = payload.get("expires_in")
    try:
        expires_at = current + max(0, int(expires_in)) if expires_in is not None else None
    except (TypeError, ValueError):
        expires_at = None

    return oauth.model_copy(
        update={
            "access_token": access_token,
            "refresh_token": str(
                payload.get("refresh_token") or oauth.refresh_token or "",
            ),
            "expires_at": expires_at,
            "token_type": str(payload.get("token_type") or oauth.token_type or "Bearer"),
        },
    )


async def prepare_client_oauth(
    *,
    agent_id: str,
    client_key: str,
    client_config: MCPClientConfig,
) -> MCPClientConfig:
    """Refresh an expiring token, persist it, and return prepared config."""

    oauth = client_config.oauth
    if not oauth.enabled or oauth_access_token_valid(oauth):
        return client_config

    async with _hold_refresh_lock((agent_id, client_key)):
        from ...config.config import load_agent_config, save_agent_config

        agent_config = load_agent_config(agent_id)
        current = (agent_config.mcp.clients or {}).get(client_key) if agent_config.mcp else None
        if current is None:
            raise ConnectorLifecycleError(
                "configuration_missing",
                f"连接器 '{client_key}' 不存在。",
                status_code=404,
            )
        if oauth_access_token_valid(current.oauth):
            return current
        current.oauth = await refresh_oauth_token(
            current.oauth,
            allow_private_network=current.allow_private_network,
        )
        save_agent_config(agent_id, agent_config)
        return current


def classify_connector_error(exc: BaseException) -> tuple[str, str]:
    """Map implementation exceptions to a stable user-facing error class."""

    if isinstance(exc, ConnectorLifecycleError):
        category = (
            "authorization"
            if exc.code.startswith("oauth") or exc.code == "authorization_required"
            else "configuration"
        )
        return category, exc.code
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "connection", "connection_timeout"
    if isinstance(exc, (ConnectionError, OSError, httpx.NetworkError)):
        return "connection", "connection_failed"
    message = str(exc).lower()
    if any(marker in message for marker in ("401", "403", "unauthorized", "forbidden")):
        return "authorization", "authorization_rejected"
    if any(marker in message for marker in ("invalid", "required", "missing")):
        return "configuration", "configuration_invalid"
    return "server", "connector_server_error"
