# -*- coding: utf-8 -*-
"""Credentials and raw Search/Fetch transport for Lantai's unified Web tools.

This store is independent of chat providers. Legacy quick-search consent and
encrypted credentials are read through until the first save or successful use;
the original file and its token accounting are retained for recovery.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from ..constant import SECRET_DIR
from ..security.secret_store import decrypt, encrypt

_STORE_LOCK = threading.RLock()
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST: dict[str, float] = {}
_MAX_RESPONSE_BYTES = 2_097_152


class DeepSeekWebError(RuntimeError):
    """A sanitized transport failure, safe to expose in tool results."""

    def __init__(self, message: str, *, code: str = "service_unavailable"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WebSearchStatus:
    configured: bool
    enabled: bool
    provider: str = "bing_rss"
    search_count: int = 0
    fetch_count: int = 0
    last_used_at: str = ""
    migrated_from_quick_search: bool = False
    credential_error: bool = False


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0


class WebSearchStore:
    """Encrypted Web credential and successful, nonempty request counters."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        legacy_path: Path | None = None,
    ) -> None:
        self.path = path or SECRET_DIR / "auxiliary" / "web_search.json"
        self.legacy_path = legacy_path or self.path.with_name(
            "deepseek_quick_search.json",
        )

    def _read(self) -> dict[str, Any]:
        source = self.path if self.path.is_file() else self.legacy_path
        if not source.is_file():
            return {}
        try:
            data = json.loads(source.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except (OSError, ValueError) as exc:
            raise ValueError(
                "Web search configuration could not be read."
            ) from exc
        if source == self.path:
            return data
        return {
            "version": 2,
            "api_key": str(data.get("api_key") or ""),
            "enabled": bool(data.get("enabled", False)),
            "search_count": 0,
            "fetch_count": 0,
            "last_used_at": "",
            "migrated_from_quick_search": True,
            "legacy_usage": {
                name: data.get(name, "" if name == "last_used_at" else 0)
                for name in (
                    "call_count",
                    "input_tokens",
                    "output_tokens",
                    "last_used_at",
                )
            },
        }

    def status(self) -> WebSearchStatus:
        with _STORE_LOCK:
            data = self._read()
        credential_error = False
        try:
            configured = bool(decrypt(str(data.get("api_key") or "")).strip())
        except ValueError:
            configured = False
            credential_error = True
        enabled = bool(data.get("enabled", False)) and configured
        return WebSearchStatus(
            configured=configured,
            enabled=enabled,
            provider="deepseek" if enabled else "bing_rss",
            search_count=_count(data.get("search_count")),
            fetch_count=_count(data.get("fetch_count")),
            last_used_at=str(data.get("last_used_at") or ""),
            migrated_from_quick_search=bool(
                data.get("migrated_from_quick_search")
            ),
            credential_error=credential_error,
        )

    def load_api_key(self) -> str:
        with _STORE_LOCK:
            return decrypt(str(self._read().get("api_key") or "")).strip()

    def save(self, *, api_key: str, enabled: bool) -> WebSearchStatus:
        with _STORE_LOCK:
            data = self._read()
            supplied = str(api_key or "").strip()
            if supplied:
                data["api_key"] = encrypt(supplied)
            elif data.get("api_key") and not str(data["api_key"]).startswith(
                "ENC:"
            ):
                data["api_key"] = encrypt(str(data["api_key"]))
            if enabled and not decrypt(str(data.get("api_key") or "")).strip():
                raise ValueError(
                    "A separate DeepSeek Web API key is required."
                )
            data.update(version=2, enabled=bool(enabled))
            self._write(data)
            return self.status()

    def record_request(self, endpoint: Literal["search", "fetch"]) -> None:
        if endpoint not in {"search", "fetch"}:
            raise ValueError("Unknown Web endpoint")
        with _STORE_LOCK:
            data = self._read()
            if not data.get("api_key"):
                return
            field = f"{endpoint}_count"
            data[field] = _count(data.get(field)) + 1
            data["last_used_at"] = datetime.now(timezone.utc).isoformat()
            data["version"] = 2
            # Legacy installations could have stored plaintext. Always encrypt
            # before writing into the new store, without rewriting the old one.
            data["api_key"] = encrypt(decrypt(str(data["api_key"])))
            self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd, name = tempfile.mkstemp(
            prefix=".web-search-", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)


async def _rate_limit(endpoint: str) -> None:
    # The documented limit is per account, not per API key. One conservative
    # process-wide queue per endpoint also covers key replacement and agents.
    with _RATE_LOCK:
        now = time.monotonic()
        start = max(now, _NEXT_REQUEST.get(endpoint, now))
        _NEXT_REQUEST[endpoint] = start + 0.2
    if start > now:
        await asyncio.sleep(start - now)


class DeepSeekWebClient:
    """Raw JSON API; no model, generated answer, or private chat context."""

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.http_client = http_client
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def search(
        self, query: str, *, count: int = 10
    ) -> list[dict[str, Any]]:
        normalized = str(query or "").strip()
        if not 1 <= len(normalized) <= 128:
            raise ValueError("Search query must contain 1 to 128 characters.")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= 50
        ):
            raise ValueError("Search count must be between 1 and 50.")
        data = await self._post(
            "search",
            {
                "query": normalized,
                "count": count,
                "need_content": False,
            },
        )
        results = data.get("results")
        if not isinstance(results, list) or any(
            not isinstance(r, dict) for r in results
        ):
            raise DeepSeekWebError(
                "DeepSeek returned invalid search results.",
                code="invalid_response",
            )
        return results

    async def fetch(self, url: str) -> dict[str, str]:
        normalized = str(url or "").strip()
        try:
            parsed = urlsplit(normalized)
            valid = (
                len(normalized) <= 8192
                and parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "Fetch requires an HTTP/HTTPS URL without credentials, up to 8192 characters."
            )
        data = await self._post("fetch", {"url": normalized})
        if not isinstance(data.get("title"), str) or not isinstance(
            data.get("content"), str
        ):
            raise DeepSeekWebError(
                "DeepSeek returned invalid page content.",
                code="invalid_response",
            )
        return {"title": data["title"], "content": data["content"]}

    async def validate(self) -> bool:
        """Verify this API directly rather than requiring a chat-model slot.

        A nonempty result is a normal billable Search request. The settings
        router records it so verification is visible in the same counter.
        """
        return bool(await self.search("DeepSeek", count=1))

    async def _post(
        self, endpoint: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.api_key:
            raise DeepSeekWebError(
                "DeepSeek Web search is not configured.", code="not_configured"
            )
        owns_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
        )
        try:
            for attempt in range(2):
                await _rate_limit(endpoint)
                try:
                    async with client.stream(
                        "POST",
                        f"https://api.deepseek.com/v1/tools/{endpoint}",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=body,
                    ) as response:
                        status = response.status_code
                        if status != 200:
                            if attempt == 0 and (
                                status in {408, 429} or 500 <= status <= 599
                            ):
                                # Includes Fetch's target-domain throttle. Do not
                                # retry auth, balance, or unchanged 4xx requests.
                                await asyncio.sleep(1.0)
                                continue
                            raise DeepSeekWebError(
                                f"DeepSeek Web request failed (HTTP {status}).",
                                code=f"http_{status}",
                            )
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > _MAX_RESPONSE_BYTES:
                                raise DeepSeekWebError(
                                    "DeepSeek Web response is too large.",
                                    code="response_too_large",
                                )
                            chunks.append(chunk)
                        try:
                            data = json.loads(b"".join(chunks))
                            if not isinstance(data, dict):
                                raise ValueError
                        except (ValueError, UnicodeError) as exc:
                            raise DeepSeekWebError(
                                "DeepSeek returned unreadable JSON.",
                                code="invalid_response",
                            ) from exc
                        return data
                except httpx.HTTPError as exc:
                    # No provider response body, credential, or request header
                    # may escape through a runtime exception or tool trace.
                    raise DeepSeekWebError(
                        "DeepSeek Web service could not be reached.",
                        code="network_unavailable",
                    ) from exc
        finally:
            if owns_client:
                await client.aclose()
        raise AssertionError("Unreachable retry state")


def get_web_search_store() -> WebSearchStore:
    return WebSearchStore()


def public_status_payload(status: WebSearchStatus) -> dict[str, Any]:
    return asdict(status)
