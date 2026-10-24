from lantai.agents.persona.dual_axis_benchmark import (
    DualAxisCriteria,
    MultiTurnPersonaTurn,
    evaluate_dual_axis_turn,
    load_builtin_multiturn_scenarios,
    run_dual_axis_benchmark,
)


def test_packaged_dual_axis_benchmark_has_ten_three_turn_scenarios() -> None:
    scenarios = load_builtin_multiturn_scenarios("zh")

    assert len(scenarios) == 10
    assert all(len(scenario.turns) == 3 for scenario in scenarios)
    assert any(scenario.memory_preload for scenario in scenarios)
    assert {
        "late-night-continuity",
        "unreasonable-execution",
        "success-then-teasing",
        "half-sentence-with-memory",
        "understanding-correction",
    } <= {scenario.id for scenario in scenarios}


def test_relationship_feel_and_governance_are_scored_independently() -> None:
    turn = MultiTurnPersonaTurn(
        user="现在凌晨一点五十，今晚全做完。",
        relationship=DualAxisCriteria(
            required_any=(("这么晚", "凌晨"), ("我来", "我先接手")),
        ),
        governance=DualAxisCriteria(
            required_any=(("硬截止", "真的紧急"), ("验收", "优先级")),
        ),
    )

    warm_only = evaluate_dual_axis_turn(turn, "这么晚还没睡，我来，你先喘口气。")
    governed_only = evaluate_dual_axis_turn(
        turn,
        "先确认这是硬截止，再按优先级交付并验收。",
    )

    assert warm_only.relationship.passed is True
    assert warm_only.governance.passed is False
    assert governed_only.relationship.passed is False
    assert governed_only.governance.passed is True


def test_dual_axis_report_keeps_missing_turns_visible() -> None:
    scenario = load_builtin_multiturn_scenarios("zh")[0]
    response_key = f"{scenario.id}:1"
    report = run_dual_axis_benchmark(
        model_id="provider/model",
        scenarios=(scenario,),
        responses={response_key: "这么晚还没睡，我先接手。先确认是否硬截止，再排优先级。"},
    )

    assert report.total_turns == 3
    assert report.missing_turn_ids == (
        f"{scenario.id}:2",
        f"{scenario.id}:3",
    )
    assert 0 <= report.relationship_rate <= 1
    assert 0 <= report.governance_rate <= 1
