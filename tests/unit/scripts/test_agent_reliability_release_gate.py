# -*- coding: utf-8 -*-
"""Tests for the auditable official Agent reliability release gate."""

from __future__ import annotations

import hashlib
import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.check_agent_reliability_release_gate import SCHEMA, audit_evidence


FAULTS = [
    "network_disconnect",
    "chat_switch",
    "renderer_reload",
    "app_restart",
    "tool_process_death",
    "approval_interruption",
]


def _official_app(tmp_path: Path) -> tuple[Path, str]:
    app = tmp_path / "澜台.app"
    contents = app / "Contents"
    executable = contents / "MacOS" / "澜台"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"official packaged app")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleExecutable": "澜台"}, handle)
    return app, hashlib.sha256(executable.read_bytes()).hexdigest()


def _artifact(tmp_path: Path, name: str) -> dict[str, str]:
    path = tmp_path / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _passing_evidence(tmp_path: Path, app: Path, digest: str) -> dict:
    started = datetime(2026, 8, 13, tzinfo=timezone.utc)
    light = [
        {
            "run_id": f"light-{index}",
            "status": "verified",
            "false_completion": False,
            "duration_seconds": 2,
            "tool_calls": 0,
            "tokens": 2,
            "artifact": _artifact(tmp_path, f"light-{index}.txt"),
        }
        for index in range(30)
    ]
    standard = [
        {
            "run_id": f"standard-{index}",
            "status": "verified",
            "failure_attributed": True,
            "false_completion": False,
            "artifacts_preserved": True,
            "artifact": _artifact(tmp_path, f"standard-{index}.txt"),
        }
        for index in range(20)
    ]
    fault_records = [
        {
            "run_id": f"fault-{index}",
            "scenario": FAULTS[index % len(FAULTS)],
            "passed": True,
            "observed_failure": "injected",
            "recovery_state": "verified_terminal",
            "artifact": _artifact(tmp_path, f"fault-{index}.txt"),
        }
        for index in range(20)
    ]
    long_runs = []
    for index in range(2):
        run_started = started + timedelta(days=index)
        run_completed = run_started + timedelta(hours=4)
        long_runs.append(
            {
                "run_id": f"sustained-{index}",
                "started_at": run_started.isoformat(),
                "completed_at": run_completed.isoformat(),
                "duration_seconds": 14_400,
                "progress_visible": True,
                "recovery_passed": True,
                "stop_passed": True,
                "artifacts_preserved": True,
                "artifact": _artifact(tmp_path, f"sustained-{index}.txt"),
                "progress_samples": [
                    {
                        "sequence": sequence + 1,
                        "observed_at": (
                            run_started + timedelta(minutes=10 * sequence)
                        ).isoformat(),
                    }
                    for sequence in range(25)
                ],
            }
        )
    effects = [
        {
            "run_id": "effects-1",
            "started_effect_ids": ["effect-1", "effect-2"],
            "settled_effect_ids": ["effect-1", "effect-2"],
            "ambiguous_replayed_effect_ids": [],
        }
    ]
    works = [
        {
            "run_id": f"work-{index}",
            "eligible": True,
            "registered": True,
            "registration_count": 1,
            "source_preserved": True,
            "artifact": _artifact(tmp_path, f"work-{index}.txt"),
        }
        for index in range(20)
    ]
    ui = [
        {
            "run_id": f"ui-{index}",
            "authority_state": "completed",
            "convergence_seconds": 0.5,
            "surfaces": {"chat": "completed", "task_center": "completed"},
        }
        for index in range(20)
    ]
    return {
        "schema": SCHEMA,
        "light": {"records": light},
        "standard": {"records": standard},
        "fault_injection": {"records": fault_records},
        "long_runs": {"records": long_runs},
        "effects": {"records": effects},
        "works": {"records": works},
        "ui_consistency": {"records": ui},
        "package": {
            "app_path": str(app),
            "distribution": "official",
            "packaged_app_tested": True,
            "executable_sha256": digest,
        },
    }


def test_complete_exact_package_evidence_passes(tmp_path: Path) -> None:
    app, digest = _official_app(tmp_path)

    assert audit_evidence(_passing_evidence(tmp_path, app, digest), app) == []


def test_summary_only_evidence_cannot_fake_a_pass(tmp_path: Path) -> None:
    app, digest = _official_app(tmp_path)
    payload = _passing_evidence(tmp_path, app, digest)
    payload["light"] = {"runs": 30, "verified_rate": 1.0}
    payload["standard"] = {"runs": 20, "success_rate": 1.0}

    errors = audit_evidence(payload, app)

    assert "Light soak requires at least 30 auditable records" in errors
    assert "Standard soak requires at least 20 auditable records" in errors


def test_false_completion_or_different_package_fails_closed(tmp_path: Path) -> None:
    app, digest = _official_app(tmp_path)
    payload = _passing_evidence(tmp_path, app, digest)
    payload["light"]["records"][0]["false_completion"] = True
    payload["package"]["executable_sha256"] = "0" * 64

    errors = audit_evidence(payload, app)

    assert "Light record 1 has an unknown or false completion state" in errors
    assert "Packaged executable digest does not match reliability evidence" in errors


def test_short_or_sparse_long_run_fails_closed(tmp_path: Path) -> None:
    app, digest = _official_app(tmp_path)
    payload = _passing_evidence(tmp_path, app, digest)
    record = payload["long_runs"]["records"][0]
    record["completed_at"] = record["started_at"]
    record["duration_seconds"] = 0
    record["progress_samples"] = record["progress_samples"][:2]

    errors = audit_evidence(payload, app)

    assert "Sustained run 1 has invalid timestamps" in errors
    assert "Sustained run 1 requires at least 25 progress samples" in errors


def test_changed_artifact_or_disagreeing_ui_fails_closed(tmp_path: Path) -> None:
    app, digest = _official_app(tmp_path)
    payload = _passing_evidence(tmp_path, app, digest)
    artifact = Path(payload["light"]["records"][0]["artifact"]["path"])
    artifact.write_text("changed", encoding="utf-8")
    payload["ui_consistency"]["records"][0]["surfaces"]["chat"] = "running"

    errors = audit_evidence(payload, app)

    assert "Light record 1 artifact digest does not match" in errors
    assert "UI record 1 surfaces disagree with authority" in errors
