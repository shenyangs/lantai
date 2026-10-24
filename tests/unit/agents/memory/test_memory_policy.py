# -*- coding: utf-8 -*-

from lantai.agents.memory.policy import MemoryPolicyEngine
from lantai.agents.memory.structured_user_memory import infer_memory_type


def test_naming_conversation_canary_produces_no_global_suggestions():
    policy = MemoryPolicyEngine()
    texts = [
        ("请你作为一名专业取名顾问", "instruction"),
        ("不要直接套用网络取名库，也不要机械摘取古诗词", "instruction"),
        ("我要的是能在现实生活中使用一辈子的中文姓名", "style"),
        ("父亲出生于北京，母亲出生于上海，宝宝生辰是 2026 年 7 月 1 日", "identity"),
        ("如果分析需要出生地，请说明哪些结论可能受影响，不要自行编造", "instruction"),
    ]

    results = [policy.evaluate(text, memory_type=kind) for text, kind in texts]

    assert not [
        result for result in results
        if result.decision == "suggest" and result.scope == "global"
    ]
    assert len([
        result for result in results
        if result.decision == "suggest" and result.scope == "project"
    ]) <= 1
    assert results[0].subject == "assistant"
    assert results[0].decision == "reject"
    assert results[3].sensitivity == "sensitive"
    assert results[3].decision == "reject"
    assert results[4].subject == "assistant"
    assert results[4].decision == "reject"


def test_durable_style_preference_is_a_high_value_global_suggestion():
    result = MemoryPolicyEngine().evaluate(
        "你不喜欢用“有事直接说”这类话收尾，希望语气更自然、克制",
        memory_type="style",
    )

    assert result.decision == "suggest"
    assert result.scope == "global"
    assert result.subject == "user"
    assert result.utility_score >= 0.8


def test_recurrence_across_sessions_can_promote_an_observation():
    result = MemoryPolicyEngine().evaluate(
        "回复时保留来源链接",
        memory_type="instruction",
        recurrence_count=2,
        distinct_session_count=2,
    )

    assert result.decision == "suggest"
    assert "2 个会话" in result.reason


def test_explicit_sensitive_memory_requires_second_confirmation():
    result = MemoryPolicyEngine().evaluate(
        "请记住我的出生时间是 1990 年 1 月 1 日 08:00",
        memory_type="identity",
        explicit_user_request=True,
    )

    assert result.decision == "suggest"
    assert result.sensitivity == "sensitive"


def test_postpartum_timeline_is_not_mistaken_for_a_future_instruction():
    text = "刚生完宝宝以后连续出差了十几天"

    assert infer_memory_type(text) == "identity"
    result = MemoryPolicyEngine().evaluate(
        text,
        memory_type="identity",
    )
    assert result.decision == "observe"


def test_current_output_feedback_stays_scoped_to_the_thread():
    result = MemoryPolicyEngine().evaluate(
        "用户对当前输出极度不满意，需要以更高标准重新做。",
        memory_type="style",
        explicit_user_request=True,
    )

    assert result.decision == "confirm"
    assert result.scope == "thread"


def test_durable_default_is_not_downgraded_by_one_off_noun():
    result = MemoryPolicyEngine().evaluate(
        "以后默认保留方案引用。",
        memory_type="instruction",
        explicit_user_request=True,
    )

    assert result.decision == "confirm"
    assert result.scope == "global"


def test_explicit_one_off_request_without_durable_signal_stays_thread_scoped():
    result = MemoryPolicyEngine().evaluate(
        "请记住本次方案只用蓝色。",
        memory_type="instruction",
        explicit_user_request=True,
    )

    assert result.decision == "confirm"
    assert result.scope == "thread"
