# -*- coding: utf-8 -*-
"""Short-lived external file grants for local file tools."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .tool_policy import _detect_distribution

FILE_GRANT_READ = "read"
FILE_GRANT_WRITE = "write"
_VALID_FILE_GRANT_OPERATIONS = {FILE_GRANT_READ, FILE_GRANT_WRITE}


class ExternalFileGrantError(ValueError):
    """Raised when an absolute path is not covered by an external grant."""


@dataclass(frozen=True)
class ExternalFileGrant:
    """An in-memory grant for one canonical file outside the workspace.

    The grant stores file identity rather than treating a bare path as
    authorization. App Store grants must be issued from a restored
    security-scoped bookmark; only that fact is stored here, not bookmark data.
    """

    path: str
    operations: tuple[str, ...]
    expires_at: float
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int
    sha256: str | None = None
    owner_id: str = ""
    security_scoped_bookmark: bool = False


_current_external_file_grants: ContextVar[tuple[ExternalFileGrant, ...]] = ContextVar(
    "current_external_file_grants",
    default=(),
)


def _normalise_operations(operations: Iterable[str] | None) -> tuple[str, ...]:
    values = tuple(
        str(operation or "").strip().lower()
        for operation in (operations or (FILE_GRANT_READ,))
    )
    if not values:
        values = (FILE_GRANT_READ,)
    invalid = [operation for operation in values if operation not in _VALID_FILE_GRANT_OPERATIONS]
    if invalid:
        raise ValueError(f"unsupported external file grant operation: {invalid[0]}")
    return tuple(dict.fromkeys(values))


def _normalise_operation(operation: str) -> str:
    value = str(operation or FILE_GRANT_READ).strip().lower()
    if value in {"append", "edit"}:
        value = FILE_GRANT_WRITE
    if value not in _VALID_FILE_GRANT_OPERATIONS:
        raise ValueError(f"unsupported external file grant operation: {operation}")
    return value


def _resolve_regular_file(path: str | Path) -> tuple[Path, os.stat_result]:
    original = Path(path).expanduser()
    if original.is_symlink():
        raise ValueError("external file grant must not reference a symlink")
    resolved = original.resolve(strict=True)
    file_stat = os.stat(str(resolved), follow_symlinks=False)
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("external file grant must reference a regular file")
    return resolved, file_stat


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def issue_external_file_grant(
    path: str | Path,
    *,
    operations: Iterable[str] | None = None,
    ttl_seconds: int = 600,
    owner_id: str = "",
    security_scoped_bookmark: bool = False,
    include_sha256: bool = True,
    distribution: str | None = None,
) -> ExternalFileGrant:
    """Issue a short-lived grant for a single existing file."""

    if _detect_distribution(distribution) == "app_store" and not security_scoped_bookmark:
        raise ValueError(
            "App Store external file grants require a restored security-scoped bookmark.",
        )
    resolved, file_stat = _resolve_regular_file(path)
    expires_at = time.time() + max(1, int(ttl_seconds or 600))
    return ExternalFileGrant(
        path=str(resolved),
        operations=_normalise_operations(operations),
        expires_at=expires_at,
        st_dev=file_stat.st_dev,
        st_ino=file_stat.st_ino,
        st_size=file_stat.st_size,
        st_mtime_ns=file_stat.st_mtime_ns,
        sha256=_sha256_file(resolved) if include_sha256 else None,
        owner_id=str(owner_id or ""),
        security_scoped_bookmark=bool(security_scoped_bookmark),
    )


def set_current_external_file_grants(
    grants: Iterable[ExternalFileGrant] | None,
) -> None:
    """Set external file grants for the current tool execution context."""

    _current_external_file_grants.set(tuple(grants or ()))


def get_current_external_file_grants() -> tuple[ExternalFileGrant, ...]:
    """Return grants bound to the current tool execution context."""

    return _current_external_file_grants.get()


def _grant_identity_matches(
    grant: ExternalFileGrant,
    path: Path,
    file_stat: os.stat_result,
) -> bool:
    if (file_stat.st_dev, file_stat.st_ino) != (grant.st_dev, grant.st_ino):
        return False
    if file_stat.st_size != grant.st_size:
        return False
    if file_stat.st_mtime_ns != grant.st_mtime_ns:
        return False
    if grant.sha256 and _sha256_file(path) != grant.sha256:
        return False
    return True


def authorize_external_file_path(
    path: str | Path,
    *,
    operation: str,
    distribution: str | None = None,
) -> Path:
    """Return a canonical path when the current context grants access."""

    operation = _normalise_operation(operation)
    try:
        resolved, file_stat = _resolve_regular_file(path)
    except Exception as exc:
        raise ExternalFileGrantError("external file grant path is not a regular file") from exc

    now = time.time()
    matching_path_errors: list[str] = []
    active_distribution = _detect_distribution(distribution)
    for grant in get_current_external_file_grants():
        if str(resolved) != grant.path:
            continue
        if now >= grant.expires_at:
            matching_path_errors.append("external file grant has expired")
            continue
        if operation not in grant.operations:
            matching_path_errors.append(
                f"external file grant does not allow {operation} access",
            )
            continue
        if active_distribution == "app_store" and not grant.security_scoped_bookmark:
            matching_path_errors.append(
                "App Store external file grant is missing a security-scoped bookmark",
            )
            continue
        if not _grant_identity_matches(grant, resolved, file_stat):
            matching_path_errors.append("external file grant no longer matches the file")
            continue
        return resolved

    if matching_path_errors:
        raise ExternalFileGrantError("; ".join(matching_path_errors))
    raise ExternalFileGrantError("no external file grant matched this path")
