"""HTTP contracts for the TokenDance balance, usage, and payment bridge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.routers import providers as providers_module
from lantai.app.routers.providers import get_provider_manager, router
from lantai.providers.tokendance_authorization import (
    TokenDanceAuthorizationError,
)
from lantai.security import cloud_account_store


class _Manager:
    def __init__(self, api_key: str = "user-secret", meta: dict | None = None) -> None:
        self.provider = SimpleNamespace(
            api_key=api_key,
            meta=meta
            or {
                "tokendance_credential_binding": {
                    "schema_version": 1,
                    "status": "active",
                    "owner_subject_id": "watcha:test-user",
                    "credential_ref": "test-credential",
                    "payment_binding_id": "test-payment",
                    "binding_revision": 1,
                    "account_epoch_at_binding": 1,
                    "ownership_evidence": "oauth_pkce",
                    "scopes": ["balance", "usage", "payment"],
                },
            },
        )
        self.payment_sessions: dict[str, tuple[str, str, int]] = {}
        self.payment_intents: dict[str, dict] = {}

    def get_provider(self, provider_id: str):
        return self.provider if provider_id == "tokendance" else None

    def record_tokendance_payment_session(self, session_id, authorization):
        self.payment_sessions[session_id] = (
            authorization.owner_subject_id,
            authorization.payment_binding_id,
            authorization.binding_revision,
        )

    def prepare_tokendance_payment_intent(
        self,
        client_intent_id,
        amount,
        authorization,
    ):
        existing = self.payment_intents.get(client_intent_id)
        if existing is not None:
            if existing["amount"] != amount:
                raise TokenDanceAuthorizationError(
                    "tokendance_payment_intent_conflict",
                    "同一充值意图不能修改充值金额。",
                )
            return existing, False
        record = {
            "client_intent_id": client_intent_id,
            "amount": amount,
            "status": "prepared",
        }
        self.payment_intents[client_intent_id] = record
        return record, True

    def complete_tokendance_payment_intent(
        self,
        client_intent_id,
        session,
        authorization,
    ):
        self.record_tokendance_payment_session(session["id"], authorization)
        self.payment_intents[client_intent_id] = {
            **self.payment_intents[client_intent_id],
            "status": "submitted",
            "session": dict(session),
        }

    def mark_tokendance_payment_intent(
        self,
        client_intent_id,
        status,
        authorization,
    ):
        self.payment_intents[client_intent_id]["status"] = status

    def assert_tokendance_payment_session_owner(
        self,
        session_id,
        authorization,
    ):
        expected = (
            authorization.owner_subject_id,
            authorization.payment_binding_id,
            authorization.binding_revision,
        )
        if self.payment_sessions.get(session_id) != expected:
            raise TokenDanceAuthorizationError(
                "tokendance_payment_session_not_owned",
                "找不到当前账号创建的词元跳动充值会话。",
                status_code=404,
            )


class _FakeAccountClient:
    created_with: str | None = None
    payment_amount: int | None = None
    payment_session_id: str | None = None
    payment_session_calls: int = 0
    payment_create_calls: int = 0
    payment_create_session: dict | None = None
    redeemed_code: str | None = None
    redemption_credits: object = 1_000_000
    redemption_calls: int = 0

    def __init__(
        self,
        api_key: str,
        *,
        authorization_context_id: str | None = None,
    ) -> None:
        self.__class__.created_with = api_key
        self.authorization_context_id = authorization_context_id

    async def balance(self):
        return {
            "balance": {
                "credits": 58_000_000,
                "credits_used": 57_837_189,
                "balance": 162_811,
            },
        }

    async def usage(self, params):
        assert params == {"limit": 2, "offset": 0, "window": "7d"}
        return {
            "items": [
                {
                    "id": "usage-1",
                    "model_id": "qwen3.8-flash",
                    "cost": 12_345,
                },
            ],
            "offset": 0,
            "limit": 2,
        }

    async def create_payment_session(self, amount: int):
        self.__class__.payment_create_calls += 1
        self.__class__.payment_amount = amount
        return {
            "session": self.__class__.payment_create_session
            or {
                "id": "pay_1",
                "amount": amount,
                "status": "pending",
                "payment_url": "https://tokendance.space/pay/pay_1",
                "status_url": (
                    "https://tokendance.space/portal/api/v1/payment/"
                    "sessions/pay_1"
                ),
                "expired_at": 1_789_300_800,
                "created_at": 1_789_299_900,
            },
        }

    async def payment_session(self, session_id: str):
        self.__class__.payment_session_calls += 1
        self.__class__.payment_session_id = session_id
        return {"session": {"id": session_id, "status": "paid"}}

    async def redeem(self, code: str):
        self.__class__.redemption_calls += 1
        self.__class__.redeemed_code = code
        return {"credits": self.__class__.redemption_credits}


@pytest.fixture
def app(monkeypatch, tmp_path) -> FastAPI:
    monkeypatch.setattr(
        cloud_account_store,
        "CLOUD_ACCOUNT_FILE",
        tmp_path / "cloud-account.json",
    )
    monkeypatch.setattr(
        cloud_account_store,
        "encrypt_dict_fields",
        lambda data, _fields: data,
    )
    monkeypatch.setattr(
        cloud_account_store,
        "decrypt_dict_fields",
        lambda data, _fields: data,
    )
    cloud_account_store.save_cloud_account(
        {
            "provider": "watcha",
            "refresh_token": "refresh-test",
            "user": {"id": "test-user"},
        },
    )
    monkeypatch.setattr(
        providers_module,
        "is_watcha_account_connected",
        lambda: True,
    )
    monkeypatch.setattr(
        providers_module,
        "TokenDanceAccountClient",
        _FakeAccountClient,
    )
    manager = _Manager()
    _FakeAccountClient.payment_session_calls = 0
    _FakeAccountClient.payment_create_calls = 0
    _FakeAccountClient.payment_create_session = None
    _FakeAccountClient.redeemed_code = None
    _FakeAccountClient.redemption_credits = 1_000_000
    _FakeAccountClient.redemption_calls = 0
    instance = FastAPI()
    instance.state.test_provider_manager = manager
    instance.include_router(router)
    instance.dependency_overrides[get_provider_manager] = lambda: manager
    return instance


@pytest.mark.asyncio
async def test_balance_and_usage_are_normalized_without_echoing_key(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        balance = await client.get("/models/tokendance/account/balance")
        usage = await client.get(
            "/models/tokendance/account/usage",
            params={"limit": 2, "window": "7d"},
        )

    assert balance.status_code == 200
    assert balance.json()["authorization_context_id"].startswith("tdctx_v1_")
    assert balance.json()["balance_cny"] == "0.162811"
    assert balance.json()["credits_used_cny"] == "57.837189"
    assert "user-secret" not in balance.text
    assert usage.status_code == 200
    assert (
        usage.json()["authorization_context_id"]
        == balance.json()["authorization_context_id"]
    )
    assert usage.json()["items"][0]["cost_cny"] == "0.012345"
    assert usage.json()["has_more"] is False
    assert _FakeAccountClient.created_with == "user-secret"


@pytest.mark.asyncio
async def test_payment_creation_and_fixed_session_lookup(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        created = await client.post(
            "/models/tokendance/account/payment/sessions",
            json={
                "amount": 199,
                "client_intent_id": "tdpi_0123456789abcdef",
            },
        )
        paid = await client.get(
            "/models/tokendance/account/payment/sessions/pay_1",
        )
        rejected = await client.get(
            "/models/tokendance/account/payment/sessions/https:%2F%2Fevil.test",
        )

    assert created.status_code == 201, created.text
    assert created.json()["authorization_context_id"].startswith("tdctx_v1_")
    assert _FakeAccountClient.payment_amount == 199
    assert paid.json()["status"] == "paid"
    assert (
        paid.json()["authorization_context_id"]
        == created.json()["authorization_context_id"]
    )
    assert _FakeAccountClient.payment_session_id == "pay_1"
    assert rejected.status_code in {404, 422}


@pytest.mark.asyncio
async def test_redemption_uses_bound_account_and_returns_only_credits(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/models/tokendance/account/redemption/redeem",
            json={"code": "  gift-code  "},
        )

    assert response.status_code == 200, response.text
    assert _FakeAccountClient.redeemed_code == "gift-code"
    assert response.json() == {
        "authorization_context_id": response.json()["authorization_context_id"],
        "credits": 1_000_000,
        "credits_cny": "1.000000",
    }
    assert response.json()["authorization_context_id"].startswith("tdctx_v1_")
    assert "gift-code" not in response.text
    assert "user-secret" not in response.text


@pytest.mark.asyncio
async def test_redemption_rejects_blank_code_without_echo_or_upstream_call(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/models/tokendance/account/redemption/redeem",
            json={"code": "   "},
        )

    assert response.status_code == 400
    assert _FakeAccountClient.redemption_calls == 0
    assert "code" not in response.text.lower()


@pytest.mark.asyncio
async def test_redemption_rejects_unbound_account_before_upstream_call(app) -> None:
    app.dependency_overrides[get_provider_manager] = lambda: _Manager(
        meta={
            "tokendance_credential_binding": {
                "schema_version": 1,
                "status": "active",
                "owner_subject_id": "watcha:another-user",
                "credential_ref": "another-credential",
                "payment_binding_id": "another-payment",
                "binding_revision": 1,
                "account_epoch_at_binding": 1,
                "ownership_evidence": "oauth_pkce",
                "scopes": ["balance", "payment"],
            },
        },
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/models/tokendance/account/redemption/redeem",
            json={"code": "gift-code"},
        )

    assert response.status_code == 403
    assert _FakeAccountClient.redemption_calls == 0
    assert "gift-code" not in response.text


@pytest.mark.asyncio
async def test_redemption_rejects_invalid_success_payload(app) -> None:
    _FakeAccountClient.redemption_credits = "gift-code"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/models/tokendance/account/redemption/redeem",
            json={"code": "gift-code"},
        )

    assert response.status_code == 502
    assert "gift-code" not in response.text


@pytest.mark.asyncio
async def test_payment_creation_reuses_completed_client_intent(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        first = await client.post(
            "/models/tokendance/account/payment/sessions",
            json={
                "amount": 199,
                "client_intent_id": "tdpi_0123456789abcdef",
            },
        )
        second = await client.post(
            "/models/tokendance/account/payment/sessions",
            json={
                "amount": 199,
                "client_intent_id": "tdpi_0123456789abcdef",
            },
        )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert _FakeAccountClient.payment_create_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session",
    [
        {
            "id": "pay_1",
            "amount": 499,
            "status": "pending",
            "payment_url": "https://tokendance.space/pay/pay_1",
            "status_url": (
                "https://tokendance.space/portal/api/v1/payment/sessions/pay_1"
            ),
        },
        {
            "id": "../pay_1",
            "amount": 199,
            "status": "pending",
            "payment_url": "https://tokendance.space/pay/pay_1",
            "status_url": (
                "https://tokendance.space/portal/api/v1/payment/sessions/pay_1"
            ),
        },
        {
            "id": "pay_1",
            "amount": 199,
            "status": "pending",
            "payment_url": "javascript:alert(1)",
            "status_url": (
                "https://tokendance.space/portal/api/v1/payment/sessions/pay_1"
            ),
        },
        {
            "id": "pay_1",
            "amount": 199,
            "status": "pending",
            "payment_url": "https://tokendance.space/pay/pay_1",
            "status_url": "https://evil.test/pay_1",
        },
    ],
)
async def test_t053_t058_payment_response_cannot_pollute_intent_or_qr(
    app,
    session,
) -> None:
    _FakeAccountClient.payment_create_session = session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/models/tokendance/account/payment/sessions",
            json={
                "amount": 199,
                "client_intent_id": "tdpi_0123456789abcdef",
            },
        )

    assert response.status_code == 502
    assert app.state.test_provider_manager.payment_sessions == {}
    intent = app.state.test_provider_manager.payment_intents[
        "tdpi_0123456789abcdef"
    ]
    assert intent["status"] == "prepared"


@pytest.mark.asyncio
async def test_unowned_payment_session_is_rejected_before_upstream_call(
    app,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get(
            "/models/tokendance/account/payment/sessions/pay_someone_else",
        )

    assert response.status_code == 404
    assert (
        response.json()["detail"]["code"]
        == "tokendance_payment_session_not_owned"
    )
    assert _FakeAccountClient.payment_session_calls == 0


@pytest.mark.asyncio
async def test_account_routes_require_a_saved_tokendance_key(
    app,
) -> None:
    app.dependency_overrides[get_provider_manager] = lambda: _Manager("")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/models/tokendance/account/balance")

    assert response.status_code == 409
    assert "连接词元跳动账号" in response.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_balance_rejects_key_bound_to_a_different_watcha_subject(
    app,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        cloud_account_store,
        "CLOUD_ACCOUNT_FILE",
        tmp_path / "cloud-account.json",
    )
    monkeypatch.setattr(
        cloud_account_store,
        "encrypt_dict_fields",
        lambda data, _fields: data,
    )
    monkeypatch.setattr(
        cloud_account_store,
        "decrypt_dict_fields",
        lambda data, _fields: data,
    )
    cloud_account_store.save_cloud_account(
        {
            "provider": "watcha",
            "refresh_token": "refresh-b",
            "user": {"id": "user-b"},
        },
    )
    app.dependency_overrides[get_provider_manager] = lambda: _Manager(
        meta={
            "tokendance_credential_binding": {
                "schema_version": 1,
                "status": "active",
                "owner_subject_id": "watcha:user-a",
                "credential_ref": "cred-a",
                "payment_binding_id": "payment-a",
                "binding_revision": 1,
                "account_epoch_at_binding": 1,
                "ownership_evidence": "oauth_pkce",
                "scopes": ["balance"],
            },
        },
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/models/tokendance/account/balance")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "credential_owner_mismatch"


@pytest.mark.asyncio
async def test_balance_discards_response_after_watcha_account_switch(
    app,
    monkeypatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _DelayedAccountClient(_FakeAccountClient):
        async def balance(self):
            entered.set()
            await release.wait()
            return await super().balance()

    monkeypatch.setattr(
        providers_module,
        "TokenDanceAccountClient",
        _DelayedAccountClient,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        request = asyncio.create_task(
            client.get("/models/tokendance/account/balance"),
        )
        await entered.wait()
        cloud_account_store.save_cloud_account(
            {
                "provider": "watcha",
                "refresh_token": "refresh-other",
                "user": {"id": "other-user"},
            },
        )
        release.set()
        response = await request

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "authorization_context_stale"
