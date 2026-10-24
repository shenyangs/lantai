from __future__ import annotations

import io
import json
import zipfile

import pytest

from lantai.backup._utils.constants import INTEGRITY_FILE
from lantai.backup.integrity import (
    BackupFileIntegrity,
    BackupIntegrityError,
    verify_zip_integrity,
    write_bytes_with_integrity,
    write_integrity_manifest,
)
from lantai.backup.models import BackupMeta


def _verified_archive() -> bytes:
    buffer = io.BytesIO()
    entries: list[BackupFileIntegrity] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        write_bytes_with_integrity(zf, b'{"version":"2"}', "meta.json", entries)
        write_bytes_with_integrity(zf, b"hello\n", "workspaces/a/note.txt", entries)
        write_integrity_manifest(zf, entries)
    return buffer.getvalue()


def test_verify_zip_integrity_accepts_complete_archive() -> None:
    with zipfile.ZipFile(io.BytesIO(_verified_archive()), "r") as zf:
        summary = verify_zip_integrity(zf, required=True)

    assert summary.status == "verified"
    assert summary.file_count == 2
    assert summary.total_bytes == len(b'{"version":"2"}') + len(b"hello\n")


def test_verify_zip_integrity_rejects_changed_member() -> None:
    original = _verified_archive()
    with zipfile.ZipFile(io.BytesIO(original), "r") as source:
        manifest = source.read(INTEGRITY_FILE)
        meta = source.read("meta.json")

    tampered = io.BytesIO()
    with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr("meta.json", meta)
        target.writestr("workspaces/a/note.txt", b"changed\n")
        target.writestr(INTEGRITY_FILE, manifest)

    with zipfile.ZipFile(io.BytesIO(tampered.getvalue()), "r") as zf:
        with pytest.raises(BackupIntegrityError, match="size mismatch|checksum mismatch"):
            verify_zip_integrity(zf, required=True)


def test_verify_zip_integrity_rejects_manifest_with_missing_member() -> None:
    original = _verified_archive()
    with zipfile.ZipFile(io.BytesIO(original), "r") as source:
        manifest = json.loads(source.read(INTEGRITY_FILE))
        meta = source.read("meta.json")
    assert manifest["file_count"] == 2

    incomplete = io.BytesIO()
    with zipfile.ZipFile(incomplete, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr("meta.json", meta)
        target.writestr(INTEGRITY_FILE, json.dumps(manifest).encode("utf-8"))

    with zipfile.ZipFile(io.BytesIO(incomplete.getvalue()), "r") as zf:
        with pytest.raises(BackupIntegrityError, match="member set differs"):
            verify_zip_integrity(zf, required=True)


def test_legacy_archive_is_explicitly_unverified() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr("meta.json", b'{"version":"1"}')

    with zipfile.ZipFile(io.BytesIO(buffer.getvalue()), "r") as zf:
        summary = verify_zip_integrity(zf, required=False)

    assert summary.status == "legacy_unverified"
    assert summary.file_count == 1


def test_metadata_without_version_remains_legacy_v1() -> None:
    meta = BackupMeta.model_validate({"name": "old backup"})

    assert meta.version == "1"


def test_current_archive_requires_integrity_manifest() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr("meta.json", b'{"version":"2"}')

    with zipfile.ZipFile(io.BytesIO(buffer.getvalue()), "r") as zf:
        with pytest.raises(BackupIntegrityError, match="missing integrity.json"):
            verify_zip_integrity(zf, required=True)
