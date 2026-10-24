# -*- coding: utf-8 -*-
"""Encrypted local persistence for optional Lantai cloud-account sessions."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..constant import SECRET_DIR
from .secret_store import decrypt_dict_fields, encrypt_dict_fields

CLOUD_ACCOUNT_FILE = SECRET_DIR / "cloud-account.json"
CLOUD_ACCOUNT_SECRET_FIELDS = frozenset({"access_token", "refresh_token"})
CLOUD_ACCOUNT_STATE_VERSION = 1
CLOUD_ACCOUNT_STATE_VERSION_KEY = "lantai_cloud_account_state_version"


@dataclass(frozen=True)
class WatchaAccountContext:
    """Trusted local identity version used to fence asynchronous work."""

    subject_id: str | None
    account_epoch: int


def _subject_id(payload: dict[str, Any]) -> str | None:
    user = payload.get("user")
    value = str(user.get("id") or "").strip() if isinstance(user, dict) else ""
    if not (
        payload.get("provider") == "watcha"
        and payload.get("refresh_token")
        and value
    ):
        return None
    return f"watcha:{value}"


def _account_epoch(payload: dict[str, Any]) -> int:
    value = payload.get("account_epoch", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def load_cloud_account() -> dict[str, Any]:
    if not CLOUD_ACCOUNT_FILE.is_file():
        return {}
    try:
        payload = json.loads(CLOUD_ACCOUNT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return decrypt_dict_fields(payload, CLOUD_ACCOUNT_SECRET_FIELDS)


def is_watcha_account_connected() -> bool:
    """Return local Watcha-backed session presence without a network call."""

    try:
        account = load_cloud_account()
    except Exception:
        # Optional cloud identity must never break local/custom providers.
        return False
    user = account.get("user")
    return bool(
        account.get("provider") == "watcha"
        and account.get("refresh_token")
        and isinstance(user, dict)
        and user.get("id")
    )


def load_watcha_account_context() -> WatchaAccountContext:
    """Return the current trusted subject and monotonic local account epoch."""

    try:
        account = load_cloud_account()
    except Exception:
        return WatchaAccountContext(subject_id=None, account_epoch=0)
    subject_id = _subject_id(account)
    account_epoch = _account_epoch(account)
    # Connected accounts saved before the versioned identity context existed
    # belong to their signed Watcha subject, but have no persisted epoch yet.
    # Treat that legacy state as the first epoch without mutating on read.
    if subject_id and account_epoch == 0:
        account_epoch = 1
    return WatchaAccountContext(
        subject_id=subject_id,
        account_epoch=account_epoch,
    )


def _write_cloud_account(payload: dict[str, Any]) -> None:
    protected = encrypt_dict_fields(payload, CLOUD_ACCOUNT_SECRET_FIELDS)
    CLOUD_ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".cloud-account-",
        suffix=".tmp",
        dir=CLOUD_ACCOUNT_FILE.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(protected, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, CLOUD_ACCOUNT_FILE)
        os.chmod(CLOUD_ACCOUNT_FILE, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_cloud_account(payload: dict[str, Any]) -> None:
    current = load_cloud_account()
    version = current.get(CLOUD_ACCOUNT_STATE_VERSION_KEY, 0)
    if not isinstance(version, int) or version < 0:
        raise ValueError("Invalid cloud-account state version")
    if version > CLOUD_ACCOUNT_STATE_VERSION:
        raise ValueError("Unsupported cloud-account state version")

    previous_subject = _subject_id(current)
    next_payload = dict(payload)
    next_subject = _subject_id(next_payload)
    previous_epoch = _account_epoch(current)
    if previous_subject and next_subject != previous_subject:
        next_epoch = max(1, previous_epoch + 1)
    elif previous_epoch:
        next_epoch = previous_epoch
    else:
        next_epoch = 1
    next_payload[CLOUD_ACCOUNT_STATE_VERSION_KEY] = CLOUD_ACCOUNT_STATE_VERSION
    next_payload["account_epoch"] = next_epoch
    _write_cloud_account(next_payload)


def clear_cloud_account() -> None:
    current = load_cloud_account()
    current_epoch = _account_epoch(current)
    if _subject_id(current) and current_epoch == 0:
        # A connected legacy record is exposed as epoch 1 by
        # load_watcha_account_context(). Advance from that logical epoch so a
        # logout/relogin cannot recreate an in-flight request's identity tuple.
        current_epoch = 1
    tombstone = {
        CLOUD_ACCOUNT_STATE_VERSION_KEY: CLOUD_ACCOUNT_STATE_VERSION,
        "account_epoch": max(1, current_epoch + 1),
    }
    _write_cloud_account(tombstone)
