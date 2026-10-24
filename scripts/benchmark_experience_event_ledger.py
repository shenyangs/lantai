# -*- coding: utf-8 -*-
"""Benchmark the experience RuntimeEvent ledger append and tail paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import lantai.app.experience.events as experience_events
from lantai.app.experience.events import (
    append_chat_event,
    build_runtime_events_response,
    events_path_for_workspace,
)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, int(round((percentile / 100.0) * (len(ordered) - 1)))),
    )
    return ordered[index]


def _ledger_paths(workspace_dir: Path) -> list[Path]:
    event_dir = events_path_for_workspace(workspace_dir).parent
    if not event_dir.exists():
        return []
    return sorted(
        path
        for path in event_dir.iterdir()
        if path.name.startswith("events") and path.suffix in {".jsonl", ".json"}
    )


async def _append_events(
    *,
    workspace_dir: Path,
    events: int,
    chat_id: str,
) -> list[float]:
    durations_ms: list[float] = []
    for index in range(events):
        started = time.perf_counter()
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="benchmark",
            chat_id=chat_id,
            session_id="console:benchmark",
            run_id=f"benchmark-run-{index}",
            kind="chat.turn.started",
            status="running",
            summary="benchmark event",
            payload={"text_preview": f"event {index}"},
        )
        durations_ms.append((time.perf_counter() - started) * 1000)
    return durations_ms


async def _run(args: argparse.Namespace) -> dict[str, object]:
    workspace_dir = Path(args.workspace).expanduser() if args.workspace else None
    cleanup = None
    if workspace_dir is None:
        cleanup = tempfile.TemporaryDirectory(prefix="lantai-event-ledger-bench-")
        workspace_dir = Path(cleanup.name)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    original_segment_max = experience_events.EVENT_LEDGER_SEGMENT_MAX_BYTES
    experience_events.EVENT_LEDGER_SEGMENT_MAX_BYTES = args.segment_max_bytes
    chat_id = args.chat_id
    before = resource.getrusage(resource.RUSAGE_SELF)
    wall_started = time.perf_counter()
    try:
        append_durations = await _append_events(
            workspace_dir=workspace_dir,
            events=args.events,
            chat_id=chat_id,
        )
        tail_started = time.perf_counter()
        tail_response = build_runtime_events_response(
            workspace_dir,
            chat_id,
            after_seq=max(0, args.events - args.tail_window),
            limit=args.tail_limit,
        )
        tail_ms = (time.perf_counter() - tail_started) * 1000
    finally:
        experience_events.EVENT_LEDGER_SEGMENT_MAX_BYTES = original_segment_max

    after = resource.getrusage(resource.RUSAGE_SELF)
    wall_ms = (time.perf_counter() - wall_started) * 1000
    paths = _ledger_paths(workspace_dir)
    total_bytes = sum(path.stat().st_size for path in paths if path.exists())
    result = {
        "schema": "com.lantai.experience-event-ledger-benchmark.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "events": args.events,
        "workspace_dir": str(workspace_dir),
        "segment_max_bytes": args.segment_max_bytes,
        "ledger_file_count": len(paths),
        "ledger_total_bytes": total_bytes,
        "append_avg_ms": statistics.fmean(append_durations)
        if append_durations
        else 0.0,
        "append_p50_ms": _percentile(append_durations, 50),
        "append_p95_ms": _percentile(append_durations, 95),
        "append_p99_ms": _percentile(append_durations, 99),
        "tail_after_seq": max(0, args.events - args.tail_window),
        "tail_limit": args.tail_limit,
        "tail_events": len(tail_response.events),
        "tail_has_more": tail_response.has_more,
        "tail_ms": tail_ms,
        "wall_ms": wall_ms,
        "user_cpu_seconds": after.ru_utime - before.ru_utime,
        "system_cpu_seconds": after.ru_stime - before.ru_stime,
    }
    if cleanup is not None and not args.keep_workspace:
        cleanup.cleanup()
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--chat-id", default="benchmark-chat")
    parser.add_argument(
        "--segment-max-bytes",
        type=int,
        default=experience_events.EVENT_LEDGER_SEGMENT_MAX_BYTES,
    )
    parser.add_argument("--tail-window", type=int, default=1_000)
    parser.add_argument("--tail-limit", type=int, default=1_000)
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--evidence-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(_run(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.evidence_output is not None:
        output = args.evidence_output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
