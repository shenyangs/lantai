"""Local-only admission guard for beta-gated trusted boundaries."""
from __future__ import annotations

from collections.abc import Callable

from ...security.beta_access import AccessBoundary, AccessSnapshot, AdmissionDecision, evaluate_admission


class BetaAccessAdmissionDenied(RuntimeError):
    def __init__(self, decision: AdmissionDecision) -> None:
        super().__init__(decision.reason)
        self.reason = decision.reason
        self.snapshot_revision = decision.snapshot_revision


class BetaAccessAdmissionGuard:
    """Read exactly one cached snapshot for each trusted boundary decision."""

    def __init__(self, snapshot_reader: Callable[[], AccessSnapshot]) -> None:
        self._snapshot_reader = snapshot_reader

    def decide(self, *, boundary: AccessBoundary, capability: str) -> AdmissionDecision:
        snapshot = self._snapshot_reader()
        return evaluate_admission(snapshot, boundary=boundary, capability=capability)

    def require(self, *, boundary: AccessBoundary, capability: str) -> AdmissionDecision:
        decision = self.decide(boundary=boundary, capability=capability)
        if not decision.allowed:
            raise BetaAccessAdmissionDenied(decision)
        return decision
