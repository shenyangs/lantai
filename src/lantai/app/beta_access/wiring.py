"""Shared application seams for locally enforced beta access."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from ...security.beta_access import AccessBoundary, AdmissionDecision
from .admission import BetaAccessAdmissionDenied, BetaAccessAdmissionGuard


def register_beta_access_runtime(app: FastAPI, runtime: Any) -> None:
    """Expose the release-scoped runtime and register its recovery routes."""

    app.state.beta_access_runtime = runtime
    if runtime.router is not None:
        app.include_router(runtime.router, prefix="/api")


def require_workspace_beta_access(
    workspace: Any,
    *,
    boundary: AccessBoundary,
    capability: str,
) -> AdmissionDecision | None:
    """Apply one cached-snapshot decision when a workspace owns a guard."""

    guard = getattr(workspace, "beta_access_admission_guard", None)
    if guard is None:
        return None
    return guard.require(boundary=boundary, capability=capability)


def require_workspace_beta_access_http(
    workspace: Any,
    *,
    boundary: AccessBoundary,
    capability: str,
) -> AdmissionDecision | None:
    """Map a local admission denial to the stable HTTP boundary contract."""

    try:
        return require_workspace_beta_access(
            workspace,
            boundary=boundary,
            capability=capability,
        )
    except BetaAccessAdmissionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "beta_access_denied",
                "reason": exc.reason,
                "snapshot_revision": exc.snapshot_revision,
            },
        ) from exc


def protected_write_denial_reason(
    guard: BetaAccessAdmissionGuard | None,
    capability: str,
) -> str | None:
    """Return a denial reason without exposing signing or network ability."""

    if guard is None:
        return None
    decision = guard.decide(
        boundary=AccessBoundary.PROTECTED_WRITE,
        capability=capability,
    )
    return None if decision.allowed else decision.reason
