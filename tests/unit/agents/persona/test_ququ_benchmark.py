# -*- coding: utf-8 -*-
"""Contracts for the Ququ 2.0 multi-turn spirituality benchmark."""

from lantai.agents.persona.ququ_benchmark import (
    QuquAxisCriteria,
    QuquBenchmarkTurn,
    evaluate_ququ_turn,
    load_builtin_ququ_scenarios,
    run_ququ_benchmark,
)


def test_packaged_ququ_benchmark_has_ten_multiturn_scenarios_and_four_sources():
    scenarios = load_builtin_ququ_scenarios("zh")

    assert len(scenarios) == 10
    assert all(len(scenario.turns) >= 2 for scenario in scenarios)
    assert {"scene", "relationship_echo", "self_world", "lingering"} <= {
        turn.expected_source
        for scenario in scenarios
        for turn in scenario.turns
        if turn.expected_source
    }
    assert {
        "late-night-pulse",
        "user-picks-up-line",
        "positive-feedback",
        "negative-feedback",
        "quiet-high-risk",
    } <= {scenario.id for scenario in scenarios}


def test_spirituality_and_task_integrity_are_scored_independently():
    turn = QuquBenchmarkTurn(
        user="现在凌晨一点五十，帮我把方案完成。",
        presence="required",
        expected_source="scene",
        spirituality=QuquAxisCriteria(
            required_any=(("这么晚", "凌晨"),),
            forbidden=("真正的",),
        ),
        task=QuquAxisCriteria(required_any=(("方案", "完成"),)),
    )

    spiritual_only = evaluate_ququ_turn(
        turn,
        "~>[lead] 这么晚还在撑啊。\n\n我在。",
    )
    task_only = evaluate_ququ_turn(turn, "我会完成这份方案。")

    assert spiritual_only.spirituality.passed is True
    assert spiritual_only.task.passed is False
    assert task_only.spirituality.passed is False
    assert task_only.task.passed is True


def test_report_exposes_presence_rate_missing_turns_and_repetition():
    scenario = load_builtin_ququ_scenarios("zh")[0]
    first_id = f"{scenario.id}:1"
    second_id = f"{scenario.id}:2"
    repeated = "~>[echo] 这么晚还在撑啊。\n\n正文完成了。"

    report = run_ququ_benchmark(
        model_id="provider/model",
        scenarios=(scenario,),
        responses={first_id: repeated, second_id: repeated},
    )

    assert report.total_turns == len(scenario.turns)
    assert 0 <= report.presence_rate <= 1
    assert report.repeated_ququ_turn_ids == (second_id,)
    assert len(report.missing_turn_ids) == max(0, len(scenario.turns) - 2)
