"""Unified Web settings preserve separate credentials and explicit consent."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lantai.app.web_search import DeepSeekWebError, WebSearchStore
from lantai.app.routers import web_search as router_module

pytestmark = pytest.mark.usefixtures("isolated_web_search_secrets")


@pytest.fixture
def configured_api(monkeypatch, tmp_path):
    store = WebSearchStore(path=tmp_path / "auxiliary" / "web_search.json")
    validated: list[str] = []

    async def validate(api_key: str) -> bool:
        validated.append(api_key)
        return True

    monkeypatch.setattr(router_module, "get_store", lambda: store)
    monkeypatch.setattr(router_module, "validate_api_key", validate)
    app = FastAPI()
    app.include_router(router_module.router)
    app.include_router(router_module.legacy_router)
    return TestClient(app), store, validated


def test_enabling_requires_own_verified_key(configured_api):
    client, store, validated = configured_api
    response = client.put("/web-search/config", json={"enabled": True})
    assert response.status_code == 400
    assert not store.path.exists()

    response = client.put(
        "/web-search/config",
        json={"api_key": "sk-web-only", "enabled": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["enabled"] is True
    assert payload["provider"] == "deepseek"
    assert payload["search_count"] == 1
    assert "sk-web-only" not in response.text
    assert "model" not in payload
    assert validated == ["sk-web-only"]
    assert store.load_api_key() == "sk-web-only"


def test_blank_disable_preserves_key_without_paid_validation(configured_api):
    client, store, validated = configured_api
    store.save(api_key="sk-existing", enabled=True)
    response = client.put(
        "/web-search/config",
        json={"api_key": "", "enabled": False},
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["provider"] == "bing_rss"
    assert store.load_api_key() == "sk-existing"
    assert validated == []


def test_failed_verification_preserves_previous_config(
    configured_api, monkeypatch
):
    client, store, _ = configured_api
    store.save(api_key="sk-existing", enabled=False)

    async def invalid(_key):
        raise DeepSeekWebError(
            "DeepSeek Web request failed (HTTP 401).", code="http_401"
        )

    monkeypatch.setattr(router_module, "validate_api_key", invalid)
    response = client.put(
        "/web-search/config",
        json={"api_key": "sk-wrong", "enabled": True},
    )
    assert response.status_code == 400
    assert store.load_api_key() == "sk-existing"
    assert store.status().enabled is False


def test_old_settings_route_writes_unified_store(configured_api):
    client, store, _ = configured_api
    response = client.put(
        "/deepseek-quick-search/config",
        json={"api_key": "sk-old-console", "enabled": True},
    )
    assert response.status_code == 200
    assert client.get("/web-search/config").json() == response.json()
    assert store.path.name == "web_search.json"


def test_unreadable_config_does_not_look_unconfigured(configured_api):
    client, store, _ = configured_api
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{broken", encoding="utf-8")
    assert client.get("/web-search/config").status_code == 409
    assert (
        client.put("/web-search/config", json={"enabled": False}).status_code
        == 400
    )
    assert store.path.read_text("utf-8") == "{broken"
