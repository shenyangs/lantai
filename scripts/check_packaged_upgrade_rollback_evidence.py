#!/usr/bin/env python3
"""Fail closed unless upgrade/rollback evidence binds two exact packaged apps."""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import sys
from pathlib import Path
from typing import Any


SCHEMA = "com.lantai.packaged-app-upgrade-rollback-smoke.v1"
EXPECTED_PHASES = (
    "previous_seed",
    "upgrade_current",
    "rollback_previous",
    "reupgrade_current",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _app_identity(app: Path) -> tuple[Path, str, str]:
    actual = app.expanduser().resolve(strict=True)
    with (actual / "Contents/Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    executable = actual / "Contents/MacOS" / str(info.get("CFBundleExecutable") or "")
    if not executable.is_file() or executable.is_symlink():
        raise ValueError(f"app executable is missing or unsafe: {executable}")
    return actual, str(info.get("CFBundleShortVersionString") or ""), _sha256(executable)


def _recorded_digest(app_record: Any) -> str:
    if not isinstance(app_record, dict):
        return ""
    digests = app_record.get("artifact_sha256")
    return str((digests or {}).get("main_executable") or "").removeprefix("sha256:")


def audit(payload: dict[str, Any], previous_app: Path, current_app: Path) -> list[str]:
    errors: list[str] = []
    if payload.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if payload.get("passed") is not True or payload.get("failure") is not None:
        errors.append("upgrade/rollback smoke did not pass")
    try:
        previous_path, previous_version, previous_digest = _app_identity(previous_app)
        current_path, current_version, current_digest = _app_identity(current_app)
    except (OSError, ValueError) as exc:
        return [*errors, str(exc)]
    if previous_path == current_path or previous_digest == current_digest:
        errors.append("previous and current apps must be distinct package artifacts")
    if _recorded_digest(payload.get("previous_app")) != previous_digest:
        errors.append("previous app executable digest does not match evidence")
    if _recorded_digest(payload.get("current_app")) != current_digest:
        errors.append("current app executable digest does not match evidence")

    phases = payload.get("phases")
    if not isinstance(phases, list) or len(phases) != 4:
        errors.append("evidence must contain all four transition phases")
        return errors
    names = tuple(str(item.get("phase") or "") for item in phases if isinstance(item, dict))
    if names != EXPECTED_PHASES:
        errors.append("transition phases are missing or out of order")
    expected_versions = (previous_version, current_version, previous_version, current_version)
    expected_digests = (previous_digest, current_digest, previous_digest, current_digest)
    snapshots: list[dict[str, Any]] = []
    nonces: list[str] = []
    for index, (phase, version, digest) in enumerate(
        zip(phases, expected_versions, expected_digests), 1
    ):
        if not isinstance(phase, dict):
            errors.append(f"transition phase {index} is invalid")
            continue
        if phase.get("version") != version:
            errors.append(f"transition phase {index} version does not match package")
        if phase.get("executable_sha256") != digest:
            errors.append(f"transition phase {index} digest does not match package")
        snapshot = phase.get("snapshot")
        if not isinstance(snapshot, dict):
            errors.append(f"transition phase {index} has no durable state snapshot")
            continue
        snapshots.append(snapshot)
        nonce = str(phase.get("desktop_instance_nonce") or "")
        if not nonce:
            errors.append(f"transition phase {index} has no desktop instance nonce")
        nonces.append(nonce)
    if len(set(nonces)) != 4:
        errors.append("each transition phase must be a distinct desktop instance")
    if snapshots and any(item != snapshots[0] for item in snapshots[1:]):
        errors.append("durable state changed across upgrade or rollback")
    if snapshots:
        snapshot = snapshots[0]
        required = {
            "profile_id": "profile_v1_upgrade_fixture",
            "profile_revision": 3,
            "active_profile_id": "profile_v1_upgrade_fixture",
            "slot_1_profile_id": "profile_v1_upgrade_fixture",
            "customization_mode": "normal",
            "task_status": "completed",
        }
        for key, expected in required.items():
            if snapshot.get(key) != expected:
                errors.append(f"durable snapshot {key} is not {expected!r}")
        effective = snapshot.get("effective_values")
        if effective != {
            "appearance.palette": "warm",
            "persona.presence": "quiet",
        }:
            errors.append("durable snapshot lost effective personalization values")
        if int(snapshot.get("chat_event_count") or 0) < 1:
            errors.append("durable snapshot has no persisted chat events")
        if "upgrade-rollback-task-sentinel" not in str(snapshot.get("task_stdout") or ""):
            errors.append("durable snapshot lost completed task output")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--previous-app", required=True)
    parser.add_argument("--current-app", required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("evidence root is not an object")
        errors = audit(payload, Path(args.previous_app), Path(args.current_app))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors = [f"cannot audit upgrade/rollback evidence: {exc}"]
    result = {"passed": not errors, "schema": SCHEMA, "errors": errors}
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif errors:
        print("Upgrade/rollback evidence gate failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
    else:
        print("Upgrade/rollback evidence gate passed.")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
