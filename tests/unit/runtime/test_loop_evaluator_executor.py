# -*- coding: utf-8 -*-
"""Behavior locks for the isolated LOOP-02 evaluator executor."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lantai.runtime import (
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopDeterministicGateResult,
    LoopEvaluationExecutor,
    LoopEvaluationGateRequest,
    LoopEvaluationRubricRequest,
    LoopRubric,
    LoopRubricDimension,
    LoopRubricDimensionScore,
)


def _contract() -> LoopContract:
    return LoopContract(
        contract_id="loop-executor-1",
        objective="Run evaluation outside the generator context",
        scope=["Loop evaluator isolation"],
        deliverables=[
            LoopDeliverable(
                deliverable_id="report",
                title="Evaluation report",
                acceptance=["No hidden generator reasoning is persisted"],
            ),
        ],
        acceptance=["Deterministic gates pass before rubric scoring"],
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
            rubric_id="executor-rubric",
            passing_score=4,
            dimensions=[
                LoopRubricDimension(
                    dimension_id="correctness",
                    name="Correctness",
                    weight=1,
                    target_score=4,
                    description="Implementation is correct.",
                ),
            ],
        ),
    )


class RecordingGateRunner:
    def __init__(self, *, required_status: str = "passed") -> None:
        self.required_status = required_status
        self.requests: list[LoopEvaluationGateRequest] = []

    def run_gate(
        self,
        request: LoopEvaluationGateRequest,
    ) -> LoopDeterministicGateResult:
        self.requests.append(request)
        status = (
            self.required_status
            if request.gate_id == "unit-tests"
            else "skipped"
        )
        return LoopDeterministicGateResult(
            gate_id=request.gate_id,
            status=status,  # type: ignore[arg-type]
            evidence_hash=f"sha256:{request.gate_id}",
        )


class RecordingRubricScorer:
    def __init__(self) -> None:
        self.requests: list[LoopEvaluationRubricRequest] = []

    def score_dimension(
        self,
        request: LoopEvaluationRubricRequest,
    ) -> LoopRubricDimensionScore:
        self.requests.append(request)
        return LoopRubricDimensionScore(
            dimension_id=request.dimension_id,
            score=4.5,
            rationale_summary="Allowed evidence was sufficient.",
            evidence_hash="sha256:rubric",
        )


def test_loop_evaluation_executor_uses_bounded_context() -> None:
    gate_runner = RecordingGateRunner()
    scorer = RecordingRubricScorer()
    executor = LoopEvaluationExecutor(
        gate_runner=gate_runner,
        rubric_scorer=scorer,
        evaluator="isolated-loop-evaluator",
        evaluator_version="2026.07",
    )

    report = executor.evaluate(
        _contract(),
        allowed_evidence_hashes=["sha256:artifact-a"],
        report_id="executor-report-1",
    )

    assert report.report_id == "executor-report-1"
    assert report.evaluator == "isolated-loop-evaluator"
    assert report.evaluator_version == "2026.07"
    assert report.conclusion == "pass"
    assert len(gate_runner.requests) == 2
    assert len(scorer.requests) == 1

    gate_payload = gate_runner.requests[0].model_dump(mode="json")
    rubric_payload = scorer.requests[0].model_dump(mode="json")
    assert gate_payload["contract_hash"].startswith("sha256:")
    assert rubric_payload["contract_hash"] == gate_payload["contract_hash"]
    assert gate_payload["allowed_evidence_hashes"] == ["sha256:artifact-a"]
    assert rubric_payload["allowed_evidence_hashes"] == ["sha256:artifact-a"]
    assert "objective" not in gate_payload
    assert "deliverables" not in gate_payload
    assert "scope" not in rubric_payload
    assert "generator_hidden_reasoning" not in rubric_payload


def test_loop_evaluation_executor_fast_fails_before_rubric_scoring() -> None:
    gate_runner = RecordingGateRunner(required_status="failed")
    scorer = RecordingRubricScorer()
    executor = LoopEvaluationExecutor(
        gate_runner=gate_runner,
        rubric_scorer=scorer,
    )

    report = executor.evaluate(_contract())

    assert report.conclusion == "blocked"
    assert report.deterministic_gate_result == "blocked"
    assert report.rubric_scores == []
    assert scorer.requests == []


def test_loop_evaluation_executor_rejects_hidden_scorer_output() -> None:
    class LeakyScorer:
        def score_dimension(
            self,
            request: LoopEvaluationRubricRequest,
        ) -> dict[str, object]:
            return {
                "dimension_id": request.dimension_id,
                "score": 5,
                "hidden_reasoning": "not allowed",
            }

    executor = LoopEvaluationExecutor(
        gate_runner=RecordingGateRunner(),
        rubric_scorer=LeakyScorer(),
    )

    with pytest.raises(ValidationError):
        executor.evaluate(_contract())
