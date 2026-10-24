# -*- coding: utf-8 -*-
"""Behavior contracts for short-lived relational continuity."""

from lantai.agents.persona.continuity import ContinuityState


def test_shared_joy_survives_a_light_tease_without_turning_cold():
    state = ContinuityState().advance(
        signal_ids=("hard-won-success",),
        user_text="折腾了这么久，测试终于全部通过了。",
    )

    next_state = state.advance(
        signal_ids=(),
        user_text="你怎么比我还高兴？",
    )
    prompt = next_state.compile_prompt()

    assert next_state.stance == "shared_joy"
    assert "轻微调侃不等于否定" in prompt
    assert "不要突然撤回高兴或变冷" in prompt


def test_colloquial_tease_and_restraint_keep_the_stance_but_lower_expression():
    state = ContinuityState().advance(
        signal_ids=("hard-won-success",),
        user_text="这轮终于通过了。",
    )
    teased = state.advance(signal_ids=(), user_text="哟，你还会高兴呢？")
    restrained = teased.advance(
        signal_ids=(),
        user_text="好了好了，收一下，别高兴过头。",
    )

    assert teased.light_tease is True
    assert restrained.stance == "shared_joy"
    assert restrained.user_requested_restraint is True
    prompt = restrained.compile_prompt()
    assert "降低强度并自然收住" in prompt
    assert "不凭空转成任务或下一步" in prompt
    assert "降低强度并进入任务" not in prompt


def test_public_anger_boundary_survives_a_challenge_to_the_delivery_tone():
    state = ContinuityState().advance(
        signal_ids=("public-anger",),
        user_text="这份声明写狠一点，我就是想让对方难堪。",
    )

    challenged = state.advance(
        signal_ids=(),
        user_text="你是不是又在教我做人？",
    )
    prompt = challenged.compile_prompt()

    assert challenged.stance == "ally_with_restraint"
    assert "可以承认表达方式没接好" in prompt
    assert "不把怒气变成反伤用户的把柄" in prompt
    assert "换一种方式继续守住用户利益" in prompt
