"""Pure local beta-access snapshot and trusted admission decision types."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BetaAccessStatus(str, Enum):
    NOT_ACTIVATED = "not_activated"
    ACTIVE = "active"
    GRACE = "grace"
    EXPIRED = "expired"
    REVOKED = "revoked"
    DEVICE_KEY_UNAVAILABLE = "device_key_unavailable"
    CERT_INVALID = "cert_invalid"
    UNSUPPORTED = "unsupported"
    ACCOUNT_SUSPENDED = "account_suspended"
    CLOCK_CHECK_REQUIRED = "clock_check_required"


class AccessBoundary(str, Enum):
    WORK_START = "work_start"
    RESUME_OR_WAKE = "resume_or_wake"
    PROTECTED_WRITE = "protected_write"


@dataclass(frozen=True)
class AccessSnapshot:
    enforcement_required: bool
    status: BetaAccessStatus
    subject: str | None
    decision_seq: int
    snapshot_revision: int
    refresh_due: bool = False
    renew_by: int | None = None
    expires_at: int | None = None

    @classmethod
    def unrestricted(cls) -> "AccessSnapshot":
        return cls(
            enforcement_required=False,
            status=BetaAccessStatus.ACTIVE,
            subject=None,
            decision_seq=0,
            snapshot_revision=0,
        )


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: str
    snapshot_revision: int


_RESCUE_CAPABILITIES = frozenset(
    {
        "account.login",
        "about.read",
        "beta.activate",
        "beta.recover",
        "data.export",
        "diagnostics.read",
        "effect.safe_settle",
        "existing_file.read",
        "history.read",
        "proactive.disable",
        "text.copy",
        "work.stop",
    }
)


def evaluate_admission(
    snapshot: AccessSnapshot,
    *,
    boundary: AccessBoundary,
    capability: str,
) -> AdmissionDecision:
    """Evaluate beta admission without network I/O or modifying tool policy."""

    if not snapshot.enforcement_required:
        return AdmissionDecision(True, "channel_not_beta_gated", snapshot.snapshot_revision)
    if capability in _RESCUE_CAPABILITIES:
        return AdmissionDecision(True, "data_rescue_allowed", snapshot.snapshot_revision)
    if snapshot.status in {BetaAccessStatus.ACTIVE, BetaAccessStatus.GRACE}:
        return AdmissionDecision(True, snapshot.status.value, snapshot.snapshot_revision)
    reason = f"beta_access_{snapshot.status.value}:{boundary.value}"
    return AdmissionDecision(False, reason, snapshot.snapshot_revision)
