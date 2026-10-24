from __future__ import annotations

import pytest

from lantai.app.beta_access.admission import BetaAccessAdmissionDenied, BetaAccessAdmissionGuard
from lantai.security.beta_access import AccessBoundary, AccessSnapshot, BetaAccessStatus


def test_runtime_guard_uses_one_local_snapshot_and_never_calls_network():
    calls = []
    snapshot = AccessSnapshot(True, BetaAccessStatus.EXPIRED, "account", 4, 9)
    guard = BetaAccessAdmissionGuard(lambda: calls.append("snapshot") or snapshot)

    with pytest.raises(BetaAccessAdmissionDenied) as raised:
        guard.require(boundary=AccessBoundary.WORK_START, capability="application.use")

    assert raised.value.reason == "beta_access_expired:work_start"
    assert raised.value.snapshot_revision == 9
    assert calls == ["snapshot"]


def test_runtime_guard_keeps_history_and_existing_byte_export_available():
    snapshot = AccessSnapshot(True, BetaAccessStatus.REVOKED, "account", 7, 11)
    guard = BetaAccessAdmissionGuard(lambda: snapshot)

    assert guard.require(boundary=AccessBoundary.PROTECTED_WRITE, capability="history.read").allowed
    assert guard.require(boundary=AccessBoundary.PROTECTED_WRITE, capability="data.export").allowed


def test_non_beta_channel_preserves_existing_runtime_behavior():
    guard = BetaAccessAdmissionGuard(AccessSnapshot.unrestricted)
    assert guard.require(boundary=AccessBoundary.WORK_START, capability="application.use").allowed
