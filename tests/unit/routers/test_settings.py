# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name
"""Unit tests for the global settings router (/api/settings/language)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers.settings import router

app = FastAPI()
app.include_router(router, prefix="/api")


@pytest.fixture(autouse=True)
def _use_tmp_settings(tmp_path: Path):
    """Redirect settings file to a temp directory for every test."""
    settings_file = tmp_path / "settings.json"
    with patch("lantai.app.routers.settings._SETTINGS_FILE", settings_file), patch(
        "lantai.app.routers.settings.WORKING_DIR",
        tmp_path,
    ):
        yield settings_file


@pytest.fixture
def api_client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ── GET /settings/language ───────────────────────────────────────────


async def test_get_language_default(api_client):
    """Should return 'zh' when no settings file exists."""
    async with api_client:
        resp = await api_client.get("/api/settings/language")
    assert resp.status_code == 200
    assert resp.json() == {"language": "zh"}


async def test_get_language_persisted(api_client, _use_tmp_settings):
    """Should return the persisted language value."""
    _use_tmp_settings.write_text(json.dumps({"language": "ja"}), "utf-8")
    async with api_client:
        resp = await api_client.get("/api/settings/language")
    assert resp.status_code == 200
    assert resp.json() == {"language": "ja"}


# ── PUT /settings/language ───────────────────────────────────────────


@pytest.mark.parametrize("lang", ["en", "zh", "ja", "ru"])
async def test_put_language_valid(
    api_client,
    lang,
    _use_tmp_settings,
):
    """Should accept all valid languages and persist them."""
    async with api_client:
        resp = await api_client.put(
            "/api/settings/language",
            json={"language": lang},
        )
    assert resp.status_code == 200
    assert resp.json() == {"language": lang}

    data = json.loads(_use_tmp_settings.read_text("utf-8"))
    assert data["language"] == lang


async def test_put_language_invalid(api_client):
    """Should reject invalid language with 400."""
    async with api_client:
        resp = await api_client.put(
            "/api/settings/language",
            json={"language": "xx"},
        )
    assert resp.status_code == 400
    assert "Invalid language" in resp.json()["detail"]


async def test_put_language_empty(api_client):
    """Should reject empty language with 400."""
    async with api_client:
        resp = await api_client.put(
            "/api/settings/language",
            json={"language": ""},
        )
    assert resp.status_code == 400


async def test_put_language_missing_key(api_client):
    """Should reject body without 'language' key with 400."""
    async with api_client:
        resp = await api_client.put(
            "/api/settings/language",
            json={"lang": "zh"},
        )
    assert resp.status_code == 400


async def test_put_then_get_roundtrip(api_client):
    """PUT then GET should return the updated language."""
    async with api_client:
        await api_client.put(
            "/api/settings/language",
            json={"language": "ru"},
        )
        resp = await api_client.get("/api/settings/language")
    assert resp.json() == {"language": "ru"}


async def test_appearance_exposes_one_canonical_persona_mode(api_client):
    async with api_client:
        initial = await api_client.get("/api/settings/appearance")
        updated = await api_client.put(
            "/api/settings/appearance",
            json={"display_settings": {"personaMode": "balanced"}},
        )

    assert initial.json()["display_settings"]["personaMode"] == "full"
    assert updated.json()["display_settings"]["personaMode"] == "balanced"


async def test_appearance_welcome_defaults_and_preferences_roundtrip(api_client):
    async with api_client:
        initial = await api_client.get("/api/settings/appearance")
        updated = await api_client.put(
            "/api/settings/appearance",
            json={
                "display_settings": {
                    "dynamicWelcome": False,
                    "aiWelcome": False,
                    "welcomeCulturalCalendar": "china",
                    "welcomeTone": "concise",
                }
            },
        )
        persisted = await api_client.get("/api/settings/appearance")

    initial_display = initial.json()["display_settings"]
    assert initial_display["dynamicWelcome"] is True
    assert initial_display["aiWelcome"] is True
    assert initial_display["welcomeCulturalCalendar"] == "none"
    assert initial_display["welcomeTone"] == "natural"
    assert persisted.json()["display_settings"] == updated.json()["display_settings"]


async def test_appearance_defaults_thinking_on_and_preserves_explicit_off(
    api_client,
):
    async with api_client:
        initial = await api_client.get("/api/settings/appearance")
        updated = await api_client.put(
            "/api/settings/appearance",
            json={"display_settings": {"showThinking": False}},
        )
        persisted = await api_client.get("/api/settings/appearance")

    assert initial.json()["display_settings"]["showThinking"] is True
    assert updated.json()["display_settings"]["showThinking"] is False
    assert persisted.json()["display_settings"]["showThinking"] is False


async def test_appearance_defaults_to_graphite_and_preserves_explicit_ink_blue(
    api_client,
):
    async with api_client:
        initial = await api_client.get("/api/settings/appearance")
        updated = await api_client.put(
            "/api/settings/appearance",
            json={"appearance_palette": "lantai"},
        )
        persisted = await api_client.get("/api/settings/appearance")

    assert initial.json()["appearance_palette"] == "mist"
    assert updated.json()["appearance_palette"] == "lantai"
    assert persisted.json()["appearance_palette"] == "lantai"


async def test_put_language_preserves_other_settings(
    api_client,
    _use_tmp_settings,
):
    """PUT should not overwrite other keys in settings.json."""
    _use_tmp_settings.write_text(
        json.dumps({"theme": "dark", "language": "en"}),
        "utf-8",
    )
    async with api_client:
        await api_client.put(
            "/api/settings/language",
            json={"language": "zh"},
        )

    data = json.loads(_use_tmp_settings.read_text("utf-8"))
    assert data["language"] == "zh"
    assert data["theme"] == "dark"


async def test_legacy_telemetry_endpoints_are_not_mounted(api_client):
    async with api_client:
        settings_response = await api_client.get("/api/settings/telemetry")
        events_response = await api_client.post(
            "/api/telemetry/events",
            json={"events": []},
        )

    assert settings_response.status_code == 404
    assert events_response.status_code == 404


async def test_appearance_sidebar_material_roundtrip(api_client):
    async with api_client:
        resp = await api_client.put(
            "/api/settings/appearance",
            json={
                "display_settings": {
                    "sidebarMaterial": "mist-glass",
                },
            },
        )
        assert resp.status_code == 200
        assert (
            resp.json()["display_settings"]["sidebarMaterial"]
            == "mist-glass"
        )

        resp = await api_client.get("/api/settings/appearance")

    assert resp.json()["display_settings"]["sidebarMaterial"] == "mist-glass"


async def test_system_behavior_defaults(api_client):
    async with api_client:
        resp = await api_client.get("/api/settings/system-behavior")

    assert resp.status_code == 200
    assert resp.json() == {
        "launch_at_login": False,
        "keep_awake_while_running": False,
        "turn_completion_notification": "when_unfocused",
        "permission_notification": True,
    }


async def test_system_behavior_roundtrip_preserves_other_settings(
    api_client,
    _use_tmp_settings,
):
    _use_tmp_settings.write_text(
        json.dumps({"language": "zh", "system_behavior": {}}),
        "utf-8",
    )
    async with api_client:
        resp = await api_client.put(
            "/api/settings/system-behavior",
            json={
                "launch_at_login": True,
                "keep_awake_while_running": True,
                "turn_completion_notification": "always",
                "permission_notification": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["launch_at_login"] is True
        assert resp.json()["turn_completion_notification"] == "always"

        resp = await api_client.get("/api/settings/system-behavior")

    assert resp.json()["keep_awake_while_running"] is True
    assert resp.json()["permission_notification"] is False
    assert json.loads(_use_tmp_settings.read_text("utf-8"))["language"] == "zh"


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"launch_at_login": "yes"}, "launch_at_login must be a boolean"),
        (
            {"turn_completion_notification": "sometimes"},
            "Invalid turn_completion_notification",
        ),
    ],
)
async def test_system_behavior_rejects_invalid_values(
    api_client,
    payload,
    detail,
):
    async with api_client:
        resp = await api_client.put(
            "/api/settings/system-behavior",
            json=payload,
        )

    assert resp.status_code == 400
    assert detail in resp.json()["detail"]
