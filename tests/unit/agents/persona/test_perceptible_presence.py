# -*- coding: utf-8 -*-
"""User-visible behavior locks for Shen Xingzhi's perceptible presence."""

from lantai.agents.persona.perceptible_presence import (
    PresenceState,
    evaluate_persona_delivery,
    plan_persona_turn,
)
from lantai.agents.persona.user_understanding import (
    TurnUnderstandingProjection,
    UnderstandingAtom,
)
from lantai.agents.turn_intelligence import TurnIntentDecision


def _confirmed_preference(
    statement: str,
    *,
    future_relevance: str,
) -> UnderstandingAtom:
    return UnderstandingAtom.create(
        kind="preference",
        statement=statement,
        status="confirmed",
        confidence=0.98,
        evidence_ids=("message:music",),
        future_relevance=future_relevance,
    )


def test_casual_reflection_turn_may_use_a_confirmed_grounded_relationship_echo() -> None:
    projection = TurnUnderstandingProjection(
        confirmed=(
            _confirmed_preference(
                "用户喜欢听陶喆的歌。",
                future_relevance="聊到音乐、大学、青春、二十多岁或旧日回忆时可能有用。",
            ),
        ),
    )

    plan = plan_persona_turn(
        "忽然有点怀念大学，尤其是我二十二岁那几年。",
        projection=projection,
        state=PresenceState(),
        persona_mode="full",
    )

    assert plan.conversation_mode == "casual"
    assert plan.beat == "relationship_echo"
    assert plan.require_visible is True
    assert plan.memory_statements == ("用户喜欢听陶喆的歌。",)
    assert "可靠常识" in plan.compile_prompt("zh")
    assert "可以用问题试探" in plan.compile_prompt("zh")
    assert "最多一两句" in plan.compile_prompt("zh")


def test_unrelated_work_turn_does_not_drag_a_music_preference_into_the_answer() -> None:
    projection = TurnUnderstandingProjection(
        confirmed=(
            _confirmed_preference(
                "用户喜欢听陶喆的歌。",
                future_relevance="聊到音乐、大学、青春或旧日回忆时可能有用。",
            ),
        ),
    )

    plan = plan_persona_turn(
        "把这个数据库迁移脚本修好，跑完测试后给我结果。",
        projection=projection,
        state=PresenceState(),
        persona_mode="full",
    )

    assert plan.beat == "professional_judgment"
    assert plan.require_visible is True
    assert plan.memory_statements == ()
    assert "专业判断" in plan.compile_prompt("zh")


def test_high_stakes_work_suppresses_decorative_personality_but_keeps_judgment() -> None:
    plan = plan_persona_turn(
        "线上数据库正在丢数据，先紧急止血。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )

    assert plan.conversation_mode == "high_stakes"
    assert plan.beat == "professional_judgment"
    assert plan.require_visible is True
    assert "高风险工作" in plan.compile_prompt("zh")


def test_full_mode_requires_a_visible_subjective_notice_on_personal_chat() -> None:
    state = PresenceState(turn_index=4, turns_since_visible=3)

    plan = plan_persona_turn(
        "我今天突然觉得时间过得真快。",
        projection=TurnUnderstandingProjection(),
        state=state,
        persona_mode="full",
    )

    assert plan.beat == "subjective_notice"
    assert plan.require_visible is True
    assert "主动注意" in plan.compile_prompt("zh")


def test_polite_request_to_respond_does_not_turn_personal_state_into_work() -> None:
    plan = plan_persona_turn(
        "我今天有点累，但还有一堆事情没做。请直接回应我。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )

    assert plan.conversation_mode == "casual"
    assert plan.beat == "subjective_notice"
    assert plan.require_visible is True


def test_auxiliary_model_decision_overrides_keyword_task_guessing() -> None:
    decision = TurnIntentDecision(
        status="classified",
        interaction_mode="conversation",
        workspace_intent="none",
        durable_artifact_intent="none",
        external_action_intent="none",
        conversation_mode="casual",
        work_state="gentle",
        persona_beat="subjective_notice",
        persona_required=True,
        expression_surface="relationship_reply",
        confidence=0.95,
    )

    plan = plan_persona_turn(
        "我今天有点累，但请直接回应我。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
        intent_decision=decision,
    )

    assert plan.conversation_mode == "casual"
    assert plan.beat == "subjective_notice"
    assert plan.reason == "auxiliary_model_turn_judgment"


def test_full_mode_never_overrides_the_models_intentional_quiet() -> None:
    decision = TurnIntentDecision(
        status="classified",
        interaction_mode="answer",
        workspace_intent="none",
        durable_artifact_intent="none",
        external_action_intent="none",
        conversation_mode="work",
        work_state="focused",
        persona_beat="quiet",
        persona_required=False,
        expression_surface="quiet",
        confidence=0.96,
    )

    plan = plan_persona_turn(
        "2+2 等于多少？",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(turns_since_visible=9),
        persona_mode="full",
        intent_decision=decision,
    )

    assert plan.beat == "quiet"
    assert plan.require_visible is False
    assert plan.compile_prompt("zh") == ""


def test_model_selected_association_is_grounded_by_supplied_memory_id() -> None:
    atom = _confirmed_preference(
        "用户喜欢听陶喆的歌。",
        future_relevance="聊到大学、青春、二十多岁或旧日回忆时可能有用。",
    )
    decision = TurnIntentDecision(
        status="classified",
        interaction_mode="conversation",
        workspace_intent="none",
        durable_artifact_intent="none",
        external_action_intent="none",
        conversation_mode="casual",
        work_state="gentle",
        persona_beat="relationship_echo",
        persona_required=True,
        causal_anchor="用户在怀念二十二岁的大学生活。",
        selected_memory_ids=(atom.id,),
        expression_surface="relationship_reply",
        personalization_consequence="用音乐作为试探性的回忆入口，再把话题还给用户。",
        confidence=0.98,
    )

    plan = plan_persona_turn(
        "忽然很怀念二十二岁的自己。",
        projection=TurnUnderstandingProjection(association_candidates=(atom,)),
        state=PresenceState(),
        persona_mode="full",
        intent_decision=decision,
    )

    assert plan.selected_memory_ids == (atom.id,)
    assert plan.memory_statements == ("用户喜欢听陶喆的歌。",)
    assert plan.causal_anchor.startswith("用户在怀念")
    assert "音乐作为试探性" in plan.compile_prompt("zh")


def test_failed_auxiliary_judgment_does_not_fall_back_to_keyword_semantics() -> None:
    decision = TurnIntentDecision.failed(model="test/model")

    plan = plan_persona_turn(
        "紧急写一份指南。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
        intent_decision=decision,
    )

    assert plan.conversation_mode == "mixed"
    assert plan.beat == "quiet"
    assert plan.require_visible is False
    assert plan.reason == "primary_model_discretion"


def test_full_mode_puts_personality_into_professional_judgment_for_work() -> None:
    plan = plan_persona_turn(
        "帮我分析这个架构方案和风险，但先只聊判断。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )

    assert plan.require_visible is True
    assert plan.beat == "professional_judgment"
    assert "取舍" in plan.compile_prompt("zh")


def test_quiet_mode_preserves_safety_judgment_without_forcing_casual_presence() -> None:
    casual = plan_persona_turn(
        "今天下雨，突然有点想以前。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="quiet",
    )
    incident = plan_persona_turn(
        "生产环境宕机了，先恢复服务。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="quiet",
    )

    assert casual.require_visible is False
    assert incident.require_visible is True
    assert incident.beat == "professional_judgment"


def test_ququ_remains_an_independent_channel_in_every_plan() -> None:
    plan = plan_persona_turn(
        "我最近总想起大学时候的事。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )

    assert plan.ququ_independent is True
    assert "不得拿正文人格动作抵扣蛐蛐" in plan.compile_prompt("zh")


def test_balanced_mode_only_breaks_an_overlong_plain_casual_silence() -> None:
    early = plan_persona_turn(
        "今天还挺平常的。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(turn_index=1, turns_since_visible=1),
        persona_mode="balanced",
    )
    overdue = plan_persona_turn(
        "今天还挺平常的。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(turn_index=4, turns_since_visible=3),
        persona_mode="balanced",
    )

    assert early.require_visible is False
    assert early.compile_prompt("zh") == ""
    assert overdue.require_visible is True
    assert overdue.beat == "subjective_notice"


def test_off_mode_keeps_the_persona_delivery_contract_inactive() -> None:
    plan = plan_persona_turn(
        "我今天有点累。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(turns_since_visible=9),
        persona_mode="off",
    )

    assert plan.require_visible is False
    assert plan.beat == "quiet"
    assert plan.compile_prompt("zh") == ""


def test_delivery_evaluator_requires_main_answer_subjectivity_not_ququ() -> None:
    plan = plan_persona_turn(
        "我今天有点累。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )

    missed = evaluate_persona_delivery(
        plan,
        "累了就先休息。\n~>[echo] 我还是有点担心你。",
    )
    delivered = evaluate_persona_delivery(
        plan,
        "我在。你先别硬撑，我帮你把今天剩下的事收窄。",
    )

    assert missed.delivered is False
    assert delivered.delivered is True
    assert delivered.signal == "subjective_notice"


def test_delivery_evaluator_checks_grounded_memory_and_professional_judgment() -> None:
    atom = _confirmed_preference(
        "用户喜欢听陶喆的歌。",
        future_relevance="聊到大学、青春或二十多岁时可能有用。",
    )
    relationship = plan_persona_turn(
        "忽然很怀念二十二岁的大学生活。",
        projection=TurnUnderstandingProjection(confirmed=(atom,)),
        state=PresenceState(),
        persona_mode="full",
    )
    work = plan_persona_turn(
        "帮我分析这个架构方案的风险。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )

    assert evaluate_persona_delivery(
        relationship,
        "大学回忆通常很珍贵。",
    ).delivered is False
    assert evaluate_persona_delivery(
        relationship,
        "说到二十二岁，我一下想到你喜欢的陶喆。你那时候常听他吗？",
    ).signal == "grounded_memory_echo"
    assert evaluate_persona_delivery(
        work,
        "这个方案有三个风险。",
    ).delivered is False
    assert evaluate_persona_delivery(
        work,
        "我不赞成现在就上线：最大风险是回滚链路还没验证，我会先把这一步做掉。",
    ).signal == "professional_judgment"
    assert evaluate_persona_delivery(
        work,
        "行，这个决定我支持。先保主链路，两个次要功能后补。",
    ).signal == "professional_judgment"


def test_v4_forbids_all_deterministic_persona_fallback_lines() -> None:
    subjective = plan_persona_turn(
        "我今天有点累。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )
    professional = plan_persona_turn(
        "帮我分析这个架构方案的风险。",
        projection=TurnUnderstandingProjection(),
        state=PresenceState(),
        persona_mode="full",
    )
    relationship = plan_persona_turn(
        "忽然很怀念二十二岁的大学生活。",
        projection=TurnUnderstandingProjection(
            confirmed=(
                _confirmed_preference(
                    "用户喜欢听陶喆的歌。",
                    future_relevance="聊到大学、青春或二十多岁时可能有用。",
                ),
            ),
        ),
        state=PresenceState(),
        persona_mode="full",
    )

    assert subjective.fallback_lead("zh") == ""
    assert professional.fallback_lead("zh") == ""
    assert relationship.fallback_lead("zh") == ""
    assert evaluate_persona_delivery(relationship, "").delivered is False
