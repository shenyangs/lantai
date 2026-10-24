# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from lantai.runtime.sanitizer import sanitize_runtime_payload
from lantai.security.storage_keys import (
    contained_storage_path,
    read_text_no_follow,
    write_text_atomic_no_follow,
)
from ..storage_lock import exclusive_path_lock
from .models import Artifact, ArtifactType

ARTIFACT_MANIFEST_SCHEMA_VERSION = 1
ArtifactGcPressureMode = Literal["observe", "schedule", "apply", "block"]
ArtifactLifecycleScope = Literal["run", "workspace"]
ArtifactLifecycleOperation = Literal["archive", "delete"]
_ARTIFACT_GC_PRESSURE_MODES = {"observe", "schedule", "apply", "block"}
_ARTIFACT_LIFECYCLE_SCOPES = {"run", "workspace"}
_ARTIFACT_LIFECYCLE_OPERATIONS = {"archive", "delete"}
_RETENTION_KEEP_POLICIES = {
    "forever",
    "keep",
    "legal_hold",
    "manual",
    "permanent",
    "user_saved",
}
_RETENTION_ORPHAN_POLICIES = {
    "attempt_lifetime",
    "ephemeral",
    "run_lifetime",
    "session_lifetime",
    "task_lifetime",
    "temporary",
    "transient",
}
_RETENTION_DURATION_RE = re.compile(r"^(?P<amount>\d+)(?P<unit>[smhdw])$")


class ArtifactManifestUnavailable(RuntimeError):
    """Raised when an Artifact manifest cannot be mutated without data loss."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def infer_artifact_type(path: str | Path) -> ArtifactType:
    suffix = Path(path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}:
        return "image"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix == ".docx":
        return "docx"
    if suffix in {".xlsx", ".xls", ".csv"}:
        return "xlsx"
    return "file"


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mime_type_for_path(path: Path) -> str | None:
    if path.suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
    mime_type, _ = mimetypes.guess_type(str(path))
    return mime_type


def artifact_from_path(
    path: str | Path,
    *,
    title: str | None = None,
    producer: str | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
    step_id: str | None = None,
    effect_id: str | None = None,
    classification: str = "internal",
    retention: str = "task_lifetime",
    metadata: dict | None = None,
) -> Artifact:
    file_path = Path(path)
    size = file_path.stat().st_size if file_path.exists() else None
    return Artifact(
        type=infer_artifact_type(file_path),
        title=title or file_path.name,
        path=str(file_path),
        mime_type=_mime_type_for_path(file_path),
        size=size,
        sha256=_sha256_file(file_path),
        producer=producer,
        run_id=run_id,
        attempt_id=attempt_id,
        step_id=step_id,
        effect_id=effect_id,
        classification=classification,
        retention=retention,
        metadata=sanitize_runtime_payload(metadata) if metadata else {},
    )


def artifact_manifest_path(workspace_dir: str | Path) -> Path:
    return contained_storage_path(
        Path(workspace_dir).expanduser(),
        ".lantai",
        "artifacts",
        "manifest.json",
    )


def load_artifact_manifest(workspace_dir: str | Path) -> dict[str, Any]:
    """Return the sanitized manifest for read-only export/report paths."""

    return _load_artifact_manifest(artifact_manifest_path(workspace_dir))


def _artifact_identity(artifact: Artifact | dict[str, Any]) -> str:
    if isinstance(artifact, Artifact):
        return artifact.id or artifact.path or artifact.url or ""
    return str(
        artifact.get("id")
        or artifact.get("path")
        or artifact.get("url")
        or "",
    )


def _load_artifact_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
            "artifacts": [],
        }
    try:
        payload = json.loads(read_text_no_follow(path))
    except Exception:
        return {
            "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
            "artifacts": [],
        }
    if not isinstance(payload, dict):
        return {
            "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
            "artifacts": [],
        }
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
    return {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "artifacts": [item for item in artifacts if isinstance(item, dict)],
    }


def _load_artifact_manifest_for_update(path: Path) -> dict[str, Any]:
    """Load a complete manifest or fail closed before a read-modify-write."""

    if not path.exists():
        return {
            "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
            "artifacts": [],
        }
    try:
        raw = read_text_no_follow(path)
    except OSError as exc:
        raise ArtifactManifestUnavailable(
            "artifact_manifest_read_failed",
            "Artifact 清单无法读取；原文件已保留，不会覆盖",
        ) from exc
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ArtifactManifestUnavailable(
            "artifact_manifest_invalid_json",
            "Artifact 清单已损坏；原文件已保留，不会当作空库",
        ) from exc
    if not isinstance(payload, dict):
        raise ArtifactManifestUnavailable(
            "artifact_manifest_invalid_root",
            "Artifact 清单根结构无效；原文件已保留",
        )
    schema_version = payload.get("schema_version")
    if schema_version != ARTIFACT_MANIFEST_SCHEMA_VERSION:
        code = (
            "artifact_manifest_future_schema"
            if isinstance(schema_version, int)
            and not isinstance(schema_version, bool)
            and schema_version > ARTIFACT_MANIFEST_SCHEMA_VERSION
            else "artifact_manifest_unsupported_schema"
        )
        raise ArtifactManifestUnavailable(
            code,
            "Artifact 清单版本不兼容；请先用对应版本恢复或升级",
        )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or any(
        not isinstance(item, dict) for item in artifacts
    ):
        raise ArtifactManifestUnavailable(
            "artifact_manifest_incompatible_records",
            "Artifact 清单含不兼容记录；原记录已全部保留",
        )
    return payload


def _probe_artifact_manifest_writable(path: Path) -> None:
    """Exercise the same parent directory before any external side effect."""

    probe = path.with_name(f".{path.name}.write-probe")
    try:
        write_text_atomic_no_follow(probe, "artifact-manifest-write-probe\n")
        probe.unlink()
    except OSError as exc:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        raise ArtifactManifestUnavailable(
            "artifact_manifest_write_failed",
            "Artifact 清单目录不可写；未覆盖原清单",
        ) from exc


def prepare_artifact_manifest_write(workspace_dir: str | Path) -> None:
    """Fail closed before a caller creates a candidate that needs a receipt."""

    path = artifact_manifest_path(workspace_dir)
    with exclusive_path_lock(path):
        _load_artifact_manifest_for_update(path)
        _probe_artifact_manifest_writable(path)


def _write_artifact_manifest(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic_no_follow(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )


def _artifact_manifest_checksum(payload: dict[str, Any]) -> str:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_artifact_manifest_snapshot(
    workspace_dir: str | Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, str]:
    source_sha256 = _artifact_manifest_checksum(manifest)
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"manifest-{timestamp}-{source_sha256[:12]}.json"
    snapshot_path = contained_storage_path(
        _workspace_root(workspace_dir),
        ".lantai",
        "artifacts",
        "manifest-backups",
        filename,
    )
    snapshot = {
        "schema_version": 1,
        "kind": "artifact_manifest_snapshot",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "source_manifest_path": str(manifest_path),
        "source_manifest_sha256": source_sha256,
        "manifest": manifest,
    }
    snapshot_text = json.dumps(
        snapshot,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    write_text_atomic_no_follow(snapshot_path, snapshot_text)
    return {
        "path": str(snapshot_path),
        "sha256": hashlib.sha256(snapshot_text.encode("utf-8")).hexdigest(),
    }


def _utc_datetime(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc_datetime(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc_datetime(parsed)


# The supported unit cases intentionally remain explicit and auditable.
# pylint: disable-next=too-many-return-statements
def _parse_retention_duration(retention: str) -> timedelta | None:
    text = retention.strip().lower().replace(" ", "")
    for prefix in ("ttl:", "ttl=", "expire_after:", "expire_after="):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.startswith("days:"):
        text = f"{text.removeprefix('days:')}d"
    elif text.startswith("hours:"):
        text = f"{text.removeprefix('hours:')}h"
    match = _RETENTION_DURATION_RE.fullmatch(text)
    if not match:
        return None
    amount = int(match.group("amount"))
    unit = match.group("unit")
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return None


def _artifact_retention_report(
    item: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    retention = (
        str(item.get("retention") or "task_lifetime").strip()
        or "task_lifetime"
    )
    retention_key = retention.lower()
    base = {
        "retention": retention,
        "retention_policy": retention_key,
        "retention_eligible": False,
        "retention_expires_at": None,
        "retention_blocked_reason": "",
    }
    if retention_key in _RETENTION_KEEP_POLICIES:
        base[
            "retention_blocked_reason"
        ] = "retention_policy_preserves_artifact"
        return base
    if retention_key in _RETENTION_ORPHAN_POLICIES:
        base["retention_eligible"] = True
        return base

    duration = _parse_retention_duration(retention)
    if duration is None:
        base["retention_blocked_reason"] = "retention_policy_unknown"
        return base

    created_at = _parse_datetime(item.get("created_at"))
    if created_at is None:
        base[
            "retention_blocked_reason"
        ] = "retention_created_at_missing_or_invalid"
        return base

    expires_at = created_at + duration
    base["retention_expires_at"] = expires_at.isoformat().replace(
        "+00:00",
        "Z",
    )
    if now >= expires_at:
        base["retention_eligible"] = True
    else:
        base["retention_blocked_reason"] = "retention_window_active"
    return base


def _artifact_manifest_size(item: dict[str, Any]) -> int:
    size = item.get("size")
    if isinstance(size, bool):
        return 0
    if isinstance(size, int):
        return max(0, size)
    return 0


def _validate_budget_limit(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _validate_pressure_mode(mode: str) -> ArtifactGcPressureMode:
    if mode not in _ARTIFACT_GC_PRESSURE_MODES:
        raise ValueError(f"unsupported artifact GC pressure mode: {mode}")
    return mode  # type: ignore[return-value]


def _validate_lifecycle_scope(scope: str) -> ArtifactLifecycleScope:
    if scope not in _ARTIFACT_LIFECYCLE_SCOPES:
        raise ValueError(f"unsupported artifact lifecycle scope: {scope}")
    return scope  # type: ignore[return-value]


def _validate_lifecycle_operation(
    operation: str,
) -> ArtifactLifecycleOperation:
    if operation not in _ARTIFACT_LIFECYCLE_OPERATIONS:
        raise ValueError(
            f"unsupported artifact lifecycle operation: {operation}",
        )
    return operation  # type: ignore[return-value]


def _artifact_budget_report(
    manifest_items: list[dict[str, Any]],
    candidate_entries: list[
        tuple[dict[str, Any], dict[str, Any], Path | None]
    ],
    *,
    max_artifact_count: int | None,
    max_artifact_bytes: int | None,
) -> dict[str, Any]:
    current_count = len(manifest_items)
    current_bytes = sum(
        _artifact_manifest_size(item) for item in manifest_items
    )
    reclaimable_items = [
        item
        for item, report, _ in candidate_entries
        if report["would_remove_manifest"]
    ]
    reclaimable_count = len(reclaimable_items)
    reclaimable_bytes = sum(
        _artifact_manifest_size(item) for item in reclaimable_items
    )
    projected_count = max(0, current_count - reclaimable_count)
    projected_bytes = max(0, current_bytes - reclaimable_bytes)
    count_over = (
        max(0, current_count - max_artifact_count)
        if max_artifact_count is not None
        else 0
    )
    bytes_over = (
        max(0, current_bytes - max_artifact_bytes)
        if max_artifact_bytes is not None
        else 0
    )
    projected_count_over = (
        max(0, projected_count - max_artifact_count)
        if max_artifact_count is not None
        else 0
    )
    projected_bytes_over = (
        max(0, projected_bytes - max_artifact_bytes)
        if max_artifact_bytes is not None
        else 0
    )
    return {
        "max_artifact_count": max_artifact_count,
        "max_artifact_bytes": max_artifact_bytes,
        "current_artifact_count": current_count,
        "current_artifact_bytes": current_bytes,
        "orphan_artifact_count": len(candidate_entries),
        "reclaimable_artifact_count": reclaimable_count,
        "reclaimable_artifact_bytes": reclaimable_bytes,
        "projected_artifact_count_after_gc": projected_count,
        "projected_artifact_bytes_after_gc": projected_bytes,
        "artifact_count_over_budget": count_over,
        "artifact_bytes_over_budget": bytes_over,
        "projected_artifact_count_over_budget_after_gc": projected_count_over,
        "projected_artifact_bytes_over_budget_after_gc": projected_bytes_over,
        "budget_exceeded": bool(count_over or bytes_over),
        "budget_satisfied_after_gc": not bool(
            projected_count_over or projected_bytes_over,
        ),
        "budget_blocked_by_live_or_retained_artifacts": bool(
            projected_count_over or projected_bytes_over,
        ),
    }


def _workspace_root(workspace_dir: str | Path) -> Path:
    return Path(workspace_dir).expanduser().resolve(strict=False)


def _path_within_workspace(
    workspace_dir: str | Path,
    raw_path: object,
) -> Path | None:
    if not raw_path:
        return None
    workspace = _workspace_root(workspace_dir)
    path = Path(str(raw_path)).expanduser().resolve(strict=False)
    try:
        path.relative_to(workspace)
    except ValueError:
        return None
    return path


def _artifact_manifest_entry_report(
    workspace_dir: str | Path,
    item: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    retention_report = _artifact_retention_report(item, now=_utc_datetime(now))
    raw_path = item.get("path")
    raw_candidate = Path(str(raw_path)).expanduser() if raw_path else None
    path = _path_within_workspace(workspace_dir, raw_path)
    exists = bool(path and path.exists())
    is_file = bool(path and path.is_file())
    is_symlink = bool(raw_candidate and raw_candidate.is_symlink())
    expected_sha256 = item.get("sha256")
    actual_sha256 = (
        _sha256_file(path) if path and is_file and not is_symlink else None
    )
    file_safe_to_delete = bool(
        path
        and exists
        and is_file
        and not is_symlink
        and (not expected_sha256 or actual_sha256 == expected_sha256),
    )
    safe_to_delete = bool(
        file_safe_to_delete and retention_report["retention_eligible"],
    )
    if path is None:
        file_blocked_reason = (
            "path_outside_workspace" if raw_path else "missing_path"
        )
    elif is_symlink:
        file_blocked_reason = "artifact_path_is_symlink"
    elif not exists:
        file_blocked_reason = "artifact_file_missing"
    elif not is_file:
        file_blocked_reason = "artifact_path_not_file"
    elif expected_sha256 and actual_sha256 != expected_sha256:
        file_blocked_reason = "artifact_sha256_mismatch"
    else:
        file_blocked_reason = ""

    if file_blocked_reason and file_blocked_reason != "artifact_file_missing":
        blocked_reason = file_blocked_reason
    elif not retention_report["retention_eligible"]:
        blocked_reason = retention_report["retention_blocked_reason"]
    elif file_blocked_reason:
        blocked_reason = file_blocked_reason
    else:
        blocked_reason = ""
    would_remove_manifest = bool(
        retention_report["retention_eligible"]
        and (
            file_safe_to_delete
            or file_blocked_reason == "artifact_file_missing"
        ),
    )
    return {
        "artifact_id": str(item.get("id") or ""),
        "path": str(raw_path or ""),
        "exists": exists,
        "size": path.stat().st_size if path and exists and is_file else None,
        "sha256": expected_sha256,
        "actual_sha256": actual_sha256,
        "reason": "not_referenced_by_live_artifacts",
        "would_delete": safe_to_delete,
        "safe_to_delete": safe_to_delete,
        "file_safe_to_delete": file_safe_to_delete,
        "file_blocked_reason": file_blocked_reason,
        "blocked_reason": blocked_reason,
        "would_remove_manifest": would_remove_manifest,
        **retention_report,
    }


def register_artifact(workspace_dir: str | Path, artifact: Artifact) -> Path:
    """Register an artifact metadata row for later retention/GC decisions."""

    path = artifact_manifest_path(workspace_dir)
    with exclusive_path_lock(path):
        manifest = _load_artifact_manifest_for_update(path)
        _probe_artifact_manifest_writable(path)
        entry = artifact.model_dump(mode="json")
        entry["metadata"] = sanitize_runtime_payload(artifact.metadata)
        identity = _artifact_identity(entry)
        artifacts = [
            item
            for item in manifest["artifacts"]
            if _artifact_identity(item) != identity
        ]
        artifacts.append(entry)
        manifest["artifacts"] = artifacts
        try:
            _write_artifact_manifest(path, manifest)
        except OSError as exc:
            raise ArtifactManifestUnavailable(
                "artifact_manifest_write_failed",
                "Artifact 清单写入失败；原清单已保留",
            ) from exc
    return path


def unregister_artifact(workspace_dir: str | Path, artifact_id: str) -> bool:
    """Remove a row after an explicit user-owned artifact removal."""

    path = artifact_manifest_path(workspace_dir)
    with exclusive_path_lock(path):
        manifest = _load_artifact_manifest_for_update(path)
        artifacts = [
            item
            for item in manifest["artifacts"]
            if str(item.get("id") or "") != str(artifact_id)
        ]
        if len(artifacts) == len(manifest["artifacts"]):
            return False
        _probe_artifact_manifest_writable(path)
        manifest["artifacts"] = artifacts
        try:
            _write_artifact_manifest(path, manifest)
        except OSError as exc:
            raise ArtifactManifestUnavailable(
                "artifact_manifest_write_failed",
                "Artifact 清单写入失败；原清单已保留",
            ) from exc
    return True


def artifact_gc_dry_run(
    workspace_dir: str | Path,
    *,
    live_artifacts: list[Artifact | dict[str, Any]],
    now: datetime | None = None,
    max_artifact_count: int | None = None,
    max_artifact_bytes: int | None = None,
) -> dict[str, Any]:
    """Return orphan artifact candidates without deleting files."""

    max_artifact_count = _validate_budget_limit(
        "max_artifact_count",
        max_artifact_count,
    )
    max_artifact_bytes = _validate_budget_limit(
        "max_artifact_bytes",
        max_artifact_bytes,
    )
    manifest = _load_artifact_manifest(artifact_manifest_path(workspace_dir))
    live = {_artifact_identity(artifact) for artifact in live_artifacts}
    candidate_entries: list[
        tuple[dict[str, Any], dict[str, Any], Path | None]
    ] = []
    candidates: list[dict[str, Any]] = []
    for item in manifest["artifacts"]:
        identity = _artifact_identity(item)
        if identity in live:
            continue
        report = _artifact_manifest_entry_report(workspace_dir, item, now=now)
        path = _path_within_workspace(workspace_dir, item.get("path"))
        candidate_entries.append((item, report, path))
        candidates.append(report)
    return {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "dry_run": True,
        "candidate_count": len(candidates),
        "budget": _artifact_budget_report(
            manifest["artifacts"],
            candidate_entries,
            max_artifact_count=max_artifact_count,
            max_artifact_bytes=max_artifact_bytes,
        ),
        "candidates": candidates,
    }


def artifact_gc_apply(
    workspace_dir: str | Path,
    *,
    live_artifacts: list[Artifact | dict[str, Any]],
    now: datetime | None = None,
    max_artifact_count: int | None = None,
    max_artifact_bytes: int | None = None,
) -> dict[str, Any]:
    """Delete safe orphan artifact files and cascade manifest removals."""

    manifest_path = artifact_manifest_path(workspace_dir)
    with exclusive_path_lock(manifest_path):
        return _artifact_gc_apply_locked(
            workspace_dir,
            live_artifacts=live_artifacts,
            now=now,
            max_artifact_count=max_artifact_count,
            max_artifact_bytes=max_artifact_bytes,
        )


def _artifact_gc_apply_locked(
    workspace_dir: str | Path,
    *,
    live_artifacts: list[Artifact | dict[str, Any]],
    now: datetime | None = None,
    max_artifact_count: int | None = None,
    max_artifact_bytes: int | None = None,
) -> dict[str, Any]:
    """Apply GC while the Artifact manifest lock is held."""

    max_artifact_count = _validate_budget_limit(
        "max_artifact_count",
        max_artifact_count,
    )
    max_artifact_bytes = _validate_budget_limit(
        "max_artifact_bytes",
        max_artifact_bytes,
    )
    manifest_path = artifact_manifest_path(workspace_dir)
    manifest = _load_artifact_manifest_for_update(manifest_path)
    live = {_artifact_identity(artifact) for artifact in live_artifacts}
    kept: list[dict[str, Any]] = []
    candidate_entries: list[
        tuple[dict[str, Any], dict[str, Any], Path | None]
    ] = []
    deleted_count = 0
    manifest_removed_count = 0
    skipped_count = 0

    for item in manifest["artifacts"]:
        identity = _artifact_identity(item)
        if identity in live:
            kept.append(item)
            continue

        report = _artifact_manifest_entry_report(workspace_dir, item, now=now)
        path = _path_within_workspace(workspace_dir, item.get("path"))
        candidate_entries.append((item, report, path))

    will_mutate = any(
        report["would_remove_manifest"] for _, report, _ in candidate_entries
    )
    budget = _artifact_budget_report(
        manifest["artifacts"],
        candidate_entries,
        max_artifact_count=max_artifact_count,
        max_artifact_bytes=max_artifact_bytes,
    )
    if will_mutate:
        _probe_artifact_manifest_writable(manifest_path)
    snapshot = (
        _write_artifact_manifest_snapshot(
            workspace_dir,
            manifest_path,
            manifest,
        )
        if will_mutate
        else None
    )

    candidates: list[dict[str, Any]] = []
    for item, report, path in candidate_entries:
        if report["safe_to_delete"] and path is not None:
            path.unlink()
            report["deleted"] = True
            report["manifest_removed"] = True
            deleted_count += 1
            manifest_removed_count += 1
        elif report["would_remove_manifest"]:
            report["deleted"] = False
            report["manifest_removed"] = True
            manifest_removed_count += 1
        else:
            report["deleted"] = False
            report["manifest_removed"] = False
            kept.append(item)
            skipped_count += 1
        candidates.append(report)

    if will_mutate:
        manifest["artifacts"] = kept
        try:
            _write_artifact_manifest(manifest_path, manifest)
        except OSError as exc:
            raise ArtifactManifestUnavailable(
                "artifact_manifest_write_failed",
                "Artifact 清单写入失败；删除前快照已保留",
            ) from exc
    return {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "dry_run": False,
        "manifest_snapshot_path": snapshot["path"] if snapshot else None,
        "manifest_snapshot_sha256": snapshot["sha256"] if snapshot else None,
        "candidate_count": len(candidates),
        "budget": budget,
        "deleted_count": deleted_count,
        "manifest_removed_count": manifest_removed_count,
        "skipped_count": skipped_count,
        "candidates": candidates,
    }


def artifact_gc_pressure_policy(
    workspace_dir: str | Path,
    *,
    live_artifacts: list[Artifact | dict[str, Any]],
    mode: ArtifactGcPressureMode = "observe",
    now: datetime | None = None,
    max_artifact_count: int | None = None,
    max_artifact_bytes: int | None = None,
) -> dict[str, Any]:
    """Evaluate budget pressure and optionally schedule or apply safe GC."""

    mode = _validate_pressure_mode(mode)
    max_artifact_count = _validate_budget_limit(
        "max_artifact_count",
        max_artifact_count,
    )
    max_artifact_bytes = _validate_budget_limit(
        "max_artifact_bytes",
        max_artifact_bytes,
    )
    dry_run = artifact_gc_dry_run(
        workspace_dir,
        live_artifacts=live_artifacts,
        now=now,
        max_artifact_count=max_artifact_count,
        max_artifact_bytes=max_artifact_bytes,
    )
    budget = dry_run["budget"]
    budget_configured = (
        max_artifact_count is not None or max_artifact_bytes is not None
    )
    pressure_triggered = bool(budget_configured and budget["budget_exceeded"])
    should_schedule_gc = False
    should_block_new_artifacts = False
    apply_report: dict[str, Any] | None = None

    if not budget_configured:
        decision = "no_budget_configured"
    elif not pressure_triggered:
        decision = "within_budget"
    elif mode == "observe":
        decision = "observe_budget_pressure"
    elif mode == "schedule":
        decision = "schedule_gc"
        should_schedule_gc = True
    elif mode == "apply":
        apply_report = artifact_gc_apply(
            workspace_dir,
            live_artifacts=live_artifacts,
            now=now,
            max_artifact_count=max_artifact_count,
            max_artifact_bytes=max_artifact_bytes,
        )
        if apply_report["budget"]["budget_satisfied_after_gc"]:
            decision = "gc_applied_budget_satisfied"
        else:
            decision = "gc_applied_budget_still_exceeded"
    else:
        if budget["budget_satisfied_after_gc"]:
            decision = "schedule_gc_before_blocking"
            should_schedule_gc = True
        else:
            decision = "block_new_artifacts"
            should_block_new_artifacts = True

    return {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "kind": "artifact_gc_pressure_policy",
        "mode": mode,
        "decision": decision,
        "budget_configured": budget_configured,
        "pressure_triggered": pressure_triggered,
        "should_schedule_gc": should_schedule_gc,
        "should_apply_gc": bool(mode == "apply" and pressure_triggered),
        "should_block_new_artifacts": should_block_new_artifacts,
        "budget": budget,
        "dry_run_report": dry_run,
        "apply_report": apply_report,
    }


def artifact_lifecycle_cascade(
    workspace_dir: str | Path,
    *,
    scope: ArtifactLifecycleScope,
    operation: ArtifactLifecycleOperation,
    run_id: str | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
    max_artifact_count: int | None = None,
    max_artifact_bytes: int | None = None,
) -> dict[str, Any]:
    """Apply artifact manifest policy for run/workspace archive or deletion."""

    scope = _validate_lifecycle_scope(scope)
    operation = _validate_lifecycle_operation(operation)
    if scope == "run" and not run_id:
        raise ValueError(
            "run_id is required for run artifact lifecycle cascades",
        )

    manifest_path = artifact_manifest_path(workspace_dir)
    manifest = _load_artifact_manifest(manifest_path)
    if scope == "run":
        matching_items = [
            item
            for item in manifest["artifacts"]
            if str(item.get("run_id") or "") == str(run_id)
        ]
        live_artifacts = [
            item
            for item in manifest["artifacts"]
            if str(item.get("run_id") or "") != str(run_id)
        ]
    else:
        matching_items = list(manifest["artifacts"])
        live_artifacts = []

    cascade = {
        "scope": scope,
        "operation": operation,
        "run_id": run_id,
        "dry_run": dry_run,
        "matching_manifest_count": len(matching_items),
        "policy": (
            "delete operation keeps non-matching artifacts live and lets "
            "matching artifacts pass through retention, integrity, and "
            "workspace containment gates"
        )
        if operation == "delete"
        else (
            "archive operation snapshots the artifact manifest without "
            "deleting files"
        ),
    }

    if operation == "archive":
        snapshot = None
        if not dry_run:
            snapshot = _write_artifact_manifest_snapshot(
                workspace_dir,
                manifest_path,
                manifest,
            )
        return {
            "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
            "kind": "artifact_lifecycle_cascade",
            "cascade": cascade,
            "manifest_snapshot_path": snapshot["path"] if snapshot else None,
            "manifest_snapshot_sha256": snapshot["sha256"]
            if snapshot
            else None,
            "artifact_count": len(manifest["artifacts"]),
            "matching_artifact_ids": [
                str(item.get("id") or "") for item in matching_items
            ],
        }

    if dry_run:
        report = artifact_gc_dry_run(
            workspace_dir,
            live_artifacts=live_artifacts,
            now=now,
            max_artifact_count=max_artifact_count,
            max_artifact_bytes=max_artifact_bytes,
        )
    else:
        report = artifact_gc_apply(
            workspace_dir,
            live_artifacts=live_artifacts,
            now=now,
            max_artifact_count=max_artifact_count,
            max_artifact_bytes=max_artifact_bytes,
        )
    report["kind"] = "artifact_lifecycle_cascade"
    report["cascade"] = cascade
    return report


def artifact_gc_after_run_delete(
    workspace_dir: str | Path,
    *,
    run_id: str,
    dry_run: bool = True,
    now: datetime | None = None,
    max_artifact_count: int | None = None,
    max_artifact_bytes: int | None = None,
) -> dict[str, Any]:
    """Cascade artifact retention policy after deleting or archiving a run."""

    return artifact_lifecycle_cascade(
        workspace_dir,
        scope="run",
        operation="delete",
        run_id=run_id,
        dry_run=dry_run,
        now=now,
        max_artifact_count=max_artifact_count,
        max_artifact_bytes=max_artifact_bytes,
    )


def artifact_gc_after_workspace_delete(
    workspace_dir: str | Path,
    *,
    dry_run: bool = True,
    now: datetime | None = None,
    max_artifact_count: int | None = None,
    max_artifact_bytes: int | None = None,
) -> dict[str, Any]:
    """Cascade artifact retention policy when a workspace is being deleted."""

    return artifact_lifecycle_cascade(
        workspace_dir,
        scope="workspace",
        operation="delete",
        dry_run=dry_run,
        now=now,
        max_artifact_count=max_artifact_count,
        max_artifact_bytes=max_artifact_bytes,
    )
