# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json

from .models import (
    EvaluationConclusion,
    EvaluationFinding,
    EvaluationReport,
    EvaluationSummary,
    TaskRun,
)


def make_finding(
    *,
    code: str,
    title: str,
    detail: str = "",
    severity: str = "critical",
    step_id: str | None = None,
    metadata: dict | None = None,
) -> EvaluationFinding:
    return EvaluationFinding(
        severity=severity,
        code=code,
        title=title,
        detail=detail,
        step_id=step_id,
        metadata=metadata or {},
    )


def conclusion_for_findings(findings: list[EvaluationFinding]) -> EvaluationConclusion:
    if any(item.severity == "critical" for item in findings):
        return "blocked"
    if any(item.severity == "major" for item in findings):
        return "needs_revision"
    return "pass"


def summary_for_report(report: EvaluationReport) -> EvaluationSummary:
    critical_count = sum(1 for item in report.findings if item.severity == "critical")
    major_count = sum(1 for item in report.findings if item.severity == "major")
    minor_count = sum(1 for item in report.findings if item.severity == "minor")
    return EvaluationSummary(
        conclusion=report.conclusion,
        summary=report.summary,
        critical_count=critical_count,
        major_count=major_count,
        minor_count=minor_count,
    )


def build_report(
    *,
    task_run_id: str,
    evaluator: str,
    evaluator_version: str = "1",
    rubric_id: str | None = None,
    rubric_version: str = "1",
    attempt_id: str | None = None,
    findings: list[EvaluationFinding],
    passed_summary: str,
    blocked_summary: str,
) -> EvaluationReport:
    conclusion = conclusion_for_findings(findings)
    evidence_payload = [item.model_dump(mode="json") for item in findings]
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            evidence_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()
    return EvaluationReport(
        task_run_id=task_run_id,
        evaluator=evaluator,
        evaluator_version=evaluator_version,
        rubric_id=rubric_id or evaluator,
        rubric_version=rubric_version,
        attempt_id=attempt_id,
        evidence_sha256=f"sha256:{evidence_sha256}",
        conclusion=conclusion,
        summary=blocked_summary if conclusion == "blocked" else passed_summary,
        gate_result=conclusion,
        findings=findings,
    )


def apply_report_summary(run: TaskRun, report: EvaluationReport) -> TaskRun:
    run.evaluation_summary = summary_for_report(report)
    return run


def report_block_reason(report: EvaluationReport) -> str:
    finding = next(
        (item for item in report.findings if item.severity == "critical"),
        report.findings[0] if report.findings else None,
    )
    if finding is None:
        return report.summary
    return f"{finding.title}：{finding.detail}" if finding.detail else finding.title
