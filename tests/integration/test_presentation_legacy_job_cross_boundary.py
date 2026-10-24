# -*- coding: utf-8 -*-
"""REC-06: migrate and resume one v3 job across real process boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lantai.presentation.jobs import PresentationJobStore
from lantai.presentation.models import PresentationJobRequest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = PROJECT_ROOT / "tests" / "support" / "presentation_legacy_job_writer.py"
HOLDER_RUN_ID = "run-current-native-rec06"
CONTENDER_RUN_ID = "run-competing-native-rec06"
_V1_FIELDS = {
    "schema_version",
    "job_id",
    "conversation_id",
    "request",
    "skill_identity",
    "stage",
    "status",
    "last_sequence",
    "checkpoints",
    "events",
    "created_at",
    "updated_at",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_for_json(
    path: Path,
    process: subprocess.Popen[str],
    *,
    timeout: float = 10,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise AssertionError(
                f"REC-06 worker exited before {path.name}; "
                f"exit={process.returncode}; stdout={stdout!r}; stderr={stderr!r}"
            )
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for REC-06 marker: {path}")


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _worker_command(
    *,
    mode: str,
    root: Path,
    job_id: str,
    runtime_run_id: str,
    result: Path,
    release: Path | None = None,
    finished: Path | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(WORKER_SCRIPT),
        "--mode",
        mode,
        "--root",
        str(root),
        "--job-id",
        job_id,
        "--runtime-run-id",
        runtime_run_id,
        "--result",
        str(result),
    ]
    if release is not None:
        command.extend(("--release", str(release)))
    if finished is not None:
        command.extend(("--finished", str(finished)))
    return command


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX flock semantics")
def test_v1_manifest_resumes_v3_job_with_one_cross_process_writer(
    tmp_path: Path,
) -> None:
    jobs_root = tmp_path / "presentation-jobs"
    store = PresentationJobStore(jobs_root)
    record = store.create(
        PresentationJobRequest(
            conversation_id="conversation-rec06",
            user_request="resume the existing v3 presentation job",
            slide_count=19,
        )
    )
    job_dir = store.job_dir(record.job_id)
    manifest_path = job_dir / "manifest.json"

    old_output = job_dir / "output" / "presentation.pptx"
    old_version = job_dir / "versions" / "legacy-approved.pptx"
    old_checkpoint = job_dir / "checkpoints" / "deck-ir.json"
    old_output.write_bytes(b"REC-06 legacy output bytes\n")
    old_version.write_bytes(b"REC-06 legacy approved version bytes\n")
    old_checkpoint.write_text(
        json.dumps(
            {"pipeline_version": "lantai-presentation-v3", "slides": 19},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    preserved_hashes = {
        "output": _sha256(old_output),
        "version": _sha256(old_version),
        "checkpoint": _sha256(old_checkpoint),
    }

    current = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy = {key: value for key, value in current.items() if key in _V1_FIELDS}
    legacy.update(
        {
            "schema_version": "1.0",
            "stage": "deck_ir",
            "status": "running",
            "checkpoints": {"deck_ir": "checkpoints/deck-ir.json"},
        }
    )
    manifest_path.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    legacy_manifest_bytes = manifest_path.read_bytes()
    assert PresentationJobStore(jobs_root).get(record.job_id).pipeline_version == (
        "lantai-presentation-v3"
    )
    assert manifest_path.read_bytes() == legacy_manifest_bytes

    acquired_marker = tmp_path / "holder-acquired.json"
    finished_marker = tmp_path / "holder-finished.json"
    release_marker = tmp_path / "allow-resume"
    contender_marker = tmp_path / "contender.json"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (str(PROJECT_ROOT), environment.get("PYTHONPATH", ""))
        if part
    )
    holder = subprocess.Popen(
        _worker_command(
            mode="hold-and-resume",
            root=jobs_root,
            job_id=record.job_id,
            runtime_run_id=HOLDER_RUN_ID,
            result=acquired_marker,
            release=release_marker,
            finished=finished_marker,
        ),
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        acquired = _wait_for_json(acquired_marker, holder)
        assert acquired["phase"] == "lease_acquired"
        assert acquired["runtime_run_id"] == HOLDER_RUN_ID
        assert acquired["schema_version"] == "2.0"
        assert acquired["generation"] == 1
        assert acquired["recovered_lease_id"] is None

        contender = subprocess.run(
            _worker_command(
                mode="contend",
                root=jobs_root,
                job_id=record.job_id,
                runtime_run_id=CONTENDER_RUN_ID,
                result=contender_marker,
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert contender.returncode == 0, (
            f"stdout={contender.stdout!r}; stderr={contender.stderr!r}"
        )
        contender_result = json.loads(contender_marker.read_text(encoding="utf-8"))
        assert contender_result == {
            "phase": "writer_busy",
            "acquired": False,
            "job_id": record.job_id,
            "runtime_run_id": CONTENDER_RUN_ID,
            "error_type": "PresentationWriterBusyError",
            "error": "presentation job already has an authoritative writer",
            "pid": contender_result["pid"],
        }
        manifest_while_held = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest_while_held["runtime_run_id"] == HOLDER_RUN_ID
        assert CONTENDER_RUN_ID not in manifest_path.read_text(encoding="utf-8")

        release_marker.write_text("resume\n", encoding="utf-8")
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, f"stdout={stdout!r}; stderr={stderr!r}"
        finished = json.loads(finished_marker.read_text(encoding="utf-8"))
        assert finished["phase"] == "lease_released"
        assert finished["active_lease_id"] is None
        assert finished["checkpoint"] == "checkpoints/rec06-resumed-quality.json"
    finally:
        _terminate_process_group(holder)

    migrated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == "2.0"
    assert migrated["job_id"] == record.job_id
    assert migrated["conversation_id"] == "conversation-rec06"
    assert migrated["runtime_run_id"] == HOLDER_RUN_ID
    assert migrated["pipeline_version"] == "lantai-presentation-v3"
    assert migrated["generation"] == 1
    assert migrated["attempt_count"] == 1
    assert migrated["active_lease_id"] is None
    assert migrated["status"] == "running"
    assert migrated["stage"] == "quality"
    assert migrated["checkpoints"] == {
        "deck_ir": "checkpoints/deck-ir.json",
        "quality": "checkpoints/rec06-resumed-quality.json",
    }
    assert CONTENDER_RUN_ID not in json.dumps(migrated, ensure_ascii=False)
    assert not (job_dir / "lease.json").exists()

    resumed_checkpoint = job_dir / "checkpoints" / "rec06-resumed-quality.json"
    resumed_payload = json.loads(resumed_checkpoint.read_text(encoding="utf-8"))
    assert resumed_payload["job_id"] == record.job_id
    assert resumed_payload["runtime_run_id"] == HOLDER_RUN_ID
    assert resumed_payload["resumed_from"] == "checkpoints/deck-ir.json"
    assert resumed_payload["writer_generation"] == 1

    assert _sha256(old_output) == preserved_hashes["output"]
    assert _sha256(old_version) == preserved_hashes["version"]
    assert _sha256(old_checkpoint) == preserved_hashes["checkpoint"]
    assert list(jobs_root.glob("presentation-*")) == [job_dir]
    assert list(jobs_root.glob("presentation-*/manifest.json")) == [manifest_path]
