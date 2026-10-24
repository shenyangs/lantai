# -*- coding: utf-8 -*-
"""Golden fixture runner for runtime evaluator regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .capabilities.evaluations import make_finding, summary_for_report
from .capabilities.models import (
    EvaluationConclusion,
    EvaluationFinding,
    EvaluationReport,
    EvaluationSummary,
)
from .experience.failure_diagnosis import failure_diagnosis_for_event
from .preview_harness import PreviewHarnessReport

GoldenFixtureKind = Literal["preview_harness", "runtime_event"]


class GoldenFixtureExpectation(BaseModel):
    conclusion: EvaluationConclusion | None = None
    category: str | None = None
    ok: bool | None = None
    summary_contains: str | None = None
    evidence_contains: str | None = None
    next_action_contains: str | None = None


class GoldenFixtureCase(BaseModel):
    id: str
    title: str
    kind: GoldenFixtureKind
    input: dict[str, Any] = Field(default_factory=dict)
    expect: GoldenFixtureExpectation = Field(default_factory=GoldenFixtureExpectation)
    tags: list[str] = Field(default_factory=list)


class GoldenFixtureFile(BaseModel):
    version: int = 1
    cases: list[GoldenFixtureCase] = Field(default_factory=list)


class GoldenFixtureResult(BaseModel):
    case_id: str
    title: str
    kind: GoldenFixtureKind
    passed: bool
    expected_conclusion: EvaluationConclusion | None = None
    actual_conclusion: EvaluationConclusion
    expected_category: str | None = None
    actual_category: str | None = None
    mismatches: list[str] = Field(default_factory=list)
    report: EvaluationReport


class GoldenFixtureSuiteResult(BaseModel):
    suite_id: str = "runtime_evaluator_golden"
    total: int = 0
    passed: int = 0
    failed: int = 0
    summary: EvaluationSummary
    results: list[GoldenFixtureResult] = Field(default_factory=list)


class _RuntimeFixtureEvent:
    def __init__(self, payload: dict[str, Any]) -> None:
        event_payload = payload.get("payload")
        self.payload = event_payload if isinstance(event_payload, dict) else {}
        self.type = str(payload.get("type") or "runtime.event")
        self.status = str(payload.get("status") or "failed")
        self.summary = str(payload.get("summary") or "")
        self.seq = payload.get("seq")


def load_golden_fixture_cases(path: str | Path) -> list[GoldenFixtureCase]:
    """Load golden evaluator fixtures from a JSON file."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return GoldenFixtureFile.model_validate(payload).cases


def _conclusion_for_findings(findings: list[EvaluationFinding]) -> EvaluationConclusion:
    if any(item.severity == "critical" for item in findings):
        return "blocked"
    if any(item.severity == "major" for item in findings):
        return "needs_revision"
    return "pass"


def _preview_report_for_case(case: GoldenFixtureCase) -> tuple[EvaluationReport, str | None, bool | None, dict[str, str]]:
    raw_report = case.input.get("report")
    report = PreviewHarnessReport.model_validate(raw_report)
    findings: list[EvaluationFinding] = []
    for finding in report.findings:
        severity = (
            "critical"
            if finding.category == "security"
            else "minor"
            if finding.severity == "minor"
            else "major"
        )
        findings.append(
            make_finding(
                code=f"preview.{finding.category}",
                title=finding.message,
                detail=finding.evidence,
                severity=severity,
                metadata={"url": finding.url or report.fetch_url},
            ),
        )

    conclusion = _conclusion_for_findings(findings)
    summary = report.summary if findings else "预览诊断通过。"
    actual_category = report.findings[0].category if report.findings else None
    evaluation_report = EvaluationReport(
        task_run_id=case.id,
        evaluator="golden_fixture.preview_harness",
        conclusion=conclusion,
        summary=summary,
        findings=findings,
    )
    diagnostics = {
        "summary": summary,
        "evidence": report.findings[0].evidence if report.findings else "",
        "next_action": "修复预览可达性、资源请求或页面运行时错误后重新验证。"
        if findings
        else "",
    }
    return evaluation_report, actual_category, report.ok, diagnostics


def _runtime_report_for_case(case: GoldenFixtureCase) -> tuple[EvaluationReport, str | None, bool | None, dict[str, str]]:
    raw_event = case.input.get("event")
    event = _RuntimeFixtureEvent(raw_event if isinstance(raw_event, dict) else case.input)
    diagnosis = failure_diagnosis_for_event(event)
    if diagnosis is None:
        report = EvaluationReport(
            task_run_id=case.id,
            evaluator="golden_fixture.runtime_event",
            conclusion="pass",
            summary="运行事件未发现失败归因。",
            findings=[],
        )
        return report, None, None, {"summary": report.summary, "evidence": "", "next_action": ""}

    category = diagnosis["category"]
    severity = "critical" if category == "blocked" else "major"
    finding = make_finding(
        code=f"runtime.{category}",
        title=diagnosis["summary"],
        detail=diagnosis.get("evidence") or diagnosis["next_action"],
        severity=severity,
        metadata={
            "label": diagnosis.get("label", ""),
            "next_action": diagnosis.get("next_action", ""),
        },
    )
    conclusion = "blocked" if severity == "critical" else "needs_revision"
    report = EvaluationReport(
        task_run_id=case.id,
        evaluator="golden_fixture.runtime_event",
        conclusion=conclusion,
        summary=diagnosis["summary"],
        findings=[finding],
    )
    return report, category, None, diagnosis


def _contains_mismatch(field: str, expected: str | None, actual: str) -> str | None:
    if expected is None:
        return None
    if expected in actual:
        return None
    return f"{field} expected to contain {expected!r}, got {actual!r}"


def evaluate_golden_fixture(case: GoldenFixtureCase) -> GoldenFixtureResult:
    """Evaluate one golden fixture and compare the result to its expectation."""

    if case.kind == "preview_harness":
        report, actual_category, actual_ok, diagnostics = _preview_report_for_case(case)
    else:
        report, actual_category, actual_ok, diagnostics = _runtime_report_for_case(case)

    mismatches: list[str] = []
    expected = case.expect
    if expected.conclusion is not None and report.conclusion != expected.conclusion:
        mismatches.append(
            f"conclusion expected {expected.conclusion!r}, got {report.conclusion!r}"
        )
    if expected.category is not None and actual_category != expected.category:
        mismatches.append(f"category expected {expected.category!r}, got {actual_category!r}")
    if expected.ok is not None and actual_ok != expected.ok:
        mismatches.append(f"ok expected {expected.ok!r}, got {actual_ok!r}")

    for mismatch in (
        _contains_mismatch("summary", expected.summary_contains, diagnostics["summary"]),
        _contains_mismatch("evidence", expected.evidence_contains, diagnostics["evidence"]),
        _contains_mismatch(
            "next_action",
            expected.next_action_contains,
            diagnostics["next_action"],
        ),
    ):
        if mismatch:
            mismatches.append(mismatch)

    return GoldenFixtureResult(
        case_id=case.id,
        title=case.title,
        kind=case.kind,
        passed=not mismatches,
        expected_conclusion=expected.conclusion,
        actual_conclusion=report.conclusion,
        expected_category=expected.category,
        actual_category=actual_category,
        mismatches=mismatches,
        report=report,
    )


def run_golden_fixture_suite(
    cases: list[GoldenFixtureCase],
    *,
    suite_id: str = "runtime_evaluator_golden",
) -> GoldenFixtureSuiteResult:
    """Run a fixture suite and summarize benchmark regressions."""

    results = [evaluate_golden_fixture(case) for case in cases]
    failed = [item for item in results if not item.passed]
    findings = [
        make_finding(
            code="golden_fixture.mismatch",
            title=item.title,
            detail="; ".join(item.mismatches),
            severity="critical",
            metadata={"case_id": item.case_id, "kind": item.kind},
        )
        for item in failed
    ]
    suite_report = EvaluationReport(
        task_run_id=suite_id,
        evaluator="golden_fixture.suite",
        conclusion="blocked" if findings else "pass",
        summary=(
            f"{len(results) - len(failed)}/{len(results)} golden fixtures passed."
            if findings
            else f"All {len(results)} golden fixtures passed."
        ),
        findings=findings,
    )
    return GoldenFixtureSuiteResult(
        suite_id=suite_id,
        total=len(results),
        passed=len(results) - len(failed),
        failed=len(failed),
        summary=summary_for_report(suite_report),
        results=results,
    )
