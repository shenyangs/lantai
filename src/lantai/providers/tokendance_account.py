# -*- coding: utf-8 -*-
"""Authenticated TokenDance account APIs used by the desktop console."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import re
import time
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx

from .tokendance_oauth import TOKENDANCE_APP_URL

TOKENDANCE_PORTAL_API = "https://tokendance.space/portal/api/v1"

TokenDanceAccountErrorKind = Literal[
    "authentication",
    "authorization",
    "insufficient_balance",
    "rate_limit",
    "client",
    "server",
    "transport",
    "invalid_response",
]

_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_INSUFFICIENT_BALANCE_CODES = {
    "balance_insufficient",
    "insufficient_balance",
    "insufficient_credits",
    "insufficient_funds",
}


@dataclass(slots=True)
class TokenDanceAccountError(Exception):
    """Sanitized upstream failure safe to return through the local API."""

    status_code: int
    code: str
    message: str
    kind: TokenDanceAccountErrorKind
    outcome_unknown: bool = False

    def __str__(self) -> str:
        return self.message


class TokenDanceAccountClient:
    """Small client that never exposes the user's TokenDance API key."""

    _USAGE_CACHE_LIMIT = 64
    _usage_lock = asyncio.Lock()
    _usage_cache: dict[
        tuple[str, tuple[tuple[str, str], ...]],
        tuple[float, dict[str, Any]],
    ] = {}
    _usage_last_request_at: dict[str, float] = {}

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 10.0,
        authorization_context_id: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_key = api_key
        self._account_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
        self._usage_scope = (
            authorization_context_id or self._account_fingerprint
        )
        self._timeout = timeout
        self._monotonic = monotonic
        self._sleep = sleep

    @classmethod
    def _prune_usage_state(cls, now: float) -> None:
        for cache_key, (cached_at, _payload) in list(
            cls._usage_cache.items(),
        ):
            if now - cached_at >= 1.0:
                cls._usage_cache.pop(cache_key, None)
        for scope, requested_at in list(cls._usage_last_request_at.items()):
            if now - requested_at >= 1.0:
                cls._usage_last_request_at.pop(scope, None)

    @classmethod
    def _cap_usage_state(cls) -> None:
        while len(cls._usage_cache) > cls._USAGE_CACHE_LIMIT:
            cls._usage_cache.pop(next(iter(cls._usage_cache)))
        while len(cls._usage_last_request_at) > cls._USAGE_CACHE_LIMIT:
            cls._usage_last_request_at.pop(
                next(iter(cls._usage_last_request_at)),
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "X-App-URL": TOKENDANCE_APP_URL,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    f"{TOKENDANCE_PORTAL_API}{path}",
                    headers=self._headers(),
                    params=params,
                    json=json,
                )
        except httpx.HTTPError as exc:
            raise TokenDanceAccountError(
                502,
                "tokendance_unavailable",
                "词元跳动服务暂时不可用，请稍后重试。",
                kind="transport",
                outcome_unknown=True,
            ) from exc

        if response.is_error:
            code = "tokendance_request_failed"
            # A redemption error can echo the submitted code in upstream
            # diagnostics. Keep that entire field out of local logs and UI.
            if path != "/redemption/redeem":
                try:
                    error = response.json().get("error", {})
                    candidate = str(error.get("code") or "") if isinstance(error, dict) else ""
                    if _SAFE_ERROR_CODE.fullmatch(candidate):
                        code = candidate
                except (ValueError, AttributeError):
                    pass
            normalized_code = code.lower()
            if response.status_code == 401:
                message = "词元跳动授权已失效，请重新连接账号。"
                status_code = 409
                kind: TokenDanceAccountErrorKind = "authentication"
            elif response.status_code == 403:
                message = "当前词元跳动账号无权执行这次请求。"
                status_code = 403
                kind = "authorization"
            elif (
                response.status_code == 402
                or normalized_code in _INSUFFICIENT_BALANCE_CODES
            ):
                message = "词元跳动账户余额不足，请充值后由你确认是否继续任务。"
                status_code = 402
                kind = "insufficient_balance"
            elif response.status_code == 429:
                message = (
                    "兑换请求过于频繁，请稍后再试。"
                    if path == "/redemption/redeem"
                    else "余额或用量查询过于频繁，请稍后重试。"
                )
                status_code = 429
                kind = "rate_limit"
            elif 400 <= response.status_code < 500:
                message = (
                    "兑换码无效、已使用、已过期或兑换过于频繁，请核对后再试。"
                    if path == "/redemption/redeem"
                    else "词元跳动拒绝了这次请求，请检查输入后重试。"
                )
                status_code = 400
                kind = "client"
            else:
                message = "词元跳动服务暂时不可用，请稍后重试。"
                status_code = 502
                kind = "server"
            raise TokenDanceAccountError(
                status_code,
                code,
                message,
                kind=kind,
                outcome_unknown=kind == "server",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenDanceAccountError(
                502,
                "tokendance_invalid_response",
                "词元跳动返回了无法识别的数据。",
                kind="invalid_response",
                outcome_unknown=True,
            ) from exc
        if not isinstance(payload, dict):
            raise TokenDanceAccountError(
                502,
                "tokendance_invalid_response",
                "词元跳动返回了无法识别的数据。",
                kind="invalid_response",
                outcome_unknown=True,
            )
        return payload

    async def balance(self) -> dict[str, Any]:
        return await self._request("GET", "/user/balance")

    async def redeem(self, code: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/redemption/redeem",
            json={"code": code},
        )

    async def usage(self, params: dict[str, Any]) -> dict[str, Any]:
        # TokenDance limits this endpoint to one request per second per user.
        # Serialize calls and reuse an identical result for that interval.
        query_key = tuple(
            sorted((key, str(value)) for key, value in params.items()),
        )
        cache_key = (self._usage_scope, query_key)
        async with self._usage_lock:
            now = self._monotonic()
            self.__class__._prune_usage_state(now)
            cached = self._usage_cache.get(cache_key)
            if cached is not None and now - cached[0] < 1.0:
                return deepcopy(cached[1])
            last_request_at = self._usage_last_request_at.get(
                self._usage_scope,
            )
            if last_request_at is not None:
                delay = 1.0 - (now - last_request_at)
                if delay > 0:
                    await self._sleep(delay)
                    now = self._monotonic()
            payload = await self._request("GET", "/usage", params=params)
            completed_at = self._monotonic()
            self.__class__._usage_cache[cache_key] = (
                completed_at,
                deepcopy(payload),
            )
            self.__class__._usage_last_request_at[self._usage_scope] = (
                completed_at
            )
            self.__class__._cap_usage_state()
            return deepcopy(payload)

    async def create_payment_session(self, amount: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/payment/sessions",
            json={"amount": amount},
        )

    async def payment_session(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/payment/sessions/{session_id}")
