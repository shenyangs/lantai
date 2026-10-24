# -*- coding: utf-8 -*-
"""RUN-10: utility calls share one PPT budget across process boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = (
    PROJECT_ROOT / "tests" / "support" / "presentation_parent_budget_worker.py"
)
RUNTIME_RUN_ID = "native-run-run10-parent-budget"
PLANNER_ROUTE = "kimi-codingplan/kimi-for-coding"
AUTHOR_ROUTE = "kimi-codingplan/kimi-for-coding"
CRITIC_ROUTE = "minimax-agent/visual-critic"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    pythonpath = [str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)]
    if existing_pythonpath:
        pythonpath.append(existing_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return environment


def _run_worker(*arguments: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(WORKER_SCRIPT), *arguments],
        cwd=PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_parent_budget_routes_unknown_cost_and_cache_survive_process_boundary(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "run10-state"
    executed = _run_worker(
        "--mode",
        "execute",
        "--state-root",
        str(state_root),
    )
    observed = _run_worker(
        "--mode",
        "observe",
        "--state-root",
        str(state_root),
        "--job-id",
        str(executed["job_id"]),
    )

    assert executed["worker_pid"] != os.getpid()
    assert observed["observer_pid"] != os.getpid()
    assert observed["observer_pid"] != executed["worker_pid"]
    assert observed["job_dir_count"] == 1
    assert observed["manifest_count"] == 1
    assert observed["runtime_run_id"] == RUNTIME_RUN_ID
    assert observed["active_lease_id"] is None

    # One planner (10), three native page authors (1 each), three page critics
    # and one deck critic (1 each) all debit the same authoritative job.
    assert observed["cost_used_units"] == 17
    assert observed["charged_metric_cost_units"] == 17
    assert observed["charged_metric_count"] == 8
    assert observed["charged_routes"] == [
        PLANNER_ROUTE,
        AUTHOR_ROUTE,
        AUTHOR_ROUTE,
        AUTHOR_ROUTE,
        CRITIC_ROUTE,
        CRITIC_ROUTE,
        CRITIC_ROUTE,
        CRITIC_ROUTE,
    ]

    # Subscription-backed providers do not expose a trustworthy per-call CNY
    # amount here. Unknown must stay unknown; it is never serialized as free.
    assert observed["charged_provider_cost_statuses"] == ["unknown"] * 8
    assert observed["charged_provider_cost_micro_cny"] == [None] * 8
    assert observed["provider_amount_zero_count"] == 0

    # The second identical critic pass reuses four durable checkpoints. Those
    # observations are auditable but neither call a provider nor debit budget.
    assert executed["critic_call_count"] == 4
    assert observed["critic_metric_cost_units"] == [1, 1, 1, 1, 0, 0, 0, 0]
    assert observed["critic_metric_provider_cost_statuses"] == [
        "unknown",
        "unknown",
        "unknown",
        "unknown",
        "not_applicable",
        "not_applicable",
        "not_applicable",
        "not_applicable",
    ]

    print(json.dumps(observed, ensure_ascii=False, sort_keys=True))
