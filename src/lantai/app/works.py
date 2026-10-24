# -*- coding: utf-8 -*-
"""Persistent, user-curated works built from generated artifacts."""

from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Mapping
from uuid import UUID, uuid4, uuid5
from zipfile import BadZipFile, ZipFile

from pydantic import BaseModel, Field, model_validator

from lantai.security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    write_text_atomic_no_follow,
)

from .capabilities.artifacts import (
    ArtifactManifestUnavailable,
    artifact_from_path,
    load_artifact_manifest,
    prepare_artifact_manifest_write,
    register_artifact,
    unregister_artifact,
)
from .storage_lock import exclusive_path_lock


WORKS_SCHEMA_VERSION = 5
WORKS_MIN_READABLE_SCHEMA_VERSION = 4
WORKS_INDEX_BACKUP_LIMIT = 8
WORK_LINEAGE_FILE = ".lantai-work-lineage.json"
WorkKind = Literal[
    "file",
    "image",
    "html",
    "markdown",
    "pdf",
    "presentation",
    "document",
    "spreadsheet",
    "audio",
    "video",
]
_MANAGED_COVER_NAME = re.compile(r"^cover-[0-9a-f]{16}\.[A-Za-z0-9]+$")
_HTML_BUNDLE_IGNORED_DIRS = {
    ".git",
    ".lantai",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
}
_HTML_BUNDLE_MAX_FILES = 600
_HTML_BUNDLE_MAX_BYTES = 100 * 1024 * 1024
_WORK_CANDIDATE_NAMESPACE = UUID("2230f569-9c01-4bdc-8fc4-982b65ee43e2")
_WORK_ARTIFACT_NAMESPACE = UUID("c3b27f13-45e2-47c7-a47a-c606f8a00e2e")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkDerivation(BaseModel):
    """Exact provenance for a different-format child artifact.

    A derivation is intentionally distinct from ``parent_work_id``: the
    latter means "next revision of the same logical Work", while this record
    means "new artifact converted from these exact source bytes".
    """

    schema_version: Literal[1] = 1
    operation: Literal["office_conversion"]
    target_format: Literal["pdf"]
    source_work_id: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    source_version: int = Field(ge=1)
    source_digest: str = Field(min_length=1)
    output_digest: str = Field(min_length=1)
    converter_output_digest: str = Field(min_length=1)
    converter: str = Field(min_length=1)


class WorkItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    artifact_id: str
    title: str
    kind: WorkKind = "file"
    path: str
    source_path: str
    mime_type: str | None = None
    size: int | None = None
    source_chat_id: str | None = None
    source_run_id: str | None = None
    source_digest: str | None = None
    parent_work_id: str | None = None
    parent_digest: str | None = None
    lineage_root_id: str | None = None
    version: int = Field(default=1, ge=1)
    derivation: WorkDerivation | None = None
    cover_path: str | None = None
    cover_mime_type: str | None = None
    cover_source: Literal["ai"] | None = None
    availability_status: Literal[
        "available",
        "missing",
        "permission_denied",
        "unknown",
    ] = "unknown"
    integrity_status: Literal["intact", "modified", "unknown"] = "unknown"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_derivation(self) -> "WorkItem":
        relation = self.derivation
        if relation is None:
            return self
        if self.parent_work_id is not None or self.parent_digest is not None:
            raise ValueError("a derived Work cannot also be a later revision")
        if relation.source_work_id == self.id:
            raise ValueError("a Work cannot be derived from itself")
        if (
            not self.source_digest
            or relation.output_digest != self.source_digest
        ):
            raise ValueError(
                "derived Work output digest does not match its bytes",
            )
        return self


class WorksIndexUnavailable(RuntimeError):
    """The canonical Works index cannot safely be used or rewritten."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        current_digest: str | None = None,
        invalid_record_indexes: tuple[int, ...] = (),
        candidate_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_digest = current_digest
        self.invalid_record_indexes = invalid_record_indexes
        self.candidate_path = candidate_path
        token = f"{code}\0{current_digest or 'none'}".encode("utf-8")
        self.diagnostic_id = (
            f"works-index-{hashlib.sha256(token).hexdigest()[:16]}"
        )

    def public_detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "diagnostic_id": self.diagnostic_id,
            "current_digest": self.current_digest,
            "invalid_record_indexes": list(self.invalid_record_indexes),
            "candidate_path": self.candidate_path,
        }


class WorkDerivationConflict(ValueError):
    """A derived artifact no longer matches its verified conversion receipt."""


class WorksIndexBackup(BaseModel):
    digest: str
    schema_version: int
    work_count: int
    created_at: datetime


class WorksIndexInspection(BaseModel):
    status: Literal[
        "ready",
        "uninitialized",
        "recovery_required",
        "unavailable",
    ]
    code: str | None = None
    diagnostic_id: str | None = None
    current_digest: str | None = None
    schema_version: int | None = None
    work_count: int = 0
    invalid_record_indexes: list[int] = Field(default_factory=list)
    managed_work_ids: list[str] = Field(default_factory=list)
    orphaned_managed_ids: list[str] = Field(default_factory=list)
    backups: list[WorksIndexBackup] = Field(default_factory=list)


class WorksIndexRecoveryPreview(BaseModel):
    current_digest: str
    snapshot_digest: str
    snapshot_schema_version: int
    snapshot_works: list[WorkItem]
    orphaned_managed_ids: list[str] = Field(default_factory=list)
    confirmation_required: bool = True


class WorksIndexRestoreResult(BaseModel):
    status: Literal["restored", "already_restored"]
    restored_digest: str
    quarantine_digest: str | None = None
    work_count: int


@dataclass(frozen=True)
class _WorksIndexSnapshot:
    works: tuple[WorkItem, ...]
    payload: dict[str, object]
    raw_bytes: bytes | None
    digest: str | None
    schema_version: int | None
    uninitialized: bool = False


# The explicit suffix map is clearer than a compressed lookup table here.
# pylint: disable-next=too-many-return-statements
def infer_work_kind(path: str | Path) -> WorkKind:
    suffix = Path(path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}:
        return "image"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".ppt", ".pptx", ".key"}:
        return "presentation"
    if suffix in {".doc", ".docx", ".pages", ".rtf"}:
        return "document"
    if suffix in {".xls", ".xlsx", ".csv", ".numbers"}:
        return "spreadsheet"
    if suffix in {".mp3", ".wav", ".m4a", ".aac"}:
        return "audio"
    if suffix in {".mp4", ".mov", ".webm", ".avi"}:
        return "video"
    return "file"


def works_index_path(workspace_dir: str | Path) -> Path:
    return contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "index.json",
    )


def _sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _read_bytes_no_follow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes_atomic_no_follow(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Storage path is a symlink")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ValueError("Storage path is a symlink")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _managed_work_ids(workspace_dir: str | Path) -> list[str]:
    root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "files",
    )
    try:
        candidates = tuple(root.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise WorksIndexUnavailable(
            "works_index_assets_io_error",
            "无法检查作品文件，作品登记暂不可用",
        ) from exc
    return sorted(
        candidate.name
        for candidate in candidates
        if not candidate.is_symlink() and candidate.is_dir()
    )


def _has_index_history(workspace_dir: str | Path) -> bool:
    """Return whether Works recovery evidence predates a missing index."""

    root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
    )
    for directory_name in ("index-backups", "index-quarantine"):
        directory = root / directory_name
        try:
            if any(
                candidate.is_file() and not candidate.is_symlink()
                for candidate in directory.glob("index-*.json")
            ):
                return True
        except OSError as exc:
            raise WorksIndexUnavailable(
                "works_index_history_io_error",
                "无法确认作品索引历史；不会按新空库初始化",
            ) from exc
    return False


def _parse_index_bytes(raw_bytes: bytes) -> _WorksIndexSnapshot:
    digest = _sha256_bytes(raw_bytes)
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorksIndexUnavailable(
            "works_index_invalid_json",
            "作品索引无法解析；已有作品文件未被删除，写入已暂停",
            current_digest=digest,
        ) from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("works"),
        list,
    ):
        raise WorksIndexUnavailable(
            "works_index_invalid_structure",
            "作品索引结构不完整；已有作品文件未被删除，写入已暂停",
            current_digest=digest,
        )
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise WorksIndexUnavailable(
            "works_index_invalid_schema",
            "作品索引缺少有效 schema；已有作品文件未被删除，写入已暂停",
            current_digest=digest,
        )
    if schema_version > WORKS_SCHEMA_VERSION:
        raise WorksIndexUnavailable(
            "works_index_future_schema",
            "作品索引来自更新版本；当前版本不会覆盖它",
            current_digest=digest,
        )
    if schema_version < WORKS_MIN_READABLE_SCHEMA_VERSION:
        raise WorksIndexUnavailable(
            "works_index_unsupported_schema",
            "作品索引版本过旧且无法安全迁移；写入已暂停",
            current_digest=digest,
        )
    works: list[WorkItem] = []
    invalid_indexes: list[int] = []
    for index, item in enumerate(payload["works"]):
        try:
            works.append(WorkItem.model_validate(item))
        except Exception:
            invalid_indexes.append(index)
    ids = [item.id for item in works]
    artifact_ids = [item.artifact_id for item in works]
    if len(ids) != len(set(ids)) or len(artifact_ids) != len(
        set(artifact_ids),
    ):
        invalid_indexes.extend(range(len(payload["works"])))
    if invalid_indexes:
        unique_indexes = tuple(sorted(set(invalid_indexes)))
        raise WorksIndexUnavailable(
            "works_index_incompatible_records",
            "作品索引包含无法兼容的记录；未跳过记录，写入已暂停",
            current_digest=digest,
            invalid_record_indexes=unique_indexes,
        )
    return _WorksIndexSnapshot(
        works=tuple(works),
        payload=payload,
        raw_bytes=raw_bytes,
        digest=digest,
        schema_version=schema_version,
    )


def _read_index_snapshot(workspace_dir: str | Path) -> _WorksIndexSnapshot:
    path = works_index_path(workspace_dir)
    try:
        raw_bytes = _read_bytes_no_follow(path)
    except FileNotFoundError as exc:
        managed_ids = _managed_work_ids(workspace_dir)
        if managed_ids:
            raise WorksIndexUnavailable(
                "works_index_missing_with_assets",
                "作品索引缺失但作品文件仍在；不会按空库继续写入",
            ) from exc
        if _has_index_history(workspace_dir):
            raise WorksIndexUnavailable(
                "works_index_missing_with_history",
                "作品索引缺失但仍有恢复历史；不会按新空库继续写入",
            ) from exc
        return _WorksIndexSnapshot(
            works=(),
            payload={"schema_version": WORKS_SCHEMA_VERSION, "works": []},
            raw_bytes=None,
            digest=None,
            schema_version=None,
            uninitialized=True,
        )
    except PermissionError as exc:
        raise WorksIndexUnavailable(
            "works_index_permission_denied",
            "没有权限读取作品索引；已有作品文件未被删除",
        ) from exc
    except OSError as exc:
        raise WorksIndexUnavailable(
            "works_index_io_error",
            "读取作品索引失败；已有作品文件未被删除",
        ) from exc
    return _parse_index_bytes(raw_bytes)


def _load_index(workspace_dir: str | Path) -> list[WorkItem]:
    return list(_read_index_snapshot(workspace_dir).works)


def _works_backup_dir(workspace_dir: str | Path) -> Path:
    return contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "index-backups",
    )


def _backup_path(workspace_dir: str | Path, digest: str) -> Path:
    digest_hex = digest.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", digest_hex) is None:
        raise ValueError("invalid Works index digest")
    return _works_backup_dir(workspace_dir) / f"index-{digest_hex}.json"


def _ensure_last_good_backup(
    workspace_dir: str | Path,
    snapshot: _WorksIndexSnapshot,
) -> None:
    if snapshot.raw_bytes is None or snapshot.digest is None:
        return
    path = _backup_path(workspace_dir, snapshot.digest)
    try:
        if path.exists():
            if _read_bytes_no_follow(path) != snapshot.raw_bytes:
                raise WorksIndexUnavailable(
                    "works_index_backup_conflict",
                    "作品索引备份冲突；写入已暂停",
                    current_digest=snapshot.digest,
                )
            return
        _write_bytes_atomic_no_follow(path, snapshot.raw_bytes)
        verified = _parse_index_bytes(_read_bytes_no_follow(path))
    except (OSError, ValueError) as exc:
        raise WorksIndexUnavailable(
            "works_index_backup_failed",
            "无法建立最后良好索引备份；写入已暂停",
            current_digest=snapshot.digest,
        ) from exc
    if verified.digest != snapshot.digest:
        raise WorksIndexUnavailable(
            "works_index_backup_failed",
            "最后良好索引备份校验失败；写入已暂停",
            current_digest=snapshot.digest,
        )


def _probe_index_writable(workspace_dir: str | Path) -> None:
    parent = works_index_path(workspace_dir).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / f".index-write-probe-{uuid4().hex}"
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(probe, flags, 0o600)
        try:
            os.write(fd, b"writable")
            os.fsync(fd)
        finally:
            os.close(fd)
        probe.unlink()
    except PermissionError as exc:
        raise WorksIndexUnavailable(
            "works_index_permission_denied",
            "没有权限更新作品索引；已有作品文件未被删除",
        ) from exc
    except OSError as exc:
        raise WorksIndexUnavailable(
            "works_index_io_error",
            "作品索引存储暂不可写；已有作品文件未被删除",
        ) from exc


def _prepare_index_write(workspace_dir: str | Path) -> _WorksIndexSnapshot:
    snapshot = _read_index_snapshot(workspace_dir)
    _probe_index_writable(workspace_dir)
    _ensure_last_good_backup(workspace_dir, snapshot)
    return snapshot


def ensure_works_index_writable(workspace_dir: str | Path) -> None:
    """Fail before side effects if Works cannot be registered."""

    with exclusive_path_lock(works_index_path(workspace_dir)):
        _prepare_index_write(workspace_dir)


def _serialize_index(
    snapshot: _WorksIndexSnapshot,
    works: list[WorkItem],
) -> bytes:
    payload = dict(snapshot.payload)
    raw_by_id = {
        raw.get("id"): raw
        for raw in snapshot.payload.get("works", [])
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    serialized: list[dict[str, object]] = []
    for item in works:
        raw = dict(raw_by_id.get(item.id, {}))
        raw.update(
            item.model_dump(
                mode="json",
                exclude={"availability_status", "integrity_status"},
            ),
        )
        serialized.append(raw)
    payload["schema_version"] = WORKS_SCHEMA_VERSION
    payload["works"] = serialized
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _prune_index_backups(workspace_dir: str | Path) -> None:
    root = _works_backup_dir(workspace_dir)
    try:
        candidates = sorted(
            (
                path
                for path in root.glob("index-*.json")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return
    for stale in candidates[WORKS_INDEX_BACKUP_LIMIT:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _write_index(
    workspace_dir: str | Path,
    works: list[WorkItem],
    *,
    baseline: _WorksIndexSnapshot | None = None,
) -> None:
    path = works_index_path(workspace_dir)
    if baseline is None:
        baseline = _prepare_index_write(workspace_dir)
    try:
        current = _parse_index_bytes(_read_bytes_no_follow(path))
    except FileNotFoundError:
        current = _WorksIndexSnapshot(
            works=(),
            payload={"schema_version": WORKS_SCHEMA_VERSION, "works": []},
            raw_bytes=None,
            digest=None,
            schema_version=None,
            uninitialized=True,
        )
    except PermissionError as exc:
        raise WorksIndexUnavailable(
            "works_index_permission_denied",
            "没有权限读取作品索引；写入已取消",
        ) from exc
    except OSError as exc:
        raise WorksIndexUnavailable(
            "works_index_io_error",
            "无法读取作品索引；写入已取消",
        ) from exc
    if (
        current.digest != baseline.digest
        or current.uninitialized != baseline.uninitialized
    ):
        raise WorksIndexUnavailable(
            "works_index_changed",
            "作品索引在写入前发生变化；本次写入已取消",
            current_digest=current.digest,
        )
    _probe_index_writable(workspace_dir)
    _ensure_last_good_backup(workspace_dir, baseline)
    raw_bytes = _serialize_index(baseline, works)
    try:
        _parse_index_bytes(raw_bytes)
        _write_bytes_atomic_no_follow(path, raw_bytes)
        committed = _read_index_snapshot(workspace_dir)
    except (OSError, ValueError) as exc:
        raise WorksIndexUnavailable(
            "works_index_write_failed",
            "作品索引写入失败；已有作品文件未被删除",
            current_digest=baseline.digest,
        ) from exc
    if committed.digest != _sha256_bytes(raw_bytes):
        raise WorksIndexUnavailable(
            "works_index_write_verification_failed",
            "作品索引写入后校验失败；请先诊断并恢复",
            current_digest=committed.digest,
        )
    _prune_index_backups(workspace_dir)


def _list_index_backups(workspace_dir: str | Path) -> list[WorksIndexBackup]:
    root = _works_backup_dir(workspace_dir)
    try:
        paths = sorted(
            (
                path
                for path in root.glob("index-*.json")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError as exc:
        raise WorksIndexUnavailable(
            "works_index_backup_io_error",
            "无法检查作品索引备份；恢复状态暂不可确认",
        ) from exc
    backups: list[WorksIndexBackup] = []
    for path in paths:
        try:
            snapshot = _parse_index_bytes(_read_bytes_no_follow(path))
            expected = path.stem.removeprefix("index-")
            if snapshot.digest != f"sha256:{expected}":
                continue
            backups.append(
                WorksIndexBackup(
                    digest=snapshot.digest,
                    schema_version=snapshot.schema_version
                    or WORKS_SCHEMA_VERSION,
                    work_count=len(snapshot.works),
                    created_at=datetime.fromtimestamp(
                        path.stat().st_mtime,
                        timezone.utc,
                    ),
                ),
            )
        except (OSError, WorksIndexUnavailable):
            continue
    return backups


def inspect_works_index(workspace_dir: str | Path) -> WorksIndexInspection:
    try:
        managed_ids = _managed_work_ids(workspace_dir)
    except WorksIndexUnavailable as exc:
        return WorksIndexInspection(
            status="unavailable",
            code=exc.code,
            diagnostic_id=exc.diagnostic_id,
            current_digest=exc.current_digest,
            invalid_record_indexes=list(exc.invalid_record_indexes),
        )
    try:
        backups = _list_index_backups(workspace_dir)
    except WorksIndexUnavailable as exc:
        return WorksIndexInspection(
            status="unavailable",
            code=exc.code,
            diagnostic_id=exc.diagnostic_id,
            current_digest=exc.current_digest,
            invalid_record_indexes=list(exc.invalid_record_indexes),
            managed_work_ids=managed_ids,
            orphaned_managed_ids=managed_ids,
        )
    try:
        snapshot = _read_index_snapshot(workspace_dir)
    except WorksIndexUnavailable as exc:
        recoverable = exc.code not in {
            "works_index_permission_denied",
            "works_index_io_error",
            "works_index_assets_io_error",
        }
        return WorksIndexInspection(
            status="recovery_required" if recoverable else "unavailable",
            code=exc.code,
            diagnostic_id=exc.diagnostic_id,
            current_digest=exc.current_digest,
            invalid_record_indexes=list(exc.invalid_record_indexes),
            managed_work_ids=managed_ids,
            orphaned_managed_ids=managed_ids,
            backups=backups,
        )
    indexed_ids = {item.id for item in snapshot.works}
    return WorksIndexInspection(
        status="uninitialized" if snapshot.uninitialized else "ready",
        current_digest=snapshot.digest,
        schema_version=snapshot.schema_version,
        work_count=len(snapshot.works),
        managed_work_ids=managed_ids,
        orphaned_managed_ids=sorted(set(managed_ids) - indexed_ids),
        backups=backups,
    )


def _read_backup_snapshot(
    workspace_dir: str | Path,
    snapshot_digest: str,
) -> _WorksIndexSnapshot:
    try:
        path = _backup_path(workspace_dir, snapshot_digest)
        raw_bytes = _read_bytes_no_follow(path)
        snapshot = _parse_index_bytes(raw_bytes)
    except FileNotFoundError as exc:
        raise WorksIndexUnavailable(
            "works_index_backup_not_found",
            "指定的作品索引备份不存在",
        ) from exc
    except (OSError, ValueError) as exc:
        raise WorksIndexUnavailable(
            "works_index_backup_unavailable",
            "指定的作品索引备份无法读取",
        ) from exc
    if snapshot.digest != snapshot_digest:
        raise WorksIndexUnavailable(
            "works_index_backup_digest_mismatch",
            "作品索引备份完整性校验失败",
            current_digest=snapshot.digest,
        )
    return snapshot


def _current_raw_and_digest(
    workspace_dir: str | Path,
) -> tuple[bytes | None, str]:
    path = works_index_path(workspace_dir)
    try:
        raw_bytes = _read_bytes_no_follow(path)
    except FileNotFoundError:
        return None, "missing"
    except PermissionError as exc:
        raise WorksIndexUnavailable(
            "works_index_permission_denied",
            "没有权限读取当前作品索引",
        ) from exc
    except OSError as exc:
        raise WorksIndexUnavailable(
            "works_index_io_error",
            "无法读取当前作品索引",
        ) from exc
    return raw_bytes, _sha256_bytes(raw_bytes)


def preview_works_index_recovery(
    workspace_dir: str | Path,
    *,
    snapshot_digest: str,
) -> WorksIndexRecoveryPreview:
    snapshot = _read_backup_snapshot(workspace_dir, snapshot_digest)
    _, current_digest = _current_raw_and_digest(workspace_dir)
    snapshot_ids = {item.id for item in snapshot.works}
    return WorksIndexRecoveryPreview(
        current_digest=current_digest,
        snapshot_digest=snapshot_digest,
        snapshot_schema_version=snapshot.schema_version
        or WORKS_SCHEMA_VERSION,
        snapshot_works=list(snapshot.works),
        orphaned_managed_ids=sorted(
            set(_managed_work_ids(workspace_dir)) - snapshot_ids,
        ),
        confirmation_required=current_digest != snapshot_digest,
    )


def _quarantine_current_index(
    workspace_dir: str | Path,
    raw_bytes: bytes | None,
    digest: str,
) -> str | None:
    if raw_bytes is None:
        return None
    digest_hex = digest.removeprefix("sha256:")
    root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "index-quarantine",
    )
    path = root / f"index-{digest_hex}.json"
    if path.exists():
        if _read_bytes_no_follow(path) != raw_bytes:
            raise WorksIndexUnavailable(
                "works_index_quarantine_conflict",
                "损坏索引保留副本冲突，恢复已取消",
                current_digest=digest,
            )
        return digest
    try:
        _write_bytes_atomic_no_follow(path, raw_bytes)
    except (OSError, ValueError) as exc:
        raise WorksIndexUnavailable(
            "works_index_quarantine_failed",
            "无法保留当前索引原件，恢复已取消",
            current_digest=digest,
        ) from exc
    return digest


def restore_works_index(
    workspace_dir: str | Path,
    *,
    expected_current_digest: str,
    snapshot_digest: str,
) -> WorksIndexRestoreResult:
    """Atomically restore a validated snapshot with CAS semantics."""

    with exclusive_path_lock(works_index_path(workspace_dir)):
        snapshot = _read_backup_snapshot(workspace_dir, snapshot_digest)
        current_raw, current_digest = _current_raw_and_digest(workspace_dir)
        if current_digest == snapshot_digest:
            return WorksIndexRestoreResult(
                status="already_restored",
                restored_digest=snapshot_digest,
                work_count=len(snapshot.works),
            )
        if current_digest != expected_current_digest:
            raise WorksIndexUnavailable(
                "works_index_changed",
                "作品索引在确认后发生变化，恢复已取消",
                current_digest=current_digest,
            )
        _probe_index_writable(workspace_dir)
        quarantine_digest = _quarantine_current_index(
            workspace_dir,
            current_raw,
            current_digest,
        )
        try:
            assert snapshot.raw_bytes is not None
            _write_bytes_atomic_no_follow(
                works_index_path(workspace_dir),
                snapshot.raw_bytes,
            )
            restored = _read_index_snapshot(workspace_dir)
        except (AssertionError, OSError, ValueError) as exc:
            raise WorksIndexUnavailable(
                "works_index_restore_failed",
                "作品索引恢复失败；原索引保留副本仍在",
                current_digest=current_digest,
            ) from exc
        if restored.digest != snapshot_digest:
            raise WorksIndexUnavailable(
                "works_index_restore_verification_failed",
                "作品索引恢复后校验失败",
                current_digest=restored.digest,
            )
        return WorksIndexRestoreResult(
            status="restored",
            restored_digest=snapshot_digest,
            quarantine_digest=quarantine_digest,
            work_count=len(restored.works),
        )


def list_works(
    workspace_dir: str | Path,
    *,
    source_chat_id: str | None = None,
) -> list[WorkItem]:
    # A Work revision is immutable history. Filesystem drift changes only the
    # projected availability/integrity state; it must not rewrite that
    # revision's size, updated_at, Artifact record, or ordering.
    works = _load_index(workspace_dir)
    if source_chat_id is not None:
        # Scope by index metadata before any file hashing. Exact byte checks
        # still run for every returned Work, never for unrelated chats.
        works = [
            item for item in works if item.source_chat_id == source_chat_id
        ]
    return sorted(
        [_project_work_state(workspace_dir, item) for item in works],
        key=lambda item: item.updated_at,
        reverse=True,
    )


def _project_work_state(workspace_dir: str | Path, item: WorkItem) -> WorkItem:
    """Inspect a saved copy without rewriting its immutable history."""

    projected = item.model_copy(deep=True)
    managed_root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "files",
        item.id,
    ).resolve(strict=False)
    candidate = Path(item.path).expanduser().resolve(strict=False)
    if candidate.parent != managed_root:
        projected.availability_status = "unknown"
        projected.integrity_status = "unknown"
        return projected
    try:
        if not candidate.is_file():
            projected.availability_status = "missing"
            projected.integrity_status = "unknown"
            return projected
        projected.availability_status = "available"
        observed_digest = _source_digest(candidate)
    except PermissionError:
        projected.availability_status = "permission_denied"
        projected.integrity_status = "unknown"
        return projected
    except OSError:
        projected.availability_status = "unknown"
        projected.integrity_status = "unknown"
        return projected
    projected.integrity_status = (
        "intact"
        if item.source_digest and observed_digest == item.source_digest
        else "modified"
        if item.source_digest
        else "unknown"
    )
    return projected


def get_work(workspace_dir: str | Path, work_id: str) -> WorkItem | None:
    return next(
        (item for item in _load_index(workspace_dir) if item.id == work_id),
        None,
    )


def get_work_state(workspace_dir: str | Path, work_id: str) -> WorkItem | None:
    """Return the current read-only availability/integrity projection."""

    item = get_work(workspace_dir, work_id)
    if item is None:
        return None
    return _project_work_state(workspace_dir, item)


def get_managed_work_file(
    workspace_dir: str | Path,
    work_id: str,
) -> tuple[WorkItem, Path] | None:
    projected = get_work_state(workspace_dir, work_id)
    if projected is None:
        return None
    if (
        projected.availability_status != "available"
        or projected.integrity_status != "intact"
    ):
        return None
    managed_root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "files",
        work_id,
    ).resolve(strict=False)
    candidate = Path(projected.path).expanduser().resolve(strict=False)
    if candidate.parent != managed_root or not candidate.is_file():
        return None
    return projected, candidate


def build_work_edit_context(
    item: WorkItem,
    entry_path: str | Path,
    *,
    source_chat_name: str | None = None,
) -> str:
    """Describe one managed work without depending on its old conversation."""

    entry = Path(entry_path).expanduser().resolve(strict=False)
    source_lines = []
    if source_chat_name:
        source_lines.append(f"- 来源会话：{source_chat_name}")
    if item.source_chat_id:
        source_lines.append(f"- 来源会话 ID：{item.source_chat_id}")
    if item.source_run_id:
        source_lines.append(f"- 来源运行 ID：{item.source_run_id}")
    source_block = "\n".join(source_lines) or "- 来源：用户保存的作品"
    return (
        f"作品：{item.title}\n"
        f"类型：{item.kind}\n"
        f"主文件：{entry}\n"
        f"工作目录：{entry.parent}\n"
        f"{source_block}\n"
        f"来源版本：V{item.version}（作品 ID：{item.id}）\n"
        "当前状态：基于收藏版本创建的独立可编辑副本已挂载为本会话工作区。"
        "HTML 作品的同级脚本、样式和资源也都在该目录内。\n"
        "收藏的来源版本保持不变；用户确认的修改只写入当前工作副本。"
        "完成后把新成果作为新版本加入「作品」，不要覆盖来源版本，也不要回头"
        "修改原对话中的临时产物。"
    )


def create_work_edit_copy(
    workspace_dir: str | Path,
    item: WorkItem,
    entry_path: str | Path,
) -> tuple[Path, str]:
    """Create an editable branch while preserving the saved version."""

    entry = Path(entry_path).expanduser().resolve(strict=True)
    managed_root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "files",
        item.id,
    ).resolve(strict=False)
    if entry.parent != managed_root or not entry.is_file():
        raise ValueError("work entry is outside its managed version")
    if not item.source_digest or _source_digest(entry) != item.source_digest:
        raise ValueError("saved work version was modified")

    edit_id = str(uuid4())
    edit_root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "edits",
        edit_id,
    )
    edit_root.mkdir(parents=True, exist_ok=False)
    try:
        if item.kind == "html":
            for candidate in managed_root.rglob("*"):
                relative = candidate.relative_to(managed_root)
                if candidate.is_symlink():
                    raise ValueError("managed work contains a symbolic link")
                if not candidate.is_file() or _is_work_metadata_file(
                    candidate,
                ):
                    continue
                destination = edit_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, destination, follow_symlinks=False)
        else:
            shutil.copy2(entry, edit_root / entry.name, follow_symlinks=False)

        edit_entry = edit_root / entry.relative_to(managed_root)
        if not edit_entry.is_file():
            raise FileNotFoundError("work edit entry was not copied")
        base_digest = item.source_digest
        lineage = {
            "schema_version": 1,
            "edit_id": edit_id,
            "parent_work_id": item.id,
            "lineage_root_id": item.lineage_root_id or item.id,
            "parent_version": item.version,
            "parent_digest": base_digest,
            "entry_name": edit_entry.name,
            "created_at": utc_now().isoformat(),
        }
        write_text_atomic_no_follow(
            edit_root / WORK_LINEAGE_FILE,
            json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True),
        )
        return edit_entry, base_digest
    except Exception:
        shutil.rmtree(edit_root, ignore_errors=True)
        raise


def _verified_page_count(path: Path, kind: WorkKind) -> int:
    """Read a page count without executing or converting the managed Work."""

    if kind == "pdf":
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(path))
        try:
            count = len(document)
        finally:
            document.close()
    elif kind == "presentation" and path.suffix.lower() == ".pptx":
        try:
            with ZipFile(path) as archive:
                root = ET.fromstring(archive.read("ppt/presentation.xml"))
        except (BadZipFile, KeyError, ET.ParseError) as exc:
            raise ValueError(
                "artifact edit presentation structure is invalid",
            ) from exc
        namespaces = {
            "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        }
        count = len(root.findall(".//p:sldId", namespaces))
    else:
        raise ValueError(
            "artifact edit page selection cannot be verified for this format",
        )
    if count < 1:
        raise ValueError("artifact edit source has no pages")
    return count


# Every permission, lineage, revision, and byte identity gate stays visible in
# this single fail-closed resolver so callers cannot accidentally omit one.
# pylint: disable-next=too-many-branches,too-many-statements
def resolve_canonical_artifact_edit_target(
    workspace_dir: str | Path,
    raw_target: object,
    *,
    chat_id: str,
) -> tuple[dict[str, object], str]:
    """Validate a one-turn edit target against durable Work lineage.

    Client paths and revisions are only hints. The managed Work, edit sidecar,
    current chat ownership, and latest lineage version are re-checked here
    before any of the target reaches the model request context.
    """

    if not isinstance(raw_target, Mapping):
        raise ValueError("artifact edit target must be an object")
    if raw_target.get("schema_version") != 1:
        raise ValueError("unsupported artifact edit target schema")
    target_chat_id = str(raw_target.get("chat_id") or "").strip()
    work_id = str(raw_target.get("work_id") or "").strip()
    asset_id = str(raw_target.get("asset_id") or "").strip()
    raw_edit_path = str(raw_target.get("edit_path") or "").strip()
    if not target_chat_id or target_chat_id != chat_id:
        raise PermissionError("artifact edit target belongs to another chat")
    if not work_id or not asset_id or not raw_edit_path:
        raise ValueError("artifact edit target is incomplete")

    revision = raw_target.get("base_revision")
    if not isinstance(revision, Mapping):
        raise ValueError("artifact edit target has no base revision")
    raw_version = revision.get("version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ValueError("artifact edit base version is invalid")
    base_digest = str(revision.get("digest") or "").strip()
    if not base_digest:
        raise ValueError("artifact edit base digest is required")

    item = get_work_state(workspace_dir, work_id)
    if item is None:
        raise FileNotFoundError("artifact edit Work was not found")
    if item.source_chat_id != chat_id:
        raise PermissionError("artifact edit Work belongs to another chat")
    if item.artifact_id != asset_id:
        raise ValueError("artifact edit asset does not match the Work")
    if item.version != raw_version or item.source_digest != base_digest:
        raise ValueError("artifact edit base revision no longer matches")
    if (
        item.availability_status != "available"
        or item.integrity_status != "intact"
    ):
        raise ValueError("artifact edit base Work is not intact")

    lineage_root_id = item.lineage_root_id or item.id
    latest_version = max(
        (
            candidate.version
            for candidate in _load_index(workspace_dir)
            if (candidate.lineage_root_id or candidate.id) == lineage_root_id
        ),
        default=item.version,
    )
    if latest_version != item.version:
        raise ValueError("a newer Work version already exists")

    edits_root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "edits",
    ).resolve(strict=False)
    raw_entry = Path(raw_edit_path).expanduser()
    if raw_entry.is_symlink():
        raise PermissionError("artifact edit entry cannot be a symbolic link")
    try:
        edit_entry = raw_entry.resolve(strict=True)
        relative = edit_entry.relative_to(edits_root)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise PermissionError(
            "artifact edit entry is outside managed edits",
        ) from exc
    if len(relative.parts) != 2 or not edit_entry.is_file():
        raise PermissionError(
            "artifact edit entry is outside its managed branch",
        )
    edit_root = edits_root / relative.parts[0]
    lineage_path = edit_root / WORK_LINEAGE_FILE
    try:
        lineage = json.loads(read_text_no_follow(lineage_path))
    except Exception as exc:
        raise ValueError(
            "artifact edit branch lineage is unavailable",
        ) from exc
    if not isinstance(lineage, dict) or lineage.get("schema_version") != 1:
        raise ValueError("artifact edit branch lineage is invalid")
    expected_lineage = {
        "parent_work_id": item.id,
        "lineage_root_id": lineage_root_id,
        "parent_version": item.version,
        "parent_digest": item.source_digest,
        "entry_name": edit_entry.name,
    }
    for key, expected in expected_lineage.items():
        if lineage.get(key) != expected:
            raise ValueError(f"artifact edit branch {key} does not match")
    if _source_digest(edit_entry) != item.source_digest:
        raise ValueError("artifact edit branch changed before confirmation")

    selection: dict[str, object] | None = None
    raw_selection = raw_target.get("selection")
    if raw_selection is not None:
        if not isinstance(raw_selection, Mapping):
            raise ValueError("artifact edit selection must be an object")
        kind = str(raw_selection.get("kind") or "").strip()
        if kind in {"page", "paragraph"}:
            value = raw_selection.get(kind)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"artifact edit {kind} must be an integer")
            if value < 1 or value > 10_000:
                raise ValueError(f"artifact edit {kind} is out of range")
            if kind == "page":
                page_count = _verified_page_count(edit_entry, item.kind)
                if value > page_count:
                    raise ValueError(
                        "artifact edit page is out of range for "
                        f"{page_count} pages",
                    )
            selection = {"kind": kind, kind: value}
        elif kind == "region":
            region = str(raw_selection.get("region") or "").strip()
            if not region or len(region) > 200:
                raise ValueError("artifact edit region is invalid")
            selection = {"kind": "region", "region": region}
        else:
            raise ValueError("artifact edit selection kind is unsupported")

    normalized: dict[str, object] = {
        "schema_version": 1,
        "chat_id": chat_id,
        "asset_id": item.artifact_id,
        "work_id": item.id,
        "edit_path": str(edit_entry),
        "base_revision": {
            "version": item.version,
            "digest": item.source_digest,
        },
    }
    selection_label = "整个成果"
    if selection is not None:
        normalized["selection"] = selection
        if selection["kind"] == "page":
            selection_label = f"第 {selection['page']} 页"
        elif selection["kind"] == "paragraph":
            selection_label = f"第 {selection['paragraph']} 段"
        else:
            selection_label = str(selection["region"])
    context = (
        "【当前会话成果修改目标】\n"
        f"作品：{item.title}\n"
        f"类型：{item.kind}\n"
        f"可编辑副本：{edit_entry}\n"
        f"工作目录：{edit_root}\n"
        f"来源版本：V{item.version}（作品 ID：{item.id}）\n"
        f"本轮选择：{selection_label}\n"
        "这是当前会话里由用户确认提交的一次精确修改目标。只修改该可编辑副本；"
        "不要覆盖收藏的来源版本。完成并验证后，将它按既有 Work lineage 保存为新版本。"
    )
    return normalized, context


def get_work_cover_file(
    workspace_dir: str | Path,
    work_id: str,
) -> tuple[WorkItem, Path] | None:
    item = get_work(workspace_dir, work_id)
    if item is None or not item.cover_path:
        return None
    managed_root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "files",
        work_id,
    ).resolve(strict=False)
    candidate = Path(item.cover_path).expanduser().resolve(strict=False)
    if candidate.parent != managed_root or not candidate.is_file():
        return None
    return item, candidate


def set_work_cover(
    workspace_dir: str | Path,
    work_id: str,
    *,
    source_path: str | Path,
    mime_type: str,
) -> WorkItem:
    """Persist one verified image as the managed visual cover for a work."""

    with exclusive_path_lock(works_index_path(workspace_dir)):
        return _set_work_cover_unlocked(
            workspace_dir,
            work_id,
            source_path=source_path,
            mime_type=mime_type,
        )


def _set_work_cover_unlocked(
    workspace_dir: str | Path,
    work_id: str,
    *,
    source_path: str | Path,
    mime_type: str,
) -> WorkItem:
    baseline = _prepare_index_write(workspace_dir)
    works = list(baseline.works)
    item = next((entry for entry in works if entry.id == work_id), None)
    if item is None:
        raise FileNotFoundError("work not found")
    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file() or not mime_type.startswith("image/"):
        raise ValueError("work cover must be an image file")
    suffix = (
        mimetypes.guess_extension(mime_type) or source.suffix.lower() or ".png"
    )
    if suffix == ".jpe":
        suffix = ".jpg"
    cover_digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    destination = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "files",
        work_id,
        f"cover-{cover_digest}{suffix}",
    )
    stale_paths: tuple[Path, ...] = ()
    if item.cover_path:
        raw_previous_cover = Path(item.cover_path).expanduser()
        previous_cover = raw_previous_cover.resolve(strict=False)
        primary_work = Path(item.path).expanduser().resolve(strict=False)
        if (
            not raw_previous_cover.is_symlink()
            and previous_cover.parent == destination.parent
            and previous_cover != destination
            and previous_cover != primary_work
            and previous_cover.is_file()
        ):
            stale_paths = (previous_cover,)
    if source != destination:
        shutil.copy2(source, destination, follow_symlinks=False)
    item.cover_path = str(destination)
    item.cover_mime_type = mime_type
    item.cover_source = "ai"
    item.updated_at = utc_now()
    try:
        _write_index(workspace_dir, works, baseline=baseline)
    except WorksIndexUnavailable as exc:
        raise WorksIndexUnavailable(
            exc.code,
            "封面候选已生成并保留，但作品索引登记失败；不会重复生成封面",
            current_digest=exc.current_digest,
            invalid_record_indexes=exc.invalid_record_indexes,
            candidate_path=str(destination),
        ) from exc
    for stale in stale_paths:
        try:
            stale.unlink()
        except OSError:
            pass
    return item


def _contained_source(
    source_path: str | Path,
    allowed_roots: Iterable[str | Path],
) -> Path:
    raw = Path(source_path).expanduser()
    if raw.is_symlink():
        raise ValueError("symbolic links cannot be added as works")
    source = raw.resolve(strict=True)
    if not source.is_file():
        raise ValueError("work source is not a file")
    for root_path in allowed_roots:
        root = Path(root_path).expanduser().resolve(strict=False)
        if source == root or root in source.parents:
            return source
    raise PermissionError("work source is outside the active workspace")


def _copy_html_bundle(source: Path, destination: Path) -> None:
    """Preserve small sibling asset trees used by self-contained web works."""

    files: list[tuple[Path, Path]] = []
    total_bytes = 0
    for candidate in source.parent.rglob("*"):
        relative = candidate.relative_to(source.parent)
        if any(part in _HTML_BUNDLE_IGNORED_DIRS for part in relative.parts):
            continue
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or _is_work_metadata_file(candidate)
        ):
            continue
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        total_bytes += size
        files.append((candidate, relative))
        if (
            len(files) > _HTML_BUNDLE_MAX_FILES
            or total_bytes > _HTML_BUNDLE_MAX_BYTES
        ):
            shutil.copy2(source, destination, follow_symlinks=False)
            return

    for candidate, relative in files:
        target = (
            destination
            if candidate == source
            else destination.parent / relative
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, target, follow_symlinks=False)


def _is_work_metadata_file(path: Path) -> bool:
    return (
        path.name == WORK_LINEAGE_FILE
        or path.name.startswith("cover.")
        or _MANAGED_COVER_NAME.fullmatch(path.name) is not None
    )


def _source_digest(source: Path) -> str:
    """Return a stable digest for one file or its small HTML bundle."""

    digest = hashlib.sha256()
    candidates: list[tuple[Path, Path]] = [(source, Path(source.name))]
    if infer_work_kind(source) == "html":
        candidates = []
        for candidate in source.parent.rglob("*"):
            relative = candidate.relative_to(source.parent)
            if any(
                part in _HTML_BUNDLE_IGNORED_DIRS for part in relative.parts
            ):
                continue
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or _is_work_metadata_file(candidate)
            ):
                continue
            candidates.append((candidate, relative))
        candidates.sort(key=lambda item: item[1].as_posix())
    for candidate, relative in candidates:
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with candidate.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _work_candidate_ids(
    *,
    source: Path,
    source_digest: str,
    source_chat_id: str | None,
    source_run_id: str | None,
    parent: WorkItem | None,
    parent_digest: str | None,
    lineage_root_id: str | None,
    version: int,
    derivation: WorkDerivation | None,
) -> tuple[str, str]:
    """Return stable IDs for an interrupted copy/register transaction."""

    intent = json.dumps(
        {
            "schema_version": 1,
            "source_path": str(source),
            "source_digest": source_digest,
            "source_chat_id": source_chat_id,
            "source_run_id": source_run_id,
            "parent_work_id": parent.id if parent is not None else None,
            "parent_digest": parent_digest,
            "lineage_root_id": lineage_root_id,
            "version": version,
            "derivation": (
                derivation.model_dump(mode="json")
                if derivation is not None
                else None
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    work_id = str(uuid5(_WORK_CANDIDATE_NAMESPACE, intent))
    artifact_id = str(
        uuid5(_WORK_ARTIFACT_NAMESPACE, f"{work_id}\0{source_digest}"),
    )
    return work_id, artifact_id


def _prepare_artifact_receipt_write(workspace_dir: str | Path) -> None:
    try:
        prepare_artifact_manifest_write(workspace_dir)
    except ArtifactManifestUnavailable as exc:
        raise WorksIndexUnavailable(exc.code, str(exc)) from exc


def _load_source_lineage(source: Path) -> dict[str, object] | None:
    lineage_path = source.parent / WORK_LINEAGE_FILE
    if not lineage_path.is_file() or lineage_path.is_symlink():
        return None
    try:
        payload = json.loads(read_text_no_follow(lineage_path))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    return payload


# Resume requires an exact conjunction of durable receipt and lineage fields.
def _resume_registered_candidate(
    workspace_dir: str | Path,
    *,
    source: Path,
    source_digest: str,
    source_chat_id: str | None,
    source_run_id: str | None,
    parent: WorkItem | None,
    parent_digest: str | None,
    lineage_root_id: str | None,
    version: int,
    derivation: WorkDerivation | None,
) -> WorkItem | None:
    """Recover one exact, previously registered candidate after index failure.

    The existing Artifact manifest is the receipt for the already completed
    copy.  Disk contents alone are never used to infer ownership.  Every
    immutable identity field and the exact candidate bytes must match before
    the caller may resume the pending index commit.
    """

    manifest = load_artifact_manifest(workspace_dir)
    expected_parent_id = parent.id if parent is not None else None
    expected_lineage_root_id = lineage_root_id or (
        parent.lineage_root_id or parent.id if parent is not None else None
    )
    expected_derivation = (
        derivation.model_dump(mode="json") if derivation is not None else None
    )
    matches: list[WorkItem] = []
    for raw_artifact in manifest.get("artifacts", []):
        if not isinstance(raw_artifact, dict):
            continue
        metadata = raw_artifact.get("metadata")
        if not isinstance(metadata, dict):
            continue
        work_id = str(metadata.get("work_id") or "").strip()
        artifact_id = str(raw_artifact.get("id") or "").strip()
        raw_candidate_path = str(raw_artifact.get("path") or "").strip()
        candidate_lineage_root_id = expected_lineage_root_id or work_id
        identity_matches = all(
            (
                raw_artifact.get("producer") == "works",
                bool(work_id),
                bool(artifact_id),
                bool(raw_candidate_path),
                metadata.get("source_path") == str(source),
                metadata.get("source_chat_id") == source_chat_id,
                raw_artifact.get("run_id") == source_run_id,
                metadata.get("source_digest") == source_digest,
                metadata.get("parent_work_id") == expected_parent_id,
                metadata.get("parent_digest") == parent_digest,
                metadata.get("lineage_root_id") == candidate_lineage_root_id,
                metadata.get("version") == version,
                metadata.get("derivation") == expected_derivation,
            ),
        )
        if not identity_matches:
            continue
        candidate = Path(raw_candidate_path).expanduser()
        expected_root = contained_storage_path(
            Path(workspace_dir).expanduser(),
            ".lantai",
            "works",
            "files",
            work_id,
        ).resolve(strict=False)
        try:
            if candidate.is_symlink():
                continue
            resolved_candidate = candidate.resolve(strict=True)
            if (
                resolved_candidate.parent != expected_root
                or not resolved_candidate.is_file()
                or _source_digest(resolved_candidate) != source_digest
            ):
                continue
        except (OSError, ValueError):
            continue
        try:
            matches.append(
                WorkItem(
                    id=work_id,
                    artifact_id=artifact_id,
                    title=str(raw_artifact.get("title") or source.stem),
                    kind=infer_work_kind(resolved_candidate),
                    path=str(resolved_candidate),
                    source_path=str(source),
                    mime_type=(
                        str(raw_artifact["mime_type"])
                        if raw_artifact.get("mime_type") is not None
                        else None
                    ),
                    size=(
                        raw_artifact.get("size")
                        if isinstance(raw_artifact.get("size"), int)
                        and not isinstance(raw_artifact.get("size"), bool)
                        else None
                    ),
                    source_chat_id=source_chat_id,
                    source_run_id=source_run_id,
                    source_digest=source_digest,
                    parent_work_id=expected_parent_id,
                    parent_digest=parent_digest,
                    lineage_root_id=candidate_lineage_root_id,
                    version=version,
                    derivation=derivation,
                    created_at=raw_artifact.get("created_at") or utc_now(),
                    updated_at=raw_artifact.get("created_at") or utc_now(),
                ),
            )
        except Exception:
            continue
    if len(matches) > 1:
        raise WorksIndexUnavailable(
            "works_candidate_ambiguous",
            "发现多个身份相同的待登记作品候选；不会自动认领或重复生成",
        )
    return matches[0] if matches else None


def add_work(
    workspace_dir: str | Path,
    *,
    source_path: str | Path,
    allowed_roots: Iterable[str | Path],
    title: str | None = None,
    source_chat_id: str | None = None,
    source_run_id: str | None = None,
    derivation: WorkDerivation | None = None,
) -> WorkItem:
    with exclusive_path_lock(works_index_path(workspace_dir)):
        return _add_work_unlocked(
            workspace_dir,
            source_path=source_path,
            allowed_roots=allowed_roots,
            title=title,
            source_chat_id=source_chat_id,
            source_run_id=source_run_id,
            derivation=derivation,
        )


# Copy, Artifact receipt, index commit, and rollback form one explicit saga.
# pylint: disable-next=too-many-branches,too-many-statements
def _add_work_unlocked(
    workspace_dir: str | Path,
    *,
    source_path: str | Path,
    allowed_roots: Iterable[str | Path],
    title: str | None = None,
    source_chat_id: str | None = None,
    source_run_id: str | None = None,
    derivation: WorkDerivation | None = None,
) -> WorkItem:
    source = _contained_source(source_path, allowed_roots)
    _prepare_artifact_receipt_write(workspace_dir)
    try:
        baseline = _prepare_index_write(workspace_dir)
    except WorksIndexUnavailable as exc:
        # A first-ever add can complete its durable copy and Artifact receipt
        # before the initial index publish fails.  That state is deliberately
        # indistinguishable from a lost historical index until the exact
        # receipt, bytes, and sole managed directory all agree.  Only that
        # narrow case may initialize the index; every other missing-index
        # state remains recovery-only.
        if exc.code != "works_index_missing_with_assets":
            raise
        source_digest = _source_digest(source)
        lineage = _load_source_lineage(source)
        if (
            derivation is not None
            or lineage is not None
            or _has_index_history(workspace_dir)
        ):
            raise
        resumed = _resume_registered_candidate(
            workspace_dir,
            source=source,
            source_digest=source_digest,
            source_chat_id=source_chat_id,
            source_run_id=source_run_id,
            parent=None,
            parent_digest=None,
            lineage_root_id=None,
            version=1,
            derivation=None,
        )
        baseline = _WorksIndexSnapshot(
            works=(),
            payload={"schema_version": WORKS_SCHEMA_VERSION, "works": []},
            raw_bytes=None,
            digest=None,
            schema_version=None,
            uninitialized=True,
        )
        _probe_index_writable(workspace_dir)
        if resumed is not None:
            if _managed_work_ids(workspace_dir) != [resumed.id]:
                raise
            try:
                _write_index(workspace_dir, [resumed], baseline=baseline)
            except WorksIndexUnavailable as commit_exc:
                raise WorksIndexUnavailable(
                    commit_exc.code,
                    "成果候选仍被保留，但作品索引登记失败；不会重复生成成果",
                    current_digest=commit_exc.current_digest,
                    invalid_record_indexes=commit_exc.invalid_record_indexes,
                    candidate_path=resumed.path,
                ) from commit_exc
            return _project_work_state(workspace_dir, resumed)

        # Registration can fail after the durable copy but before the Artifact
        # receipt exists.  In that narrower window, a deterministic intent id,
        # the sole managed directory, exact filename, and exact byte-tree hash
        # jointly identify the candidate; anything less remains recovery-only.
        pending_work_id, _ = _work_candidate_ids(
            source=source,
            source_digest=source_digest,
            source_chat_id=source_chat_id,
            source_run_id=source_run_id,
            parent=None,
            parent_digest=None,
            lineage_root_id=None,
            version=1,
            derivation=None,
        )
        pending_path = contained_storage_path(
            Path(workspace_dir).expanduser(),
            ".lantai",
            "works",
            "files",
            pending_work_id,
            source.name,
        )
        try:
            pending_matches = (
                _managed_work_ids(workspace_dir) == [pending_work_id]
                and not pending_path.parent.is_symlink()
                and not pending_path.is_symlink()
                and pending_path.is_file()
                and _source_digest(pending_path) == source_digest
            )
        except (OSError, ValueError):
            pending_matches = False
        if not pending_matches:
            raise
    source_digest = _source_digest(source)
    works = list(baseline.works)
    lineage = _load_source_lineage(source)
    parent: WorkItem | None = None
    parent_digest: str | None = None
    lineage_root_id: str | None = None
    version = 1
    effective_source_chat_id = source_chat_id
    if derivation is not None and lineage is not None:
        raise ValueError("a derived Work cannot also be a later revision")
    derivation_parent: WorkItem | None = None
    if derivation is not None:
        derivation_parent = next(
            (item for item in works if item.id == derivation.source_work_id),
            None,
        )
        if derivation_parent is None:
            raise ValueError("derived Work source no longer exists")
        if get_managed_work_file(workspace_dir, derivation_parent.id) is None:
            raise ValueError("derived Work source file is no longer intact")
        if (
            derivation_parent.artifact_id != derivation.source_artifact_id
            or derivation_parent.version != derivation.source_version
            or derivation_parent.source_digest != derivation.source_digest
        ):
            raise ValueError("derived Work source identity does not match")
        if source_digest != derivation.output_digest:
            raise ValueError("derived Work output digest does not match")
        if (
            source_chat_id is not None
            and source_chat_id != derivation_parent.source_chat_id
        ):
            raise ValueError(
                "derived Work canonical chat does not match its source",
            )
        effective_source_chat_id = derivation_parent.source_chat_id
        if (
            source_run_id is not None
            and source_run_id != derivation_parent.source_run_id
        ):
            raise ValueError("derived Work run does not match its source")
        source_run_id = derivation_parent.source_run_id
    if lineage is not None:
        parent_id = str(lineage.get("parent_work_id") or "").strip()
        parent = next((item for item in works if item.id == parent_id), None)
        if parent is None:
            raise ValueError("work version source no longer exists")
        if (
            source_chat_id is not None
            and parent.source_chat_id is not None
            and source_chat_id != parent.source_chat_id
        ):
            raise ValueError(
                "work version canonical chat does not match its parent",
            )
        # A version remains owned by the canonical chat that owns its parent.
        # Legacy works created before canonical ownership may acquire an owner
        # on their first versioned edit, but an owned lineage must never move to
        # another chat or become detached.
        effective_source_chat_id = parent.source_chat_id or source_chat_id
        managed_parent = get_managed_work_file(workspace_dir, parent.id)
        if managed_parent is None:
            raise ValueError("work version source file no longer exists")
        parent_digest = _source_digest(managed_parent[1])
        expected_parent_digest = str(
            lineage.get("parent_digest") or "",
        ).strip()
        if (
            not expected_parent_digest
            or parent_digest != expected_parent_digest
        ):
            raise ValueError(
                "work version source changed after the edit branch "
                "was created",
            )
        if source_digest == parent_digest:
            return _project_work_state(workspace_dir, parent)
        lineage_root_id = parent.lineage_root_id or parent.id
        latest_version = max(
            (
                item.version
                for item in works
                if (item.lineage_root_id or item.id) == lineage_root_id
            ),
            default=parent.version,
        )
        if latest_version > parent.version:
            raise ValueError(
                "newer work version already exists; "
                "create a fresh edit branch",
            )
        version = latest_version + 1
    existing = next(
        (
            item
            for item in works
            if (
                Path(item.source_path) == source
                and item.source_chat_id == effective_source_chat_id
                and item.derivation == derivation
                and item.source_digest == source_digest
            )
            or (
                bool(source_run_id)
                and item.source_run_id == source_run_id
                and item.source_chat_id == effective_source_chat_id
                and item.source_digest == source_digest
                and item.derivation == derivation
            )
        ),
        None,
    )
    if existing is not None and Path(existing.path).is_file():
        return _project_work_state(workspace_dir, existing)

    resumed = _resume_registered_candidate(
        workspace_dir,
        source=source,
        source_digest=source_digest,
        source_chat_id=effective_source_chat_id,
        source_run_id=source_run_id,
        parent=parent,
        parent_digest=parent_digest,
        lineage_root_id=lineage_root_id,
        version=version,
        derivation=derivation,
    )
    if resumed is not None:
        try:
            _write_index(workspace_dir, [*works, resumed], baseline=baseline)
        except WorksIndexUnavailable as exc:
            raise WorksIndexUnavailable(
                exc.code,
                "成果候选仍被保留，但作品索引登记失败；不会重复生成成果",
                current_digest=exc.current_digest,
                invalid_record_indexes=exc.invalid_record_indexes,
                candidate_path=resumed.path,
            ) from exc
        return _project_work_state(workspace_dir, resumed)

    work_id, artifact_id = _work_candidate_ids(
        source=source,
        source_digest=source_digest,
        source_chat_id=effective_source_chat_id,
        source_run_id=source_run_id,
        parent=parent,
        parent_digest=parent_digest,
        lineage_root_id=lineage_root_id,
        version=version,
        derivation=derivation,
    )
    # Path.name is already a single, valid component on the source filesystem.
    # Keeping it exact also keeps the byte-tree digest stable across recovery.
    filename = source.name
    destination = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "files",
        work_id,
        filename,
    )
    if destination.parent.exists():
        try:
            valid_candidate = (
                not destination.parent.is_symlink()
                and not destination.is_symlink()
                and destination.is_file()
                and _source_digest(destination) == source_digest
            )
        except (OSError, ValueError):
            valid_candidate = False
        if not valid_candidate:
            raise WorksIndexUnavailable(
                "works_candidate_conflict",
                "已有同一作用身份的不完整候选；已保留现场，不会覆盖或重复生成",
                candidate_path=str(destination),
            )
    else:
        destination.parent.mkdir(parents=True, exist_ok=False)
        if infer_work_kind(source) == "html":
            _copy_html_bundle(source, destination)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
            if derivation is not None:
                try:
                    source_still_matches = (
                        _source_digest(source) == source_digest
                    )
                    managed_copy_matches = (
                        _source_digest(destination) == source_digest
                    )
                except (OSError, ValueError):
                    source_still_matches = False
                    managed_copy_matches = False
                if not source_still_matches or not managed_copy_matches:
                    try:
                        destination.unlink(missing_ok=True)
                        destination.parent.rmdir()
                    except OSError as exc:
                        raise WorksIndexUnavailable(
                            "works_candidate_cleanup_failed",
                            "派生成果在登记时发生变化，未登记候选无法安全清理",
                            candidate_path=str(destination),
                        ) from exc
                    raise WorkDerivationConflict(
                        "derived Work output changed during registration"
                    )

    display_title = (title or source.stem).strip() or source.name
    artifact = artifact_from_path(
        destination,
        title=display_title,
        producer="works",
        run_id=source_run_id,
        retention="user_saved",
        metadata={
            "work_id": work_id,
            "source_path": str(source),
            "source_chat_id": effective_source_chat_id,
            "source_digest": source_digest,
            "parent_work_id": parent.id if parent is not None else None,
            "parent_digest": parent_digest,
            "lineage_root_id": lineage_root_id or work_id,
            "version": version,
            "derivation": (
                derivation.model_dump(mode="json")
                if derivation is not None
                else None
            ),
        },
    ).model_copy(update={"id": artifact_id})
    try:
        register_artifact(workspace_dir, artifact)
    except ArtifactManifestUnavailable as exc:
        raise WorksIndexUnavailable(
            exc.code,
            "成果候选已生成并保留，但 Artifact 清单登记失败；继续登记时不会重复生成",
            candidate_path=str(destination),
        ) from exc
    except OSError as exc:
        raise WorksIndexUnavailable(
            "artifact_manifest_write_failed",
            "成果候选已生成并保留，但 Artifact 清单登记失败；继续登记时不会重复生成",
            candidate_path=str(destination),
        ) from exc
    item = WorkItem(
        id=work_id,
        artifact_id=artifact.id,
        title=display_title,
        kind=infer_work_kind(destination),
        path=str(destination),
        source_path=str(source),
        mime_type=artifact.mime_type
        or mimetypes.guess_type(destination.name)[0],
        size=artifact.size,
        source_chat_id=effective_source_chat_id,
        source_run_id=source_run_id,
        source_digest=source_digest,
        parent_work_id=parent.id if parent is not None else None,
        parent_digest=parent_digest,
        lineage_root_id=lineage_root_id or work_id,
        version=version,
        derivation=derivation,
    )
    updated_works = [entry for entry in works if entry.id != item.id]
    updated_works.append(item)
    try:
        _write_index(workspace_dir, updated_works, baseline=baseline)
    except WorksIndexUnavailable as exc:
        raise WorksIndexUnavailable(
            exc.code,
            "成果文件已生成并保留，但作品索引登记失败；请恢复索引后继续登记",
            current_digest=exc.current_digest,
            invalid_record_indexes=exc.invalid_record_indexes,
            candidate_path=str(destination),
        ) from exc
    return _project_work_state(workspace_dir, item)


# Reuse is allowed only when every source and result integrity field matches.
def derive_work_to_pdf(
    workspace_dir: str | Path,
    work_id: str,
) -> WorkItem:
    """Convert one exact saved PPT/PPTX revision into a related PDF Work."""

    ensure_works_index_writable(workspace_dir)
    managed = get_managed_work_file(workspace_dir, work_id)
    if managed is None:
        raise FileNotFoundError("saved work source is missing or modified")
    parent, source = managed
    if parent.kind != "presentation" or source.suffix.lower() not in {
        ".ppt",
        ".pptx",
    }:
        raise ValueError(
            "PDF derivation currently supports saved PPT/PPTX works",
        )
    if not parent.source_digest:
        raise ValueError("saved work source has no immutable digest")

    digest_token = parent.source_digest.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", digest_token) is None:
        raise ValueError("saved work source digest is invalid")
    output_dir = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "derivations",
        parent.id,
        digest_token,
    )
    derivation_lock = output_dir / ".derive"
    with exclusive_path_lock(derivation_lock):
        ensure_works_index_writable(workspace_dir)
        refreshed = get_managed_work_file(workspace_dir, work_id)
        if refreshed is None:
            raise FileNotFoundError("saved work source is missing or modified")
        current_parent, current_source = refreshed
        if (
            current_parent.artifact_id != parent.artifact_id
            or current_parent.version != parent.version
            or current_parent.source_digest != parent.source_digest
        ):
            raise ValueError(
                "saved work source identity changed during derivation",
            )

        for candidate in list_works(workspace_dir):
            relation = candidate.derivation
            if relation is None:
                continue
            reuse_matches = all(
                (
                    relation.operation == "office_conversion",
                    relation.target_format == "pdf",
                    relation.source_work_id == current_parent.id,
                    relation.source_artifact_id == current_parent.artifact_id,
                    relation.source_version == current_parent.version,
                    relation.source_digest == current_parent.source_digest,
                    candidate.availability_status == "available",
                    candidate.integrity_status == "intact",
                ),
            )
            if reuse_matches:
                return candidate

        from .office_conversion import convert_office_document

        result = convert_office_document(current_source, "pdf", output_dir)
        raw_output_digest = (
            "sha256:"
            f"{hashlib.sha256(result.output_path.read_bytes()).hexdigest()}"
        )
        if raw_output_digest != result.sha256:
            raise ValueError("converted PDF digest does not match its receipt")
        work_output_digest = _source_digest(result.output_path)
        relation = WorkDerivation(
            operation="office_conversion",
            target_format="pdf",
            source_work_id=current_parent.id,
            source_artifact_id=current_parent.artifact_id,
            source_version=current_parent.version,
            source_digest=current_parent.source_digest,
            output_digest=work_output_digest,
            converter_output_digest=result.sha256,
            converter=result.converter,
        )
        return add_work(
            workspace_dir,
            source_path=result.output_path,
            allowed_roots=[output_dir],
            title=f"{current_parent.title}（PDF）",
            source_chat_id=current_parent.source_chat_id,
            source_run_id=current_parent.source_run_id,
            derivation=relation,
        )


def remove_work(workspace_dir: str | Path, work_id: str) -> WorkItem | None:
    with exclusive_path_lock(works_index_path(workspace_dir)):
        return _remove_work_unlocked(workspace_dir, work_id)


def _remove_work_unlocked(
    workspace_dir: str | Path,
    work_id: str,
) -> WorkItem | None:
    baseline = _prepare_index_write(workspace_dir)
    works = list(baseline.works)
    item = next((entry for entry in works if entry.id == work_id), None)
    if item is None:
        return None
    removed_item = _project_work_state(workspace_dir, item)
    _write_index(
        workspace_dir,
        [entry for entry in works if entry.id != work_id],
        baseline=baseline,
    )

    managed_root = contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "works",
        "files",
        work_id,
    ).resolve(strict=False)
    managed_path = Path(item.path).expanduser().resolve(strict=False)
    if managed_root == managed_path.parent and managed_root.is_dir():
        shutil.rmtree(managed_root)
    unregister_artifact(workspace_dir, item.artifact_id)
    return removed_item
