#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize optional legacy Runtime writer observation JSONL evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lantai.app.runtime_legacy_writers import (
    summarize_legacy_runtime_writer_observations,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="JSONL file written via LANTAI_RUNTIME_LEGACY_WRITER_OBSERVATION_FILE.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "build/release-evidence/runtime-legacy-writer-observation-summary.json",
        ),
        help="JSON summary evidence file to write.",
    )
    parser.add_argument(
        "--release-edition",
        choices=("official", "mas"),
        default="official",
        help="Release edition recorded in the summary evidence.",
    )
    parser.add_argument(
        "--require-zero-observations",
        action="store_true",
        help="Exit non-zero when any legacy writer invocation was observed.",
    )
    parser.add_argument(
        "--require-zero-fallback-writes",
        action="store_true",
        help="Exit non-zero when any active legacy fallback write was observed.",
    )
    return parser


def write_runtime_legacy_writer_observation_summary(
    *,
    input_path: Path,
    output: Path,
    release_edition: str,
) -> Path:
    summary = summarize_legacy_runtime_writer_observations(
        input_path.expanduser(),
        release_edition=release_edition,
    )
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = write_runtime_legacy_writer_observation_summary(
            input_path=args.input,
            output=args.output,
            release_edition=args.release_edition,
        )
        summary = json.loads(output.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - command-line diagnostics
        print(f"Runtime legacy writer observation summary failed: {exc}", file=sys.stderr)
        return 2

    if args.require_zero_observations and not summary.get("zero_observations"):
        print(
            "Legacy Runtime writer observations were found: "
            f"event_count={summary.get('event_count')}",
            file=sys.stderr,
        )
        return 3
    if args.require_zero_fallback_writes and not summary.get("zero_fallback_writes"):
        print(
            "Legacy Runtime fallback writes were found: "
            f"fallback_write_count={summary.get('fallback_write_count')}",
            file=sys.stderr,
        )
        return 4

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
