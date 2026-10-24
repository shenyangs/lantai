# -*- coding: utf-8 -*-
"""Behavior locks for LOOP-02 evaluator ordering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lantai.runtime import (
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopDeterministicGateResult,
    LoopEvaluationReport,
    LoopRubric,
    LoopRubricDimension,
    LoopRubricDimensionScore,
    evaluate_loop_contract,
)


def _contract() -> LoopContract:
    return LoopContract(
        contract_id="loop-eval-1",
        objective="Validate a long-running remediation loop",
        scope=["Runtime migration evidence"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="evidence",
                title="Evidence ledger",
                acceptance=["Commands and results are recorded"],
            )
        ],
        acceptance=["All required deterministic gates pass"],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="unit-tests",
                name="Unit tests",
                kind="test",
                command="uv run pytest tests/unit/runtime -q",
                required=True,
            ),
            LoopDeterministicGate(
                gate_id="manual-note",
                name="Manual note",
                kind="manual_check",
                required=False,
            ),
        ],
        rubric=LoopRubric(
            rubric_id="loop-quality",
            passing_score=4.0,
            dimensions=[
                LoopRubricDimension(
                    dimension_id="correctness",
                    name="Correctness",
                    weight=0.6,
                    target_score=4.0,
                ),
                LoopRubricDimension(
                    dimension_id="evidence",
                    name="Evidence",
                    weight=0.4,
                    target_score=4.0,
                ),
            ],
        ),
    )


def _passing_scores() -> list[LoopRubricDimensionScore]:
    return [
        LoopRubricDimensionScore(
            dimension_id="correctness",
            score=5,
            rationale_summary="Deterministic gates and code review passed.",
            evidence_hash="sha256:correctness",
        ),
        LoopRubricDimensionScore(
            dimension_id="evidence",
            score=4,
            rationale_summary="Evidence ledger is complete.",
            evidence_hash="sha256:evidence",
        ),
    ]


def test_loop_evaluation_fast_fails_required_deterministic_gate() -> None:
    report = evaluate_loop_contract(
        _contract(),
        gate_results=[
            LoopDeterministicGateResult(
                gate_id="unit-tests",
                status="failed",
                summary="runtime tests failed",
                evidence_hash="sha256:test-log",
            )
        ],
        rubric_scores=_passing_scores(),
    )

    assert report.deterministic_gate_result == "blocked"
    assert report.conclusion == "blocked"
    assert report.weighted_score is None
    assert "Required deterministic gates failed" in report.summary


def test_loop_evaluation_scores_rubric_after_gates_pass() -> None:
    report = evaluate_loop_contract(
        _contract(),
        gate_results=[
            LoopDeterministicGateResult(gate_id="unit-tests", status="passed"),
            LoopDeterministicGateResult(gate_id="manual-note", status="skipped"),
        ],
        rubric_scores=_passing_scores(),
    )

    assert report.deterministic_gate_result == "passed"
    assert report.conclusion == "pass"
    assert report.weighted_score == pytest.approx(4.6)
    assert report.evidence_summary_hash.startswith("sha256:")
    assert report.rubric_id == "loop-quality"


def test_loop_evaluation_human_gate_runs_after_rubric() -> None:
    report = evaluate_loop_contract(
        _contract(),
        gate_results=[
            LoopDeterministicGateResult(gate_id="unit-tests", status="passed")
        ],
        rubric_scores=_passing_scores(),
        human_gate_decision="pending",
    )

    assert report.conclusion == "waiting_human"
    assert report.human_gate_decision == "pending"
    assert report.weighted_score == pytest.approx(4.6)


def test_loop_evaluation_rejects_unknown_or_hidden_evaluator_inputs() -> None:
    with pytest.raises(ValidationError):
        LoopRubricDimensionScore.model_validate(
            {
                "dimension_id": "correctness",
                "score": 5,
                "hidden_reasoning": "must not be persisted",
            }
        )

    with pytest.raises(ValueError, match="unknown rubric dimension"):
        evaluate_loop_contract(
            _contract(),
            gate_results=[
                LoopDeterministicGateResult(gate_id="unit-tests", status="passed")
            ],
            rubric_scores=[
                LoopRubricDimensionScore(dimension_id="unknown", score=5)
            ],
        )

    with pytest.raises(ValidationError):
        LoopEvaluationReport.model_validate(
            {
                "report_id": "report-1",
                "contract_id": "loop-eval-1",
                "contract_hash": "sha256:contract",
                "evidence_summary_hash": "sha256:evidence",
                "deterministic_gate_result": "passed",
                "rubric_id": "loop-quality",
                "rubric_version": "1",
                "conclusion": "pass",
                "summary": "passed",
                "generator_hidden_reasoning": "nope",
            }
        )
