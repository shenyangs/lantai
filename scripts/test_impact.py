#!/usr/bin/env python3
"""Conservative, local affected-test feedback; CI always runs the full suites."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Only leaf modules whose static importer graph is supported by Vitest.
# Shared configuration, dynamic imports, product Python changes and unknown
# files deliberately fall back to all tests, not a guessed dependency map.
RELATED_SAFE = {
    "console/src/utils/latestRequest.ts",
    "console/src/utils/routingDecision.ts",
    "console/src/utils/runtimeFailureDiagnosis.ts",
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8")


def changed_paths(base):
    ancestor = git("merge-base", base, "HEAD").strip()
    # One diff includes committed, staged and unstaged changes. --no-renames
    # keeps both old and new paths so deletion cannot hide a dependency.
    tracked = git("diff", "--no-renames", "--name-only", "-z", ancestor, "--")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set(filter(None, (tracked + untracked).split("\0"))))


def build_plan(changed):
    python = set()
    related = []
    frontend_tests = []
    reasons = []
    full = False
    for path in changed:
        file = ROOT / path
        if not file.is_file():
            full = True
            reasons.append(f"Deleted/renamed path: {path}")
        elif (
            path.startswith("tests/unit/")
            and file.name.startswith("test_")
            and file.suffix == ".py"
        ):
            python.add(file.parent.relative_to(ROOT).as_posix())
            reasons.append(f"Test plus adjacent directory: {path}")
        elif path in RELATED_SAFE:
            related.append(path.removeprefix("console/"))
            reasons.append(f"Reviewed static importer graph: {path}")
        elif path.startswith("console/src/") and path.endswith(
            (".test.ts", ".test.tsx")
        ):
            frontend_tests.append(path.removeprefix("console/"))
        elif path.startswith("docs/") and path.endswith(".md"):
            reasons.append(f"Documentation only: {path}")
        else:
            full = True
            reasons.append(f"Shared or unclassified change: {path}")
    npm = "npm.cmd" if os.name == "nt" else "npm"
    commands = []

    def add(cwd, argv):
        commands.append({"cwd": str(cwd), "argv": argv})

    if full:
        paths = ["tests"]
        add(
            ROOT,
            [sys.executable, "-m", "pytest", "tests", "-q", "--durations=20"],
        )
        add(ROOT / "console", [npm, "run", "test:run"])
        add(ROOT / "console", [npm, "run", "test:electron"])
    else:
        paths = sorted(python)
        # Coalesce parent/child directories to prevent duplicate collection.
        paths = [
            p
            for p in paths
            if not any(p.startswith(q + "/") for q in paths if p != q)
        ]
        if paths:
            paths += ["tests/contract", "tests/integration"]
            add(
                ROOT,
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    *paths,
                    "-q",
                    "--durations=20",
                ],
            )
        if frontend_tests or related:
            add(ROOT / "console", [npm, "run", "test:source-pin"])
        vitest = [npm, "exec", "--offline", "--", "vitest"]
        if frontend_tests:
            add(
                ROOT / "console",
                [*vitest, "run", *frontend_tests, "--maxWorkers=2"],
            )
        if related:
            # No --passWithNoTests: an empty relation is a failure to investigate.
            add(
                ROOT / "console",
                [*vitest, "related", "--run", *related, "--maxWorkers=2"],
            )
    gate = [sys.executable, "scripts/check_foundation_experience_gate.py"]
    if changed and any(not p.startswith("docs/") for p in changed):
        gate += ["--run-verifications"]
    add(ROOT, gate)
    return {
        "status": "planned",
        "scope": "local-feedback-not-release-evidence",
        "selection": (
            "full" if full else ("affected" if changed else "no_changes")
        ),
        "changed": changed,
        "reasons": reasons,
        "python": paths,
        "frontend": "full" if full else frontend_tests + related,
        "commands": commands,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default="HEAD", help="Compare merge-base plus local changes"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        plan = build_plan(changed_paths(args.base))
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.error(f"Cannot determine changed paths: {exc}")
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    code = 0
    for command in plan["commands"]:
        start = time.perf_counter()
        try:
            result = subprocess.run(command["argv"], cwd=command["cwd"])
            status = result.returncode
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            status = 127
        command.update(
            exit_code=status,
            duration_seconds=round(time.perf_counter() - start, 3),
        )
        code = code or status
    plan.update(
        status="checks_passed" if code == 0 else "failed", exit_code=code
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        f"Local {plan['selection']} checks: {'PASS' if code == 0 else 'FAIL'}; full CI is still required"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
