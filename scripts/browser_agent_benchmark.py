#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate or evaluate the Browser Agent P0 benchmark release gate."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from lantai.app.browser_workbench.agent.benchmark import (
    BrowserBenchmarkReport,
    contract_self_test_results,
    evaluate_browser_benchmark,
    load_browser_benchmark_catalog,
    load_browser_benchmark_results,
    write_browser_benchmark_report,
)
from lantai.app.browser_workbench.agent.benchmark_runner import (
    BrowserBenchmarkRunner,
    HttpBrowserBenchmarkTransport,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "tests" / "fixtures" / "browser_agent" / "scenarios.json"
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "browser_agent" / "synthetic.html"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("validate", "self-test", "run", "evaluate"),
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--results", type=Path)
    parser.add_argument(
        "--baseline",
        type=Path,
        help="previous release-ready report used for the <=5 point regression gate",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--api-base",
        default="http://127.0.0.1:8000/api",
        help="running Lantai API base for live benchmark execution",
    )
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument(
        "--category",
        action="append",
        choices=("synthetic", "public_read", "authenticated_read", "draft_write"),
        default=[],
    )
    parser.add_argument("--authorized-profile", action="append", default=[])
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--wait-for-human", action="store_true")
    parser.add_argument("--environment", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--fixture-version", default="")
    parser.add_argument(
        "--fixture-base-url",
        default="",
        help="operator-hosted synthetic.html URL used for synthetic live runs",
    )
    parser.add_argument(
        "--auth-token-env",
        default="LANTAI_BENCHMARK_API_TOKEN",
        help="environment variable containing the API bearer token",
    )
    parser.add_argument(
        "--desktop-token-env",
        default="LANTAI_DESKTOP_TOKEN",
        help="environment variable containing the packaged desktop API token",
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--task-timeout-seconds", type=float, default=900.0)
    args = parser.parse_args()

    catalog = load_browser_benchmark_catalog(args.catalog)
    if args.command == "validate":
        print(
            f"browser-agent catalog {catalog.catalog_version}: "
            f"{len(catalog.scenarios)} scenarios, schema valid"
        )
        return 0

    if args.command == "run":
        if args.output is None:
            parser.error("run requires --output")
        if not args.environment.strip() or not args.provider.strip() or not args.model.strip():
            parser.error("run requires --environment, --provider, and --model")
        transport = HttpBrowserBenchmarkTransport(
            args.api_base,
            token_env=args.auth_token_env,
            desktop_token_env=args.desktop_token_env,
        )
        runner = BrowserBenchmarkRunner(
            transport,
            environment=args.environment,
            fixture_version=args.fixture_version or catalog.catalog_version,
            fixture_base_url=args.fixture_base_url,
            provider=args.provider,
            model=args.model,
            authorized_profiles=args.authorized_profile,
            allow_write=args.allow_write,
            wait_for_human=args.wait_for_human,
            poll_interval_seconds=args.poll_seconds,
            task_timeout_seconds=args.task_timeout_seconds,
        )
        rows = asyncio.run(
            runner.run(
                catalog,
                scenario_ids=args.scenario,
                categories=args.category,
            )
        )
        evidence_mode = "real_execution"
    elif args.command == "self-test":
        rows = contract_self_test_results(catalog, fixture_path=args.fixture)
        evidence_mode = "contract_self_test"
    else:
        if args.results is None:
            parser.error("evaluate requires --results")
        rows = load_browser_benchmark_results(args.results)
        evidence_mode = "real_execution"

    report = evaluate_browser_benchmark(
        catalog,
        rows,
        evidence_mode=evidence_mode,
        baseline_report=(
            BrowserBenchmarkReport.model_validate_json(
                args.baseline.read_text(encoding="utf-8")
            )
            if args.baseline
            else None
        ),
    )
    if args.output:
        write_browser_benchmark_report(report, args.output)
    print(report.model_dump_json(indent=2))
    return 0 if report.release_ready or args.command == "self-test" else 2


if __name__ == "__main__":
    raise SystemExit(main())
