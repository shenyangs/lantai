# -*- coding: utf-8 -*-
"""Contract tests for the customization HTTP control plane."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers import customization


@pytest.fixture(autouse=True)
def _working_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(customization, "WORKING_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def api_client():
    app = FastAPI()
    app.include_router(customization.router, prefix="/api")
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    )


async def test_registry_factory_and_five_slots(api_client) -> None:
    async with api_client:
        registry = await api_client.get("/api/customization/registry")
        effective = await api_client.get("/api/customization/effective-state")
        slots = await api_client.get("/api/customization/slots")

    assert registry.status_code == 200
    assert len(registry.json()["items"]) >= 20
    assert effective.json()["values"]["appearance.palette"] == "mist"
    assert len(slots.json()) == 5


async def test_profile_plan_preview_apply_and_revision_conflict(api_client) -> None:
    async with api_client:
        created = await api_client.post(
            "/api/customization/profiles",
            json={"name": "夜航", "icon": "moon"},
        )
        profile = created.json()
        plan = {
            "target_profile_id": profile["id"],
            "base_revision": 0,
            "intent": "安静并使用暖色",
            "operations": [
                {"op": "set", "path": "appearance.palette", "value": "warm"},
                {"op": "set", "path": "persona.presence", "value": "quiet"},
            ],
        }
        preview = await api_client.post(
            "/api/customization/plans/preview",
            json=plan,
        )
        plan_id = preview.json()["plan"]["id"]
        denied = await api_client.post(
            "/api/customization/plans/apply",
            json={"plan_id": plan_id},
        )
        confirmation = await api_client.post(
            f"/api/customization/plans/{plan_id}/confirm",
        )
        applied = await api_client.post(
            "/api/customization/plans/apply",
            json={
                "plan_id": plan_id,
                "confirmation_token": confirmation.json()["token"],
            },
        )
        conflict = await api_client.post(
            "/api/customization/plans/apply",
            json={
                "plan_id": plan_id,
                "confirmation_token": confirmation.json()["token"],
            },
        )

    assert created.status_code == 200
    assert preview.json()["risk"] == "medium"
    assert preview.json()["requires_confirmation"] is True
    assert denied.status_code == 400
    assert confirmation.status_code == 200
    assert applied.json()["status"] == "committed"
    assert conflict.status_code == 409


async def test_legacy_settings_migrate_without_reinjecting_css(
    api_client,
    _working_dir,
) -> None:
    (_working_dir / "settings.json").write_text(
        json.dumps(
            {
                "appearance": {
                    "appearance_palette": "warm",
                    "custom_theme_css": (
                        ":root { --lt-accent: #b86424; } "
                        "body { display: none; } @import 'https://bad.test/x.css';"
                    ),
                }
            }
        ),
        encoding="utf-8",
    )

    async with api_client:
        profiles = await api_client.get("/api/customization/profiles")
        effective = await api_client.get("/api/customization/effective-state")

    assert len(profiles.json()) == 1
    overrides = profiles.json()[0]["overrides"]
    assert overrides["appearance.palette"] == "warm"
    assert overrides["appearance.design_tokens"] == {"--lt-accent": "#b86424"}
    assert "display: none" not in json.dumps(overrides)
    assert effective.json()["values"]["appearance.palette"] == "warm"
    archive = (
        _working_dir
        / "customization"
        / "imports"
        / "quarantined"
        / "legacy_custom_css.css.txt"
    )
    assert "display: none" in archive.read_text("utf-8")


async def test_factory_mode_preserves_profile_and_returns_factory_state(
    api_client,
) -> None:
    async with api_client:
        created = await api_client.post(
            "/api/customization/profiles",
            json={"name": "暖色", "overrides": {"appearance.palette": "warm"}},
        )
        profile_id = created.json()["id"]
        await api_client.post(f"/api/customization/profiles/{profile_id}/activate")
        normal = await api_client.get("/api/customization/effective-state")
        recovered = await api_client.post("/api/customization/recovery/factory-mode")
        factory = await api_client.get("/api/customization/effective-state")
        profiles = await api_client.get("/api/customization/profiles")

    assert normal.json()["values"]["appearance.palette"] == "warm"
    assert recovered.json()["mode"] == "factory"
    assert factory.json()["values"]["appearance.palette"] == "mist"
    assert profiles.json()[0]["overrides"]["appearance.palette"] == "warm"


async def test_export_import_undo_and_history(api_client) -> None:
    async with api_client:
        created = await api_client.post(
            "/api/customization/profiles",
            json={"name": "模板", "overrides": {"appearance.palette": "warm"}},
        )
        profile_id = created.json()["id"]
        exported = await api_client.get(
            f"/api/customization/profiles/{profile_id}/export"
        )
        imported = await api_client.post(
            "/api/customization/profiles/import",
            json=exported.json(),
        )
        copied = await api_client.post(
            f"/api/customization/profiles/{profile_id}/copy"
        )
        history = await api_client.get(
            f"/api/customization/profiles/{profile_id}/revisions"
        )
        undone = await api_client.post(
            f"/api/customization/profiles/{profile_id}/undo"
        )

    assert exported.status_code == 200
    assert imported.status_code == 200
    assert copied.status_code == 200
    assert copied.json()["overrides"] == {"appearance.palette": "warm"}
    assert imported.json()["report"]["activated"] is False
    assert history.json()[0]["revision"] == 1
    assert undone.json()["status"] == "committed"
