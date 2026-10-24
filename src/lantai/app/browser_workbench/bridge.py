# -*- coding: utf-8 -*-
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

from .models import BrowserAction

_ACTION_CAPABILITY_TTL_SECONDS = 10.0


class ElectronBrowserBridgeClient:
    """Tiny HTTP client for the Electron Browser Workbench bridge."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("LANTAI_BROWSER_BRIDGE_URL") or "").rstrip(
            "/",
        )
        self.token = token or os.getenv("LANTAI_BROWSER_BRIDGE_TOKEN") or ""
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.token)

    async def execute(self, action: BrowserAction) -> dict[str, Any]:
        if not self.available:
            return {
                "ok": False,
                "error": "Electron Browser Bridge is not available in this runtime.",
                "error_code": "electron_bridge_unavailable",
                "runtime": "electron_bridge",
            }
        return await asyncio.to_thread(
            self._post_action,
            action,
            self._timeout_for_action(action),
        )

    async def get_state(self) -> dict[str, Any]:
        if not self.available:
            return {
                "ok": False,
                "error": "Electron Browser Bridge is not available in this runtime.",
                "error_code": "electron_bridge_unavailable",
                "runtime": "electron_bridge",
            }
        return await asyncio.to_thread(self._get_state)

    def _post_action(
        self,
        action: BrowserAction,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        payload = action.model_dump(mode="json")
        # Electron bridge accepts tab_id while legacy browser_use accepts page_id.
        payload["tab_id"] = payload.get("page_id") or "default"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/action",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Lantai-Browser-Bridge-Capability": (
                    self._action_capability(body)
                ),
            },
        )
        try:
            with urllib.request.urlopen(
                req,
                timeout=timeout_seconds or self.timeout_seconds,
            ) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return self._http_error_payload(exc)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Electron Browser Bridge request failed: {exc}",
                "error_code": "electron_bridge_request_failed",
                "runtime": "electron_bridge",
            }
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": "Electron Browser Bridge returned invalid JSON.",
                "error_code": "electron_bridge_invalid_json",
                "runtime": "electron_bridge",
                "text": raw,
            }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "error": "Electron Browser Bridge returned non-object JSON.",
                "error_code": "electron_bridge_invalid_response",
                "runtime": "electron_bridge",
            }
        payload.setdefault("runtime", "electron_bridge")
        return payload

    def _action_capability(self, body: bytes) -> str:
        expires_at_ms = int(
            (time.time() + _ACTION_CAPABILITY_TTL_SECONDS) * 1000,
        )
        nonce = secrets.token_hex(16)
        body_hash = hashlib.sha256(body).hexdigest()
        message = "\n".join(
            ["v1", str(expires_at_ms), nonce, body_hash],
        ).encode("utf-8")
        signature = hmac.new(
            self.token.encode("utf-8"),
            message,
            hashlib.sha256,
        ).hexdigest()
        return ":".join(
            ["v1", str(expires_at_ms), nonce, body_hash, signature],
        )

    def _timeout_for_action(self, action: BrowserAction) -> float:
        raw_timeout = action.metadata.get("browser_task_action_timeout_seconds")
        try:
            action_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return self.timeout_seconds
        if action_timeout <= 0:
            return self.timeout_seconds
        return max(0.001, min(self.timeout_seconds, action_timeout))

    def _get_state(self) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/state",
            method="GET",
            headers={
                "X-Lantai-Browser-Bridge-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return self._http_error_payload(exc)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Electron Browser Bridge state request failed: {exc}",
                "error_code": "electron_bridge_state_request_failed",
                "runtime": "electron_bridge",
            }
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": "Electron Browser Bridge returned invalid state JSON.",
                "error_code": "electron_bridge_invalid_state_json",
                "runtime": "electron_bridge",
                "text": raw,
            }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "error": "Electron Browser Bridge returned non-object state.",
                "error_code": "electron_bridge_invalid_state",
                "runtime": "electron_bridge",
            }
        payload.setdefault("runtime", "electron_bridge")
        return payload

    @staticmethod
    def _http_error_payload(exc: urllib.error.HTTPError) -> dict[str, Any]:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": raw or str(exc)}
        if not isinstance(payload, dict):
            payload = {"ok": False, "error": str(exc)}
        status = int(getattr(exc, "code", 0) or 0)
        payload.setdefault("ok", False)
        payload.setdefault("runtime", "electron_bridge")
        payload.setdefault("http_status", status)
        payload.setdefault(
            "error_code",
            "electron_bridge_unauthorized"
            if status == 401
            else "electron_bridge_http_error",
        )
        return payload
