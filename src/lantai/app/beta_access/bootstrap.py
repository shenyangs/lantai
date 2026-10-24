"""Release-scoped composition for the private-beta access subsystem.

The application composes this object once at process start, then supplies the
returned router and admission guard to the shared integration points. Ordinary
release channels never read the beta profile or keychain.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from ...security.beta_access import AccessSnapshot
from ...security.beta_access_store import BetaLicenseStore
from ...security.beta_device_key import DeviceIdentity, DeviceKeyStore
from ...security.beta_license import BetaLicenseVerifier
from ...security.beta_profile import BetaAccessProfile, BetaProfileError, load_beta_access_profile
from ...security.beta_status import BetaStatusVerifier
from ..cloud_account import cloud_account_service
from .account import BetaMemberSession, load_beta_member_session
from .admission import BetaAccessAdmissionGuard
from .client import BetaAccessClient
from .refresh import BetaRefreshCoordinator
from .router import create_beta_access_router
from .service import BetaAccessApplicationService


PRIVATE_BETA_CHANNEL = "private_beta"
RELEASE_CHANNEL_ENV = "LANTAI_RELEASE_CHANNEL"
BETA_PROFILE_ENV = "LANTAI_BETA_ACCESS_PROFILE"


class BetaAccessBootstrapError(RuntimeError):
    """The private-beta release is missing a required trusted component."""


class _LazyDeviceIdentity:
    """Expose device binding without opening Keychain before it is needed."""

    def __init__(self, device_keys: DeviceKeyStore) -> None:
        self._device_keys = device_keys

    @property
    def jkt(self) -> str:
        return self._identity().jkt

    def prove_local_possession(self) -> bool:
        return self._identity().prove_local_possession()

    def _identity(self) -> DeviceIdentity:
        return self._device_keys.load_or_create()


@dataclass(frozen=True)
class BetaAccessRuntime:
    """Composition result consumed by shared app integration points."""

    enabled: bool
    profile: BetaAccessProfile | None
    service: BetaAccessApplicationService | None
    router: APIRouter | None
    admission_guard: BetaAccessAdmissionGuard
    snapshot_reader: Callable[[], AccessSnapshot]


def _release_channel(value: str | None) -> str:
    raw = os.environ.get(RELEASE_CHANNEL_ENV, "") if value is None else value
    return str(raw).strip().lower()


def _disabled_runtime() -> BetaAccessRuntime:
    snapshot_reader = AccessSnapshot.unrestricted
    return BetaAccessRuntime(
        enabled=False,
        profile=None,
        service=None,
        router=None,
        admission_guard=BetaAccessAdmissionGuard(snapshot_reader),
        snapshot_reader=snapshot_reader,
    )


def build_beta_access_runtime(
    *,
    release_channel: str | None = None,
    profile_path: Path | str | None = None,
    license_root: Path | None = None,
    account_service: Any | None = None,
    session_loader: Callable[[], BetaMemberSession] = load_beta_member_session,
    device_key_store_factory: Callable[..., DeviceKeyStore] = DeviceKeyStore,
) -> BetaAccessRuntime:
    """Build the beta subsystem once from release-pinned local inputs.

    A private-beta release fails closed when its profile is unavailable or
    invalid.  The profile is loaded only from the local release path and never
    from a network response.  Other channels are returned as unrestricted and
    do not inspect the profile or protected device storage.
    """

    if _release_channel(release_channel) != PRIVATE_BETA_CHANNEL:
        return _disabled_runtime()

    selected_profile_path = profile_path
    if selected_profile_path is None:
        selected_profile_path = os.environ.get(BETA_PROFILE_ENV, "").strip()
    if not selected_profile_path:
        raise BetaAccessBootstrapError("private-beta access profile is required")
    try:
        profile = load_beta_access_profile(selected_profile_path)
    except BetaProfileError as exc:
        raise BetaAccessBootstrapError("private-beta access profile is invalid") from exc

    try:
        device_keys = device_key_store_factory(
            allowed_audiences=frozenset({profile.proof_audience}),
        )
    except Exception as exc:
        # Construction may import the packaged keyring adapter, but no private
        # key is read or created here. A missing adapter is a release defect.
        raise BetaAccessBootstrapError("private-beta protected key store is unavailable") from exc
    device_identity = _LazyDeviceIdentity(device_keys)
    verifier = BetaLicenseVerifier(
        issuer=profile.issuer,
        audience=profile.audience,
        key_ring=profile.key_ring,
    )
    status_verifier = BetaStatusVerifier(
        issuer=profile.issuer,
        key_ring=profile.key_ring,
    )
    store = BetaLicenseStore(
        verifier=verifier,
        device_identity=device_identity,
        root=license_root,
        enforcement_required=True,
    )
    client = BetaAccessClient(
        base_url=profile.service_origin,
        device_keys=device_keys,
    )

    def snapshot_reader() -> AccessSnapshot:
        selected = session_loader().context
        return store.snapshot(
            account_id=selected.account_id,
            account_epoch=selected.account_epoch,
        )

    refresh = BetaRefreshCoordinator(
        store=store,
        client=client,
        account_context=lambda: session_loader().context,
        status_verifier=status_verifier,
    )
    service = BetaAccessApplicationService(
        account_service=account_service or cloud_account_service,
        session_loader=session_loader,
        client=client,
        store=store,
        refresh=refresh,
    )
    return BetaAccessRuntime(
        enabled=True,
        profile=profile,
        service=service,
        router=create_beta_access_router(service=service),
        admission_guard=BetaAccessAdmissionGuard(snapshot_reader),
        snapshot_reader=snapshot_reader,
    )
