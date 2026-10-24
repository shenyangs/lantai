# -*- coding: utf-8 -*-
"""A finite synthetic process that owns real presentation state."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import psutil
import pytest


def _load_driver_dependencies():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lifecycle_locks import test_whole_job_lifecycle as fixtures
    from lifecycle_locks.test_real_process_settlement import (
        RealProductionPath as ProcessPath,
    )

    from lantai.presentation.jobs import PresentationJobStore as JobStore
    from lantai.presentation.models import JobStage as Stage
    from lantai.presentation.models import JobStatus as Status

    return fixtures, ProcessPath, JobStore, Stage, Status


(
    base,
    RealProductionPath,
    PresentationJobStore,
    JobStage,
    JobStatus,
) = _load_driver_dependencies()

mode, root_arg, ready_arg = sys.argv[1:]
root, ready = Path(root_arg), Path(ready_arg)
release = root / "owner.release"
root.mkdir(parents=True, exist_ok=True)


def write_ready(data):
    ready.write_text(json.dumps(data), encoding="utf-8")


def wait_release():
    until = time.monotonic() + 60
    while not release.exists() and time.monotonic() < until:
        time.sleep(0.02)


if mode == "lease":
    store = PresentationJobStore(root / "jobs")
    record = store.create(base.request())
    with store.writer_lease(record.job_id) as lease:
        store.consume_cost(record.job_id, 7)
        store.append_event(
            record.job_id,
            stage=JobStage.PLAN,
            status=JobStatus.RUNNING,
            message="synthetic owner entered",
        )
        write_ready(
            {
                "jobs_root": str(store.root),
                "job_id": record.job_id,
                "conversation_id": record.conversation_id,
                "lease_id": lease.lease_id,
                "owner_pid": psutil.Process().pid,
                "deadline": lease.deadline_at.isoformat(),
                "cost": 7,
            },
        )
        wait_release()
elif mode == "native":
    monkeypatch = pytest.MonkeyPatch()
    path = RealProductionPath(root, monkeypatch, "native")
    try:
        path.start()
        write_ready(
            {
                "jobs_root": str(path.service.store.root),
                "job_id": path.record.job_id,
                "conversation_id": path.record.conversation_id,
                "lease_id": path.active_owner(),
                "owner_pid": psutil.Process().pid,
                "deadline": path.service.store.get(
                    path.record.job_id,
                ).deadline_at.isoformat(),
                "state": str(path.state),
                "script": str(path.script),
                "parent": path.parent,
                "child": path.child,
            },
        )
        wait_release()
    finally:
        path.close()
        monkeypatch.undo()
else:
    raise AssertionError("unexpected synthetic mode")
