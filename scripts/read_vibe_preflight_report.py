#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize vibe-coding preflight JSON reports."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = REPO_ROOT / ".lantai" / "preflight" / "vibe-coding-report.json"
MAX_KEY_ERROR_LINES = 12

CATEGORY_HINTS = {
    "golden_evaluator": (
        "Runtime evaluator golden fixtures failed; check whether diagnosis rules drifted or fixtures need an intentional update."
    ),
    "backend_runtime": (
        "Backend runtime checks failed; start with runtime events, preview harness, continue/retry, or runner lifecycle."
    ),
    "frontend_runtime": (
        "Frontend runtime checks failed; start with ToolCallCard, TurnEventPreview, TaskCenter, or runtime repair prompt behavior."
    ),
    "frontend_build": (
        "Frontend production build failed; fix the first TypeScript/Vite error before rerunning the full preflight."
    ),
}

ERROR_LINE_PATTERN = re.compile(
    r"(error|failed|failure|traceback|exception|assertionerror|syntaxerror|typeerror|valueerror|\bts\d{4}\b|\be\s+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReportDiagnosis:
    status: str
    summary: str
    report_path: str
    failed_categories: list[str]
    first_failed_name: str | None
    first_failed_category: str | None
    first_failed_exit_code: int | None
    first_failed_command: str | None
    next_action: str | None
    key_error_lines: list[str]
    diagnosis: str
    error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.status == "passed":
            return 0
        if self.status == "failed":
            return 1
        return 2

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "summary": self.summary,
            "report_path": self.report_path,
            "failed_categories": self.failed_categories,
            "first_failed_name": self.first_failed_name,
            "first_failed_category": self.first_failed_category,
            "first_failed_exit_code": self.first_failed_exit_code,
            "first_failed_command": self.first_failed_command,
            "next_action": self.next_action,
            "key_error_lines": self.key_error_lines,
            "diagnosis": self.diagnosis,
            "error": self.error,
        }


def _string(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (_string(item).strip() for item in value) if item]


def _commands(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _command_failed(command: dict[str, Any]) -> bool:
    status = _string(command.get("status")).lower()
    if status == "failed":
        return True
    exit_code = command.get("exit_code")
    return isinstance(exit_code, int) and exit_code != 0


def extract_key_error_lines(output_tail: Any) -> list[str]:
    lines = _string_list(output_tail)
    if not lines:
        return []

    selected: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not ERROR_LINE_PATTERN.search(line):
            continue
        if line in seen:
            continue
        selected.append(line)
        seen.add(line)
        if len(selected) >= MAX_KEY_ERROR_LINES:
            break
    if selected:
        return selected
    return lines[-min(8, len(lines)) :]


def _diagnosis_for(
    *,
    status: str,
    summary: str,
    first_failed_category: str | None,
) -> str:
    if status == "passed":
        return f"Preflight is clean: {summary}"
    if status == "missing":
        return "Report file not found. Run scripts/vibe_coding_preflight.py first, or pass --report with the artifact path."
    if status == "invalid":
        return "Report JSON is invalid. Re-run the preflight to regenerate a fresh report before diagnosing."

    hint = CATEGORY_HINTS.get(
        first_failed_category or "",
        "Preflight failed; start with the first failed command and its output_tail.",
    )
    return f"{hint} Then rerun python scripts/vibe_coding_preflight.py with the same scope."


def diagnose_report(path: Path) -> ReportDiagnosis:
    report_path = path.expanduser().resolve()
    if not report_path.exists():
        return ReportDiagnosis(
            status="missing",
            summary="No preflight report found.",
            report_path=str(report_path),
            failed_categories=[],
            first_failed_name=None,
            first_failed_category=None,
            first_failed_exit_code=None,
            first_failed_command=None,
            next_action="Run python scripts/vibe_coding_preflight.py --report <path>.",
            key_error_lines=[],
            diagnosis=_diagnosis_for(
                status="missing",
                summary="No preflight report found.",
                first_failed_category=None,
            ),
            error="Report file not found.",
        )

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return ReportDiagnosis(
            status="invalid",
            summary="Report JSON is invalid.",
            report_path=str(report_path),
            failed_categories=[],
            first_failed_name=None,
            first_failed_category=None,
            first_failed_exit_code=None,
            first_failed_command=None,
            next_action="Re-run python scripts/vibe_coding_preflight.py to regenerate the report.",
            key_error_lines=[],
            diagnosis=_diagnosis_for(
                status="invalid",
                summary="Report JSON is invalid.",
                first_failed_category=None,
            ),
            error=f"{exc.msg} at line {exc.lineno}, column {exc.colno}.",
        )

    if not isinstance(payload, dict):
        return ReportDiagnosis(
            status="invalid",
            summary="Report JSON root is not an object.",
            report_path=str(report_path),
            failed_categories=[],
            first_failed_name=None,
            first_failed_category=None,
            first_failed_exit_code=None,
            first_failed_command=None,
            next_action="Re-run python scripts/vibe_coding_preflight.py to regenerate the report.",
            key_error_lines=[],
            diagnosis=_diagnosis_for(
                status="invalid",
                summary="Report JSON root is not an object.",
                first_failed_category=None,
            ),
            error="Expected a JSON object at the report root.",
        )

    status = _string(payload.get("status"), "unknown").lower()
    summary = _string(payload.get("summary"), "No summary provided.")
    failed_categories = _string_list(payload.get("failed_categories"))
    commands = _commands(payload.get("commands"))
    first_failed = next((command for command in commands if _command_failed(command)), None)

    first_failed_category = (
        _string(first_failed.get("category")) if first_failed is not None else None
    )
    exit_code = first_failed.get("exit_code") if first_failed is not None else None
    return ReportDiagnosis(
        status="failed" if status == "failed" else "passed" if status == "passed" else status,
        summary=summary,
        report_path=str(report_path),
        failed_categories=failed_categories,
        first_failed_name=_string(first_failed.get("name")) if first_failed is not None else None,
        first_failed_category=first_failed_category,
        first_failed_exit_code=exit_code if isinstance(exit_code, int) else None,
        first_failed_command=(
            _string(first_failed.get("command_text")) if first_failed is not None else None
        ),
        next_action=_string(first_failed.get("next_action")) if first_failed is not None else None,
        key_error_lines=(
            extract_key_error_lines(first_failed.get("output_tail"))
            if first_failed is not None
            else []
        ),
        diagnosis=_diagnosis_for(
            status="failed" if status == "failed" else "passed" if status == "passed" else status,
            summary=summary,
            first_failed_category=first_failed_category,
        ),
    )


def print_text(diagnosis: ReportDiagnosis) -> None:
    print("Vibe coding preflight diagnosis")
    print(f"Report: {diagnosis.report_path}")
    print(f"Status: {diagnosis.status}")
    print(f"Summary: {diagnosis.summary}")
    if diagnosis.failed_categories:
        print(f"Failed categories: {', '.join(diagnosis.failed_categories)}")
    if diagnosis.first_failed_name:
        category = diagnosis.first_failed_category or "unknown"
        exit_code = (
            str(diagnosis.first_failed_exit_code)
            if diagnosis.first_failed_exit_code is not None
            else "unknown"
        )
        print(f"First failed: [{category}] {diagnosis.first_failed_name} (exit {exit_code})")
    if diagnosis.first_failed_command:
        print(f"Command: {diagnosis.first_failed_command}")
    if diagnosis.next_action:
        print(f"Next action: {diagnosis.next_action}")
    if diagnosis.key_error_lines:
        print("Key error lines:")
        for line in diagnosis.key_error_lines:
            print(f"  - {line}")
    if diagnosis.error:
        print(f"Error: {diagnosis.error}")
    print(f"Diagnosis: {diagnosis.diagnosis}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read a vibe-coding preflight report and print a concise diagnosis.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Path to the JSON preflight report.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the diagnosis as JSON.",
    )
    parser.add_argument(
        "--always-zero",
        action="store_true",
        help="Print the diagnosis but exit 0, useful for CI summary steps.",
    )
    args = parser.parse_args(argv)

    diagnosis = diagnose_report(args.report)
    if args.json:
        print(json.dumps(diagnosis.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_text(diagnosis)
    return 0 if args.always_zero else diagnosis.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
