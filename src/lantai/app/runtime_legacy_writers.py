# -*- coding: utf-8 -*-
"""Shared guard for legacy Runtime writer surfaces during migration."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


LEGACY_RUNTIME_WRITER_MODE_ENV = "LANTAI_RUNTIME_LEGACY_WRITER_MODE"
LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV = (
    "LANTAI_RUNTIME_LEGACY_WRITER_OBSERVATION_FILE"
)
LEGACY_RUNTIME_WRITER_OBSERVATION_SCHEMA = (
    "com.lantai.runtime-legacy-writer-observation.v1"
)
LEGACY_RUNTIME_WRITER_OBSERVATION_SUMMARY_SCHEMA = (
    "com.lantai.runtime-legacy-writer-observation-summary.v1"
)

LegacyRuntimeWriterMode = Literal["active", "read_only"]
LegacyRuntimeWriterObservationOutcome = Literal["allowed", "blocked"]

LEGACY_RUNTIME_WRITER_IDS: tuple[str, ...] = (
    "legacy_runtime_event_ledger_writer",
    "legacy_task_run_snapshot_writer",
    "legacy_evaluation_report_writer",
    "legacy_mission_state_writer",
    "legacy_browser_task_state_writer",
)

_KNOWN_WRITER_IDS = set(LEGACY_RUNTIME_WRITER_IDS)
_MODE_ALIASES: dict[str, LegacyRuntimeWriterMode] = {
    "active": "active",
    "enabled": "active",
    "write": "active",
    "write_enabled": "active",
    "legacy_write_active": "active",
    "read_only": "read_only",
    "readonly": "read_only",
    "disabled": "read_only",
    "off": "read_only",
    "legacy_write_stopped_read_compatibility": "read_only",
}


class LegacyRuntimeWriterBlocked(RuntimeError):
    """Raised when a legacy Runtime writer is disabled by migration mode."""

    def __init__(
        self,
        writer_id: str,
        *,
        mode: LegacyRuntimeWriterMode,
        path: str | Path | None = None,
    ) -> None:
        self.writer_id = writer_id
        self.mode = mode
        self.path = str(path) if path is not None else None
        path_suffix = f" for {self.path}" if self.path else ""
        super().__init__(
            f"Legacy Runtime writer {writer_id!r} is disabled in {mode!r} mode"
            f"{path_suffix}",
        )


class LegacyRuntimeWriterObservationError(RuntimeError):
    """Raised when explicit legacy-writer observation evidence cannot be written."""


def normalize_legacy_runtime_writer_mode(
    value: str | None,
) -> LegacyRuntimeWriterMode:
    """Normalize an operator-facing legacy writer mode value."""

    if value is None or not value.strip():
        return "active"
    key = value.strip().lower().replace("-", "_")
    mode = _MODE_ALIASES.get(key)
    if mode is None:
        allowed = ", ".join(sorted(set(_MODE_ALIASES)))
        raise ValueError(
            f"Unsupported {LEGACY_RUNTIME_WRITER_MODE_ENV}={value!r}; "
            f"expected one of: {allowed}",
        )
    return mode


def get_legacy_runtime_writer_mode() -> LegacyRuntimeWriterMode:
    """Return the current legacy writer mode from the environment."""

    return normalize_legacy_runtime_writer_mode(
        os.environ.get(LEGACY_RUNTIME_WRITER_MODE_ENV),
    )


def legacy_runtime_writer_allows_writes() -> bool:
    """Whether legacy Runtime writer surfaces may mutate legacy stores."""

    return get_legacy_runtime_writer_mode() == "active"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _path_hash(path: str | Path | None) -> str | None:
    if path is None:
        return None
    value = str(path)
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _observation_path_name(path: str | Path | None) -> str | None:
    if path is None:
        return None
    name = Path(path).name
    return name or None


def _observation_file_path() -> Path | None:
    value = os.environ.get(LEGACY_RUNTIME_WRITER_OBSERVATION_FILE_ENV)
    if value is None or not value.strip():
        return None
    return Path(value).expanduser()


def _observation_record(
    *,
    writer_id: str,
    mode: LegacyRuntimeWriterMode,
    outcome: LegacyRuntimeWriterObservationOutcome,
    path: str | Path | None,
) -> dict[str, Any]:
    return {
        "schema": LEGACY_RUNTIME_WRITER_OBSERVATION_SCHEMA,
        "event_id": f"legacy-writer-observation:{uuid.uuid4().hex}",
        "observed_at": _utc_now_iso(),
        "pid": os.getpid(),
        "writer_id": writer_id,
        "mode": mode,
        "outcome": outcome,
        "target_path_present": path is not None,
        "target_path_sha256": _path_hash(path),
        "target_path_name": _observation_path_name(path),
    }


def record_legacy_runtime_writer_observation(
    *,
    writer_id: str,
    mode: LegacyRuntimeWriterMode,
    outcome: LegacyRuntimeWriterObservationOutcome,
    path: str | Path | None = None,
) -> None:
    """Append explicit release-cycle evidence for a legacy writer check."""

    observation_file = _observation_file_path()
    if observation_file is None:
        return
    if writer_id not in _KNOWN_WRITER_IDS:
        raise ValueError(f"Unknown legacy Runtime writer id: {writer_id}")

    try:
        observation_file.parent.mkdir(parents=True, exist_ok=True)
        line = (
            json.dumps(
                _observation_record(
                    writer_id=writer_id,
                    mode=mode,
                    outcome=outcome,
                    path=path,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        fd = os.open(observation_file, flags, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as exc:
        raise LegacyRuntimeWriterObservationError(
            "Failed to append legacy Runtime writer observation evidence "
            f"to {observation_file}",
        ) from exc


def summarize_legacy_runtime_writer_observations(
    observation_file: str | Path,
    *,
    release_edition: str,
) -> dict[str, Any]:
    """Summarize optional legacy-writer release-cycle observation JSONL."""

    path = Path(observation_file).expanduser()
    counts_by_writer = {writer_id: 0 for writer_id in LEGACY_RUNTIME_WRITER_IDS}
    counts_by_outcome: dict[str, int] = {"allowed": 0, "blocked": 0}
    malformed_count = 0
    unknown_schema_count = 0
    invalid_writer_ids: set[str] = set()
    event_count = 0

    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                malformed_count += 1
                continue
            if not isinstance(record, dict):
                malformed_count += 1
                continue
            if record.get("schema") != LEGACY_RUNTIME_WRITER_OBSERVATION_SCHEMA:
                unknown_schema_count += 1
                continue
            writer_id = str(record.get("writer_id") or "")
            if writer_id not in _KNOWN_WRITER_IDS:
                invalid_writer_ids.add(writer_id or "<missing>")
                continue
            outcome = str(record.get("outcome") or "")
            if outcome not in counts_by_outcome:
                malformed_count += 1
                continue
            event_count += 1
            counts_by_writer[writer_id] += 1
            counts_by_outcome[outcome] += 1

    writer_ids_without_observations = sorted(
        writer_id for writer_id, count in counts_by_writer.items() if count == 0
    )
    return {
        "schema": LEGACY_RUNTIME_WRITER_OBSERVATION_SUMMARY_SCHEMA,
        "generated_at": _utc_now_iso(),
        "release_edition": release_edition,
        "observation_file": str(path),
        "observation_file_exists": path.exists(),
        "event_count": event_count,
        "fallback_write_count": counts_by_outcome["allowed"],
        "blocked_attempt_count": counts_by_outcome["blocked"],
        "malformed_count": malformed_count,
        "unknown_schema_count": unknown_schema_count,
        "invalid_writer_ids": sorted(invalid_writer_ids),
        "counts_by_writer": counts_by_writer,
        "counts_by_outcome": counts_by_outcome,
        "writer_ids_without_observations": writer_ids_without_observations,
        "zero_observations": (
            event_count == 0
            and malformed_count == 0
            and unknown_schema_count == 0
            and not invalid_writer_ids
        ),
        "zero_fallback_writes": (
            counts_by_outcome["allowed"] == 0
            and malformed_count == 0
            and unknown_schema_count == 0
            and not invalid_writer_ids
        ),
    }


def assert_legacy_runtime_writer_allowed(
    writer_id: str,
    *,
    path: str | Path | None = None,
) -> None:
    """Reject legacy writes when Runtime migration is in read-only mode."""

    if writer_id not in _KNOWN_WRITER_IDS:
        raise ValueError(f"Unknown legacy Runtime writer id: {writer_id}")
    mode = get_legacy_runtime_writer_mode()
    if mode != "active":
        record_legacy_runtime_writer_observation(
            writer_id=writer_id,
            mode=mode,
            outcome="blocked",
            path=path,
        )
        raise LegacyRuntimeWriterBlocked(writer_id, mode=mode, path=path)
    record_legacy_runtime_writer_observation(
        writer_id=writer_id,
        mode=mode,
        outcome="allowed",
        path=path,
    )
