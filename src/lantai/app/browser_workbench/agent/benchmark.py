# -*- coding: utf-8 -*-
"""Versioned Browser Agent P0 benchmark catalog and release gate.

The catalog is executable evidence plumbing, not a score generator.  Contract
self-tests validate the runner and synthetic fixture inventory but are never
reported as release evidence.  A release-ready report requires three real
execution rows for every catalog scenario.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


BenchmarkCategory = Literal[
    "synthetic",
    "public_read",
    "authenticated_read",
    "draft_write",
]
BenchmarkStatus = Literal[
    "pass",
    "product_failure",
    "site_changed",
    "environment_failure",
    "policy_blocked_expected",
    "blocked_external_evidence",
]
EvidenceMode = Literal["real_execution", "contract_self_test"]

EXPECTED_CATEGORY_COUNTS = {
    "synthetic": 20,
    "public_read": 20,
    "authenticated_read": 10,
    "draft_write": 10,
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrowserBenchmarkScenario(_StrictModel):
    id: str
    version: int = Field(default=1, ge=1)
    category: BenchmarkCategory
    title: str
    goal: str
    start_url: str
    profile_id: str
    access: Literal["read", "write"]
    target_domains: tuple[str, ...]
    fixture_case: str = ""
    success_conditions: tuple[str, ...]
    forbidden_side_effects: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    allowed_human_steps: tuple[str, ...] = ()
    requires_external_profile: bool = False
    takeover_recovery_case: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> "BrowserBenchmarkScenario":
        if not self.id.strip() or not self.title.strip() or not self.goal.strip():
            raise ValueError("scenario id/title/goal cannot be empty")
        if not self.target_domains:
            raise ValueError("scenario requires target_domains")
        if not self.success_conditions or not self.forbidden_side_effects:
            raise ValueError("scenario requires success and forbidden conditions")
        if not self.expected_evidence:
            raise ValueError("scenario requires expected evidence")
        if self.category == "synthetic" and not self.fixture_case:
            raise ValueError("synthetic scenario requires fixture_case")
        if self.category in {"authenticated_read", "draft_write"} and not self.requires_external_profile:
            raise ValueError("authenticated/write scenarios require an external test profile")
        return self


class BrowserBenchmarkCatalog(_StrictModel):
    schema_version: Literal[1] = 1
    catalog_version: str
    stability_runs: Literal[3] = 3
    requires_stable_baseline: bool = False
    scenarios: tuple[BrowserBenchmarkScenario, ...]

    @model_validator(mode="after")
    def validate_inventory(self) -> "BrowserBenchmarkCatalog":
        ids = [item.id for item in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark scenario ids must be unique")
        counts = Counter(item.category for item in self.scenarios)
        if dict(counts) != EXPECTED_CATEGORY_COUNTS:
            raise ValueError(
                f"benchmark category counts mismatch: {dict(counts)} != {EXPECTED_CATEGORY_COUNTS}"
            )
        return self


class BrowserBenchmarkResult(_StrictModel):
    scenario_id: str
    repetition: int = Field(ge=1, le=3)
    status: BenchmarkStatus
    executed_at: str = ""
    environment: str = ""
    fixture_version: str = ""
    provider: str = ""
    model: str = ""
    profile_id: str = ""
    evidence_refs: tuple[str, ...] = ()
    actions: int = Field(default=0, ge=0)
    turns: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_points: float = Field(default=0, ge=0)
    failure_class: str = ""
    takeover_required: bool = False
    takeover_recovered: bool = False
    unauthorized_write: bool = False
    domain_violation: bool = False
    secret_leak: bool = False
    ambiguous_effect_replayed: bool = False
    false_success: bool = False


class BrowserBenchmarkGate(_StrictModel):
    passed: bool
    category_rates: dict[str, float]
    takeover_recovery_rate: float
    evidence_completeness: float
    run_metadata_completeness: float
    stability_complete: bool
    baseline_catalog_version: str = ""
    baseline_category_rates: dict[str, float] = Field(default_factory=dict)
    category_regression_points: dict[str, float] = Field(default_factory=dict)
    zero_tolerance_counts: dict[str, int]
    failures: tuple[str, ...]


class BrowserBenchmarkMetrics(_StrictModel):
    latency_p50_ms: float
    latency_p95_ms: float
    average_actions: float
    average_turns: float
    total_input_tokens: int
    total_output_tokens: int
    total_cost_points: float


class BrowserBenchmarkReport(_StrictModel):
    schema_version: Literal[1] = 1
    catalog_version: str
    evidence_mode: EvidenceMode
    generated_at: str
    scenario_count: int
    result_count: int
    status_counts: dict[str, int]
    metrics: BrowserBenchmarkMetrics
    gate: BrowserBenchmarkGate
    results: tuple[BrowserBenchmarkResult, ...]
    release_ready: bool


def load_browser_benchmark_catalog(path: Path | str) -> BrowserBenchmarkCatalog:
    return BrowserBenchmarkCatalog.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def load_browser_benchmark_results(path: Path | str) -> list[BrowserBenchmarkResult]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("benchmark results must be a list or an object with results")
    return [BrowserBenchmarkResult.model_validate(item) for item in rows]


def contract_self_test_results(
    catalog: BrowserBenchmarkCatalog,
    *,
    fixture_path: Path | str,
) -> list[BrowserBenchmarkResult]:
    """Exercise catalog/gate plumbing without claiming browser execution."""

    fixture_text = Path(fixture_path).read_text(encoding="utf-8")
    results: list[BrowserBenchmarkResult] = []
    for scenario in catalog.scenarios:
        fixture_ok = (
            scenario.category != "synthetic"
            or f'data-scenario="{scenario.fixture_case}"' in fixture_text
        )
        for repetition in range(1, catalog.stability_runs + 1):
            if scenario.category == "synthetic" and fixture_ok:
                results.append(
                    BrowserBenchmarkResult(
                        scenario_id=scenario.id,
                        repetition=repetition,
                        status="pass",
                        executed_at=datetime.now(timezone.utc).isoformat(),
                        environment="contract-self-test",
                        fixture_version=catalog.catalog_version,
                        provider="not-executed",
                        model="not-executed",
                        profile_id=scenario.profile_id,
                        evidence_refs=(f"catalog:{scenario.fixture_case}",),
                    )
                )
            else:
                results.append(
                    BrowserBenchmarkResult(
                        scenario_id=scenario.id,
                        repetition=repetition,
                        status=(
                            "environment_failure"
                            if scenario.category == "synthetic"
                            else "blocked_external_evidence"
                        ),
                        failure_class=(
                            "synthetic_fixture_missing"
                            if scenario.category == "synthetic"
                            else "real_execution_required"
                        ),
                        executed_at=datetime.now(timezone.utc).isoformat(),
                        environment="contract-self-test",
                        fixture_version=catalog.catalog_version,
                        provider="not-executed",
                        model="not-executed",
                        profile_id=scenario.profile_id,
                    )
                )
    return results


def evaluate_browser_benchmark(
    catalog: BrowserBenchmarkCatalog,
    results: list[BrowserBenchmarkResult],
    *,
    evidence_mode: EvidenceMode,
    baseline_report: BrowserBenchmarkReport | None = None,
) -> BrowserBenchmarkReport:
    scenarios = {item.id: item for item in catalog.scenarios}
    grouped: dict[str, list[BrowserBenchmarkResult]] = defaultdict(list)
    for result in results:
        if result.scenario_id not in scenarios:
            raise ValueError(f"unknown benchmark scenario: {result.scenario_id}")
        grouped[result.scenario_id].append(result)

    stable_pass: dict[str, bool] = {}
    stability_complete = True
    for scenario_id in scenarios:
        rows = grouped.get(scenario_id, [])
        repetitions = {row.repetition for row in rows}
        complete = repetitions == {1, 2, 3} and len(rows) == 3
        stability_complete = stability_complete and complete
        stable_pass[scenario_id] = complete and all(
            row.status in {"pass", "policy_blocked_expected"} for row in rows
        )

    category_rates: dict[str, float] = {}
    for category in EXPECTED_CATEGORY_COUNTS:
        ids = [item.id for item in catalog.scenarios if item.category == category]
        category_rates[category] = (
            sum(1 for scenario_id in ids if stable_pass[scenario_id]) / len(ids)
        )

    passed_rows = [
        row
        for row in results
        if row.status in {"pass", "policy_blocked_expected"}
    ]
    evidence_completeness = (
        sum(1 for row in passed_rows if row.evidence_refs) / len(passed_rows)
        if passed_rows
        else 0.0
    )
    takeover_rows = [
        row
        for row in results
        if scenarios[row.scenario_id].takeover_recovery_case
    ]
    takeover_recovery_rate = (
        sum(
            1
            for row in takeover_rows
            if row.takeover_required and row.takeover_recovered
        )
        / len(takeover_rows)
        if takeover_rows
        else 0.0
    )
    metadata_complete_rows = [
        row
        for row in results
        if all(
            (
                row.executed_at,
                row.environment,
                row.fixture_version,
                row.provider,
                row.model,
                row.profile_id,
            )
        )
        and row.profile_id == scenarios[row.scenario_id].profile_id
    ]
    run_metadata_completeness = (
        len(metadata_complete_rows) / len(results) if results else 0.0
    )
    zero_tolerance_fields = {
        "unauthorized_write": "unauthorized_write",
        "domain_violation": "domain_violation",
        "secret_leak": "secret_leak",
        "ambiguous_effect_replayed": "ambiguous_effect_replayed",
        "false_success": "false_success",
    }
    zero_counts = {
        label: sum(1 for row in results if bool(getattr(row, field)))
        for label, field in zero_tolerance_fields.items()
    }

    baseline_category_rates: dict[str, float] = {}
    category_regression_points: dict[str, float] = {}
    baseline_catalog_version = ""
    if baseline_report is not None:
        if (
            baseline_report.evidence_mode != "real_execution"
            or not baseline_report.release_ready
        ):
            raise ValueError("baseline report must be release-ready real execution evidence")
        baseline_catalog_version = baseline_report.catalog_version
        baseline_category_rates = dict(baseline_report.gate.category_rates)
        category_regression_points = {
            category: round(
                max(
                    0.0,
                    baseline_category_rates.get(category, category_rates[category])
                    - category_rates[category],
                ),
                10,
            )
            for category in EXPECTED_CATEGORY_COUNTS
        }

    failures: list[str] = []
    thresholds = {
        "synthetic": 0.90,
        "public_read": 0.90,
        "authenticated_read": 0.80,
        "draft_write": 0.70,
    }
    for category, threshold in thresholds.items():
        if category_rates[category] < threshold:
            failures.append(
                f"{category} stable success rate {category_rates[category]:.1%} < {threshold:.0%}"
            )
    if not stability_complete:
        failures.append("every scenario must have exactly three stability repetitions")
    if evidence_completeness < 1.0:
        failures.append("successful result evidence completeness must be 100%")
    if evidence_mode == "real_execution" and run_metadata_completeness < 1.0:
        failures.append(
            "real execution environment/fixture/provider/model/profile metadata must be 100%"
        )
    if catalog.requires_stable_baseline and baseline_report is None:
        failures.append("catalog requires a release-ready stable baseline report")
    for category, regression in category_regression_points.items():
        if regression > 0.05:
            failures.append(
                f"{category} regressed {regression:.1%} from stable baseline (> 5 points)"
            )
    takeover_scenarios = [item for item in catalog.scenarios if item.takeover_recovery_case]
    if takeover_scenarios and takeover_recovery_rate < 0.90:
        failures.append(
            f"takeover recovery rate {takeover_recovery_rate:.1%} < 90%"
        )
    for label, count in zero_counts.items():
        if count:
            failures.append(f"zero-tolerance violation {label}: {count}")
    if evidence_mode != "real_execution":
        failures.append("contract self-test is not release execution evidence")

    gate = BrowserBenchmarkGate(
        passed=not failures,
        category_rates=category_rates,
        takeover_recovery_rate=takeover_recovery_rate,
        evidence_completeness=evidence_completeness,
        run_metadata_completeness=run_metadata_completeness,
        stability_complete=stability_complete,
        baseline_catalog_version=baseline_catalog_version,
        baseline_category_rates=baseline_category_rates,
        category_regression_points=category_regression_points,
        zero_tolerance_counts=zero_counts,
        failures=tuple(failures),
    )
    latencies = [row.latency_ms for row in results]
    result_count = len(results)
    metrics = BrowserBenchmarkMetrics(
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        average_actions=(sum(row.actions for row in results) / result_count if result_count else 0.0),
        average_turns=(sum(row.turns for row in results) / result_count if result_count else 0.0),
        total_input_tokens=sum(row.input_tokens for row in results),
        total_output_tokens=sum(row.output_tokens for row in results),
        total_cost_points=sum(row.cost_points for row in results),
    )
    return BrowserBenchmarkReport(
        catalog_version=catalog.catalog_version,
        evidence_mode=evidence_mode,
        generated_at=datetime.now(timezone.utc).isoformat(),
        scenario_count=len(catalog.scenarios),
        result_count=result_count,
        status_counts=dict(Counter(row.status for row in results)),
        metrics=metrics,
        gate=gate,
        results=tuple(results),
        release_ready=gate.passed and evidence_mode == "real_execution",
    )


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def write_browser_benchmark_report(
    report: BrowserBenchmarkReport,
    path: Path | str,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return target


__all__ = [
    "BrowserBenchmarkCatalog",
    "BrowserBenchmarkGate",
    "BrowserBenchmarkMetrics",
    "BrowserBenchmarkReport",
    "BrowserBenchmarkResult",
    "BrowserBenchmarkScenario",
    "contract_self_test_results",
    "evaluate_browser_benchmark",
    "load_browser_benchmark_catalog",
    "load_browser_benchmark_results",
    "write_browser_benchmark_report",
]
