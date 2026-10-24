# -*- coding: utf-8 -*-
"""Independent writer processes for the REC-06 migration boundary."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from lantai.presentation.jobs import (
    PresentationJobStore,
    PresentationWriterBusyError,
)
from lantai.presentation.models import JobStage, JobStatus


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _hold_and_resume(args: argparse.Namespace) -> int:
    store = PresentationJobStore(args.root)
    with store.writer_lease(
        args.job_id,
        runtime_run_id=args.runtime_run_id,
    ) as lease:
        acquired = store.get(args.job_id)
        _atomic_json(
            args.result,
            {
                "phase": "lease_acquired",
                "job_id": args.job_id,
                "runtime_run_id": acquired.runtime_run_id,
                "schema_version": acquired.schema_version,
                "lease_id": lease.lease_id,
                "generation": lease.generation,
                "attempt_id": lease.attempt_id,
                "recovered_lease_id": lease.recovered_lease_id,
                "pid": os.getpid(),
            },
        )
        deadline = time.monotonic() + args.timeout
        while not args.release.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting to resume legacy job")
            time.sleep(0.02)

        checkpoint = store.write_checkpoint(
            args.job_id,
            "rec06-resumed-quality.json",
            json.dumps(
                {
                    "job_id": args.job_id,
                    "runtime_run_id": args.runtime_run_id,
                    "resumed_from": "checkpoints/deck-ir.json",
                    "writer_generation": lease.generation,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        store.append_event(
            args.job_id,
            stage=JobStage.QUALITY,
            status=JobStatus.RUNNING,
            message="REC-06 resumed the existing v3 job in its sole Native writer",
            checkpoint=f"checkpoints/{checkpoint.name}",
            recoverable=True,
        )

    released = store.get(args.job_id)
    _atomic_json(
        args.finished,
        {
            "phase": "lease_released",
            "job_id": args.job_id,
            "runtime_run_id": released.runtime_run_id,
            "schema_version": released.schema_version,
            "active_lease_id": released.active_lease_id,
            "generation": released.generation,
            "attempt_count": released.attempt_count,
            "checkpoint": released.checkpoints.get(JobStage.QUALITY.value),
            "pid": os.getpid(),
        },
    )
    return 0


def _contend(args: argparse.Namespace) -> int:
    store = PresentationJobStore(args.root)
    try:
        with store.writer_lease(
            args.job_id,
            runtime_run_id=args.runtime_run_id,
        ) as lease:
            _atomic_json(
                args.result,
                {
                    "phase": "unexpectedly_acquired",
                    "acquired": True,
                    "job_id": args.job_id,
                    "runtime_run_id": args.runtime_run_id,
                    "lease_id": lease.lease_id,
                    "pid": os.getpid(),
                },
            )
            return 3
    except PresentationWriterBusyError as exc:
        _atomic_json(
            args.result,
            {
                "phase": "writer_busy",
                "acquired": False,
                "job_id": args.job_id,
                "runtime_run_id": args.runtime_run_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "pid": os.getpid(),
            },
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("hold-and-resume", "contend"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--runtime-run-id", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--finished", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if args.mode == "hold-and-resume":
        if args.release is None or args.finished is None:
            parser.error("hold-and-resume requires --release and --finished")
        return _hold_and_resume(args)
    return _contend(args)


if __name__ == "__main__":
    raise SystemExit(main())
