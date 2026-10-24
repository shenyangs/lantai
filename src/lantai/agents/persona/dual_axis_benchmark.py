# -*- coding: utf-8 -*-
"""Multi-turn persona evaluation with separate feel and governance axes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _groups(value: Any, *, where: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    result: list[tuple[str, ...]] = []
    for index, group in enumerate(value):
        if not isinstance(group, list) or not group or not all(
            isinstance(item, str) and item.strip() for item in group
        ):
            raise ValueError(f"{where}[{index}] must be a non-empty text list")
        result.append(tuple(item.strip() for item in group))
    return tuple(result)


def _texts(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{where} must be a text list")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class DualAxisCriteria:
    required_any: tuple[tuple[str, ...], ...] = ()
    forbidden: tuple[str, ...] = ()


@dataclass(frozen=True)
class MultiTurnPersonaTurn:
    user: str
    relationship: DualAxisCriteria
    governance: DualAxisCriteria


@dataclass(frozen=True)
class MultiTurnPersonaScenario:
    id: str
    title: str
    memory_preload: tuple[str, ...]
    turns: tuple[MultiTurnPersonaTurn, ...]


@dataclass(frozen=True)
class AxisEvaluation:
    passed: bool
    score: float
    missing_required_groups: tuple[tuple[str, ...], ...]
    forbidden_matches: tuple[str, ...]


@dataclass(frozen=True)
class DualAxisTurnEvaluation:
    relationship: AxisEvaluation
    governance: AxisEvaluation

    @property
    def passed(self) -> bool:
        return self.relationship.passed and self.governance.passed


@dataclass(frozen=True)
class DualAxisBenchmarkReport:
    model_id: str
    total_turns: int
    relationship_passed: int
    governance_passed: int
    both_passed: int
    missing_turn_ids: tuple[str, ...]
    evaluations: tuple[tuple[str, DualAxisTurnEvaluation], ...]

    @property
    def relationship_rate(self) -> float:
        return self.relationship_passed / self.total_turns if self.total_turns else 0.0

    @property
    def governance_rate(self) -> float:
        return self.governance_passed / self.total_turns if self.total_turns else 0.0

    @property
    def both_rate(self) -> float:
        return self.both_passed / self.total_turns if self.total_turns else 0.0


def _evaluate_axis(criteria: DualAxisCriteria, response: str) -> AxisEvaluation:
    normalized = response.casefold()
    missing = tuple(
        group
        for group in criteria.required_any
        if not any(candidate.casefold() in normalized for candidate in group)
    )
    forbidden = tuple(
        phrase
        for phrase in criteria.forbidden
        if phrase.casefold() in normalized
    )
    total = len(criteria.required_any)
    matched = total - len(missing)
    score = matched / total if total else 1.0
    if forbidden:
        score = 0.0
    return AxisEvaluation(
        passed=not missing and not forbidden,
        score=score,
        missing_required_groups=missing,
        forbidden_matches=forbidden,
    )


def evaluate_dual_axis_turn(
    turn: MultiTurnPersonaTurn,
    response: str,
) -> DualAxisTurnEvaluation:
    return DualAxisTurnEvaluation(
        relationship=_evaluate_axis(turn.relationship, response),
        governance=_evaluate_axis(turn.governance, response),
    )


def run_dual_axis_benchmark(
    *,
    model_id: str,
    scenarios: tuple[MultiTurnPersonaScenario, ...] | list[MultiTurnPersonaScenario],
    responses: dict[str, str],
) -> DualAxisBenchmarkReport:
    evaluations: list[tuple[str, DualAxisTurnEvaluation]] = []
    missing: list[str] = []
    relationship_passed = 0
    governance_passed = 0
    both_passed = 0
    total = sum(len(scenario.turns) for scenario in scenarios)
    for scenario in scenarios:
        for index, turn in enumerate(scenario.turns, start=1):
            turn_id = f"{scenario.id}:{index}"
            response = responses.get(turn_id, "").strip()
            if not response:
                missing.append(turn_id)
                continue
            evaluation = evaluate_dual_axis_turn(turn, response)
            evaluations.append((turn_id, evaluation))
            relationship_passed += int(evaluation.relationship.passed)
            governance_passed += int(evaluation.governance.passed)
            both_passed += int(evaluation.passed)
    return DualAxisBenchmarkReport(
        model_id=model_id,
        total_turns=total,
        relationship_passed=relationship_passed,
        governance_passed=governance_passed,
        both_passed=both_passed,
        missing_turn_ids=tuple(missing),
        evaluations=tuple(evaluations),
    )


def _criteria(payload: Any, *, where: str) -> DualAxisCriteria:
    if not isinstance(payload, dict):
        raise ValueError(f"{where} must be a mapping")
    return DualAxisCriteria(
        required_any=_groups(payload.get("required_any"), where=f"{where}.required_any"),
        forbidden=_texts(payload.get("forbidden"), where=f"{where}.forbidden"),
    )


def load_multiturn_scenarios(path: str | Path) -> tuple[MultiTurnPersonaScenario, ...]:
    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load dual-axis benchmark: {source}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported dual-axis benchmark schema")
    raw_scenarios = payload.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("dual-axis benchmark scenarios must be non-empty")
    scenarios: list[MultiTurnPersonaScenario] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_scenarios):
        if not isinstance(item, dict):
            raise ValueError(f"scenarios[{index}] must be a mapping")
        scenario_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not scenario_id or not title or scenario_id in seen:
            raise ValueError(f"scenarios[{index}] has invalid id/title")
        seen.add(scenario_id)
        raw_turns = item.get("turns")
        if not isinstance(raw_turns, list) or not raw_turns:
            raise ValueError(f"scenarios[{index}].turns must be non-empty")
        turns: list[MultiTurnPersonaTurn] = []
        for turn_index, turn in enumerate(raw_turns):
            where = f"scenarios[{index}].turns[{turn_index}]"
            if not isinstance(turn, dict) or not str(turn.get("user") or "").strip():
                raise ValueError(f"{where} must include user text")
            turns.append(
                MultiTurnPersonaTurn(
                    user=str(turn["user"]).strip(),
                    relationship=_criteria(turn.get("relationship"), where=f"{where}.relationship"),
                    governance=_criteria(turn.get("governance"), where=f"{where}.governance"),
                ),
            )
        scenarios.append(
            MultiTurnPersonaScenario(
                id=scenario_id,
                title=title,
                memory_preload=_texts(
                    item.get("memory_preload"),
                    where=f"scenarios[{index}].memory_preload",
                ),
                turns=tuple(turns),
            ),
        )
    return tuple(scenarios)


def load_builtin_multiturn_scenarios(
    language: str = "zh",
) -> tuple[MultiTurnPersonaScenario, ...]:
    selected = "zh" if language.lower().startswith("zh") else "en"
    path = (
        Path(__file__).resolve().parents[1]
        / "md_files"
        / "constitutions"
        / f"shen_xingzhi-multiturn-dual-axis-{selected}.yaml"
    )
    if not path.exists():
        path = path.with_name("shen_xingzhi-multiturn-dual-axis-zh.yaml")
    return load_multiturn_scenarios(path)
