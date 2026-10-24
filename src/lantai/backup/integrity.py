# -*- coding: utf-8 -*-
"""Checksummed backup archive writer and verifier.

The integrity manifest is deliberately separate from ``meta.json``.  It
describes every other file in the archive (including ``meta.json``) and is
verified before an imported backup is stored or a restore starts writing.
This detects missing members, duplicate paths, truncation and content drift;
it is a corruption check, not a cryptographic signature.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ._utils.constants import INTEGRITY_FILE

INTEGRITY_FORMAT_VERSION = 1
INTEGRITY_ALGORITHM = "sha256"
_COPY_CHUNK_BYTES = 1024 * 1024


class BackupIntegrityError(ValueError):
    """Raised when a checksummed backup cannot be trusted for restore."""


@dataclass(frozen=True)
class BackupFileIntegrity:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BackupIntegritySummary:
    status: str
    file_count: int
    total_bytes: int


def _normalized_archive_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip().lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise BackupIntegrityError(f"Unsafe backup member path: {path!r}")
    return "/".join(parts)


def write_path_with_integrity(
    zf: zipfile.ZipFile,
    source: Path,
    archive_path: str,
    entries: list[BackupFileIntegrity],
) -> BackupFileIntegrity:
    """Write one file to *zf* while hashing the exact bytes being written."""
    arcname = _normalized_archive_path(archive_path)
    digest = hashlib.sha256()
    size = 0
    info = zipfile.ZipInfo.from_file(source, arcname=arcname)
    info.compress_type = zf.compression
    with source.open("rb") as src, zf.open(info, "w") as dst:
        while chunk := src.read(_COPY_CHUNK_BYTES):
            dst.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    entry = BackupFileIntegrity(path=arcname, size=size, sha256=digest.hexdigest())
    entries.append(entry)
    return entry


def write_bytes_with_integrity(
    zf: zipfile.ZipFile,
    payload: bytes,
    archive_path: str,
    entries: list[BackupFileIntegrity],
) -> BackupFileIntegrity:
    """Write in-memory bytes and append their integrity record."""
    arcname = _normalized_archive_path(archive_path)
    zf.writestr(arcname, payload)
    entry = BackupFileIntegrity(
        path=arcname,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    entries.append(entry)
    return entry


def write_integrity_manifest(
    zf: zipfile.ZipFile,
    entries: list[BackupFileIntegrity],
) -> BackupIntegritySummary:
    """Write the final archive manifest after all protected members."""
    paths = [entry.path for entry in entries]
    if len(paths) != len(set(paths)):
        raise BackupIntegrityError("Backup contains duplicate member paths")
    total_bytes = sum(entry.size for entry in entries)
    payload = {
        "format_version": INTEGRITY_FORMAT_VERSION,
        "algorithm": INTEGRITY_ALGORITHM,
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "files": [asdict(entry) for entry in sorted(entries, key=lambda item: item.path)],
    }
    zf.writestr(
        INTEGRITY_FILE,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return BackupIntegritySummary(
        status="verified",
        file_count=len(entries),
        total_bytes=total_bytes,
    )


def _parse_manifest(raw: bytes) -> tuple[list[BackupFileIntegrity], dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("Backup integrity manifest is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BackupIntegrityError("Backup integrity manifest must be an object")
    if payload.get("format_version") != INTEGRITY_FORMAT_VERSION:
        raise BackupIntegrityError(
            f"Unsupported integrity format: {payload.get('format_version')!r}"
        )
    if payload.get("algorithm") != INTEGRITY_ALGORITHM:
        raise BackupIntegrityError(
            f"Unsupported integrity algorithm: {payload.get('algorithm')!r}"
        )
    rows = payload.get("files")
    if not isinstance(rows, list):
        raise BackupIntegrityError("Backup integrity manifest has no file list")
    entries: list[BackupFileIntegrity] = []
    for row in rows:
        if not isinstance(row, dict):
            raise BackupIntegrityError("Backup integrity file entry must be an object")
        try:
            path = _normalized_archive_path(str(row["path"]))
            size = int(row["size"])
            sha256 = str(row["sha256"]).lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupIntegrityError("Backup integrity file entry is malformed") from exc
        if size < 0 or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise BackupIntegrityError(f"Backup integrity entry is invalid: {path}")
        entries.append(BackupFileIntegrity(path=path, size=size, sha256=sha256))
    return entries, payload


def verify_zip_integrity(
    zf: zipfile.ZipFile,
    *,
    required: bool,
) -> BackupIntegritySummary:
    """Verify every protected member before import or restore.

    Legacy version-1 backups have no integrity manifest and return an explicit
    ``legacy_unverified`` summary when *required* is false.
    """
    infos = [info for info in zf.infolist() if not info.is_dir()]
    names = [info.filename.replace("\\", "/") for info in infos]
    if len(names) != len(set(names)):
        raise BackupIntegrityError("Backup contains duplicate archive members")
    if INTEGRITY_FILE not in names:
        if required:
            raise BackupIntegrityError("Backup is missing integrity.json")
        return BackupIntegritySummary(
            status="legacy_unverified",
            file_count=max(0, len(infos)),
            total_bytes=sum(info.file_size for info in infos),
        )

    entries, payload = _parse_manifest(zf.read(INTEGRITY_FILE))
    expected = {entry.path: entry for entry in entries}
    if len(expected) != len(entries):
        raise BackupIntegrityError("Backup integrity manifest contains duplicate paths")
    actual_infos = {
        info.filename.replace("\\", "/"): info
        for info in infos
        if info.filename.replace("\\", "/") != INTEGRITY_FILE
    }
    if set(actual_infos) != set(expected):
        missing = sorted(set(expected) - set(actual_infos))
        unexpected = sorted(set(actual_infos) - set(expected))
        raise BackupIntegrityError(
            "Backup member set differs from integrity manifest "
            f"(missing={missing[:5]}, unexpected={unexpected[:5]})"
        )

    total_bytes = 0
    for path, entry in expected.items():
        info = actual_infos[path]
        if info.file_size != entry.size:
            raise BackupIntegrityError(
                f"Backup member size mismatch: {path} "
                f"(expected {entry.size}, got {info.file_size})"
            )
        digest = hashlib.sha256()
        size = 0
        with zf.open(info, "r") as src:
            while chunk := src.read(_COPY_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
        if size != entry.size or digest.hexdigest() != entry.sha256:
            raise BackupIntegrityError(f"Backup member checksum mismatch: {path}")
        total_bytes += size

    if payload.get("file_count") != len(entries):
        raise BackupIntegrityError("Backup integrity file count does not match")
    if payload.get("total_bytes") != total_bytes:
        raise BackupIntegrityError("Backup integrity byte count does not match")
    return BackupIntegritySummary(
        status="verified",
        file_count=len(entries),
        total_bytes=total_bytes,
    )
