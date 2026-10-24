# -*- coding: utf-8 -*-
"""RUN-05: a lost final response must not publish a PPTX twice."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lantai.presentation.jobs import PresentationJobStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_delivery_receipt_worker.py"
)
CONVERSATION_ID = "conversation-run05-lost-final-response"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_snapshot(root: Path) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        metadata = path.lstat()
        if path.is_symlink():
            snapshot[relative] = {
                "kind": "symlink",
                "target": os.readlink(path),
            }
        elif path.is_file():
            snapshot[relative] = {
                "kind": "file",
                "sha256": _sha256(path),
                "size": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
        elif path.is_dir():
            snapshot[relative] = {
                "kind": "directory",
                "mtime_ns": metadata.st_mtime_ns,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
    return snapshot


def _worker_command(
    *,
    mode: str,
    agent_home: Path,
    state_root: Path,
    project_root: Path,
    marker: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(WORKER_SCRIPT),
        "--mode",
        mode,
        "--agent-home",
        str(agent_home),
        "--state-root",
        str(state_root),
        "--project-root",
        str(project_root),
        "--conversation-id",
        CONVERSATION_ID,
    ]
    if mode == "prepare":
        command.extend(("--marker", str(marker)))
    return command


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


def _wait_for_marker(
    marker: Path,
    worker: subprocess.Popen[str],
    *,
    timeout: float = 30,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.is_file():
            return json.loads(marker.read_text(encoding="utf-8"))
        if worker.poll() is not None:
            stdout, stderr = worker.communicate(timeout=2)
            raise AssertionError(
                "delivery worker exited before preparing the final response; "
                f"exit={worker.returncode}; stdout={stdout!r}; stderr={stderr!r}"
            )
        time.sleep(0.02)
    raise AssertionError("timed out waiting for durable delivery marker")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
def test_lost_final_delivery_response_reuses_one_durable_publication(
    tmp_path: Path,
) -> None:
    agent_home = tmp_path / "agent-home"
    state_root = tmp_path / "state"
    project_root = tmp_path / "workspace"
    marker = tmp_path / "delivery-response-prepared.json"
    for path in (agent_home, state_root, project_root):
        path.mkdir()

    worker = subprocess.Popen(
        _worker_command(
            mode="prepare",
            agent_home=agent_home,
            state_root=state_root,
            project_root=project_root,
            marker=marker,
        ),
        cwd=PROJECT_ROOT,
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        prepared = _wait_for_marker(marker, worker)
        assert prepared["response_prepared"] is True
        assert prepared["file_block_count"] == 1
        os.killpg(worker.pid, signal.SIGKILL)
        assert worker.wait(timeout=5) == -signal.SIGKILL
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=5)

    first_stdout, first_stderr = worker.communicate(timeout=2)
    assert first_stdout.strip() == "", first_stdout
    assert "Traceback" not in first_stderr

    destination = Path(str(prepared["destination"]))
    receipt_path = Path(str(prepared["receipt_path"]))
    assert destination.is_file()
    assert receipt_path.is_file()
    assert _sha256(destination) == prepared["destination_sha256"]
    assert _sha256(receipt_path) == prepared["receipt_sha256"]

    state_before_retry = _tree_snapshot(state_root)
    project_before_retry = _tree_snapshot(project_root)
    resumed = subprocess.run(
        _worker_command(
            mode="resume",
            agent_home=agent_home,
            state_root=state_root,
            project_root=project_root,
            marker=marker,
        ),
        cwd=PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert resumed.returncode == 0, (
        f"stdout={resumed.stdout!r}; stderr={resumed.stderr!r}"
    )
    retry_result = json.loads(resumed.stdout.strip().splitlines()[-1])

    assert retry_result["is_error"] is False
    assert retry_result["job_id"] == prepared["job_id"]
    assert retry_result["reused"] is True
    assert retry_result["file_block_count"] == 1
    assert retry_result["verification_receipt"] == str(receipt_path)
    assert retry_result["output_path"] == str(destination)
    assert retry_result["forbidden_calls"] == {}
    assert _tree_snapshot(state_root) == state_before_retry
    assert _tree_snapshot(project_root) == project_before_retry

    receipts = list(
        (state_root / "presentation-tool-receipts").glob("*.json")
    )
    records = PresentationJobStore(
        state_root / "presentation-jobs",
        read_only=True,
    ).list_records(CONVERSATION_ID)
    assert receipts == [receipt_path]
    assert len(records) == 1
    assert records[0].job_id == prepared["job_id"]

    print(
        json.dumps(
            {
                "killed_after_response_prepared": True,
                "first_response_observed_by_parent": False,
                "retry_file_blocks": 1,
                "durable_delivery_count": len(receipts),
                "job_count": len(records),
                "job_id_reused": True,
                "generation_service_calls_on_retry": 0,
                "copy_calls_on_retry": 0,
                "state_tree_unchanged_on_retry": True,
                "project_tree_unchanged_on_retry": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
