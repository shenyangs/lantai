#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write a side-effect-free Runtime migration dry-run report.

The report is release evidence for CLOSE-01. It inventories legacy Runtime
writers and source files, but it must not migrate, repair, delete, or rebuild
workspace data.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from lantai.app.runtime_legacy_writers import (
    LEGACY_RUNTIME_WRITER_MODE_ENV,
    LegacyRuntimeWriterMode,
    normalize_legacy_runtime_writer_mode,
)
from lantai.app.runtime_migration import build_runtime_migration_report
from lantai.runtime.runs import RuntimeReleaseEdition


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace/root directory to scan without mutating it.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/release-evidence/runtime-migration-dry-run-report.json"),
        help="JSON evidence file to write.",
    )
    parser.add_argument(
        "--release-edition",
        choices=("official", "mas"),
        default="official",
        help="Release edition recorded in projected Runtime contracts.",
    )
    parser.add_argument(
        "--legacy-writer-mode",
        choices=("active", "read_only"),
        default=None,
        help=(
            "Temporarily set LANTAI_RUNTIME_LEGACY_WRITER_MODE while building "
            "the read-only inventory evidence. Omit to use the current environment."
        ),
    )
    return parser


@contextlib.contextmanager
def _temporary_legacy_writer_mode(
    mode: LegacyRuntimeWriterMode | None,
):
    if mode is None:
        yield
        return

    previous = os.environ.get(LEGACY_RUNTIME_WRITER_MODE_ENV)
    os.environ[LEGACY_RUNTIME_WRITER_MODE_ENV] = mode
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(LEGACY_RUNTIME_WRITER_MODE_ENV, None)
        else:
            os.environ[LEGACY_RUNTIME_WRITER_MODE_ENV] = previous


def write_runtime_migration_dry_run_report(
    *,
    workspace: Path,
    output: Path,
    release_edition: RuntimeReleaseEdition,
    legacy_writer_mode: LegacyRuntimeWriterMode | str | None = None,
) -> Path:
    normalized_mode = (
        normalize_legacy_runtime_writer_mode(str(legacy_writer_mode))
        if legacy_writer_mode is not None
        else None
    )
    with _temporary_legacy_writer_mode(normalized_mode):
        report = build_runtime_migration_report(
            workspace.expanduser(),
            release_edition=release_edition,
        )
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = write_runtime_migration_dry_run_report(
            workspace=args.workspace,
            output=args.output,
            release_edition=args.release_edition,
            legacy_writer_mode=args.legacy_writer_mode,
        )
    except Exception as exc:  # noqa: BLE001 - command-line diagnostics
        print(f"Runtime migration dry-run report failed: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
