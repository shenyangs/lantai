# -*- coding: utf-8 -*-
"""Best-effort observations for legacy Runtime execution surfaces.

This module deliberately does not control routing.  It records only bounded,
non-user dimensions so enabling or disabling observation cannot change a turn.
Legacy writer attempts remain sourced from :mod:`runtime_legacy_writers` and
must be reported separately from the three execution categories below.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV = (
    "LANTAI_RUNTIME_LEGACY_EXECUTION_OBSERVATION_FILE"
)
LEGACY_RUNTIME_EXECUTION_OBSERVATION_SCHEMA = (
    "com.lantai.runtime-legacy-execution-observation.v1"
)
LEGACY_RUNTIME_EXECUTION_OBSERVATION_SUMMARY_SCHEMA = (
    "com.lantai.runtime-legacy-execution-observation-summary.v1"
)
LEGACY_RUNTIME_BOUNDARY_OBSERVATION_SUMMARY_SCHEMA = (
    "com.lantai.runtime-legacy-boundary-observation-summary.v1"
)

LegacyRuntimeExecutionObservationKind = Literal[
    "legacy_agent_constructed",
    "legacy_loop_invoked",
    "legacy_runtime_adapter_selected",
]

LEGACY_RUNTIME_EXECUTION_OBSERVATION_KINDS: tuple[
    LegacyRuntimeExecutionObservationKind,
    ...,
] = (
    "legacy_agent_constructed",
    "legacy_loop_invoked",
    "legacy_runtime_adapter_selected",
)

_KNOWN_KINDS = set(LEGACY_RUNTIME_EXECUTION_OBSERVATION_KINDS)
_SAFE_DIMENSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_UNKNOWN_DIMENSION = "unknown"
_REDACTED_DIMENSION = "redacted"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _safe_dimension(value: str | None) -> str:
    """Return a bounded operational label without preserving arbitrary text."""

    if value is None:
        return _UNKNOWN_DIMENSION
    normalized = value.strip()
    if not normalized:
        return _UNKNOWN_DIMENSION
    if not _SAFE_DIMENSION_PATTERN.fullmatch(normalized):
        return _REDACTED_DIMENSION
    return normalized


def _observation_file_path() -> Path | None:
    value = os.environ.get(LEGACY_RUNTIME_EXECUTION_OBSERVATION_FILE_ENV)
    if value is None or not value.strip():
        return None
    return Path(value).expanduser()


def _observation_record(
    *,
    kind: LegacyRuntimeExecutionObservationKind,
    surface: str,
    release: str | None,
    entrypoint: str | None,
    engine: str | None,
    channel: str | None,
    reason_code: str | None,
) -> dict[str, Any]:
    return {
        "schema": LEGACY_RUNTIME_EXECUTION_OBSERVATION_SCHEMA,
        "event_id": f"legacy-execution-observation:{uuid.uuid4().hex}",
        "observed_at": _utc_now_iso(),
        "pid": os.getpid(),
        "kind": kind,
        "surface": _safe_dimension(surface),
        "release": _safe_dimension(release),
        "entrypoint": _safe_dimension(entrypoint),
        "engine": _safe_dimension(engine),
        "channel": _safe_dimension(channel),
        "reason_code": _safe_dimension(reason_code),
    }


def record_legacy_runtime_execution_observation(
    *,
    kind: LegacyRuntimeExecutionObservationKind,
    surface: str,
    release: str | None = None,
    entrypoint: str | None = None,
    engine: str | None = None,
    channel: str | None = None,
    reason_code: str | None = None,
) -> bool:
    """Append one privacy-bounded observation without affecting product flow.

    ``False`` means observation was disabled, invalid, or could not be written.
    The function intentionally never raises: observability failure must not alter
    Runtime selection, replay a turn, or fail a user request.
    """

    observation_file = _observation_file_path()
    if observation_file is None or kind not in _KNOWN_KINDS:
        return False

    try:
        observation_file.parent.mkdir(parents=True, exist_ok=True)
        line = (
            json.dumps(
                _observation_record(
                    kind=kind,
                    surface=surface,
                    release=release,
                    entrypoint=entrypoint,
                    engine=engine,
                    channel=channel,
                    reason_code=reason_code,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        fd = os.open(
            observation_file,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return False
    return True


def summarize_legacy_runtime_execution_observations(
    observation_file: str | Path,
    *,
    release_edition: str,
) -> dict[str, Any]:
    """Summarize optional execution observations as release evidence."""

    path = Path(observation_file).expanduser()
    counts_by_kind = {
        kind: 0 for kind in LEGACY_RUNTIME_EXECUTION_OBSERVATION_KINDS
    }
    counts_by_surface: dict[str, int] = {}
    malformed_count = 0
    unknown_schema_count = 0
    invalid_kind_count = 0
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
            if record.get("schema") != LEGACY_RUNTIME_EXECUTION_OBSERVATION_SCHEMA:
                unknown_schema_count += 1
                continue
            kind = str(record.get("kind") or "")
            if kind not in _KNOWN_KINDS:
                invalid_kind_count += 1
                continue
            surface = _safe_dimension(str(record.get("surface") or ""))
            event_count += 1
            counts_by_kind[kind] += 1
            counts_by_surface[surface] = counts_by_surface.get(surface, 0) + 1

    evidence_valid = (
        malformed_count == 0
        and unknown_schema_count == 0
        and invalid_kind_count == 0
    )
    return {
        "schema": LEGACY_RUNTIME_EXECUTION_OBSERVATION_SUMMARY_SCHEMA,
        "generated_at": _utc_now_iso(),
        "release_edition": _safe_dimension(release_edition),
        "observation_file": str(path),
        "observation_file_exists": path.exists(),
        "event_count": event_count,
        "malformed_count": malformed_count,
        "unknown_schema_count": unknown_schema_count,
        "invalid_kind_count": invalid_kind_count,
        "counts_by_kind": counts_by_kind,
        "counts_by_surface": dict(sorted(counts_by_surface.items())),
        "evidence_valid": evidence_valid,
        "zero_legacy_loop_invocations": (
            evidence_valid and counts_by_kind["legacy_loop_invoked"] == 0
        ),
        "zero_legacy_runtime_adapter_selections": (
            evidence_valid
            and counts_by_kind["legacy_runtime_adapter_selected"] == 0
        ),
        "construction_only": (
            evidence_valid
            and counts_by_kind["legacy_agent_constructed"] > 0
            and counts_by_kind["legacy_loop_invoked"] == 0
            and counts_by_kind["legacy_runtime_adapter_selected"] == 0
        ),
    }


def summarize_legacy_runtime_boundary_observations(
    execution_observation_file: str | Path,
    writer_observation_file: str | Path,
    *,
    release_edition: str,
) -> dict[str, Any]:
    """Combine execution and writer evidence without conflating their meaning."""

    from .runtime_legacy_writers import (  # Avoid broad import-time coupling.
        summarize_legacy_runtime_writer_observations,
    )

    execution = summarize_legacy_runtime_execution_observations(
        execution_observation_file,
        release_edition=release_edition,
    )
    writer = summarize_legacy_runtime_writer_observations(
        writer_observation_file,
        release_edition=release_edition,
    )
    writer_evidence_valid = (
        writer["malformed_count"] == 0
        and writer["unknown_schema_count"] == 0
        and not writer["invalid_writer_ids"]
    )
    counts_by_kind = {
        **execution["counts_by_kind"],
        "legacy_writer_attempted": writer["event_count"],
    }
    evidence_valid = execution["evidence_valid"] and writer_evidence_valid
    forbidden_activity_count = (
        counts_by_kind["legacy_loop_invoked"]
        + counts_by_kind["legacy_runtime_adapter_selected"]
        + counts_by_kind["legacy_writer_attempted"]
    )
    observation_files_exist = (
        execution["observation_file_exists"]
        and writer["observation_file_exists"]
    )
    return {
        "schema": LEGACY_RUNTIME_BOUNDARY_OBSERVATION_SUMMARY_SCHEMA,
        "generated_at": _utc_now_iso(),
        "release_edition": _safe_dimension(release_edition),
        "counts_by_kind": counts_by_kind,
        "forbidden_activity_count": forbidden_activity_count,
        "evidence_valid": evidence_valid,
        "observation_files_exist": observation_files_exist,
        "zero_forbidden_legacy_activity": (
            evidence_valid
            and observation_files_exist
            and forbidden_activity_count == 0
        ),
        "construction_only": (
            evidence_valid
            and observation_files_exist
            and counts_by_kind["legacy_agent_constructed"] > 0
            and forbidden_activity_count == 0
        ),
        "execution_observation": execution,
        "writer_observation": writer,
    }
