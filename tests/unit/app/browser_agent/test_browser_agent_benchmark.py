# -*- coding: utf-8 -*-
from __future__ import annotations

from collections import Counter
from pathlib import Path

from lantai.app.browser_workbench.agent.benchmark import (
    BrowserBenchmarkResult,
    contract_self_test_results,
    evaluate_browser_benchmark,
    load_browser_benchmark_catalog,
)


TESTS_ROOT = Path(__file__).resolve().parents[3]
CATALOG = TESTS_ROOT / "fixtures" / "browser_agent" / "scenarios.json"
FIXTURE = TESTS_ROOT / "fixtures" / "browser_agent" / "synthetic.html"


def test_catalog_has_exact_versioned_60_scenario_inventory():
    catalog = load_browser_benchmark_catalog(CATALOG)

    assert len(catalog.scenarios) == 60
    assert Counter(item.category for item in catalog.scenarios) == {
        "synthetic": 20,
        "public_read": 20,
        "authenticated_read": 10,
        "draft_write": 10,
    }
    assert all(item.success_conditions for item in catalog.scenarios)
    assert all(item.forbidden_side_effects for item in catalog.scenarios)
    assert all(item.expected_evidence for item in catalog.scenarios)


def test_contract_self_test_never_claims_release_readiness():
    catalog = load_browser_benchmark_catalog(CATALOG)
    rows = contract_self_test_results(catalog, fixture_path=FIXTURE)
    report = evaluate_browser_benchmark(
        catalog,
        rows,
        evidence_mode="contract_self_test",
    )

    assert len(rows) == 180
    assert report.release_ready is False
    assert "contract self-test is not release execution evidence" in report.gate.failures
    assert report.status_counts["pass"] == 60
    assert report.status_counts["blocked_external_evidence"] == 120


def test_zero_tolerance_violation_blocks_otherwise_passing_gate():
    catalog = load_browser_benchmark_catalog(CATALOG)
    rows = [
        BrowserBenchmarkResult(
            scenario_id=scenario.id,
            repetition=repetition,
            status="pass",
            executed_at="2026-08-10T00:00:00Z",
            environment="test",
            fixture_version=catalog.catalog_version,
            provider="fake",
            model="fake-browser",
            profile_id=scenario.profile_id,
            evidence_refs=(f"evidence:{scenario.id}:{repetition}",),
            takeover_required=scenario.takeover_recovery_case,
            takeover_recovered=scenario.takeover_recovery_case,
        )
        for scenario in catalog.scenarios
        for repetition in (1, 2, 3)
    ]
    rows[0] = rows[0].model_copy(update={"secret_leak": True})

    report = evaluate_browser_benchmark(
        catalog,
        rows,
        evidence_mode="real_execution",
    )

    assert report.release_ready is False
    assert report.gate.zero_tolerance_counts["secret_leak"] == 1


def test_more_than_five_point_regression_from_stable_report_blocks_gate():
    catalog = load_browser_benchmark_catalog(CATALOG)
    baseline_rows = [
        BrowserBenchmarkResult(
            scenario_id=scenario.id,
            repetition=repetition,
            status="pass",
            executed_at="2026-08-10T00:00:00Z",
            environment="test",
            fixture_version=catalog.catalog_version,
            provider="fake",
            model="fake-browser",
            profile_id=scenario.profile_id,
            evidence_refs=(f"evidence:{scenario.id}:{repetition}",),
            latency_ms=float(repetition * 100),
            takeover_required=scenario.takeover_recovery_case,
            takeover_recovered=scenario.takeover_recovery_case,
        )
        for scenario in catalog.scenarios
        for repetition in (1, 2, 3)
    ]
    baseline = evaluate_browser_benchmark(
        catalog,
        baseline_rows,
        evidence_mode="real_execution",
    )
    assert baseline.release_ready is True
    assert baseline.metrics.latency_p50_ms == 200.0

    public_ids = [
        scenario.id
        for scenario in catalog.scenarios
        if scenario.category == "public_read"
    ][:2]
    current_rows = [
        row.model_copy(
            update={"status": "product_failure", "evidence_refs": ()}
        )
        if row.scenario_id in public_ids
        else row
        for row in baseline_rows
    ]
    report = evaluate_browser_benchmark(
        catalog,
        current_rows,
        evidence_mode="real_execution",
        baseline_report=baseline,
    )

    assert report.release_ready is False
    assert report.gate.category_regression_points["public_read"] == 0.1
    assert any("regressed 10.0%" in failure for failure in report.gate.failures)
