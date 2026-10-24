#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail closed unless the exact official app has auditable P0 soak evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import plistlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = "com.lantai.agent-reliability-release-evidence.v2"
REQUIRED_FAULT_SCENARIOS = {
    "network_disconnect",
    "chat_switch",
    "renderer_reload",
    "app_restart",
    "tool_process_death",
    "approval_interruption",
}


def _records(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    section = payload.get(key)
    if not isinstance(section, dict) or not isinstance(section.get("records"), list):
        return []
    return [item for item in section["records"] if isinstance(item, dict)]


def _number(payload: dict[str, Any], key: str, default: float = -1) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def _executable_for_app(app_path: Path) -> Path:
    info_path = app_path / "Contents" / "Info.plist"
    if not info_path.is_file():
        raise ValueError(f"official app has no Info.plist: {info_path}")
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    executable_name = str(info.get("CFBundleExecutable") or "").strip()
    if not executable_name:
        raise ValueError("official app Info.plist has no CFBundleExecutable")
    executable = app_path / "Contents" / "MacOS" / executable_name
    if not executable.is_file():
        raise ValueError(f"official app executable is missing: {executable}")
    return executable


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_error(record: dict[str, Any], label: str) -> str | None:
    artifact = record.get("artifact")
    if not isinstance(artifact, dict):
        return f"{label} has no artifact proof"
    raw_path = str(artifact.get("path") or "").strip()
    recorded_digest = str(artifact.get("sha256") or "").strip().lower()
    if not raw_path or not recorded_digest:
        return f"{label} artifact proof is incomplete"
    path = Path(raw_path).expanduser()
    if not path.is_file() or path.is_symlink():
        return f"{label} artifact is missing or unsafe"
    if _sha256(path) != recorded_digest:
        return f"{label} artifact digest does not match"
    return None


def _audit_light(payload: dict[str, Any], errors: list[str]) -> None:
    records = _records(payload, "light")
    if len(records) < 30:
        errors.append("Light soak requires at least 30 auditable records")
        return
    durations: list[float] = []
    tool_calls: list[float] = []
    tokens: list[float] = []
    for index, record in enumerate(records, 1):
        label = f"Light record {index}"
        if record.get("status") != "verified":
            errors.append(f"{label} is not verified")
        if record.get("false_completion") is not False:
            errors.append(f"{label} has an unknown or false completion state")
        artifact_error = _artifact_error(record, label)
        if artifact_error:
            errors.append(artifact_error)
        durations.append(_number(record, "duration_seconds"))
        tool_calls.append(_number(record, "tool_calls"))
        tokens.append(_number(record, "tokens"))
    if _percentile(durations, 0.95) < 0 or _percentile(durations, 0.95) > 600:
        errors.append("Light P95 duration must be <= 10 minutes")
    if _percentile(tool_calls, 0.95) < 0 or _percentile(tool_calls, 0.95) > 20:
        errors.append("Light P95 tool calls must be <= 20")
    if _percentile(tokens, 0.95) < 0 or _percentile(tokens, 0.95) > 100_000:
        errors.append("Light P95 tokens must be <= 100000")


def _audit_standard(payload: dict[str, Any], errors: list[str]) -> None:
    records = _records(payload, "standard")
    if len(records) < 20:
        errors.append("Standard soak requires at least 20 auditable records")
        return
    successes = 0
    for index, record in enumerate(records, 1):
        label = f"Standard record {index}"
        if record.get("status") == "verified":
            successes += 1
        elif record.get("failure_attributed") is not True:
            errors.append(f"{label} failed without an attributed reason")
        if record.get("false_completion") is not False:
            errors.append(f"{label} has an unknown or false completion state")
        if record.get("artifacts_preserved") is not True:
            errors.append(f"{label} did not preserve partial artifacts")
        artifact_error = _artifact_error(record, label)
        if artifact_error:
            errors.append(artifact_error)
    if successes / len(records) < 0.95:
        errors.append("Standard success rate must be >= 0.95")


def _audit_faults(payload: dict[str, Any], errors: list[str]) -> None:
    records = _records(payload, "fault_injection")
    if len(records) < 20:
        errors.append("Fault injection requires at least 20 auditable records")
    scenarios = {str(record.get("scenario") or "") for record in records}
    missing = sorted(REQUIRED_FAULT_SCENARIOS - scenarios)
    if missing:
        errors.append("Fault scenarios missing: " + ", ".join(missing))
    for index, record in enumerate(records, 1):
        label = f"Fault record {index}"
        if record.get("passed") is not True:
            errors.append(f"{label} did not pass")
        if not str(record.get("observed_failure") or "").strip():
            errors.append(f"{label} did not record the injected failure")
        if not str(record.get("recovery_state") or "").strip():
            errors.append(f"{label} did not record recovery state")
        artifact_error = _artifact_error(record, label)
        if artifact_error:
            errors.append(artifact_error)


def _audit_long_runs(payload: dict[str, Any], errors: list[str]) -> None:
    records = _records(payload, "long_runs")
    if len(records) < 2:
        errors.append("At least two sustained run records are required")
        return
    for index, record in enumerate(records, 1):
        label = f"Sustained run {index}"
        started = _iso_timestamp(record.get("started_at"))
        completed = _iso_timestamp(record.get("completed_at"))
        recorded_duration = _number(record, "duration_seconds")
        if started is None or completed is None or completed <= started:
            errors.append(f"{label} has invalid timestamps")
        else:
            actual_duration = (completed - started).total_seconds()
            if actual_duration < 4 * 60 * 60:
                errors.append(f"{label} did not reach four hours")
            if recorded_duration < 4 * 60 * 60 or abs(actual_duration - recorded_duration) > 5:
                errors.append(f"{label} duration does not match timestamps")
        samples = record.get("progress_samples")
        if not isinstance(samples, list) or len(samples) < 25:
            errors.append(f"{label} requires at least 25 progress samples")
        else:
            sequences = [sample.get("sequence") for sample in samples if isinstance(sample, dict)]
            if sequences != list(range(1, len(samples) + 1)):
                errors.append(f"{label} progress sequence is incomplete")
            sample_times = [
                _iso_timestamp(sample.get("observed_at"))
                for sample in samples
                if isinstance(sample, dict)
            ]
            if len(sample_times) != len(samples) or any(item is None for item in sample_times):
                errors.append(f"{label} progress timestamps are invalid")
            elif any(
                current <= previous
                for previous, current in zip(sample_times, sample_times[1:])
            ):
                errors.append(f"{label} progress timestamps are not monotonic")
        for key in ("progress_visible", "recovery_passed", "stop_passed", "artifacts_preserved"):
            if record.get(key) is not True:
                errors.append(f"{label} {key} must be true")
        artifact_error = _artifact_error(record, label)
        if artifact_error:
            errors.append(artifact_error)


def _audit_effects(payload: dict[str, Any], errors: list[str]) -> None:
    records = _records(payload, "effects")
    if not records:
        errors.append("Effect settlement evidence has no terminal run records")
        return
    for index, record in enumerate(records, 1):
        started = {str(item) for item in record.get("started_effect_ids") or ()}
        settled = {str(item) for item in record.get("settled_effect_ids") or ()}
        replayed = {str(item) for item in record.get("ambiguous_replayed_effect_ids") or ()}
        if not started or settled != started:
            errors.append(f"Effect record {index} has unsettled effects")
        if replayed:
            errors.append(f"Effect record {index} blindly replayed ambiguous effects")


def _audit_works(payload: dict[str, Any], errors: list[str]) -> None:
    records = _records(payload, "works")
    eligible = [record for record in records if record.get("eligible") is True]
    if not eligible:
        errors.append("Works evidence has no eligible deliverables")
        return
    for index, record in enumerate(eligible, 1):
        if record.get("registered") is not True:
            errors.append(f"Works record {index} was not registered")
        if _number(record, "registration_count") != 1:
            errors.append(f"Works record {index} was registered more or less than once")
        if record.get("source_preserved") is not True:
            errors.append(f"Works record {index} damaged its source artifact")
        artifact_error = _artifact_error(record, f"Works record {index}")
        if artifact_error:
            errors.append(artifact_error)


def _audit_ui(payload: dict[str, Any], errors: list[str]) -> None:
    records = _records(payload, "ui_consistency")
    if not records:
        errors.append("UI consistency evidence has no terminal records")
        return
    for index, record in enumerate(records, 1):
        convergence = _number(record, "convergence_seconds")
        if convergence < 0 or convergence > 2:
            errors.append(f"UI record {index} did not converge within two seconds")
        authority = str(record.get("authority_state") or "")
        surfaces = record.get("surfaces")
        if not authority or not isinstance(surfaces, dict) or len(surfaces) < 2:
            errors.append(f"UI record {index} lacks independent surface states")
        elif any(str(value) != authority for value in surfaces.values()):
            errors.append(f"UI record {index} surfaces disagree with authority")


def audit_evidence(payload: dict[str, Any], app_path: Path) -> list[str]:
    errors: list[str] = []
    if payload.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    _audit_light(payload, errors)
    _audit_standard(payload, errors)
    _audit_faults(payload, errors)
    _audit_long_runs(payload, errors)
    _audit_effects(payload, errors)
    _audit_works(payload, errors)
    _audit_ui(payload, errors)

    package = dict(payload.get("package") or {})
    try:
        actual_app = app_path.expanduser().resolve(strict=True)
        executable = _executable_for_app(actual_app)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        return errors
    recorded_app = str(package.get("app_path") or "").strip()
    try:
        recorded_resolved = Path(recorded_app).expanduser().resolve(strict=True)
    except OSError:
        recorded_resolved = None
    if recorded_resolved != actual_app:
        errors.append("Reliability evidence was not produced from this .app")
    if package.get("distribution") != "official":
        errors.append("Reliability evidence must target the official distribution")
    if package.get("packaged_app_tested") is not True:
        errors.append("The packaged .app must be the object under test")
    recorded_digest = str(package.get("executable_sha256") or "").lower()
    if recorded_digest != _sha256(executable):
        errors.append("Packaged executable digest does not match reliability evidence")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--official-app", required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    evidence_path = Path(args.evidence).expanduser()
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors = [f"cannot read reliability evidence: {exc}"]
    else:
        errors = audit_evidence(payload, Path(args.official_app))
    result = {
        "schema": SCHEMA,
        "passed": not errors,
        "evidence": str(evidence_path),
        "official_app": str(Path(args.official_app).expanduser()),
        "errors": errors,
    }
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif errors:
        print("Agent reliability release gate failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
    else:
        print("Agent reliability release gate passed.")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
