# -*- coding: utf-8 -*-
"""Real child-process fixtures for steering transaction boundary tests."""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from lantai.utils.authoritative_store import fsync_directory


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def _wait_mode(*, ready_file: Path, stopped_file: Path | None) -> None:
    def stop(_signum: int, _frame: object) -> None:
        if stopped_file is not None:
            _write_json(
                stopped_file,
                {"pid": os.getpid(), "status": "terminated"},
            )
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    _write_json(ready_file, {"pid": os.getpid(), "status": "waiting"})
    while True:
        time.sleep(0.05)


def _atomic_write_mode(
    *,
    ready_file: Path,
    release_file: Path,
    output_file: Path,
    done_file: Path,
    content: str,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_file.with_suffix(output_file.suffix + ".pending")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    _write_json(
        ready_file,
        {
            "pid": os.getpid(),
            "status": "prepared",
            "temporary": str(temporary),
            "output": str(output_file),
        },
    )
    while not release_file.exists():
        time.sleep(0.02)
    os.replace(temporary, output_file)
    fsync_directory(output_file.parent)
    _write_json(
        done_file,
        {
            "pid": os.getpid(),
            "status": "committed",
            "output": str(output_file),
            "size": output_file.stat().st_size,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("wait", "atomic-write"),
        required=True,
    )
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--stopped-file", type=Path)
    parser.add_argument("--release-file", type=Path)
    parser.add_argument("--output-file", type=Path)
    parser.add_argument("--done-file", type=Path)
    parser.add_argument("--content", default="")
    args = parser.parse_args()

    if args.mode == "wait":
        _wait_mode(
            ready_file=args.ready_file,
            stopped_file=args.stopped_file,
        )
        return
    if (
        args.release_file is None
        or args.output_file is None
        or args.done_file is None
    ):
        parser.error("atomic-write requires release, output, and done files")
    _atomic_write_mode(
        ready_file=args.ready_file,
        release_file=args.release_file,
        output_file=args.output_file,
        done_file=args.done_file,
        content=args.content,
    )


if __name__ == "__main__":
    main()
