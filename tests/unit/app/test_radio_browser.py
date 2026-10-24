# -*- coding: utf-8 -*-
"""Behavior locks for the Radio Browser directory adapter."""

from __future__ import annotations

import socket

import httpx
import pytest

from lantai.app import radio_browser
from lantai.app.radio_browser import (
    RadioBrowserClient,
    RadioBrowserUnavailableError,
    discover_radio_browser_mirrors,
)


@pytest.mark.asyncio
async def test_dns_discovery_resolves_all_mirrors_without_a_fixed_host(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        radio_browser.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("203.0.113.10", 443),
            ),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2001:db8::10", 443, 0, 0),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("203.0.113.10", 443),
            ),
        ],
    )
    reverse_names = {
        "203.0.113.10": "de1.api.radio-browser.info",
        "2001:db8::10": "fi1.api.radio-browser.info.",
    }
    monkeypatch.setattr(
        radio_browser.socket,
        "gethostbyaddr",
        lambda address: (reverse_names[address], [], [address]),
    )

    mirrors = await discover_radio_browser_mirrors()

    assert mirrors == (
        "de1.api.radio-browser.info",
        "fi1.api.radio-browser.info",
    )


@pytest.mark.asyncio
async def test_search_fails_over_and_uses_v2_fields_and_user_agent() -> None:
    requests: list[httpx.Request] = []

    async def resolve_mirrors() -> tuple[str, ...]:
        return (
            "de1.api.radio-browser.info",
            "fi1.api.radio-browser.info",
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "de1.api.radio-browser.info":
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "stationuuid": "2c1c0b01-1234-4567-89ab-0123456789ab",
                    "id": "legacy-id-must-not-win",
                    "name": "Example FM",
                    "url": "https://stream.example/radio",
                    "url_resolved": "https://cdn.example/radio",
                    "country": "United States",
                    "countrycode": "us",
                    "tags": "news, talk",
                    "codec": "AAC",
                    "bitrate": 128,
                }
            ],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http:
        client = RadioBrowserClient(
            http_client=http,
            mirror_resolver=resolve_mirrors,
            shuffle_mirrors=lambda _mirrors: None,
        )
        stations = await client.search_stations(
            name="Example",
            countrycode="us",
        )

    assert [request.url.host for request in requests] == [
        "de1.api.radio-browser.info",
        "fi1.api.radio-browser.info",
    ]
    assert requests[1].url.params["countrycode"] == "US"
    assert "country" not in requests[1].url.params
    assert requests[1].headers["user-agent"].startswith("Lantai/")
    assert "RadioBrowser" in requests[1].headers["user-agent"]
    assert len(stations) == 1
    assert stations[0].stationuuid == ("2c1c0b01-1234-4567-89ab-0123456789ab")
    assert stations[0].countrycode == "US"
    assert stations[0].stream_url == "https://cdn.example/radio"


@pytest.mark.asyncio
async def test_search_ignores_legacy_station_and_country_fields() -> None:
    async def resolve_mirrors() -> tuple[str, ...]:
        return ("nl1.api.radio-browser.info",)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "id": "legacy-only-id",
                    "name": "Skipped",
                    "country": "Germany",
                },
                {
                    "stationuuid": "valid-station-uuid",
                    "name": "Kept",
                    "country": "Germany",
                    "countrycode": "not-an-iso-code",
                    "url": "https://stream.example/kept",
                },
            ],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http:
        stations = await RadioBrowserClient(
            http_client=http,
            mirror_resolver=resolve_mirrors,
            shuffle_mirrors=lambda _mirrors: None,
        ).search_stations()

    assert [station.stationuuid for station in stations] == [
        "valid-station-uuid",
    ]
    assert stations[0].countrycode == ""


@pytest.mark.asyncio
async def test_search_rejects_non_iso_countrycode_before_network_io() -> None:
    async def should_not_resolve() -> tuple[str, ...]:
        raise AssertionError("invalid input must not trigger DNS")

    client = RadioBrowserClient(mirror_resolver=should_not_resolve)

    with pytest.raises(ValueError, match="two-letter ISO 3166-1"):
        await client.search_stations(countrycode="United States")


@pytest.mark.asyncio
async def test_all_failed_mirrors_raise_one_stable_adapter_error() -> None:
    async def resolve_mirrors() -> tuple[str, ...]:
        return (
            "de1.api.radio-browser.info",
            "fi1.api.radio-browser.info",
        )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http:
        client = RadioBrowserClient(
            http_client=http,
            mirror_resolver=resolve_mirrors,
            shuffle_mirrors=lambda _mirrors: None,
        )
        with pytest.raises(
            RadioBrowserUnavailableError,
            match="all discovered mirrors",
        ):
            await client.search_stations()
