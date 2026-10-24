from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from lantai.app.beta_access.refresh import BetaAccountContext, BetaRefreshCoordinator, RefreshOutcome
from lantai.app.beta_access.client import BetaAccessClientError
from lantai.security.beta_access import AccessSnapshot, BetaAccessStatus


class Store:
    def __init__(self) -> None:
        self.installed: list[tuple[str, str, int]] = []

    def read_current_license(self) -> str:
        return "old-signed-license"

    def install_candidate(self, token, *, account_id, account_epoch, subject=None, now):
        self.installed.append((token, account_id, account_epoch, subject or account_id))


class Client:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.release = asyncio.Event()

    async def refresh(self, *, license_token, request_id):
        await self.release.wait()
        if self.error:
            raise self.error
        return self.result


def due_snapshot() -> AccessSnapshot:
    return AccessSnapshot(True, BetaAccessStatus.ACTIVE, "account-a", 1, 1, refresh_due=True)


@pytest.mark.asyncio
async def test_refresh_is_background_and_network_failure_keeps_existing_license() -> None:
    store = Store()
    client = Client(error=BetaAccessClientError("network_unavailable", "offline"))
    context = BetaAccountContext("account-a", 4)
    coordinator = BetaRefreshCoordinator(
        store=store,
        client=client,
        account_context=lambda: context,
        clock=lambda: 1_800_000_000.0,
        random_source=lambda: 0.5,
    )

    assert coordinator.maybe_refresh(due_snapshot()) is RefreshOutcome.STARTED
    assert store.installed == []
    client.release.set()
    assert await coordinator.wait_for_idle() is RefreshOutcome.BACKOFF
    assert store.installed == []
    assert coordinator.maybe_refresh(due_snapshot()) is RefreshOutcome.BACKOFF


@pytest.mark.asyncio
async def test_late_old_account_allow_is_discarded_after_epoch_switch() -> None:
    store = Store()
    client = Client(result={"status": "issued", "license": "new-signed-license"})
    selected = BetaAccountContext("account-a", 7)
    coordinator = BetaRefreshCoordinator(
        store=store,
        client=client,
        account_context=lambda: selected,
        clock=lambda: 1_800_000_000.0,
    )

    assert coordinator.maybe_refresh(due_snapshot()) is RefreshOutcome.STARTED
    selected = BetaAccountContext("account-b", 8)
    client.release.set()
    assert await coordinator.wait_for_idle() is RefreshOutcome.ACCOUNT_CHANGED
    assert store.installed == []


@pytest.mark.asyncio
async def test_not_due_does_not_create_network_task() -> None:
    client = Client(result={"status": "issued", "license": "unused"})
    coordinator = BetaRefreshCoordinator(
        store=Store(),
        client=client,
        account_context=lambda: BetaAccountContext("account-a", 1),
    )
    assert coordinator.maybe_refresh(replace(due_snapshot(), refresh_due=False)) is RefreshOutcome.NOT_DUE


@pytest.mark.asyncio
async def test_installation_refresh_is_not_paused_by_logout_or_account_switch() -> None:
    store = Store()
    client = Client(result={"status": "issued", "license": "new-installation-license"})
    selected = BetaAccountContext(None, 0)
    coordinator = BetaRefreshCoordinator(
        store=store,
        client=client,
        account_context=lambda: selected,
        clock=lambda: 1_800_000_000.0,
    )
    snapshot = AccessSnapshot(
        True,
        BetaAccessStatus.ACTIVE,
        "installation:device-jkt",
        1,
        1,
        refresh_due=True,
    )

    assert coordinator.maybe_refresh(snapshot) is RefreshOutcome.STARTED
    selected = BetaAccountContext("account-b", 11)
    client.release.set()
    assert await coordinator.wait_for_idle() is RefreshOutcome.REFRESHED
    assert store.installed == [
        ("new-installation-license", None, 0, "installation:device-jkt")
    ]
