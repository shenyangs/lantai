#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the runtime evaluator golden fixture preflight."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "runtime_evaluator" / "golden_cases.json"
)

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lantai.app.evaluator_fixtures import (  # noqa: E402
    load_golden_fixture_cases,
    run_golden_fixture_suite,
)


def _print_text_summary(fixture_path: Path) -> int:
    cases = load_golden_fixture_cases(fixture_path)
    suite = run_golden_fixture_suite(cases)

    print("Runtime evaluator golden preflight")
    print(f"Fixtures: {fixture_path}")
    print(f"Total: {suite.total}, passed: {suite.passed}, failed: {suite.failed}")
    print()

    for result in suite.results:
        status = "PASS" if result.passed else "FAIL"
        category = result.actual_category or "-"
        print(
            f"[{status}] {result.case_id} ({result.kind}) "
            f"conclusion={result.actual_conclusion} category={category}"
        )
        for mismatch in result.mismatches:
            print(f"  - {mismatch}")

    if suite.failed:
        print()
        print(f"Preflight failed: {suite.failed} golden fixture mismatch(es).")
        return 1

    print()
    print(f"Preflight passed: all {suite.total} golden fixtures matched.")
    return 0


def _print_json_summary(fixture_path: Path) -> int:
    cases = load_golden_fixture_cases(fixture_path)
    suite = run_golden_fixture_suite(cases)
    print(json.dumps(suite.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 1 if suite.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run runtime evaluator golden fixtures and fail on drift.",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURE_PATH,
        help="Path to the golden fixture JSON file.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of text.",
    )
    args = parser.parse_args()

    fixture_path = args.fixtures.expanduser().resolve()
    if not fixture_path.exists():
        print(f"Fixture file not found: {fixture_path}", file=sys.stderr)
        return 2

    if args.json:
        return _print_json_summary(fixture_path)
    return _print_text_summary(fixture_path)


if __name__ == "__main__":
    raise SystemExit(main())
