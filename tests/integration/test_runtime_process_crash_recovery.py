# -*- coding: utf-8 -*-
"""Real process-kill locks for Runtime effect recovery boundaries."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from urllib.request import urlopen
from uuid import uuid4

import pytest

from lantai.app.recovery_coordinator import RecoveryCoordinator
from lantai.app.runner.models import ChatSpec
from lantai.runtime.effects import RuntimeEffectJournal
from lantai.runtime.store import RuntimeRunStore


ROOT = Path(__file__).resolve().parents[2]
SERVICE_SCRIPT = ROOT / "tests" / "support" / "runtime_effect_service.py"
WORKER_SCRIPT = ROOT / "tests" / "support" / "runtime_crash_worker.py"


class _ChatManager:
    def __init__(self, run_id: str) -> None:
        self.chat = ChatSpec(
            id="chat-crash",
            session_id="console:chat-crash",
            user_id="user-crash",
            channel="console",
            meta={
                "last_turn_run_id": run_id,
                "last_turn_status": "running",
                "pending_turn": {"run_id": run_id, "status": "running"},
            },
        )

    async def list_chats(self):
        return [self.chat]

    async def merge_chat_meta(self, _chat_id, values, remove_keys=()):
        meta = dict(self.chat.meta)
        meta.update(values)
        for key in remove_keys:
            meta.pop(key, None)
        self.chat = self.chat.model_copy(update={"meta": meta})
        return self.chat


def _wait_for_json(
    path: Path,
    *,
    timeout: float = 10,
    process: subprocess.Popen[str] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            last_error = exc
            if process is not None and process.poll() is not None:
                output, error = process.communicate(timeout=2)
                raise AssertionError(
                    (
                        "process exited before durable JSON "
                        f"{path}"
                        "; exit="
                        f"{process.returncode}"
                        "; stdout="
                        f"{output!r}"
                        "; stderr="
                        f"{error!r}"
                    ),
                ) from exc
            time.sleep(0.02)
    raise AssertionError(
        f"timed out waiting for durable barrier {path}: {last_error}",
    )


def _python_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    entries = [str(ROOT / "src"), str(ROOT)]
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["LANTAI_WORKING_DIR"] = str(home / "lantai-state")
    return env


def _snapshot(service_url: str) -> dict[str, Any]:
    with urlopen(
        f"{service_url}/snapshot",
        timeout=5,
    ) as response:  # noqa: S310
        return json.loads(response.read())


# pylint: disable=R0915
@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGKILL semantics")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_effect_status", "expected_count", "expected_code"),
    [
        ("after_intent", "cancelled", 0, "recovery_explicit_resume_required"),
        (
            "during_external_response",
            "ambiguous",
            1,
            "recovery_effect_unknown",
        ),
        ("after_external", "ambiguous", 1, "recovery_effect_unknown"),
        ("after_commit", "committed", 1, "recovery_effect_unknown"),
    ],
)
async def test_process_kill_preserves_public_recovery_effect_boundary(
    tmp_path: Path,
    scenario: str,
    expected_effect_status: str,
    expected_count: int,
    expected_code: str,
) -> None:
    state_root = tmp_path / "runtime-state"
    service_state = tmp_path / "external-service.json"
    service_ready = tmp_path / "external-service-ready.json"
    barrier_file = tmp_path / "worker-barrier.json"
    nonce = uuid4().hex
    run_id = f"run-{scenario}-{nonce[:8]}"
    effect_id = f"effect-{scenario}-{nonce[:8]}"
    env = _python_env(tmp_path / "isolated-home")

    service = subprocess.Popen(
        [
            sys.executable,
            str(SERVICE_SCRIPT),
            "--state-file",
            str(service_state),
            "--ready-file",
            str(service_ready),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker: subprocess.Popen[str] | None = None
    try:
        ready = _wait_for_json(service_ready, process=service)
        service_url = str(ready["url"])
        assert int(ready["pid"]) == service.pid

        worker = subprocess.Popen(
            [
                sys.executable,
                str(WORKER_SCRIPT),
                "--scenario",
                scenario,
                "--state-root",
                str(state_root),
                "--barrier-file",
                str(barrier_file),
                "--service-url",
                service_url,
                "--run-id",
                run_id,
                "--effect-id",
                effect_id,
                "--nonce",
                nonce,
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            barrier = _wait_for_json(barrier_file)
        except AssertionError as exc:
            worker_output, worker_error = worker.communicate(timeout=2)
            raise AssertionError(
                f"{exc}; worker exit={worker.returncode}; "
                f"stdout={worker_output!r}; stderr={worker_error!r}",
            ) from exc
        barrier_worker_pid = int(
            barrier.get("pid") or barrier.get("target_worker_pid") or 0,
        )
        assert barrier_worker_pid == worker.pid
        assert barrier["nonce"] == nonce
        assert barrier["scenario"] == scenario
        assert service.poll() is None

        os.kill(worker.pid, signal.SIGKILL)
        assert worker.wait(timeout=5) == -signal.SIGKILL
        assert service.poll() is None
        before_recovery = _snapshot(service_url)
        assert len(before_recovery["mutations"]) == expected_count

        chat_manager = _ChatManager(run_id)
        workspace = SimpleNamespace(
            workspace_dir=state_root,
            agent_id="default",
            chat_manager=chat_manager,
        )
        report = await RecoveryCoordinator(workspace).recover()

        contract = await RuntimeRunStore(state_root).load_contract(run_id)
        assert contract is not None
        assert contract.status == "stalled"
        assert contract.error is not None
        assert contract.error.code == expected_code
        latest = RuntimeEffectJournal(state_root).latest_effect(
            run_id,
            contract.attempt_id,
            effect_id,
        )
        assert latest is not None
        assert latest.status == expected_effect_status
        assert report.scanned == 1
        assert report.stalled == 1

        after_recovery = _snapshot(service_url)
        assert after_recovery == before_recovery
        assert service.poll() is None
        print(
            json.dumps(
                {
                    "scenario": scenario,
                    "worker_pid": worker.pid,
                    "service_pid": service.pid,
                    "nonce": nonce,
                    "sigkill_exit": worker.returncode,
                    "external_count_before_recovery": expected_count,
                    "external_count_after_recovery": len(
                        after_recovery["mutations"],
                    ),
                    "effect_status_after_recovery": latest.status,
                    "run_error_code": contract.error.code,
                    "service_survived_worker_kill": service.poll() is None,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        service.terminate()
        try:
            service.wait(timeout=5)
        except subprocess.TimeoutExpired:
            service.kill()
            service.wait(timeout=5)


# pylint: enable=R0915
