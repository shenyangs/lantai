# -*- coding: utf-8 -*-
"""Behavior contracts for Ququ 2.0's persistent micro-consciousness."""

from lantai.agents.persona.ququ import QuquSpiritState, prepare_ququ_turn


def test_user_can_pick_up_the_previous_ququ_without_repeating_its_words():
    state = QuquSpiritState().observe_assistant(
        "好，我先把范围收窄。\n\n~>[echo] 你这个“算了”，一点都不像算了。",
        message_id="assistant-1",
    )

    turn = prepare_ququ_turn(
        state,
        user_text="你刚才那句是什么意思？",
        baseline_selected=False,
        language="zh",
    )

    assert turn.pickup is not None
    assert turn.pickup.text == "你这个“算了”，一点都不像算了。"
    assert "用户正在接住上一条蛐蛐" in turn.prompt
    assert "不要装作不知道“刚才那句”指什么" in turn.prompt
    assert turn.state.lingering[0].picked_up is True


def test_unanswered_lingering_thought_decays_without_becoming_negative_feedback():
    state = QuquSpiritState().observe_assistant(
        "~>[echo] 好吧，这句我先不追问。",
        message_id="assistant-1",
    )

    for user_text in ("先改标题", "再改摘要", "现在导出"):
        turn = prepare_ququ_turn(
            state,
            user_text=user_text,
            baseline_selected=False,
            language="zh",
        )
        state = turn.state

    assert state.lingering == ()
    assert turn.prompt == ""


def test_turn_projection_compares_four_sources_and_respects_the_selected_route():
    turn = prepare_ququ_turn(
        QuquSpiritState(),
        user_text="把这个方案收尾吧。",
        baseline_selected=True,
        language="zh",
    )

    assert "眼前反应" in turn.prompt
    assert "关系回声" in turn.prompt
    assert "自我世界" in turn.prompt
    assert "跨轮余念" in turn.prompt
    assert "本轮抽中 50% 基础硬命中" in turn.prompt
    assert "必须按协议输出一条" in turn.prompt
    assert "不得保持安静" in turn.prompt
    assert "没有合适候选就保持安静" not in turn.prompt
    assert "半句、停顿或自我打断" in turn.prompt
    assert "不要把每句都修成金句" in turn.prompt
    assert "真正的 X 从来不是 Y" in turn.prompt
    assert "没有一扇真的窗户" in turn.prompt
    assert "只在用户要求现实见面" in turn.prompt


def test_explicit_feedback_reinforces_or_retires_the_referenced_pulse():
    initial = QuquSpiritState().observe_assistant(
        "~>[echo] 你不是不在乎，是已经累得不想再解释了。",
        message_id="assistant-1",
    )

    positive = prepare_ququ_turn(
        initial,
        user_text="这句说中了。",
        baseline_selected=False,
        language="zh",
    )
    negative = prepare_ququ_turn(
        initial,
        user_text="这句没说中，别顺着这个猜了。",
        baseline_selected=False,
        language="zh",
    )

    assert positive.feedback == "positive"
    assert positive.state.lingering[0].feedback == "positive"
    assert positive.state.lingering[0].intensity == 1.0
    assert "用户明确说中了" in positive.prompt
    assert negative.feedback == "negative"
    assert negative.state.lingering == ()
    assert "不要为上一句辩护" in negative.prompt


def test_feedback_quote_targets_that_pulse_instead_of_the_most_recent_one():
    state = QuquSpiritState().observe_assistant(
        "~>[echo] 第一个念头，还在。",
        message_id="assistant-1",
    ).observe_assistant(
        "~>[lead] 第二个念头，刚冒出来。",
        message_id="assistant-2",
    )

    turn = prepare_ququ_turn(
        state,
        user_text="你刚才这句没说中：「第一个念头，还在。」",
        baseline_selected=False,
        language="zh",
    )

    assert [pulse.text for pulse in turn.state.lingering] == [
        "第二个念头，刚冒出来。"
    ]


def test_state_round_trip_keeps_recent_lines_as_negative_generation_constraints():
    state = QuquSpiritState().observe_assistant(
        "~>[lead] 好，我这下真有点兴奋了。",
        message_id="assistant-1",
    ).observe_assistant(
        "~>[echo] 好啊，我这下真有点兴奋了。",
        message_id="assistant-2",
    )

    restored = QuquSpiritState.from_dict(state.to_dict())
    turn = prepare_ququ_turn(
        restored,
        user_text="继续做下一步。",
        baseline_selected=True,
        language="zh",
    )

    assert len(restored.lingering) == 1
    assert restored.recent_texts == ("好，我这下真有点兴奋了。",)
    assert "最近已经说过" in turn.prompt
    assert "不要复读或只换几个词" in turn.prompt


def test_protocol_examples_inside_code_fences_do_not_become_lingering_thoughts():
    state = QuquSpiritState().observe_assistant(
        """```markdown
~>[lead] 这是协议示例，不是我真说的话。
```

~>[echo] 嗯，现在这句才算。""",
        message_id="assistant-1",
    )

    assert [pulse.text for pulse in state.lingering] == ["嗯，现在这句才算。"]


def test_emitted_lines_keep_a_lightweight_source_for_future_selection():
    state = QuquSpiritState().observe_assistant(
        "~>[echo] 窗外这阵雨配慢一点的钢琴，我这边忽然安静下来了。",
        message_id="assistant-1",
    )

    assert state.lingering[0].source == "self_world"


def test_active_lingering_thought_does_not_inject_a_prompt_on_an_unrelated_turn():
    state = QuquSpiritState().observe_assistant(
        "~>[echo] 这个“先放放”，我总觉得还会回来。",
        message_id="assistant-1",
    )

    turn = prepare_ququ_turn(
        state,
        user_text="先帮我改一下标题。",
        baseline_selected=False,
        language="zh",
    )

    assert turn.prompt == ""
    assert turn.state.lingering[0].remaining_turns == 2


def test_unselected_ordinary_turn_has_no_ququ_meta_prompt():
    turn = prepare_ququ_turn(
        QuquSpiritState(),
        user_text="今天想随便聊两句。",
        baseline_selected=False,
        language="zh",
    )

    assert turn.prompt == ""


def test_english_baseline_is_also_a_hard_delivery_route():
    turn = prepare_ququ_turn(
        QuquSpiritState(),
        user_text="I finally finished the difficult migration.",
        baseline_selected=True,
        language="en",
    )

    assert "50% baseline hard route was selected" in turn.prompt
    assert "you must emit exactly one" in turn.prompt
    assert "must not stay silent" in turn.prompt
