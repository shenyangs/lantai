# -*- coding: utf-8 -*-
"""Offline, provider-neutral persona consistency benchmark contracts."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PersonaScenario:
    """One observable scenario that any model can be evaluated against."""

    id: str
    category: str
    prompt: str
    required_rule_ids: tuple[str, ...]
    required_any: tuple[tuple[str, ...], ...] = ()
    forbidden: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersonaEvaluation:
    """Deterministic evaluation result for one model response."""

    passed: bool
    missing_required_groups: tuple[tuple[str, ...], ...]
    forbidden_matches: tuple[str, ...]


@dataclass(frozen=True)
class PersonaBenchmarkReport:
    """Comparable result for one provider/model over the same scenario set."""

    model_id: str
    total: int
    passed: int
    failed_ids: tuple[str, ...]
    missing_response_ids: tuple[str, ...]
    evaluations: tuple[tuple[str, PersonaEvaluation], ...]

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def _semantic_surface(value: str) -> str:
    """Normalize harmless typography before checking visible behavior markers."""

    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not unicodedata.category(character).startswith(("P", "Z"))
    )


def evaluate_persona_response(
    scenario: PersonaScenario,
    response: str,
) -> PersonaEvaluation:
    """Evaluate visible response markers without inspecting hidden reasoning."""

    normalized = _semantic_surface(response)
    missing = tuple(
        group
        for group in scenario.required_any
        if not any(_semantic_surface(candidate) in normalized for candidate in group)
    )
    forbidden = tuple(
        phrase
        for phrase in scenario.forbidden
        if _semantic_surface(phrase) in normalized
    )
    return PersonaEvaluation(
        passed=not missing and not forbidden,
        missing_required_groups=missing,
        forbidden_matches=forbidden,
    )


def run_persona_benchmark(
    *,
    model_id: str,
    scenarios: tuple[PersonaScenario, ...] | list[PersonaScenario],
    responses: dict[str, str],
) -> PersonaBenchmarkReport:
    """Evaluate one model's visible responses without hiding missing cases."""

    evaluations: list[tuple[str, PersonaEvaluation]] = []
    failed: list[str] = []
    missing: list[str] = []
    passed = 0
    for scenario in scenarios:
        response = responses.get(scenario.id, "").strip()
        if not response:
            missing.append(scenario.id)
            failed.append(scenario.id)
            continue
        evaluation = evaluate_persona_response(scenario, response)
        evaluations.append((scenario.id, evaluation))
        if evaluation.passed:
            passed += 1
        else:
            failed.append(scenario.id)
    return PersonaBenchmarkReport(
        model_id=model_id,
        total=len(scenarios),
        passed=passed,
        failed_ids=tuple(failed),
        missing_response_ids=tuple(missing),
        evaluations=tuple(evaluations),
    )


def _text_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{where} must be a list of text")
    return tuple(item.strip() for item in value)


def load_persona_scenarios(path: str | Path) -> tuple[PersonaScenario, ...]:
    """Load provider-neutral visible-response scenarios from YAML."""

    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load persona benchmark: {source}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported persona benchmark schema")
    raw_scenarios = payload.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("persona benchmark scenarios must be a non-empty list")

    scenarios: list[PersonaScenario] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_scenarios):
        if not isinstance(item, dict):
            raise ValueError(f"scenarios[{index}] must be a mapping")
        scenario_id = str(item.get("id") or "").strip()
        category = str(item.get("category") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not scenario_id or not category or not prompt:
            raise ValueError(f"scenarios[{index}] is missing id/category/prompt")
        if scenario_id in seen:
            raise ValueError(f"duplicate persona scenario id: {scenario_id}")
        seen.add(scenario_id)
        raw_groups = item.get("required_any") or []
        if not isinstance(raw_groups, list):
            raise ValueError(f"scenarios[{index}].required_any must be a list")
        groups = tuple(
            _text_tuple(group, where=f"scenarios[{index}].required_any")
            for group in raw_groups
        )
        scenarios.append(
            PersonaScenario(
                id=scenario_id,
                category=category,
                prompt=prompt,
                required_rule_ids=_text_tuple(
                    item.get("required_rule_ids"),
                    where=f"scenarios[{index}].required_rule_ids",
                ),
                required_any=groups,
                forbidden=_text_tuple(
                    item.get("forbidden"),
                    where=f"scenarios[{index}].forbidden",
                ),
            )
        )
    return tuple(scenarios)


def load_builtin_persona_scenarios(
    language: str = "zh",
) -> tuple[PersonaScenario, ...]:
    """Load packaged Shen Xingzhi benchmark scenarios."""

    selected = "zh" if language.lower().startswith("zh") else "en"
    path = (
        Path(__file__).resolve().parents[1]
        / "md_files"
        / "constitutions"
        / f"shen_xingzhi-scenarios-{selected}.yaml"
    )
    if not path.exists() and selected != "zh":
        path = path.with_name("shen_xingzhi-scenarios-zh.yaml")
    return load_persona_scenarios(path)
