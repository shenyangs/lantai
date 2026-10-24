# -*- coding: utf-8 -*-
"""Optional cloud identity client for Lantai Desktop.

The browser authenticates with Watcha through the hosted account gateway.  The
renderer never receives Watcha credentials or Lantai refresh tokens; this
module keeps the short login capability in memory and persists only encrypted
Lantai session tokens.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from ..constant import EnvVarLoader
from ..security.cloud_account_store import (
    clear_cloud_account,
    load_cloud_account,
    save_cloud_account,
)

DEFAULT_ACCOUNT_SERVICE_URL = "https://auth.lantai.guanlan.xin"
LOGIN_POLL_INTERVAL_SECONDS = 2


class CloudAccountError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass
class PendingCloudLogin:
    attempt_id: str
    code_verifier: str
    expires_at: str
    status: str = "pending"
    error: str = ""


class PendingCloudLoginRegistry:
    def __init__(self) -> None:
        self._items: dict[str, PendingCloudLogin] = {}
        self._lock = Lock()

    def create(
        self,
        *,
        attempt_id: str,
        code_verifier: str,
        expires_at: str,
    ) -> str:
        with self._lock:
            # The local desktop has one interactive owner. A new login attempt
            # supersedes any abandoned browser window and keeps the in-memory
            # verifier registry bounded.
            self._items.clear()
            self._items[attempt_id] = PendingCloudLogin(
                attempt_id=attempt_id,
                code_verifier=code_verifier,
                expires_at=expires_at,
            )
        return attempt_id

    def get(self, attempt_id: str) -> PendingCloudLogin | None:
        with self._lock:
            return self._items.get(attempt_id)

    def remove(self, attempt_id: str) -> None:
        with self._lock:
            self._items.pop(attempt_id, None)

    def finish(self, attempt_id: str, *, status: str, error: str = "") -> None:
        with self._lock:
            pending = self._items.get(attempt_id)
            if pending is not None:
                pending.status = status
                pending.error = error


pending_cloud_logins = PendingCloudLoginRegistry()


def account_service_url() -> str:
    value = EnvVarLoader.get_str(
        "LANTAI_ACCOUNT_SERVICE_URL",
        DEFAULT_ACCOUNT_SERVICE_URL,
    ).strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise CloudAccountError(
            "account_service_url_invalid",
            "澜台账号服务地址无效。",
            status_code=500,
        )
    return value


class CloudAccountService:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self.base_url = (base_url or account_service_url()).rstrip("/")
        self._client_factory = client_factory

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        access_token: str = "",
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        try:
            async with self._client_factory(
                timeout=httpx.Timeout(15.0),
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise CloudAccountError(
                "account_service_unreachable",
                "暂时无法连接澜台账号服务，请稍后重试。",
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudAccountError(
                "account_service_invalid_response",
                "澜台账号服务返回了无法识别的响应。",
            ) from exc
        if not isinstance(payload, dict):
            raise CloudAccountError(
                "account_service_invalid_response",
                "澜台账号服务返回了无法识别的响应。",
            )
        return response.status_code, payload

    async def start_watcha(self, *, desktop_redirect_uri: str) -> dict[str, Any]:
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest(),
        ).rstrip(b"=").decode("ascii")
        status, payload = await self._request(
            "POST",
            "/api/auth/watcha/start",
            json={
                "desktopRedirectUri": desktop_redirect_uri,
                "desktopCodeChallenge": code_challenge,
            },
        )
        if status != 200:
            raise self._response_error(status, payload)
        required = ("flowId", "authorizationUrl", "expiresAt")
        if any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
            raise CloudAccountError(
                "account_service_invalid_response",
                "澜台账号服务没有返回完整的登录信息。",
            )
        attempt_id = pending_cloud_logins.create(
            attempt_id=payload["flowId"],
            code_verifier=code_verifier,
            expires_at=payload["expiresAt"],
        )
        return {
            "attempt_id": attempt_id,
            "authorization_url": payload["authorizationUrl"],
            "expires_at": payload["expiresAt"],
            "poll_interval_seconds": LOGIN_POLL_INTERVAL_SECONDS,
        }

    async def poll_watcha(self, attempt_id: str) -> dict[str, Any]:
        pending = pending_cloud_logins.get(attempt_id)
        if pending is None:
            raise CloudAccountError(
                "login_attempt_missing",
                "登录请求不存在或应用已经重启，请重新使用观猹登录。",
                status_code=410,
            )
        if pending.status == "pending":
            return {"status": "pending"}
        if pending.status == "failed":
            pending_cloud_logins.remove(attempt_id)
            raise CloudAccountError(
                pending.error or "authorization_failed",
                "观猹没有完成本次授权，请重新登录。",
                status_code=400,
            )
        if pending.status != "complete":
            raise CloudAccountError(
                "login_attempt_invalid",
                "登录请求状态无效，请重新使用观猹登录。",
                status_code=410,
            )
        pending_cloud_logins.remove(attempt_id)
        return self._public_status(load_cloud_account())

    def cancel_watcha(self, attempt_id: str) -> dict[str, bool]:
        pending_cloud_logins.remove(attempt_id)
        return {"cancelled": True}

    async def complete_watcha(
        self,
        *,
        attempt_id: str,
        ticket: str = "",
        error: str = "",
    ) -> bool:
        pending = pending_cloud_logins.get(attempt_id)
        if pending is None:
            raise CloudAccountError(
                "login_attempt_missing",
                "登录请求不存在或应用已经重启，请返回澜台重新登录。",
                status_code=410,
            )
        if error or not ticket:
            pending_cloud_logins.finish(
                attempt_id,
                status="failed",
                error=error or "authorization_failed",
            )
            return False
        try:
            status, payload = await self._request(
                "POST",
                "/api/auth/watcha/exchange",
                json={"ticket": ticket, "codeVerifier": pending.code_verifier},
            )
            if status != 200:
                raise self._response_error(status, payload)
            tokens = payload.get("tokens")
            user = payload.get("user")
            if not isinstance(tokens, dict) or not isinstance(user, dict):
                raise CloudAccountError(
                    "account_service_invalid_response",
                    "澜台账号服务没有返回完整的账号会话。",
                )
            access_token = str(tokens.get("accessToken") or "")
            refresh_token = str(tokens.get("refreshToken") or "")
            if not access_token or not refresh_token:
                raise CloudAccountError(
                    "account_service_invalid_response",
                    "澜台账号服务没有返回完整的账号会话。",
                )
            save_cloud_account(
                {
                    "service_url": self.base_url,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "user": user,
                    "membership": payload.get("membership") or {},
                    "provider": "watcha",
                    "updated_at": int(time.time()),
                },
            )
        except CloudAccountError as exc:
            pending_cloud_logins.finish(
                attempt_id,
                status="failed",
                error=exc.code,
            )
            raise
        except Exception as exc:
            pending_cloud_logins.finish(
                attempt_id,
                status="failed",
                error="cloud_account_store_failed",
            )
            raise CloudAccountError(
                "cloud_account_store_failed",
                "澜台账号已经验证，但本机无法安全保存登录状态。",
                status_code=500,
            ) from exc
        pending_cloud_logins.finish(attempt_id, status="complete")
        return True

    async def status(self) -> dict[str, Any]:
        account = load_cloud_account()
        if not account.get("refresh_token"):
            return self._public_status({})

        access_token = str(account.get("access_token") or "")
        status, payload = await self._request(
            "GET",
            "/api/auth/me",
            access_token=access_token,
        )
        if status == 401:
            account = await self._refresh(account)
            status, payload = await self._request(
                "GET",
                "/api/auth/me",
                access_token=str(account.get("access_token") or ""),
            )
        if status != 200:
            raise self._response_error(status, payload)
        if isinstance(payload.get("user"), dict):
            account["user"] = payload["user"]
        if isinstance(payload.get("membership"), dict):
            account["membership"] = payload["membership"]
        account["updated_at"] = int(time.time())
        save_cloud_account(account)
        return self._public_status(account)

    async def _refresh(self, account: dict[str, Any]) -> dict[str, Any]:
        status, payload = await self._request(
            "POST",
            "/api/auth/refresh",
            json={"refreshToken": account.get("refresh_token")},
        )
        if status != 200:
            if status in (401, 403):
                clear_cloud_account()
            raise self._response_error(status, payload)
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict) or not tokens.get("accessToken"):
            raise CloudAccountError(
                "account_service_invalid_response",
                "澜台账号服务没有返回新的访问令牌。",
            )
        account["access_token"] = str(tokens["accessToken"])
        if isinstance(payload.get("user"), dict):
            account["user"] = payload["user"]
        if isinstance(payload.get("membership"), dict):
            account["membership"] = payload["membership"]
        save_cloud_account(account)
        return account

    async def logout(self) -> dict[str, Any]:
        account = load_cloud_account()
        confirmed = False
        if account.get("access_token"):
            try:
                status, _ = await self._request(
                    "POST",
                    "/api/auth/logout",
                    access_token=str(account["access_token"]),
                )
                confirmed = status == 200
            except CloudAccountError:
                confirmed = False
        clear_cloud_account()
        return {"connected": False, "remote_revocation_confirmed": confirmed}

    @staticmethod
    def _response_error(status: int, payload: dict[str, Any]) -> CloudAccountError:
        code = str(payload.get("code") or "account_service_error")
        message = str(payload.get("error") or "澜台账号服务暂时无法完成请求。")
        return CloudAccountError(code, message, status_code=status)

    @staticmethod
    def _public_status(account: dict[str, Any]) -> dict[str, Any]:
        user = account.get("user") if isinstance(account.get("user"), dict) else None
        return {
            "connected": bool(account.get("refresh_token") and user),
            "provider": account.get("provider") if user else None,
            "user": user,
            "membership": account.get("membership") if user else None,
            "service_url": account.get("service_url") or DEFAULT_ACCOUNT_SERVICE_URL,
        }


cloud_account_service = CloudAccountService()
