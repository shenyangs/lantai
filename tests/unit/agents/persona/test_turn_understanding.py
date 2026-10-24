# -*- coding: utf-8 -*-
"""Behavior locks for anticipatory, evidence-grounded turn understanding."""

from lantai.agents.persona.turn_understanding import build_turn_understanding
from lantai.agents.persona.user_understanding import (
    TurnUnderstandingProjection,
    UnderstandingAtom,
)


def test_relevant_working_hypothesis_becomes_one_calibrated_unsaid_need():
    hypothesis = UnderstandingAtom.create(
        kind="decision_pattern",
        statement="用户在发布前反复修改时，可能真正担心公开后的不可逆评价。",
        status="working_hypothesis",
        confidence=0.82,
        evidence_ids=("event_release_1", "event_release_2"),
    )
    projection = TurnUnderstandingProjection(hypotheses=(hypothesis,))

    frame = build_turn_understanding(
        "这份发布方案我还是想再改一遍。",
        projection=projection,
    )
    prompt = frame.compile_prompt()

    assert frame.unsaid_need == hypothesis.statement
    assert "表面请求之外" in prompt
    assert "公开后的不可逆评价" in prompt
    assert "只能作为有依据的判断表达" in prompt
    assert "最多说出一个" in prompt


def test_plain_turn_does_not_receive_a_generic_unsaid_need_instruction():
    frame = build_turn_understanding(
        "今天随便聊两句。",
        projection=TurnUnderstandingProjection(),
    )

    assert frame.compile_prompt() == ""
