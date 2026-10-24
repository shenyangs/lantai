"""HTTP contracts for TokenDance desktop and private cloud OAuth."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import lantai.providers.provider_manager as provider_manager_module
import lantai.providers.tokendance_authorization as tokendance_authorization_module
from lantai.app.auth import _PUBLIC_PATHS
from lantai.app.routers import providers as providers_router_module
from lantai.app.routers.providers import get_provider_manager, router
from lantai.providers.provider_manager import ProviderManager
from lantai.providers.tokendance_oauth import TokenDanceOAuthRegistry
from lantai.security.secret_store import is_encrypted


@pytest.fixture(autouse=True)
def _watcha_account_connected(monkeypatch):
    monkeypatch.delenv("LANTAI_TOKENDANCE_OAUTH_CALLBACK_URL", raising=False)
    context = SimpleNamespace(
        subject_id="watcha:test-user",
        account_epoch=1,
    )
    monkeypatch.setattr(
        providers_router_module,
        "is_watcha_account_connected",
        lambda: True,
    )
    monkeypatch.setattr(
        providers_router_module,
        "load_watcha_account_context",
        lambda: context,
        raising=False,
    )
    monkeypatch.setattr(
        provider_manager_module,
        "load_watcha_account_context",
        lambda: context,
        raising=False,
    )
    monkeypatch.setattr(
        tokendance_authorization_module,
        "load_watcha_account_context",
        lambda: context,
    )
    return context


class _Manager:
    def __init__(self) -> None:
        self.provider = SimpleNamespace(
            id="tokendance",
            meta={"oauth_mode": "api_key_pkce"},
        )
        self.updated: dict | None = None

    def get_provider(self, provider_id: str):
        return self.provider if provider_id == "tokendance" else None

    def bind_tokendance_credential(self, api_key: str, **kwargs) -> dict:
        self.updated = {"api_key": api_key, **kwargs}
        return {"status": "active"}


@pytest.mark.asyncio
async def test_oauth_start_callback_and_status_never_echo_key(monkeypatch) -> None:
    manager = _Manager()
    registry = TokenDanceOAuthRegistry(ttl_seconds=600)
    monkeypatch.setattr(
        providers_router_module,
        "tokendance_oauth_registry",
        registry,
    )

    async def fake_exchange(**_kwargs) -> str:
        return "oauth-created-secret"

    monkeypatch.setattr(
        providers_router_module,
        "exchange_authorization_code",
        fake_exchange,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_provider_manager] = lambda: manager

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8088",
    ) as client:
        start_response = await client.post("/models/tokendance/oauth/start")
        assert start_response.status_code == 200
        started = start_response.json()
        assert "oauth-created-secret" not in start_response.text
        authorize_query = parse_qs(
            urlparse(started["authorization_url"]).query,
        )
        callback_url = authorize_query["callback_url"][0]
        callback_query = parse_qs(urlparse(callback_url).query)
        assert callback_url.startswith(
            "http://127.0.0.1:8088/api/models/tokendance/oauth/callback"
        )

        callback_response = await client.get(
            "/models/tokendance/oauth/callback",
            params={"flow": callback_query["flow"][0], "code": "once"},
        )
        assert callback_response.status_code == 200
        assert callback_response.headers["cache-control"] == "no-store"
        assert "oauth-created-secret" not in callback_response.text
        assert manager.updated == {
            "api_key": "oauth-created-secret",
            "expected_owner_subject_id": "watcha:test-user",
            "expected_account_epoch": 1,
            "ownership_evidence": "oauth_pkce",
        }

        status_response = await client.get(
            f"/models/tokendance/oauth/status/{started['flow_id']}",
        )
        assert status_response.json()["status"] == "succeeded"
        assert "oauth-created-secret" not in status_response.text


def test_tokendance_callback_is_public() -> None:
    assert "/api/models/tokendance/oauth/callback" in _PUBLIC_PATHS


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:8088", "https://cloud.lantai.guanlan.xin"],
)
@pytest.mark.asyncio
async def test_oauth_callback_persists_only_encrypted_key(
    monkeypatch,
    tmp_path,
    base_url,
) -> None:
    if base_url.startswith("https:"):
        monkeypatch.setenv(
            "LANTAI_TOKENDANCE_OAUTH_CALLBACK_URL",
            base_url + "/api/models/tokendance/oauth/callback",
        )
    secret_dir = tmp_path / "secret"
    monkeypatch.setattr(provider_manager_module, "SECRET_DIR", secret_dir)
    manager = ProviderManager()
    registry = TokenDanceOAuthRegistry(ttl_seconds=600)
    monkeypatch.setattr(
        providers_router_module,
        "tokendance_oauth_registry",
        registry,
    )

    async def fake_exchange(**_kwargs) -> str:
        return "oauth-created-secret"

    monkeypatch.setattr(
        providers_router_module,
        "exchange_authorization_code",
        fake_exchange,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_provider_manager] = lambda: manager

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=base_url,
    ) as client:
        started = (await client.post("/models/tokendance/oauth/start")).json()
        callback = parse_qs(
            urlparse(
                parse_qs(urlparse(started["authorization_url"]).query)["callback_url"][
                    0
                ],
            ).query,
        )
        response = await client.get(
            "/models/tokendance/oauth/callback",
            params={"flow": callback["flow"][0], "code": "once"},
        )

    assert response.status_code == 200
    raw = json.loads(
        (secret_dir / "providers" / "builtin" / "tokendance.json").read_text(
            encoding="utf-8",
        ),
    )
    assert raw["api_key"] != "oauth-created-secret"
    assert is_encrypted(raw["api_key"])


@pytest.mark.asyncio
async def test_cloud_oauth_uses_configured_origin_and_rejects_other_origins(
    monkeypatch,
) -> None:
    callback_base = (
        "https://cloud.lantai.guanlan.xin/api/models/tokendance/oauth/callback"
    )
    monkeypatch.setenv("LANTAI_TOKENDANCE_OAUTH_CALLBACK_URL", callback_base)
    manager = _Manager()
    registry = TokenDanceOAuthRegistry()
    monkeypatch.setattr(
        providers_router_module, "tokendance_oauth_registry", registry,
    )
    exchanges = []

    async def fake_exchange(**kwargs) -> str:
        exchanges.append(kwargs)
        return "cloud-oauth-created-secret"

    monkeypatch.setattr(
        providers_router_module, "exchange_authorization_code", fake_exchange,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_provider_manager] = lambda: manager
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="https://cloud.lantai.guanlan.xin",
    ) as client:
        response = await client.post("/api/models/tokendance/oauth/start")
        assert response.status_code == 200
        started = response.json()
        query = parse_qs(urlparse(started["authorization_url"]).query)
        callback = urlparse(query["callback_url"][0])
        assert callback._replace(query="").geturl() == callback_base
        assert query["code_challenge_method"] == ["S256"]
        assert "code_verifier" not in started
        params = {"flow": started["flow_id"], "code": "once"}

        for origin in (
            "https://other.example",
            "http://cloud.lantai.guanlan.xin",
            "http://127.0.0.1:8088",
        ):
            async with AsyncClient(transport=transport, base_url=origin) as other:
                bad_start = await other.post(
                    "/api/models/tokendance/oauth/start",
                )
                bad_callback = await other.get(
                    "/api/models/tokendance/oauth/callback", params=params,
                )
            assert bad_start.status_code == 400
            assert bad_callback.status_code == 400
            assert bad_callback.headers["referrer-policy"] == "no-referrer"
            assert registry.status(started["flow_id"]).status == "pending"
            assert exchanges == []

        completed = await client.get(
            "/api/models/tokendance/oauth/callback", params=params,
        )
        assert completed.status_code == 200
        assert "云端实例" in completed.text
        assert "cloud-oauth-created-secret" not in completed.text
        assert completed.headers["cache-control"] == "no-store"
        assert completed.headers["referrer-policy"] == "no-referrer"
        replay = await client.get(
            "/api/models/tokendance/oauth/callback", params=params,
        )
        assert replay.status_code == 400
        assert len(exchanges) == 1
        assert registry.status(started["flow_id"]).status == "succeeded"
        assert manager.updated["expected_owner_subject_id"] == "watcha:test-user"


@pytest.mark.asyncio
async def test_unconfigured_remote_oauth_fails_instead_of_using_desktop_callback(
) -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_provider_manager] = _Manager
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://cloud.example",
    ) as client:
        response = await client.post("/models/tokendance/oauth/start")
    assert response.status_code == 400
    assert "127.0.0.1" not in response.text


@pytest.mark.asyncio
async def test_oauth_callback_discards_key_after_watcha_account_switch(
    monkeypatch,
    _watcha_account_connected,
) -> None:
    manager = _Manager()
    registry = TokenDanceOAuthRegistry(ttl_seconds=600)
    monkeypatch.setattr(
        providers_router_module,
        "tokendance_oauth_registry",
        registry,
    )

    async def fake_exchange(**_kwargs) -> str:
        _watcha_account_connected.subject_id = "watcha:other-user"
        _watcha_account_connected.account_epoch = 2
        return "must-not-be-saved"

    monkeypatch.setattr(
        providers_router_module,
        "exchange_authorization_code",
        fake_exchange,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_provider_manager] = lambda: manager

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8088",
    ) as client:
        started = (await client.post("/models/tokendance/oauth/start")).json()
        callback = parse_qs(
            urlparse(
                parse_qs(urlparse(started["authorization_url"]).query)[
                    "callback_url"
                ][0],
            ).query,
        )
        response = await client.get(
            "/models/tokendance/oauth/callback",
            params={"flow": callback["flow"][0], "code": "once"},
        )

    assert response.status_code == 400
    assert manager.updated is None
    assert "must-not-be-saved" not in response.text
    assert registry.status(started["flow_id"]).status == "failed"


@pytest.mark.asyncio
async def test_oauth_status_rejects_watcha_account_switch(
    monkeypatch,
    _watcha_account_connected,
) -> None:
    manager = _Manager()
    registry = TokenDanceOAuthRegistry(ttl_seconds=600)
    monkeypatch.setattr(
        providers_router_module,
        "tokendance_oauth_registry",
        registry,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_provider_manager] = lambda: manager

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8088",
    ) as client:
        started = (await client.post("/models/tokendance/oauth/start")).json()
        _watcha_account_connected.subject_id = "watcha:other-user"
        _watcha_account_connected.account_epoch = 2

        response = await client.get(
            f"/models/tokendance/oauth/status/{started['flow_id']}",
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "oauth_context_stale"


@pytest.mark.asyncio
async def test_tokendance_config_rejects_unattested_key(monkeypatch) -> None:
    manager = _Manager()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_provider_manager] = lambda: manager

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8088",
    ) as client:
        response = await client.put(
            "/models/tokendance/config",
            json={"api_key": "unattested-secret"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ownership_attestation_required"
    assert manager.updated is None
    assert "unattested-secret" not in response.text


@pytest.mark.asyncio
async def test_tokendance_config_binds_attested_key_to_current_watcha_subject(
    monkeypatch,
    tmp_path,
) -> None:
    secret_dir = tmp_path / "secret"
    monkeypatch.setattr(provider_manager_module, "SECRET_DIR", secret_dir)
    manager = ProviderManager()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_provider_manager] = lambda: manager

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8088",
    ) as client:
        response = await client.put(
            "/models/tokendance/config",
            json={
                "api_key": "attested-synthetic-secret",
                "credential_ownership_attested": True,
            },
        )

    assert response.status_code == 200
    provider = manager.get_provider("tokendance")
    binding = provider.meta["tokendance_credential_binding"]
    assert provider.api_key == "attested-synthetic-secret"
    assert binding["owner_subject_id"] == "watcha:test-user"
    assert binding["ownership_evidence"] == "user_attested"
    assert response.json()["meta"]["credential_status"] == "active"
    assert "tokendance_credential_binding" not in response.text
    assert "attested-synthetic-secret" not in response.text
