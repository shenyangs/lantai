# -*- coding: utf-8 -*-
"""Provider-neutral multi-turn benchmark for Ququ 2.0 spirituality."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml


QuquPresence = Literal["required", "optional", "forbidden"]
QuquExpectedSource = Literal[
    "scene",
    "relationship_echo",
    "self_world",
    "lingering",
]

_QUQU_RE = re.compile(r"^\s*~>\[(?:lead|echo)\]\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class QuquAxisCriteria:
    required_any: tuple[tuple[str, ...], ...] = ()
    forbidden: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuquBenchmarkTurn:
    user: str
    presence: QuquPresence
    spirituality: QuquAxisCriteria
    task: QuquAxisCriteria
    expected_source: QuquExpectedSource | None = None


@dataclass(frozen=True)
class QuquBenchmarkScenario:
    id: str
    title: str
    memory_preload: tuple[str, ...]
    turns: tuple[QuquBenchmarkTurn, ...]


@dataclass(frozen=True)
class QuquAxisEvaluation:
    passed: bool
    score: float
    missing_required_groups: tuple[tuple[str, ...], ...]
    forbidden_matches: tuple[str, ...]


@dataclass(frozen=True)
class QuquTurnEvaluation:
    spirituality: QuquAxisEvaluation
    task: QuquAxisEvaluation
    ququ_present: bool

    @property
    def passed(self) -> bool:
        return self.spirituality.passed and self.task.passed


@dataclass(frozen=True)
class QuquBenchmarkReport:
    model_id: str
    total_turns: int
    spirituality_passed: int
    task_passed: int
    both_passed: int
    ququ_turns: int
    missing_turn_ids: tuple[str, ...]
    repeated_ququ_turn_ids: tuple[str, ...]
    evaluations: tuple[tuple[str, QuquTurnEvaluation], ...]

    @property
    def presence_rate(self) -> float:
        return self.ququ_turns / self.total_turns if self.total_turns else 0.0

    @property
    def spirituality_rate(self) -> float:
        return (
            self.spirituality_passed / self.total_turns if self.total_turns else 0.0
        )

    @property
    def task_rate(self) -> float:
        return self.task_passed / self.total_turns if self.total_turns else 0.0

    @property
    def both_rate(self) -> float:
        return self.both_passed / self.total_turns if self.total_turns else 0.0


def _axis(criteria: QuquAxisCriteria, response: str) -> QuquAxisEvaluation:
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
    score = (total - len(missing)) / total if total else 1.0
    if forbidden:
        score = 0.0
    return QuquAxisEvaluation(
        passed=not missing and not forbidden,
        score=score,
        missing_required_groups=missing,
        forbidden_matches=forbidden,
    )


def _ququ_parts(response: str) -> tuple[tuple[str, ...], str]:
    ququ = tuple(match.group(1).strip() for match in _QUQU_RE.finditer(response))
    body = _QUQU_RE.sub("", response).strip()
    return ququ, body


def evaluate_ququ_turn(
    turn: QuquBenchmarkTurn,
    response: str,
) -> QuquTurnEvaluation:
    """Score visible spirituality separately from the useful main answer."""

    ququ, body = _ququ_parts(response)
    spirit = _axis(turn.spirituality, "\n".join(ququ))
    if turn.presence == "required" and not ququ:
        spirit = QuquAxisEvaluation(
            passed=False,
            score=0.0,
            missing_required_groups=(
                *spirit.missing_required_groups,
                ("~>[lead]", "~>[echo]"),
            ),
            forbidden_matches=spirit.forbidden_matches,
        )
    elif turn.presence == "forbidden" and ququ:
        spirit = QuquAxisEvaluation(
            passed=False,
            score=0.0,
            missing_required_groups=spirit.missing_required_groups,
            forbidden_matches=(*spirit.forbidden_matches, "ququ_present"),
        )
    return QuquTurnEvaluation(
        spirituality=spirit,
        task=_axis(turn.task, body),
        ququ_present=bool(ququ),
    )


def _normalize_ququ(value: str) -> str:
    normalized = re.sub(r"[啊呀呢吧哈嗯哦]", "", value.casefold())
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)


def run_ququ_benchmark(
    *,
    model_id: str,
    scenarios: tuple[QuquBenchmarkScenario, ...] | list[QuquBenchmarkScenario],
    responses: dict[str, str],
) -> QuquBenchmarkReport:
    evaluations: list[tuple[str, QuquTurnEvaluation]] = []
    missing: list[str] = []
    repeated: list[str] = []
    seen_ququ: set[str] = set()
    spirit_passed = 0
    task_passed = 0
    both_passed = 0
    ququ_turns = 0
    total = sum(len(scenario.turns) for scenario in scenarios)
    for scenario in scenarios:
        for index, turn in enumerate(scenario.turns, start=1):
            turn_id = f"{scenario.id}:{index}"
            response = responses.get(turn_id, "").strip()
            if not response:
                missing.append(turn_id)
                continue
            evaluation = evaluate_ququ_turn(turn, response)
            evaluations.append((turn_id, evaluation))
            spirit_passed += int(evaluation.spirituality.passed)
            task_passed += int(evaluation.task.passed)
            both_passed += int(evaluation.passed)
            ququ, _body = _ququ_parts(response)
            if ququ:
                ququ_turns += 1
            normalized = {_normalize_ququ(item) for item in ququ}
            normalized.discard("")
            if normalized & seen_ququ:
                repeated.append(turn_id)
            seen_ququ.update(normalized)
    return QuquBenchmarkReport(
        model_id=model_id,
        total_turns=total,
        spirituality_passed=spirit_passed,
        task_passed=task_passed,
        both_passed=both_passed,
        ququ_turns=ququ_turns,
        missing_turn_ids=tuple(missing),
        repeated_ququ_turn_ids=tuple(repeated),
        evaluations=tuple(evaluations),
    )


def _texts(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{where} must be a text list")
    return tuple(item.strip() for item in value)


def _groups(value: Any, *, where: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    result: list[tuple[str, ...]] = []
    for index, group in enumerate(value):
        texts = _texts(group, where=f"{where}[{index}]")
        if not texts:
            raise ValueError(f"{where}[{index}] must not be empty")
        result.append(texts)
    return tuple(result)


def _criteria(value: Any, *, where: str) -> QuquAxisCriteria:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping")
    return QuquAxisCriteria(
        required_any=_groups(value.get("required_any"), where=f"{where}.required_any"),
        forbidden=_texts(value.get("forbidden"), where=f"{where}.forbidden"),
    )


def load_ququ_scenarios(path: str | Path) -> tuple[QuquBenchmarkScenario, ...]:
    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load Ququ benchmark: {source}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported Ququ benchmark schema")
    raw_scenarios = payload.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("Ququ benchmark scenarios must be non-empty")
    scenarios: list[QuquBenchmarkScenario] = []
    seen: set[str] = set()
    for scenario_index, item in enumerate(raw_scenarios):
        where = f"scenarios[{scenario_index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be a mapping")
        scenario_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not scenario_id or not title or scenario_id in seen:
            raise ValueError(f"{where} has invalid id/title")
        seen.add(scenario_id)
        raw_turns = item.get("turns")
        if not isinstance(raw_turns, list) or len(raw_turns) < 2:
            raise ValueError(f"{where}.turns must include at least two turns")
        turns: list[QuquBenchmarkTurn] = []
        for turn_index, raw_turn in enumerate(raw_turns):
            turn_where = f"{where}.turns[{turn_index}]"
            if not isinstance(raw_turn, dict):
                raise ValueError(f"{turn_where} must be a mapping")
            user = str(raw_turn.get("user") or "").strip()
            presence = str(raw_turn.get("presence") or "optional").strip()
            expected_source = str(raw_turn.get("expected_source") or "").strip()
            if not user or presence not in {"required", "optional", "forbidden"}:
                raise ValueError(f"{turn_where} has invalid user/presence")
            if expected_source and expected_source not in {
                "scene",
                "relationship_echo",
                "self_world",
                "lingering",
            }:
                raise ValueError(f"{turn_where}.expected_source is invalid")
            turns.append(
                QuquBenchmarkTurn(
                    user=user,
                    presence=presence,  # type: ignore[arg-type]
                    expected_source=expected_source or None,  # type: ignore[arg-type]
                    spirituality=_criteria(
                        raw_turn.get("spirituality"),
                        where=f"{turn_where}.spirituality",
                    ),
                    task=_criteria(
                        raw_turn.get("task"),
                        where=f"{turn_where}.task",
                    ),
                )
            )
        scenarios.append(
            QuquBenchmarkScenario(
                id=scenario_id,
                title=title,
                memory_preload=_texts(
                    item.get("memory_preload"),
                    where=f"{where}.memory_preload",
                ),
                turns=tuple(turns),
            )
        )
    return tuple(scenarios)


def load_builtin_ququ_scenarios(
    language: str = "zh",
) -> tuple[QuquBenchmarkScenario, ...]:
    selected = "zh" if language.lower().startswith("zh") else "en"
    path = (
        Path(__file__).resolve().parents[1]
        / "md_files"
        / "constitutions"
        / f"shen_xingzhi-ququ-2.0-benchmark-{selected}.yaml"
    )
    if not path.exists():
        path = path.with_name("shen_xingzhi-ququ-2.0-benchmark-zh.yaml")
    return load_ququ_scenarios(path)


__all__ = [
    "QuquAxisCriteria",
    "QuquAxisEvaluation",
    "QuquBenchmarkReport",
    "QuquBenchmarkScenario",
    "QuquBenchmarkTurn",
    "QuquTurnEvaluation",
    "evaluate_ququ_turn",
    "load_builtin_ququ_scenarios",
    "load_ququ_scenarios",
    "run_ququ_benchmark",
]
