"""Narrow HTTPS client for beta activation and device-bound refresh."""
from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from ...security.beta_device_key import DeviceIdentity, DeviceKeyStore, DeviceKeyUnavailableError


class BetaAccessClientError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class BetaAccessClient:
    def __init__(
        self,
        *,
        base_url: str,
        device_keys: DeviceKeyStore,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("beta access service must use a fixed HTTPS origin")
        self.base_url = normalized
        self.device_keys = device_keys
        self._client_factory = client_factory

    async def _request(
        self,
        path: str,
        *,
        payload: dict[str, Any],
        access_token: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with self._client_factory(
                timeout=httpx.Timeout(15.0),
                follow_redirects=False,
            ) as client:
                response = await client.post(f"{self.base_url}{path}", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise BetaAccessClientError("network_unavailable", "内测访问服务暂时不可用。") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise BetaAccessClientError(
                "invalid_response",
                "内测访问服务返回了无法识别的响应。",
                status_code=response.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise BetaAccessClientError("invalid_response", "内测访问服务返回了无法识别的响应。")
        if response.status_code not in {200, 201, 202}:
            # An unsigned HTTP denial is diagnostic only. The caller must not
            # turn it into a durable local revocation decision.
            code = body.get("code") if isinstance(body.get("code"), str) else "request_failed"
            raise BetaAccessClientError(code, "内测访问请求未完成。", status_code=response.status_code)
        return body

    async def activate(
        self,
        *,
        code: str,
        request_id: str,
        access_token: str = "",
        device_label: str | None = None,
    ) -> dict[str, Any]:
        identity = self._load_identity()
        challenge_body = await self._request(
            "/api/beta/challenges",
            access_token=access_token,
            payload={
                "purpose": "activate",
                "request_id": request_id,
                "code": code,
                "public_jwk": identity.public_jwk,
            },
        )
        challenge = self._challenge(challenge_body)
        proof = self._sign_challenge(challenge)
        return await self._request(
            "/api/beta/activate",
            access_token=access_token,
            idempotency_key=request_id,
            payload={
                "request_id": request_id,
                "code": code,
                "public_jwk": identity.public_jwk,
                "device_label": device_label,
                "challenge_id": challenge["challengeId"],
                "proof": proof,
            },
        )

    async def refresh(self, *, license_token: str, request_id: str) -> dict[str, Any]:
        identity = self._load_identity()
        challenge_body = await self._request(
            "/api/beta/challenges",
            payload={
                "purpose": "refresh",
                "request_id": request_id,
                "license": license_token,
                "public_jwk": identity.public_jwk,
            },
        )
        challenge = self._challenge(challenge_body)
        proof = self._sign_challenge(challenge)
        response = await self._request(
            "/api/beta/licenses/refresh",
            idempotency_key=request_id,
            payload={
                "request_id": request_id,
                "license": license_token,
                "public_jwk": identity.public_jwk,
                "challenge_id": challenge["challengeId"],
                "proof": proof,
            },
        )
        # Local-only correlation data: never trust response fields to define
        # the binding against which a signed revocation is verified.
        response["_challenge_id"] = challenge["challengeId"]
        return response

    async def issue(
        self,
        *,
        request_id: str,
        access_token: str,
        device_label: str | None = None,
    ) -> dict[str, Any]:
        """Register this installation against an existing beta entitlement."""

        identity = self._load_identity()
        challenge_body = await self._request(
            "/api/beta/challenges",
            access_token=access_token,
            payload={
                "purpose": "issue",
                "request_id": request_id,
                "public_jwk": identity.public_jwk,
            },
        )
        challenge = self._challenge(challenge_body)
        proof = self._sign_challenge(challenge)
        return await self._request(
            "/api/beta/licenses/issue",
            access_token=access_token,
            idempotency_key=request_id,
            payload={
                "request_id": request_id,
                "public_jwk": identity.public_jwk,
                "device_label": device_label,
                "challenge_id": challenge["challengeId"],
                "proof": proof,
            },
        )

    async def operation_status(
        self,
        *,
        operation_id: str,
        access_token: str = "",
        recovery_token: str | None = None,
    ) -> dict[str, Any]:
        """Recover an already-committed issuance without redeeming again."""

        return await self._request(
            "/api/beta/operations/status",
            access_token=access_token,
            payload={
                "operation_id": operation_id,
                **({"recovery_token": recovery_token} if recovery_token else {}),
            },
        )

    def _load_identity(self) -> DeviceIdentity:
        try:
            return self.device_keys.load_or_create()
        except DeviceKeyUnavailableError as exc:
            raise BetaAccessClientError(
                "device_key_unavailable",
                "设备安全存储暂时不可用，请解锁系统钥匙串后重试。",
            ) from exc

    def _sign_challenge(self, challenge: dict[str, Any]) -> str:
        try:
            return self.device_keys.sign_server_challenge(challenge)
        except DeviceKeyUnavailableError as exc:
            raise BetaAccessClientError(
                "device_key_unavailable",
                "设备安全存储暂时不可用，请解锁系统钥匙串后重试。",
            ) from exc
        except ValueError as exc:
            raise BetaAccessClientError(
                "invalid_response",
                "内测访问服务返回了不可信的设备挑战。",
            ) from exc

    @staticmethod
    def _challenge(payload: dict[str, Any]) -> dict[str, Any]:
        challenge = payload.get("challenge")
        required = {
            "challengeId",
            "nonce",
            "purpose",
            "subject",
            "requestId",
            "intentCommitment",
            "deviceJkt",
            "audience",
            "expiresAt",
        }
        if not isinstance(challenge, dict) or any(not isinstance(challenge.get(key), str) or not challenge[key] for key in required):
            raise BetaAccessClientError("invalid_response", "内测访问服务没有返回完整的设备挑战。")
        return challenge
