#!/usr/bin/env python3
"""Summarize exported Agent run events without external dependencies."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


SECRET_MARKERS = (
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "password",
    "secret",
    "token",
)
TIME_KEYS = ("timestamp", "created_at", "time", "ts")
TYPE_KEYS = ("event_type", "type", "name", "phase")
STATUS_KEYS = ("status", "state", "result")
TOOL_KEYS = ("tool_name", "tool", "function_name")
ERROR_KEYS = ("error", "error_message", "exception", "detail")


def _redact(value: Any, parent_key: str = "") -> Any:
    lowered = parent_key.lower()
    if any(marker in lowered for marker in SECRET_MARKERS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(key): _redact(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, parent_key) for item in value]
    return value


def _load_events(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(payload, dict):
        for key in ("events", "items", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("input must be a JSON event list/object or JSONL")
    return [_redact(item) for item in payload if isinstance(item, dict)]


def _first(event: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = event.get(key)
        if value not in (None, "", [], {}):
            return value
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        for key in keys:
            value = metadata.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _event_summary(event: dict[str, Any], index: int) -> dict[str, Any]:
    error = _first(event, ERROR_KEYS)
    if isinstance(error, (dict, list)):
        error = json.dumps(error, ensure_ascii=False, sort_keys=True)
    return {
        "index": index,
        "time": _parse_time(_first(event, TIME_KEYS)),
        "type": str(_first(event, TYPE_KEYS) or "unknown"),
        "status": str(_first(event, STATUS_KEYS) or ""),
        "tool": str(_first(event, TOOL_KEYS) or ""),
        "error": str(error or ""),
        "run_id": str(_first(event, ("run_id", "task_id", "session_id")) or ""),
        "attempt_id": str(_first(event, ("attempt_id", "attempt")) or ""),
    }


def analyze(events: list[dict[str, Any]], gap_seconds: float) -> dict[str, Any]:
    summaries = [_event_summary(event, index) for index, event in enumerate(events, 1)]
    timed = sorted(
        (row for row in summaries if row["time"]),
        key=lambda row: row["time"],
    )
    gaps = []
    for previous, current in zip(timed, timed[1:]):
        seconds = (current["time"] - previous["time"]).total_seconds()
        if seconds >= gap_seconds:
            gaps.append(
                {
                    "seconds": round(seconds, 3),
                    "after_index": previous["index"],
                    "before_index": current["index"],
                }
            )
    tools = Counter(row["tool"] for row in summaries if row["tool"])
    repeated_tools = {name: count for name, count in tools.items() if count > 1}
    errors = [
        row
        for row in summaries
        if row["error"]
        or row["status"].lower() in {"error", "failed", "failure"}
    ]
    statuses = Counter(row["status"] for row in summaries if row["status"])
    return {
        "event_count": len(summaries),
        "timestamped_count": len(timed),
        "time_range": {
            "start": timed[0]["time"].isoformat() if timed else None,
            "end": timed[-1]["time"].isoformat() if timed else None,
        },
        "statuses": dict(statuses),
        "long_gaps": gaps,
        "repeated_tools": repeated_tools,
        "errors": errors,
        "timeline": summaries,
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Agent Trace Summary",
        "",
        f"- Events: {report['event_count']}",
        f"- Timestamped: {report['timestamped_count']}",
        (
            f"- Time range: {report['time_range']['start']} → "
            f"{report['time_range']['end']}"
        ),
        "- Statuses: "
        + json.dumps(report["statuses"], ensure_ascii=False, sort_keys=True),
        "- Repeated tools: "
        + json.dumps(
            report["repeated_tools"],
            ensure_ascii=False,
            sort_keys=True,
        ),
        "",
        "## Long gaps",
        "",
    ]
    if report["long_gaps"]:
        lines.extend(
            (
                f"- {gap['seconds']}s between events "
                f"{gap['after_index']} and {gap['before_index']}"
            )
            for gap in report["long_gaps"]
        )
    else:
        lines.append("- None")
    lines.extend(["", "## Errors", ""])
    if report["errors"]:
        for row in report["errors"]:
            message = row["error"] or "status indicates failure"
            lines.append(
                f"- Event {row['index']} [{row['type']}] "
                f"{row['status']}: {message}"
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Timeline", ""])
    for row in report["timeline"]:
        stamp = row["time"].isoformat() if row["time"] else "no-time"
        details = " ".join(part for part in (row["status"], row["tool"]) if part)
        lines.append(f"- {row['index']}. {stamp} [{row['type']}] {details}".rstrip())
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="JSON or JSONL event export",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--gap-seconds", type=float, default=30.0)
    args = parser.parse_args()
    if args.gap_seconds < 0:
        parser.error("--gap-seconds must be non-negative")
    try:
        events = _load_events(args.input)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"error: could not read trace: {exc}\n")
    report = analyze(events, args.gap_seconds)
    if args.format == "json":
        serializable = json.loads(json.dumps(report, default=str))
        print(json.dumps(serializable, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
