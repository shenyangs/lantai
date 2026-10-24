# -*- coding: utf-8 -*-
"""RUN-06: explicit Stop must survive real presentation worker boundaries."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lantai.presentation.jobs import cancel_active_job_for_runtime_run


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_explicit_stop_worker.py"
)
CONVERSATION_ID = "conversation-run06-explicit-stop"
RUNTIME_RUN_ID = "native-run-run06-explicit-stop"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (
            str(PROJECT_ROOT / "src"),
            str(PROJECT_ROOT),
            environment.get("PYTHONPATH", ""),
        )
        if part
    )
    return environment


def _compose_command(
    state_root: Path,
    *,
    stage: str,
    marker: Path,
    release: Path,
) -> list[str]:
    return [
        sys.executable,
        str(WORKER_SCRIPT),
        "--mode",
        "compose",
        "--state-root",
        str(state_root),
        "--stage",
        stage,
        "--marker",
        str(marker),
        "--release",
        str(release),
    ]


def _observe(state_root: Path, job_id: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(WORKER_SCRIPT),
            "--mode",
            "observe",
            "--state-root",
            str(state_root),
            "--job-id",
            job_id,
        ],
        cwd=PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _wait_for_marker(
    marker: Path,
    worker: subprocess.Popen[str],
    *,
    timeout: float = 20,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.is_file():
            return json.loads(marker.read_text(encoding="utf-8"))
        if worker.poll() is not None:
            stdout, stderr = worker.communicate(timeout=2)
            raise AssertionError(
                "RUN-06 worker exited before reaching its stop point; "
                f"exit={worker.returncode}; stdout={stdout!r}; stderr={stderr!r}"
            )
        time.sleep(0.02)
    raise AssertionError("timed out waiting for RUN-06 stage marker")


def _spawn(
    state_root: Path,
    *,
    stage: str,
    marker: Path,
    release: Path,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _compose_command(
            state_root,
            stage=stage,
            marker=marker,
            release=release,
        ),
        cwd=PROJECT_ROOT,
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


@pytest.mark.parametrize("blocked_stage", ["compile", "critic"])
def test_explicit_stop_blocks_every_post_stop_stage_and_delivery(
    tmp_path: Path,
    blocked_stage: str,
) -> None:
    state_root = tmp_path / blocked_stage / "state"
    state_root.mkdir(parents=True)
    marker = tmp_path / blocked_stage / "blocked.json"
    release = tmp_path / blocked_stage / "release"
    worker = _spawn(
        state_root,
        stage=blocked_stage,
        marker=marker,
        release=release,
    )
    try:
        blocked = _wait_for_marker(marker, worker)
        assert blocked["blocked_stage"] == blocked_stage
        assert blocked["pid"] == worker.pid
        assert cancel_active_job_for_runtime_run(
            state_root,
            RUNTIME_RUN_ID,
            str(blocked["job_id"]),
            conversation_id=CONVERSATION_ID,
        )
        release.write_text("continue to the next execution guard\n", encoding="utf-8")
        stdout, stderr = worker.communicate(timeout=15)
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=5)

    assert worker.returncode == 0, f"stdout={stdout!r}; stderr={stderr!r}"
    result = json.loads(stdout.strip().splitlines()[-1])
    assert result["interrupted"] is True
    assert result["status"] == "cancelled"
    assert result["stage"] == "cancelled"
    assert result["authoritative_output_exists"] is False
    assert result["candidate_count"] == 1
    assert result["completed_event_count"] == 0
    assert "completed" not in result["event_statuses"]
    if blocked_stage == "compile":
        assert "render" not in result["event_stages"]
        assert "quality" not in result["event_stages"]
    else:
        critic_metrics = [
            outcome
            for operation, outcome in zip(
                result["metric_operations"],
                result["metric_outcomes"],
                strict=True,
            )
            if operation == "critic"
        ]
        assert critic_metrics == ["cancelled"]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_explicit_stop_remains_terminal_after_worker_sigkill_and_restart(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "killed" / "state"
    state_root.mkdir(parents=True)
    marker = tmp_path / "killed" / "blocked.json"
    release = tmp_path / "killed" / "never-release"
    worker = _spawn(
        state_root,
        stage="compile",
        marker=marker,
        release=release,
    )
    try:
        blocked = _wait_for_marker(marker, worker)
        assert blocked["pid"] == worker.pid
        job_id = str(blocked["job_id"])
        assert cancel_active_job_for_runtime_run(
            state_root,
            RUNTIME_RUN_ID,
            job_id,
            conversation_id=CONVERSATION_ID,
        )
        os.killpg(worker.pid, signal.SIGKILL)
        assert worker.wait(timeout=5) == -signal.SIGKILL
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=5)

    observed = _observe(state_root, job_id)
    assert observed["status"] == "cancelled"
    assert observed["stage"] == "cancelled"
    assert observed["runtime_run_id"] == RUNTIME_RUN_ID
    assert observed["active_lease_id"]
    assert observed["authoritative_output_exists"] is False
    assert observed["candidate_count"] == 1
    assert observed["completed_event_count"] == 0
    assert "render" not in observed["event_stages"]
    assert "quality" not in observed["event_stages"]
    assert "completed" not in observed["event_statuses"]

    print(
        json.dumps(
            {
                "cancelled_stages_proved": ["compile", "critic"],
                "killed_pid": worker.pid,
                "killed_returncode": worker.returncode,
                "restart_observer_pid": observed["pid"],
                "runtime_run_id_preserved": True,
                "active_lease_id_preserved_after_kill": True,
                "authoritative_output_exists": False,
                "completed_event_count": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
