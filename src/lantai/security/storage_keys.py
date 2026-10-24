# -*- coding: utf-8 -*-
"""Safe storage keys for user- or model-controlled identifiers."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4


_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _slug(value: str, *, max_chars: int = 40) -> str:
    slug = _UNSAFE_KEY_CHARS.sub("-", value.strip()).strip(" .-_")
    slug = slug[:max_chars].strip(" .-_")
    if not slug:
        slug = "id"
    if slug.upper() in _WINDOWS_RESERVED_NAMES:
        slug = f"id-{slug}"
    return slug


def safe_storage_key(
    kind: str,
    external_id: object,
    *,
    suffix: str = "",
    hash_chars: int = 24,
) -> str:
    """Return a stable filename segment for an external identifier."""

    text = str(external_id or "").strip() or "empty"
    digest = hashlib.sha256(f"{kind}\0{text}".encode("utf-8")).hexdigest()[
        :hash_chars
    ]
    return f"{_slug(text)}-{digest}{suffix}"


def contained_storage_path(root: Path, *parts: str) -> Path:
    """Build a path and ensure its resolved parent remains under ``root``."""

    root_resolved = root.expanduser().resolve(strict=False)
    candidate = root_resolved.joinpath(*parts)
    parent = candidate.parent.resolve(strict=False)
    if parent != root_resolved and root_resolved not in parent.parents:
        raise ValueError("Storage path escapes its root")
    return candidate


def contained_existing_file(root: Path, *parts: str) -> Path | None:
    """Return an existing contained file, or ``None`` if missing/unsafe."""

    try:
        candidate = contained_storage_path(root, *parts)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _no_follow_flags(flags: int) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    return flags | no_follow


def read_text_no_follow(
    path: Path,
    encoding: str = "utf-8",
    errors: str | None = None,
) -> str:
    """Read a regular file without following a final symlink where supported."""

    fd = os.open(path, _no_follow_flags(os.O_RDONLY))
    try:
        with os.fdopen(fd, "r", encoding=encoding, errors=errors) as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def append_text_no_follow(
    path: Path,
    text: str,
    encoding: str = "utf-8",
) -> None:
    """Append text without following a final symlink where supported."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Storage path is a symlink")
    fd = os.open(
        path,
        _no_follow_flags(os.O_WRONLY | os.O_CREAT | os.O_APPEND),
        0o600,
    )
    try:
        with os.fdopen(fd, "a", encoding=encoding) as handle:
            fd = -1
            handle.write(text)
    finally:
        if fd >= 0:
            os.close(fd)


def write_text_atomic_no_follow(
    path: Path,
    text: str,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace a file without writing through a final symlink."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Storage path is a symlink")

    tmp_path: Path | None = None
    for _ in range(10):
        candidate = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            fd = os.open(
                candidate,
                _no_follow_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                0o600,
            )
        except FileExistsError:
            continue
        tmp_path = candidate
        break
    else:
        raise FileExistsError(f"Could not allocate temp file for {path}")

    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ValueError("Storage path is a symlink")
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
