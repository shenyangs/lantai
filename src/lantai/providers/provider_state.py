"""Versioned, offline provider-state upgrades. User data is never reset."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile

from .tokendance_catalog import CATALOG

STATE_VERSION = 3
VERSION_KEY = "lantai_provider_state_version"
TOKENDANCE_BINDING_META_KEY = "tokendance_credential_binding"
TOKENDANCE_VERIFIED_CAPABILITIES_META_KEY = (
    "tokendance_verified_capabilities"
)


def atomic_write_provider(
    path: Path, data: dict, original: bytes | None = None
):
    """Write private JSON without exposing a partial provider file."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if original is not None and path.read_bytes() != original:
            raise RuntimeError(
                "Provider changed during migration; retry required"
            )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def normalize_tokendance(data: dict) -> dict:
    """Attach release verification without rewriting the live catalog.

    ``models`` is provider-owned evidence.  Replacing its protocol fields with
    a bundled catalog made a fresh TokenDance response indistinguishable from
    an old offline snapshot, and could silently change the selected transport.
    The bundled catalog is therefore kept as a separate verified-capability
    layer used only when a legacy row contains no protocol declaration.
    """
    result = deepcopy(data)
    result.setdefault("meta", {})[
        TOKENDANCE_VERIFIED_CAPABILITIES_META_KEY
    ] = {
        "schema_version": 1,
        "revision": "release-2026-09-12",
        "models": deepcopy(CATALOG),
    }
    return result


def upgrade_provider_data(data: dict) -> dict:
    """Pure migration. Ciphertext and unknown fields pass through untouched."""
    if data.get("id") != "tokendance":
        return deepcopy(data)
    version = data.get("meta", {}).get(VERSION_KEY, 0)
    if not isinstance(version, int) or version < 0 or version > STATE_VERSION:
        raise ValueError(
            "Unsupported provider-state version; original retained"
        )
    if version == STATE_VERSION:
        return deepcopy(data)
    result = deepcopy(data)
    if version < 1:
        result = normalize_tokendance(result)
        result.setdefault("meta", {})[VERSION_KEY] = 1
        version = 1
    if version < 2:
        meta = result.setdefault("meta", {})
        if result.get("api_key") and TOKENDANCE_BINDING_META_KEY not in meta:
            meta[TOKENDANCE_BINDING_META_KEY] = {
                "schema_version": 1,
                "status": "reauthorization_required",
                "owner_subject_id": None,
                "credential_ref": "legacy-unconfirmed",
                "payment_binding_id": "",
                "binding_revision": 0,
                "account_epoch_at_binding": None,
                "ownership_evidence": "legacy_unconfirmed",
                "scopes": [],
            }
        meta[VERSION_KEY] = 2
        version = 2
    if version < 3:
        result = normalize_tokendance(result)
        result.setdefault("meta", {})[VERSION_KEY] = 3
    return result


def migrate_provider_file(path: Path, data: dict) -> dict:
    """Backup before atomic replacement. Failure leaves original retryable.

    Called after validation by the single provider manager. A backup is
    deliberately not named *.json, so provider discovery cannot load it.
    """
    upgraded = upgrade_provider_data(data)
    if upgraded == data:
        return upgraded
    original = path.read_bytes()
    if json.loads(original) != data:
        raise RuntimeError("Provider changed since load; retry required")
    backup = path.with_suffix(
        f".json.pre-provider-state-v{STATE_VERSION}.bak",
    )
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.backup.", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, backup)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    atomic_write_provider(path, upgraded, original)
    return upgraded
