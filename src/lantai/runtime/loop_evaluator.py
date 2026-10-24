# -*- coding: utf-8 -*-
"""Isolated executor for LOOP-02 deterministic gates and rubric scoring."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .loops import (
    LoopContract,
    LoopDeterministicGateResult,
    LoopGateKind,
    LoopHumanGateDecision,
    LoopPolicyMode,
    LoopRubricDimensionScore,
    evaluate_loop_contract,
    loop_contract_with_hash,
)


_STRICT_CONFIG = ConfigDict(extra="forbid", frozen=True)


class LoopEvaluationGateRequest(BaseModel):
    """Bounded deterministic-gate context exposed to a gate runner."""

    model_config = _STRICT_CONFIG

    contract_id: str
    contract_hash: str
    gate_id: str
    name: str
    kind: LoopGateKind
    command: str | None = None
    expected: str | None = None
    required: bool = True
    timeout_seconds: int | None = None
    allowed_evidence_hashes: tuple[str, ...] = Field(default_factory=tuple)
    tool_policy: LoopPolicyMode
    network_policy: LoopPolicyMode
    file_policy: LoopPolicyMode
    browser_policy: LoopPolicyMode
    shell_policy: LoopPolicyMode


class LoopEvaluationRubricRequest(BaseModel):
    """Bounded rubric context exposed to a rubric scorer."""

    model_config = _STRICT_CONFIG

    contract_id: str
    contract_hash: str
    rubric_id: str
    rubric_version: str
    dimension_id: str
    name: str
    target_score: float
    description: str = ""
    allowed_evidence_hashes: tuple[str, ...] = Field(default_factory=tuple)
    tool_policy: LoopPolicyMode
    network_policy: LoopPolicyMode
    file_policy: LoopPolicyMode
    browser_policy: LoopPolicyMode
    shell_policy: LoopPolicyMode


GateRunnerResult = LoopDeterministicGateResult | Mapping[str, object]
RubricScorerResult = LoopRubricDimensionScore | Mapping[str, object]


class LoopGateRunner(Protocol):
    def run_gate(self, request: LoopEvaluationGateRequest) -> GateRunnerResult:
        """Run one deterministic gate from bounded request context."""


class LoopRubricScorer(Protocol):
    def score_dimension(
        self,
        request: LoopEvaluationRubricRequest,
    ) -> RubricScorerResult:
        """Score one rubric dimension from bounded request context."""


class LoopEvaluationExecutor:
    """Run Loop evaluation outside the generator context.

    The executor intentionally exposes only bounded request models to the gate
    runner and rubric scorer. Callers may inject command/test runners or model
    scorers, but hidden generator reasoning and raw contract internals never
    enter the persisted report path.
    """

    def __init__(
        self,
        *,
        gate_runner: LoopGateRunner,
        rubric_scorer: LoopRubricScorer,
        evaluator: str = "loop_evaluator",
        evaluator_version: str = "1",
    ) -> None:
        self._gate_runner = gate_runner
        self._rubric_scorer = rubric_scorer
        self._evaluator = evaluator
        self._evaluator_version = evaluator_version

    def evaluate(
        self,
        contract: LoopContract,
        *,
        allowed_evidence_hashes: Sequence[str] | None = None,
        human_gate_decision: LoopHumanGateDecision = "not_required",
        report_id: str | None = None,
    ):
        hashed = loop_contract_with_hash(contract)
        evidence_hashes = tuple(allowed_evidence_hashes or ())

        gate_results: list[LoopDeterministicGateResult] = []
        for gate in hashed.deterministic_gates:
            raw_result = self._gate_runner.run_gate(
                LoopEvaluationGateRequest(
                    contract_id=hashed.contract_id,
                    contract_hash=hashed.contract_hash,
                    gate_id=gate.gate_id,
                    name=gate.name,
                    kind=gate.kind,
                    command=gate.command,
                    expected=gate.expected,
                    required=gate.required,
                    timeout_seconds=gate.timeout_seconds,
                    allowed_evidence_hashes=evidence_hashes,
                    tool_policy=hashed.edition_policy.tool_policy,
                    network_policy=hashed.edition_policy.network_policy,
                    file_policy=hashed.edition_policy.file_policy,
                    browser_policy=hashed.edition_policy.browser_policy,
                    shell_policy=hashed.edition_policy.shell_policy,
                )
            )
            result = LoopDeterministicGateResult.model_validate(raw_result)
            if result.gate_id != gate.gate_id:
                raise ValueError(
                    f"gate runner returned mismatched gate_id: {result.gate_id} != {gate.gate_id}",
                )
            gate_results.append(result)

        blocked_by_required_gate = any(
            gate.required
            and any(
                result.gate_id == gate.gate_id and result.status != "passed"
                for result in gate_results
            )
            for gate in hashed.deterministic_gates
        )

        rubric_scores: list[LoopRubricDimensionScore] = []
        if not blocked_by_required_gate:
            for dimension in hashed.rubric.dimensions:
                raw_score = self._rubric_scorer.score_dimension(
                    LoopEvaluationRubricRequest(
                        contract_id=hashed.contract_id,
                        contract_hash=hashed.contract_hash,
                        rubric_id=hashed.rubric.rubric_id,
                        rubric_version=hashed.rubric.version,
                        dimension_id=dimension.dimension_id,
                        name=dimension.name,
                        target_score=dimension.target_score,
                        description=dimension.description,
                        allowed_evidence_hashes=evidence_hashes,
                        tool_policy=hashed.edition_policy.tool_policy,
                        network_policy=hashed.edition_policy.network_policy,
                        file_policy=hashed.edition_policy.file_policy,
                        browser_policy=hashed.edition_policy.browser_policy,
                        shell_policy=hashed.edition_policy.shell_policy,
                    )
                )
                score = LoopRubricDimensionScore.model_validate(raw_score)
                if score.dimension_id != dimension.dimension_id:
                    raise ValueError(
                        "rubric scorer returned mismatched dimension_id: "
                        f"{score.dimension_id} != {dimension.dimension_id}",
                    )
                rubric_scores.append(score)

        return evaluate_loop_contract(
            hashed,
            gate_results=gate_results,
            rubric_scores=rubric_scores,
            human_gate_decision=human_gate_decision,
            evaluator=self._evaluator,
            evaluator_version=self._evaluator_version,
            report_id=report_id,
        )
