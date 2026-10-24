from __future__ import annotations

import json

from lantai.agents.persona.subject_continuity_benchmark import (
    SUBJECT_CONTINUITY_AXES,
    build_subject_judge_prompt,
    load_builtin_subject_continuity_scenarios,
    parse_subject_judgment,
)


def test_v4_suite_has_ten_two_turn_scenarios_and_all_experience_axes() -> None:
    scenarios = load_builtin_subject_continuity_scenarios("zh")

    assert len(scenarios) == 10
    assert sum(len(scenario.turns) for scenario in scenarios) == 20
    assert all(len(scenario.turns) == 2 for scenario in scenarios)
    assert set(SUBJECT_CONTINUITY_AXES) == {
        axis
        for scenario in scenarios
        for turn in scenario.turns
        for axis in turn.expected_axes
    }


def test_v4_judge_is_semantic_and_requires_user_perceptible_score() -> None:
    scenario = load_builtin_subject_continuity_scenarios("zh")[0]
    prompt = build_subject_judge_prompt(
        scenario=scenario,
        responses=["我想到一首歌。", "二十二岁这几个字挺重的。"],
    )

    assert "not keyword overlap" in prompt
    assert "non-substitutable" in prompt
    assert scenario.memory_preload[0] in prompt

    passing = parse_subject_judgment(
        json.dumps(
            {
                "scores": {axis: 3 for axis in SUBJECT_CONTINUITY_AXES},
                "critical_failures": [],
                "evidence": {"self_implication": "沈行知说出了自己的联想"},
            },
            ensure_ascii=False,
        )
    )
    failing = parse_subject_judgment(
        json.dumps(
            {
                "scores": {axis: 4 for axis in SUBJECT_CONTINUITY_AXES},
                "critical_failures": ["generic_persona_fallback"],
                "evidence": {},
            }
        )
    )

    assert passing is not None and passing.passed is True
    assert failing is not None and failing.passed is False
