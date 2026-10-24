#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write four-category Legacy Runtime boundary observation evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lantai.app.runtime_legacy_execution_observations import (
    summarize_legacy_runtime_boundary_observations,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-input", type=Path, required=True)
    parser.add_argument("--writer-input", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "build/release-evidence/"
            "runtime-legacy-boundary-observation-summary.json"
        ),
    )
    parser.add_argument(
        "--release-edition",
        choices=("official", "mas"),
        default="official",
    )
    parser.add_argument(
        "--require-zero-forbidden-activity",
        action="store_true",
    )
    return parser


def write_runtime_legacy_boundary_observation_summary(
    *,
    execution_input: Path,
    writer_input: Path,
    output: Path,
    release_edition: str,
) -> Path:
    summary = summarize_legacy_runtime_boundary_observations(
        execution_input,
        writer_input,
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
        output = write_runtime_legacy_boundary_observation_summary(
            execution_input=args.execution_input,
            writer_input=args.writer_input,
            output=args.output,
            release_edition=args.release_edition,
        )
        summary = json.loads(output.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - command-line diagnostics
        print(f"Legacy boundary observation summary failed: {exc}", file=sys.stderr)
        return 2

    if (
        args.require_zero_forbidden_activity
        and not summary.get("zero_forbidden_legacy_activity")
    ):
        print(
            "Forbidden Legacy Runtime activity was observed: "
            f"counts={summary.get('counts_by_kind')}",
            file=sys.stderr,
        )
        return 3

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
