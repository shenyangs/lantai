"""Authenticated HTTP client for Electron's loopback Mac Use bridge."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Any

from .models import WRITE_ACTIONS, MacUseActionRequest, MacUseActionResult

_CAPABILITY_TTL_SECONDS = 10.0


class ElectronMacUseBridgeClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("LANTAI_MAC_USE_BRIDGE_URL") or "").rstrip("/")
        self.token = token or os.getenv("LANTAI_MAC_USE_BRIDGE_TOKEN") or ""
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.token)

    async def execute(self, request: MacUseActionRequest) -> MacUseActionResult:
        if not self.available:
            return MacUseActionResult.unavailable(request)
        payload = await asyncio.to_thread(self._post, request)
        try:
            return MacUseActionResult.model_validate(payload)
        except Exception as exc:
            if request.action in WRITE_ACTIONS:
                return MacUseActionResult(
                    action=request.action,
                    run_id=request.run_id,
                    action_id=request.action_id,
                    effect_id=request.effect_id,
                    ok=False,
                    dispatch_status="unknown",
                    effect_status="ambiguous",
                    error=f"Mac Use bridge returned an invalid response: {exc}",
                    error_code="mac_use_bridge_invalid_response",
                    retry_safe=False,
                )
            return MacUseActionResult(
                action=request.action,
                run_id=request.run_id,
                action_id=request.action_id,
                effect_id=request.effect_id,
                ok=False,
                dispatch_status="transport_failed",
                effect_status="failed",
                error=f"Mac Use bridge returned an invalid response: {exc}",
                error_code="mac_use_bridge_invalid_response",
                retry_safe=True,
            )

    def _post(self, request: MacUseActionRequest) -> dict[str, Any]:
        body = json.dumps(request.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/action",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Lantai-Mac-Use-Capability": self._capability(body),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"ok": False, "error": raw or str(exc)}
            if isinstance(payload, dict):
                return payload
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            write = request.action in WRITE_ACTIONS
            return {
                **self._identity(request),
                "ok": False,
                "dispatch_status": "unknown" if write else "transport_failed",
                "effect_status": "ambiguous" if write else "failed",
                "error": f"Mac Use bridge request failed: {exc}",
                "error_code": "mac_use_bridge_request_failed",
                "retry_safe": not write,
            }
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            write = request.action in WRITE_ACTIONS
            return {
                **self._identity(request),
                "ok": False,
                "dispatch_status": "unknown" if write else "transport_failed",
                "effect_status": "ambiguous" if write else "failed",
                "error": "Mac Use bridge returned non-object JSON.",
                "error_code": "mac_use_bridge_invalid_json",
                "retry_safe": not write,
            }
        return payload

    def _capability(self, body: bytes) -> str:
        expires_at_ms = int((time.time() + _CAPABILITY_TTL_SECONDS) * 1000)
        nonce = secrets.token_hex(16)
        body_hash = hashlib.sha256(body).hexdigest()
        message = "\n".join(["v1", str(expires_at_ms), nonce, body_hash]).encode("utf-8")
        signature = hmac.new(self.token.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return ":".join(["v1", str(expires_at_ms), nonce, body_hash, signature])

    @staticmethod
    def _identity(request: MacUseActionRequest) -> dict[str, Any]:
        return {
            "protocol_version": 1,
            "action": request.action,
            "run_id": request.run_id,
            "action_id": request.action_id,
            "effect_id": request.effect_id,
            "policy_status": "allowed",
        }
