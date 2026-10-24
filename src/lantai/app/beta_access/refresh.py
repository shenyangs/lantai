"""Non-blocking beta-license refresh with backoff and account fencing."""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol
from uuid import uuid4

from ...security.beta_access import AccessSnapshot, BetaAccessStatus
from ...security.beta_access_store import BetaLicenseStore, LicenseCandidateRejected
from ...security.beta_status import BetaStatusVerifier
from .client import BetaAccessClientError


@dataclass(frozen=True)
class BetaAccountContext:
    account_id: str | None
    account_epoch: int


@dataclass(frozen=True)
class _RefreshSelection:
    subject: str
    account: BetaAccountContext
    installation_bound: bool


class RefreshOutcome(str, Enum):
    NOT_DUE = "not_due"
    STARTED = "started"
    REFRESHED = "refreshed"
    BACKOFF = "backoff"
    ACCOUNT_CHANGED = "account_changed"
    PENDING = "pending"
    REVOKED = "revoked"


class RefreshClient(Protocol):
    async def refresh(self, *, license_token: str, request_id: str) -> dict: ...


class BetaRefreshCoordinator:
    """Start refresh in the background; never await this from first paint."""

    def __init__(
        self,
        *,
        store: BetaLicenseStore,
        client: RefreshClient,
        account_context: Callable[[], BetaAccountContext],
        status_verifier: BetaStatusVerifier | None = None,
        clock: Callable[[], float] = time.time,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self.store = store
        self.client = client
        self.account_context = account_context
        self.status_verifier = status_verifier
        self.clock = clock
        self.random_source = random_source
        self._task: asyncio.Task[RefreshOutcome] | None = None
        self._failure_count = 0
        self._next_attempt_at = 0.0
        self._backoff_context: _RefreshSelection | None = None

    def maybe_refresh(self, snapshot: AccessSnapshot) -> RefreshOutcome:
        if snapshot.status not in {BetaAccessStatus.ACTIVE, BetaAccessStatus.GRACE} or not snapshot.refresh_due:
            return RefreshOutcome.NOT_DUE
        if self._task is not None and not self._task.done():
            return RefreshOutcome.STARTED
        if not isinstance(snapshot.subject, str) or not snapshot.subject:
            return RefreshOutcome.ACCOUNT_CHANGED
        current_account = self.account_context()
        installation_bound = snapshot.subject.startswith("installation:")
        if installation_bound:
            selected = _RefreshSelection(
                subject=snapshot.subject,
                account=BetaAccountContext(account_id=None, account_epoch=0),
                installation_bound=True,
            )
        else:
            if not current_account.account_id or current_account.account_id != snapshot.subject:
                return RefreshOutcome.ACCOUNT_CHANGED
            selected = _RefreshSelection(
                subject=snapshot.subject,
                account=current_account,
                installation_bound=False,
            )
        if self._backoff_context != selected:
            reader = getattr(self.store, "read_refresh_backoff", None)
            if callable(reader):
                self._failure_count, self._next_attempt_at = reader(
                    account_id=selected.account.account_id,
                    account_epoch=selected.account.account_epoch,
                    subject=selected.subject,
                )
            else:
                self._failure_count, self._next_attempt_at = (0, 0.0)
            self._backoff_context = selected
        if self.clock() < self._next_attempt_at:
            return RefreshOutcome.BACKOFF
        self._task = asyncio.create_task(self._run(selected), name="beta-license-refresh")
        return RefreshOutcome.STARTED

    async def wait_for_idle(self) -> RefreshOutcome:
        task = self._task
        if task is None:
            return RefreshOutcome.NOT_DUE
        return await asyncio.shield(task)

    async def _run(self, selected: _RefreshSelection) -> RefreshOutcome:
        request_id = str(uuid4())
        try:
            response = await self.client.refresh(
                license_token=self.store.read_current_license(),
                request_id=request_id,
            )
            current = self.account_context()
            if not selected.installation_bound and current != selected.account:
                return RefreshOutcome.ACCOUNT_CHANGED
            status = response.get("status")
            token = response.get("license")
            if status == "issuance_pending":
                return RefreshOutcome.PENDING
            if status == "decision_pending":
                return RefreshOutcome.PENDING
            if status == "revoked":
                signed_status = response.get("signedStatus")
                challenge_id = response.get("_challenge_id")
                if (
                    self.status_verifier is None
                    or not isinstance(signed_status, str)
                    or not signed_status
                    or not isinstance(challenge_id, str)
                    or not challenge_id
                ):
                    raise BetaAccessClientError("invalid_response", "撤销状态响应不完整。")
                self.store.apply_signed_revocation(
                    signed_status,
                    status_verifier=self.status_verifier,
                    account_id=selected.account.account_id,
                    account_epoch=selected.account.account_epoch,
                    subject=selected.subject,
                    request_id=request_id,
                    challenge_id=challenge_id,
                    now=datetime.fromtimestamp(self.clock(), tz=timezone.utc),
                )
                self._failure_count = 0
                self._next_attempt_at = 0.0
                return RefreshOutcome.REVOKED
            if status != "issued" or not isinstance(token, str) or not token:
                raise BetaAccessClientError("invalid_response", "续期响应不完整。")
            self.store.install_candidate(
                token,
                account_id=selected.account.account_id,
                account_epoch=selected.account.account_epoch,
                subject=selected.subject,
                now=datetime.fromtimestamp(self.clock(), tz=timezone.utc),
            )
        except (BetaAccessClientError, LicenseCandidateRejected, OSError):
            self._failure_count = min(self._failure_count + 1, 8)
            base = min(6 * 60 * 60, 60 * (2 ** (self._failure_count - 1)))
            jitter = 0.8 + (0.4 * self.random_source())
            self._next_attempt_at = self.clock() + (base * jitter)
            writer = getattr(self.store, "record_refresh_backoff", None)
            if callable(writer):
                try:
                    writer(
                        account_id=selected.account.account_id,
                        account_epoch=selected.account.account_epoch,
                        subject=selected.subject,
                        failure_count=self._failure_count,
                        next_attempt_at=self._next_attempt_at,
                    )
                except LicenseCandidateRejected:
                    return RefreshOutcome.ACCOUNT_CHANGED
            return RefreshOutcome.BACKOFF
        self._failure_count = 0
        self._next_attempt_at = 0.0
        return RefreshOutcome.REFRESHED
