from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from lantai.analysis import worker

import pytest

from lantai.analysis import AnalysisError
from lantai.analysis.worker import run_worker


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_restricted_analysis_runs_in_a_separate_process() -> None:
    blob = b"team,amount\nA,10.20\nA,2.30\n"
    imported = run_worker("ingest", blob=blob, suffix=".csv", sheet_name=None)
    assert imported["worker_pid"] != os.getpid()
    assert imported["columns"] == ["team", "amount"]
    assert imported["row_count"] == 2
    calculated = run_worker(
        "compute", blob=blob, suffix=".csv", sheet_name=None,
        plan={"group_by": ["team"], "metrics": [{"op": "sum", "column": "amount", "as": "total"}]},
    )
    assert calculated["worker_pid"] != os.getpid()
    assert calculated["rows"] == [{"team": "A", "total": "12.50"}]


def test_desktop_entry_routes_fixed_analysis_worker_before_backend_init() -> None:
    request = {
        "operation": "compute",
        "blob": base64.b64encode(b"team,amount\nA,10.20\nA,2.30\n").decode("ascii"),
        "suffix": ".csv",
        "sheet_name": None,
        "plan": {
            "group_by": ["team"],
            "metrics": [{"op": "sum", "column": "amount", "as": "total"}],
        },
    }
    child = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "electron" / "entry.py"), "--analysis-worker"],
        input=json.dumps(request).encode("utf-8"), capture_output=True, timeout=10, check=False,
    )
    assert child.returncode == 0, child.stderr.decode("utf-8", errors="replace")
    assert child.stderr == b""
    response = json.loads(child.stdout)
    assert response["ok"] is True
    assert response["value"]["rows"] == [{"team": "A", "total": "12.50"}]


def test_worker_refuses_other_operations() -> None:
    with pytest.raises(AnalysisError, match="operation"):
        run_worker("execute_python", blob=b"", suffix=".csv", sheet_name=None)


def test_worker_has_timeout_and_no_inherited_provider_secret(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-worker")
    monkeypatch.setattr(worker, "_command", lambda: [sys.executable, "-c", "import time; time.sleep(10)"])
    started = time.monotonic()
    with pytest.raises(AnalysisError, match="timed out"):
        run_worker("ingest", blob=b"a\n1\n", suffix=".csv", sheet_name=None, timeout_seconds=0.1)
    assert time.monotonic() - started < 2
    assert "OPENAI_API_KEY" not in worker._child_environment()
    assert worker._child_environment()["LANTAI_ANALYSIS_WORKER_CHILD"] == "1"


def test_worker_cancel_kills_child_before_return(monkeypatch) -> None:
    monkeypatch.setattr(worker, "_command", lambda: [sys.executable, "-c", "import time; time.sleep(10)"])
    cancel = threading.Event()
    outcome = []

    def execute() -> None:
        try:
            run_worker("ingest", blob=b"a\n1\n", suffix=".csv", sheet_name=None, cancel_event=cancel)
        except AnalysisError as exc:
            outcome.append(str(exc))

    thread = threading.Thread(target=execute)
    started = time.monotonic()
    thread.start()
    time.sleep(0.15)
    cancel.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert time.monotonic() - started < 2
    assert outcome and "cancel" in outcome[0]


def test_waiting_worker_can_be_cancelled_without_launch(monkeypatch) -> None:
    first_started = threading.Event()
    command_calls = []
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(worker, "_WORKER_SLOTS", slots)

    def command() -> list[str]:
        command_calls.append(True)
        first_started.set()
        return [sys.executable, "-c", "import time; time.sleep(0.5)"]

    monkeypatch.setattr(worker, "_command", command)
    first_error = []

    def first_run() -> None:
        try:
            run_worker("ingest", blob=b"a\n1\n", suffix=".csv", sheet_name=None, timeout_seconds=2)
        except AnalysisError as exc:
            first_error.append(str(exc))

    first = threading.Thread(target=first_run)
    first.start()
    assert first_started.wait(timeout=1)
    cancellation = threading.Event()
    second_error = []

    def second_run() -> None:
        try:
            run_worker("ingest", blob=b"a\n1\n", suffix=".csv", sheet_name=None, timeout_seconds=2, cancel_event=cancellation)
        except AnalysisError as exc:
            second_error.append(str(exc))

    second = threading.Thread(target=second_run)
    second.start()
    time.sleep(0.05)
    cancellation.set()
    second.join(timeout=1)
    first.join(timeout=3)
    assert not second.is_alive() and not first.is_alive()
    assert second_error and "cancelled" in second_error[0]
    assert first_error
    assert len(command_calls) == 1
