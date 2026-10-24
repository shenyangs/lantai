# -*- coding: utf-8 -*-
"""Resilient client for the public Radio Browser station directory.

Radio Browser is a federated service.  Its documented ``all`` DNS record is
the source of truth for live mirrors; no individual country mirror is a
permanent dependency of this adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import random
import re
import socket
import time
from typing import Any

import httpx

from ..__version__ import __version__


RADIO_BROWSER_DNS_NAME = "all.api.radio-browser.info"
RADIO_BROWSER_DOMAIN_SUFFIX = ".api.radio-browser.info"
RADIO_BROWSER_USER_AGENT = f"Lantai/{__version__} RadioBrowser/1.0"

_COUNTRYCODE_PATTERN = re.compile(r"[A-Z]{2}")
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

MirrorResolver = Callable[[], Awaitable[Sequence[str]]]
MirrorShuffler = Callable[[list[str]], None]


class RadioBrowserError(RuntimeError):
    """Base error raised by the Radio Browser adapter."""


class RadioBrowserDiscoveryError(RadioBrowserError):
    """DNS discovery did not yield a usable Radio Browser mirror."""


class RadioBrowserUnavailableError(RadioBrowserError):
    """Every discovered Radio Browser mirror failed for one request."""


def _normalize_countrycode(value: object, *, strict: bool) -> str:
    normalized = str(value or "").strip().upper()
    if not normalized:
        return ""
    if _COUNTRYCODE_PATTERN.fullmatch(normalized):
        return normalized
    if strict:
        raise ValueError("countrycode must be a two-letter ISO 3166-1 code")
    return ""


def _normalize_mirror_hostname(value: object) -> str:
    hostname = str(value or "").strip().lower().rstrip(".")
    if (
        not hostname.endswith(RADIO_BROWSER_DOMAIN_SUFFIX)
        or hostname == RADIO_BROWSER_DNS_NAME
    ):
        return ""
    return hostname


def _discover_radio_browser_mirrors_sync() -> tuple[str, ...]:
    try:
        addresses = socket.getaddrinfo(
            RADIO_BROWSER_DNS_NAME,
            443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise RadioBrowserDiscoveryError(
            "Radio Browser mirror DNS lookup failed.",
        ) from exc

    ip_addresses = {str(item[4][0]) for item in addresses if item[4]}
    hostnames: set[str] = set()
    for address in ip_addresses:
        try:
            reverse_name = socket.gethostbyaddr(address)[0]
        except OSError:
            continue
        hostname = _normalize_mirror_hostname(reverse_name)
        if hostname:
            hostnames.add(hostname)

    if not hostnames:
        raise RadioBrowserDiscoveryError(
            "Radio Browser DNS returned no usable mirrors.",
        )
    return tuple(sorted(hostnames))


async def discover_radio_browser_mirrors() -> tuple[str, ...]:
    """Discover current API mirrors from Radio Browser's aggregate DNS name."""

    return await asyncio.to_thread(_discover_radio_browser_mirrors_sync)


def _optional_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return str(value).strip() if value is not None else ""


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class RadioStation:
    """Stable station fields exposed to Lantai callers."""

    stationuuid: str
    name: str
    countrycode: str
    stream_url: str
    homepage: str = ""
    favicon: str = ""
    tags: tuple[str, ...] = ()
    codec: str = ""
    bitrate: int = 0

    @classmethod
    def from_payload(cls, payload: object) -> RadioStation | None:
        """Normalize a v2 API row without consulting legacy identity fields."""

        if not isinstance(payload, dict):
            return None
        stationuuid = _optional_text(payload, "stationuuid")
        if not stationuuid:
            return None
        resolved_url = _optional_text(payload, "url_resolved")
        stream_url = resolved_url or _optional_text(payload, "url")
        tags = tuple(
            item
            for raw_item in _optional_text(payload, "tags").split(",")
            if (item := raw_item.strip())
        )
        return cls(
            stationuuid=stationuuid,
            name=_optional_text(payload, "name"),
            countrycode=_normalize_countrycode(
                payload.get("countrycode"),
                strict=False,
            ),
            stream_url=stream_url,
            homepage=_optional_text(payload, "homepage"),
            favicon=_optional_text(payload, "favicon"),
            tags=tags,
            codec=_optional_text(payload, "codec"),
            bitrate=_non_negative_int(payload.get("bitrate")),
        )


class RadioBrowserClient:
    """Search discovered Radio Browser mirrors with automatic failover."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        mirror_resolver: MirrorResolver = discover_radio_browser_mirrors,
        shuffle_mirrors: MirrorShuffler | None = None,
        user_agent: str = RADIO_BROWSER_USER_AGENT,
        timeout_seconds: float = 10.0,
        discovery_cache_seconds: float = 300.0,
    ) -> None:
        self.http_client = http_client
        self.mirror_resolver = mirror_resolver
        self.shuffle_mirrors = shuffle_mirrors or random.SystemRandom().shuffle
        self.user_agent = str(user_agent or RADIO_BROWSER_USER_AGENT).strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.discovery_cache_seconds = max(
            0.0,
            float(discovery_cache_seconds),
        )
        self._cached_mirrors: tuple[str, ...] = ()
        self._cache_expires_at = 0.0
        self._discovery_lock = asyncio.Lock()

    async def search_stations(
        self,
        *,
        name: str = "",
        countrycode: str = "",
        limit: int = 100,
        hide_broken: bool = True,
    ) -> list[RadioStation]:
        """Search stations using v2 identity and ISO country-code fields."""

        normalized_countrycode = _normalize_countrycode(
            countrycode,
            strict=True,
        )
        normalized_name = str(name or "").strip()
        normalized_limit = max(1, min(1_000, int(limit)))
        params: dict[str, str | int] = {
            "limit": normalized_limit,
            "hidebroken": "true" if hide_broken else "false",
            "order": "votes",
            "reverse": "true",
        }
        if normalized_name:
            params["name"] = normalized_name
        if normalized_countrycode:
            params["countrycode"] = normalized_countrycode

        payload = await self._request_json(
            "/json/stations/search",
            params=params,
        )
        if not isinstance(payload, list):
            raise RadioBrowserError(
                "Radio Browser returned an invalid station list.",
            )
        stations: list[RadioStation] = []
        for item in payload:
            station = RadioStation.from_payload(item)
            if station is not None:
                stations.append(station)
        return stations

    async def _mirrors(self) -> list[str]:
        async with self._discovery_lock:
            now = time.monotonic()
            if self._cached_mirrors and now < self._cache_expires_at:
                mirrors = list(self._cached_mirrors)
            else:
                try:
                    resolved = await self.mirror_resolver()
                    normalized = {
                        hostname
                        for value in resolved
                        if (hostname := _normalize_mirror_hostname(value))
                    }
                    if not normalized:
                        raise RadioBrowserDiscoveryError(
                            "Radio Browser discovery returned no usable "
                            "mirrors.",
                        )
                    self._cached_mirrors = tuple(sorted(normalized))
                    self._cache_expires_at = now + self.discovery_cache_seconds
                except (OSError, RadioBrowserDiscoveryError) as exc:
                    if not self._cached_mirrors:
                        if isinstance(exc, RadioBrowserDiscoveryError):
                            raise
                        raise RadioBrowserDiscoveryError(
                            "Radio Browser mirror discovery failed.",
                        ) from exc
                mirrors = list(self._cached_mirrors)
        self.shuffle_mirrors(mirrors)
        return mirrors

    async def _request_json(
        self,
        path: str,
        *,
        params: dict[str, str | int],
    ) -> Any:
        mirrors = await self._mirrors()
        owns_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
        )
        last_error: Exception | None = None
        try:
            for mirror in mirrors:
                try:
                    response = await client.get(
                        f"https://{mirror}{path}",
                        params=params,
                        headers={"User-Agent": self.user_agent},
                    )
                except httpx.HTTPError as exc:
                    last_error = exc
                    continue

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    last_error = httpx.HTTPStatusError(
                        "retryable Radio Browser response",
                        request=response.request,
                        response=response,
                    )
                    continue
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise RadioBrowserError(
                        "Radio Browser rejected the station request "
                        f"(HTTP {response.status_code}).",
                    ) from exc
                try:
                    return response.json()
                except ValueError as exc:
                    last_error = exc
                    continue
        finally:
            if owns_client:
                await client.aclose()

        self._cache_expires_at = 0.0
        raise RadioBrowserUnavailableError(
            "Radio Browser request failed on all discovered mirrors.",
        ) from last_error
