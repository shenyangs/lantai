"""Read the existing cloud account without inventing a second identity store."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ...security.cloud_account_store import load_cloud_account
from .refresh import BetaAccountContext


@dataclass(frozen=True)
class BetaMemberSession:
    """The server subject and short-lived bearer currently selected locally."""

    context: BetaAccountContext
    access_token: str | None


def load_beta_member_session() -> BetaMemberSession:
    """Map the Watcha-backed account to its canonical ``member_users.id``.

    Chat identity uses a namespaced ``watcha:<uuid>`` subject. Beta licenses
    intentionally bind to the raw member-gateway UUID, so that namespaced value
    must never be copied into the license protocol.
    """

    try:
        payload = load_cloud_account()
    except Exception:
        payload = {}
    epoch = payload.get("account_epoch", 0)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        epoch = 0
    user = payload.get("user")
    candidate = user.get("id") if isinstance(user, dict) else None
    account_id: str | None = None
    if payload.get("provider") == "watcha" and payload.get("refresh_token") and isinstance(candidate, str):
        try:
            account_id = str(UUID(candidate))
        except ValueError:
            account_id = None
    if account_id and epoch == 0:
        epoch = 1
    token = payload.get("access_token")
    access_token = token if account_id and isinstance(token, str) and token else None
    return BetaMemberSession(
        context=BetaAccountContext(account_id=account_id, account_epoch=epoch),
        access_token=access_token,
    )
