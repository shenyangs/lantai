from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from lantai.app.beta_access.account import BetaMemberSession
from lantai.app.beta_access.refresh import BetaAccountContext, RefreshOutcome
from lantai.app.beta_access.service import BetaAccessApplicationError, BetaAccessApplicationService
from lantai.app.cloud_account import CloudAccountError
from lantai.security.beta_access import AccessSnapshot, BetaAccessStatus
from lantai.security.beta_access_store import LicenseCandidateRejected


ACCOUNT = "7c8468d0-54aa-4665-ad51-2f47c38c1151"


class AccountService:
    def __init__(self, on_status=None):
        self.calls = 0
        self.on_status = on_status

    async def status(self):
        self.calls += 1
        if self.on_status:
            self.on_status()
        return {"connected": True}


class Client:
    def __init__(self, response=None):
        self.response = response or {"status": "issued", "license": "signed-license", "operationId": "operation-1"}
        self.activations = []
        self.operations = []

    async def activate(self, **kwargs):
        self.activations.append(kwargs)
        return dict(self.response)

    async def issue(self, **kwargs):
        return dict(self.response)

    async def operation_status(self, **kwargs):
        self.operations.append(kwargs)
        return dict(self.response)


class Store:
    def __init__(self):
        self.installs = []
        self.current = AccessSnapshot(True, BetaAccessStatus.NOT_ACTIVATED, None, 0, 0)

    def snapshot(self, *, account_id, account_epoch):
        return replace(self.current, subject=account_id)

    def installation_subject(self):
        return "installation:device-jkt"

    def install_candidate(self, token, *, account_id, account_epoch, subject=None):
        expected_subject = subject or account_id
        self.installs.append((token, account_id, account_epoch, expected_subject))
        self.current = AccessSnapshot(True, BetaAccessStatus.ACTIVE, expected_subject, 1, 1)
        return self.current


class Refresh:
    def __init__(self):
        self.snapshots = []

    def maybe_refresh(self, snapshot):
        self.snapshots.append(snapshot)
        return RefreshOutcome.NOT_DUE


def session(account_id=ACCOUNT, epoch=4, token="short-lived-access"):
    UUID(account_id)
    return BetaMemberSession(BetaAccountContext(account_id, epoch), token)


@pytest.mark.asyncio
async def test_activation_refreshes_existing_member_session_and_preserves_code_exactly():
    selected = session(token="expired")

    def refreshed():
        nonlocal selected
        selected = session(token="fresh")

    client = Client()
    store = Store()
    service = BetaAccessApplicationService(
        account_service=AccountService(refreshed),
        session_loader=lambda: selected,
        client=client,
        store=store,
        refresh=Refresh(),
    )

    result = await service.activate(code="朋友 请坐", request_id="stable-request")

    assert result["status"] == "active"
    assert client.activations == [{
        "code": "朋友 请坐",
        "request_id": "stable-request",
        "access_token": "fresh",
        "device_label": None,
    }]
    assert store.installs == [("signed-license", ACCOUNT, 4, ACCOUNT)]
    assert "license" not in result


@pytest.mark.asyncio
async def test_late_allow_is_not_installed_after_account_epoch_changes():
    selected = session(epoch=8)

    class SwitchingClient(Client):
        async def activate(self, **kwargs):
            nonlocal selected
            selected = session(epoch=9)
            return await super().activate(**kwargs)

    store = Store()
    service = BetaAccessApplicationService(
        account_service=AccountService(),
        session_loader=lambda: selected,
        client=SwitchingClient(),
        store=store,
        refresh=Refresh(),
    )

    with pytest.raises(BetaAccessApplicationError, match="账号已经切换"):
        await service.activate(code="春", request_id="stable-request")
    assert store.installs == []


@pytest.mark.asyncio
async def test_pending_activation_keeps_operation_for_recovery_without_exposing_license():
    service = BetaAccessApplicationService(
        account_service=AccountService(),
        session_loader=lambda: session(),
        client=Client({"status": "issuance_pending", "operationId": "operation-unknown"}),
        store=Store(),
        refresh=Refresh(),
    )

    result = await service.activate(code="春", request_id="stable-request")

    assert result == {"status": "issuance_pending", "operation_id": "operation-unknown"}


@pytest.mark.asyncio
async def test_activation_without_a_member_session_binds_to_this_installation():
    account_service = AccountService()
    client = Client()
    store = Store()
    service = BetaAccessApplicationService(
        account_service=account_service,
        session_loader=lambda: BetaMemberSession(BetaAccountContext(None, 0), None),
        client=client,
        store=store,
        refresh=Refresh(),
    )

    result = await service.activate(code="春", request_id="anonymous-request")

    assert result["status"] == "active"
    assert account_service.calls == 0
    assert client.activations == [{
        "code": "春",
        "request_id": "anonymous-request",
        "access_token": "",
        "device_label": None,
    }]
    assert store.installs == [
        ("signed-license", None, 0, "installation:device-jkt")
    ]


@pytest.mark.asyncio
async def test_anonymous_pending_operation_uses_scoped_recovery_token_without_login():
    client = Client({
        "status": "issuance_pending",
        "operationId": "operation-anonymous",
        "recoveryToken": "scoped-token",
    })
    store = Store()
    service = BetaAccessApplicationService(
        account_service=AccountService(),
        session_loader=lambda: BetaMemberSession(BetaAccountContext(None, 0), None),
        client=client,
        store=store,
        refresh=Refresh(),
    )

    pending = await service.activate(code="春", request_id="anonymous-request")
    assert pending == {
        "status": "issuance_pending",
        "operation_id": "operation-anonymous",
        "recovery_token": "scoped-token",
    }

    client.response = {"status": "issued", "license": "signed-license", "operationId": "operation-anonymous"}
    recovered = await service.recover(
        operation_id=pending["operation_id"],
        recovery_token=pending["recovery_token"],
    )
    assert recovered["status"] == "active"
    assert client.operations == [{
        "operation_id": "operation-anonymous",
        "access_token": "",
        "recovery_token": "scoped-token",
    }]
    assert store.installs == [
        ("signed-license", None, 0, "installation:device-jkt")
    ]


def test_local_status_never_waits_for_network_and_only_schedules_refresh():
    account_service = AccountService()
    refresh = Refresh()
    store = Store()
    store.current = AccessSnapshot(True, BetaAccessStatus.ACTIVE, ACCOUNT, 3, 7, refresh_due=True)
    service = BetaAccessApplicationService(
        account_service=account_service,
        session_loader=lambda: session(),
        client=Client(),
        store=store,
        refresh=refresh,
    )

    result = service.local_status()

    assert result["status"] == "active"
    assert result["refresh_scheduled"] is False
    assert len(refresh.snapshots) == 1
    assert account_service.calls == 0


@pytest.mark.asyncio
async def test_invalid_signed_candidate_does_not_report_activation_success():
    class RejectingStore(Store):
        def install_candidate(self, *args, **kwargs):
            raise LicenseCandidateRejected("bad signature")

    service = BetaAccessApplicationService(
        account_service=AccountService(),
        session_loader=lambda: session(),
        client=Client(),
        store=RejectingStore(),
        refresh=Refresh(),
    )

    with pytest.raises(BetaAccessApplicationError, match="本地验签失败"):
        await service.activate(code="春", request_id="stable-request")


@pytest.mark.asyncio
async def test_member_session_refresh_failure_is_a_stable_application_error():
    class FailingAccountService(AccountService):
        async def status(self):
            raise CloudAccountError("account_network", "temporary upstream detail", status_code=503)

    service = BetaAccessApplicationService(
        account_service=FailingAccountService(),
        session_loader=lambda: session(),
        client=Client(),
        store=Store(),
        refresh=Refresh(),
    )

    with pytest.raises(BetaAccessApplicationError) as raised:
        await service.activate(code="春", request_id="stable-request")

    assert raised.value.code == "account_network"
    assert raised.value.status_code == 503
    assert str(raised.value) == "澜台账号暂时无法确认，请稍后再试。"
