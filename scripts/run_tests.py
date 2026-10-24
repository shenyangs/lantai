#!/usr/bin/env python3
"""Run Lantai's Python suites once, using the active Python environment."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = ["tests/unit", "tests/contract", "tests/integration"]
# Reviewed pure, in-memory contracts. Everything else stays serial until its
# global state, ports, subprocesses and filesystem isolation have been audited.
PARALLEL_SAFE = [
    "tests/unit/runtime/test_recovery_policy.py",
    "tests/unit/runtime/test_run_contracts.py",
    "tests/unit/runtime/test_task_contract.py",
    "tests/unit/runtime/test_effect_capabilities.py",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    suite = parser.add_mutually_exclusive_group()
    suite.add_argument("-u", "--unit", nargs="?", const="", metavar="DIR")
    suite.add_argument(
        "-i", "--integration", "--integrated", action="store_true"
    )
    suite.add_argument("--contract", action="store_true")
    suite.add_argument("-a", "--all", action="store_true")
    parser.add_argument("-c", "--coverage", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "-p",
        "--parallel",
        action="store_true",
        help="Parallelize only the reviewed allowlist",
    )
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument(
        "--report",
        type=Path,
        help="Write machine-readable timing and exit status",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    paths = list(SUITES)
    if args.unit is not None:
        paths = [str(Path("tests/unit") / args.unit)]
    elif args.integration:
        paths = ["tests/integration"]
    elif args.contract:
        paths = ["tests/contract"]
    for path in paths:
        target = (ROOT / path).resolve()
        if not target.is_relative_to(ROOT / "tests") or not target.exists():
            parser.error(f"Missing or invalid test target: {path}")
    command = [sys.executable, "-m", "pytest", *paths, "-q", "--durations=20"]
    if args.coverage:
        command += [
            "--cov=src/lantai",
            "--cov-branch",
            "--cov-report=xml",
            "--cov-report=html",
            "--cov-report=term-missing",
        ]
    extra = args.pytest_args
    command += extra[1:] if extra[:1] == ["--"] else extra
    commands = [command]
    if args.parallel and args.workers > 1:
        safe = [
            item
            for item in PARALLEL_SAFE
            if (ROOT / item).is_file()
            and any(
                (ROOT / item).is_relative_to((ROOT / path).resolve())
                for path in paths
            )
        ]
        if safe:
            candidates = set()
            for path in paths:
                target = ROOT / path
                if target.is_file():
                    candidates.add(target.relative_to(ROOT).as_posix())
                else:
                    candidates.update(
                        p.relative_to(ROOT).as_posix()
                        for p in target.rglob("*.py")
                        if p.name.startswith("test_")
                        or p.name.endswith("_test.py")
                    )
            if candidates == set(safe):
                # Preserve normal coverage reports and its final threshold when
                # there is no serial remainder. Never add an empty pytest run.
                command += ["-n", str(args.workers), "--dist=loadfile"]
                safe = []
        if safe:
            parallel = [
                sys.executable,
                "-m",
                "pytest",
                *safe,
                "-q",
                "--durations=20",
                "-n",
                str(args.workers),
                "--dist=loadfile",
            ]
            parallel += extra[1:] if extra[:1] == ["--"] else extra
            if args.coverage:
                parallel += [
                    "--cov=src/lantai",
                    "--cov-branch",
                    "--cov-report=",
                    "--cov-fail-under=0",
                ]
                command += ["--cov-append"]
            command += [f"--ignore={item}" for item in safe]
            commands = [parallel, command]
    plan = {
        "status": "planned",
        "commands": [{"cwd": str(ROOT), "argv": item} for item in commands],
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    started = time.perf_counter()
    returncode = 0
    for item in plan["commands"]:
        stage_start = time.perf_counter()
        result = subprocess.run(item["argv"], cwd=ROOT)
        item.update(
            exit_code=result.returncode,
            duration_seconds=round(time.perf_counter() - stage_start, 3),
        )
        returncode = returncode or result.returncode
    plan.update(
        status="passed" if returncode == 0 else "failed",
        exit_code=returncode,
        duration_seconds=round(time.perf_counter() - started, 3),
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(plan, indent=2) + "\n", encoding="utf-8"
        )
    print("PASS" if returncode == 0 else f"FAIL (exit {returncode})")
    return returncode


if __name__ == "__main__":
    sys.exit(main())
