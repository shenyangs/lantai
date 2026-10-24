#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run focused preflight checks for vibe-coding reliability."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE_ROOT = REPO_ROOT / "console"
DEFAULT_REPORT_PATH = REPO_ROOT / ".lantai" / "preflight" / "vibe-coding-report.json"
OUTPUT_TAIL_LINES = 80

BACKEND_TESTS = [
    "tests/unit/app/test_runtime_evaluator_fixtures.py",
    "tests/unit/app/test_preview_harness.py",
    "tests/unit/app/test_runtime_events.py",
    "tests/unit/app/test_runtime_continue.py",
    "tests/unit/app/test_runner_interrupt_behavior.py",
    "tests/unit/app/test_runner_behavior.py",
]

FRONTEND_TESTS = [
    "src/api/modules/runtime.test.ts",
    "src/utils/runtimeFailureDiagnosis.test.ts",
    "src/pages/Chat/hooks/usePreviewErrorBackfeed.test.ts",
    "src/pages/Chat/components/ToolCallCard.test.tsx",
    "src/pages/Chat/components/TurnEventPreview.test.tsx",
    "src/pages/Control/TaskCenter/index.test.tsx",
    "src/pages/Chat/runtimeStall.test.ts",
    "src/pages/Chat/runtimeEventStream.test.ts",
    "src/pages/Chat/toolRenderers/registry.test.ts",
    "src/pages/Chat/vibeCodingSignals.test.ts",
    "src/pages/Chat/planSubmitGuard.test.ts",
]


@dataclass(frozen=True)
class PreflightCommand:
    name: str
    category: str
    cwd: Path
    argv: list[str]
    next_action: str


@dataclass(frozen=True)
class CommandResult:
    command: PreflightCommand
    status: str
    exit_code: int
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    output_tail: list[str]


def _npm_binary() -> str:
    binary = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    return binary or ("npm.cmd" if os.name == "nt" else "npm")


def build_commands(
    *,
    include_backend: bool = True,
    include_frontend: bool = True,
    full: bool = False,
) -> list[PreflightCommand]:
    commands: list[PreflightCommand] = [
        PreflightCommand(
            name="Foundation experience preservation ratchet",
            category="foundation_experience",
            cwd=REPO_ROOT,
            argv=[
                sys.executable,
                "scripts/check_foundation_experience_gate.py",
                "--run-verifications",
            ],
            next_action=(
                "先恢复被删除或削弱的受保护体验契约、验证入口或历史事故 fixture；"
                "不得通过降级严重性或取消发布阻断来让检查通过。"
            ),
        )
    ]
    if include_backend:
        commands.extend(
            [
                PreflightCommand(
                    name="Runtime evaluator golden fixtures",
                    category="golden_evaluator",
                    cwd=REPO_ROOT,
                    argv=[sys.executable, "scripts/runtime_evaluator_preflight.py"],
                    next_action=(
                        "先看 runtime_evaluator golden fixture mismatch，确认是诊断规则漂移还是 fixture 需要更新。"
                    ),
                ),
                PreflightCommand(
                    name="Backend runtime, preview, continue, and runner tests",
                    category="backend_runtime",
                    cwd=REPO_ROOT,
                    argv=[sys.executable, "-m", "pytest", *BACKEND_TESTS],
                    next_action=(
                        "先看首个 pytest failure；优先检查 runtime events、preview harness、continue/retry 或 runner 生命周期。"
                    ),
                ),
            ],
        )
    if include_frontend:
        npm = _npm_binary()
        commands.append(
            PreflightCommand(
                name="Frontend runtime/vibe-coding focused tests",
                category="frontend_runtime",
                cwd=CONSOLE_ROOT,
                argv=[npm, "run", "test:run", "--", *FRONTEND_TESTS],
                next_action=(
                    "先看首个 Vitest failure；优先检查 ToolCallCard、TurnEventPreview、TaskCenter 或 runtime repair prompt。"
                ),
            ),
        )
        if full:
            commands.append(
                PreflightCommand(
                    name="Frontend production build",
                    category="frontend_build",
                    cwd=CONSOLE_ROOT,
                    argv=[npm, "run", "build"],
                    next_action="先看首个 TypeScript/Vite build error，修复对应文件行后重跑 --full。",
                ),
            )
    return commands


def _format_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(item) for item in argv)


def print_command_plan(commands: list[PreflightCommand]) -> None:
    print("Vibe coding preflight plan")
    print(f"Repo: {REPO_ROOT}")
    print()
    for index, command in enumerate(commands, start=1):
        print(f"{index}. {command.name}")
        print(f"   category: {command.category}")
        print(f"   cwd: {command.cwd}")
        print(f"   cmd: {_format_command(command.argv)}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _run_command(command: PreflightCommand) -> CommandResult:
    started_at = _utc_now()
    tail: list[str] = []
    try:
        process = subprocess.Popen(
            command.argv,
            cwd=command.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        ended_at = _utc_now()
        return CommandResult(
            command=command,
            status="failed",
            exit_code=127,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=int((ended_at - started_at).total_seconds() * 1000),
            output_tail=[str(exc)],
        )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        tail.append(line.rstrip("\n"))
        if len(tail) > OUTPUT_TAIL_LINES:
            tail = tail[-OUTPUT_TAIL_LINES:]
    exit_code = process.wait()
    ended_at = _utc_now()
    return CommandResult(
        command=command,
        status="passed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=int((ended_at - started_at).total_seconds() * 1000),
        output_tail=tail,
    )


def _command_result_json(result: CommandResult) -> dict[str, object]:
    command = result.command
    return {
        "name": command.name,
        "category": command.category,
        "status": result.status,
        "exit_code": result.exit_code,
        "cwd": str(command.cwd),
        "command": command.argv,
        "command_text": _format_command(command.argv),
        "next_action": command.next_action,
        "started_at": _iso(result.started_at),
        "ended_at": _iso(result.ended_at),
        "duration_ms": result.duration_ms,
        "output_tail": result.output_tail,
    }


def build_report(
    *,
    results: list[CommandResult],
    started_at: datetime,
    ended_at: datetime,
    full: bool,
    include_backend: bool,
    include_frontend: bool,
) -> dict[str, object]:
    failures = [result for result in results if result.status == "failed"]
    failed_categories = [result.command.category for result in failures]
    return {
        "schema_version": 1,
        "name": "vibe_coding_preflight",
        "status": "failed" if failures else "passed",
        "summary": (
            f"{len(failures)}/{len(results)} checks failed."
            if failures
            else f"All {len(results)} checks passed."
        ),
        "repo": str(REPO_ROOT),
        "full": full,
        "include_backend": include_backend,
        "include_frontend": include_frontend,
        "started_at": _iso(started_at),
        "ended_at": _iso(ended_at),
        "duration_ms": int((ended_at - started_at).total_seconds() * 1000),
        "failed_categories": failed_categories,
        "commands": [_command_result_json(result) for result in results],
    }


def write_report(report: dict[str, object], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def run_self_test_failure_report(report_path: Path) -> int:
    started_at = _utc_now()
    command = PreflightCommand(
        name="Synthetic frontend build failure",
        category="frontend_build",
        cwd=CONSOLE_ROOT,
        argv=["self-test", "frontend-build"],
        next_action="先看首个 TypeScript/Vite build error，修复对应文件行后重跑 --full。",
    )
    ended_at = _utc_now()
    result = CommandResult(
        command=command,
        status="failed",
        exit_code=2,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=int((ended_at - started_at).total_seconds() * 1000),
        output_tail=[
            "src/App.tsx(12,7): error TS2322: Type 'string' is not assignable to type 'number'.",
        ],
    )
    report = build_report(
        results=[result],
        started_at=started_at,
        ended_at=ended_at,
        full=True,
        include_backend=False,
        include_frontend=True,
    )
    write_report(report, report_path)

    print("Vibe coding preflight failure self-test")
    print(f"Repo: {REPO_ROOT}")
    print(f"Report: {report_path}")
    print()
    print("Preflight failed: 1 check(s) failed.")
    print(f"- {command.category}: {command.name} exited with {result.exit_code}")
    print(f"  next: {command.next_action}")
    print(f"Report written: {report_path}")
    return 1


def run_commands(
    commands: list[PreflightCommand],
    *,
    report_path: Path = DEFAULT_REPORT_PATH,
    full: bool = False,
    include_backend: bool = True,
    include_frontend: bool = True,
) -> int:
    started_at = _utc_now()
    results: list[CommandResult] = []
    print("Vibe coding preflight")
    print(f"Repo: {REPO_ROOT}")
    print(f"Report: {report_path}")
    print(flush=True)

    for index, command in enumerate(commands, start=1):
        print(f"==> [{index}/{len(commands)}] {command.name}")
        print(f"category: {command.category}")
        print(f"cwd: {command.cwd}")
        print(f"cmd: {_format_command(command.argv)}", flush=True)
        result = _run_command(command)
        results.append(result)
        if result.exit_code == 0:
            print(f"PASS: {command.name}")
        else:
            print(f"FAIL: {command.name} exited with {result.exit_code}")
        print(flush=True)

    ended_at = _utc_now()
    report = build_report(
        results=results,
        started_at=started_at,
        ended_at=ended_at,
        full=full,
        include_backend=include_backend,
        include_frontend=include_frontend,
    )
    write_report(report, report_path)

    failures = [result for result in results if result.status == "failed"]
    if failures:
        print(f"Preflight failed: {len(failures)} check(s) failed.")
        for result in failures:
            command = result.command
            print(f"- {command.category}: {command.name} exited with {result.exit_code}")
            print(f"  next: {command.next_action}")
        print(f"Report written: {report_path}")
        return failures[0].exit_code or 1

    print(f"Preflight passed: {len(commands)} check(s) completed.")
    print(f"Report written: {report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run focused backend/frontend checks for vibe-coding reliability.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--backend-only",
        action="store_true",
        help="Run only backend runtime/evaluator checks.",
    )
    scope.add_argument(
        "--frontend-only",
        action="store_true",
        help="Run only frontend runtime/vibe-coding checks.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also run the frontend production build.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the planned commands without running them.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Path for the JSON preflight report.",
    )
    parser.add_argument(
        "--self-test-failure-report",
        action="store_true",
        help="Write a deterministic failed report and exit non-zero for smoke-testing diagnosis handling.",
    )
    args = parser.parse_args()

    include_backend = not args.frontend_only
    include_frontend = not args.backend_only
    report_path = args.report.expanduser().resolve()
    if args.self_test_failure_report:
        if args.list or args.backend_only or args.frontend_only or args.full:
            parser.error("--self-test-failure-report can only be combined with --report")
        return run_self_test_failure_report(report_path)

    commands = build_commands(
        include_backend=include_backend,
        include_frontend=include_frontend,
        full=args.full,
    )
    if args.list:
        print_command_plan(commands)
        return 0
    return run_commands(
        commands,
        report_path=report_path,
        full=args.full,
        include_backend=include_backend,
        include_frontend=include_frontend,
    )


if __name__ == "__main__":
    raise SystemExit(main())
