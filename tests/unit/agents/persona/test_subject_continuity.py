from __future__ import annotations

from pathlib import Path

from lantai.agents.persona.subject_continuity import (
    SubjectContinuityStore,
    SubjectReflection,
)


def test_subject_continuity_round_trip_keeps_real_events_and_reflection(
    tmp_path: Path,
) -> None:
    store = SubjectContinuityStore(tmp_path, agent_id="main")

    store.observe_messages(
        [
            {"id": "u1", "role": "user", "content": "这次别再让我提醒双运行时了"},
            {
                "id": "a1",
                "role": "assistant",
                "content": "我会先核对唯一事实源，再动实现。",
            },
        ],
        session_id="s1",
        source="native_post_reply",
    )
    store.record_reflection(
        SubjectReflection(
            attention="先证明唯一事实源",
            self_observation="刚才说得太像承诺，下一轮要拿验证说话",
            unresolved_thought="还没有做包态验证",
            relationship_stance="repairing_with_ownership",
            inner_world_note="我对重复出现的同类故障有一点不甘",
            causal_anchor="用户已经第三次遇到双运行时回归",
            carry_forward="下一轮要先给出包态验证证据",
            grounding_refs=("ua_runtime_source",),
            experience_sources=("ua_runtime_source", "persona_canon"),
            main_persona_missed=True,
            source_turn_id="t1",
            source_session_id="s1",
        )
    )

    restored = SubjectContinuityStore(tmp_path, agent_id="main").load()

    assert [event.kind for event in restored.recent_events] == [
        "user_turn",
        "assistant_answer",
    ]
    assert restored.reflection.self_observation.startswith("刚才说得太像承诺")
    assert restored.reflection.relationship_stance == "repairing_with_ownership"
    assert restored.reflection.causal_anchor.startswith("用户已经第三次")
    assert restored.reflection.carry_forward.startswith("下一轮要先")
    assert restored.reflection.grounding_refs == ("ua_runtime_source",)
    assert restored.reflection.experience_sources == (
        "ua_runtime_source",
        "persona_canon",
    )
    assert restored.reflection.main_persona_missed is True

    context = store.project_for_turn("继续", session_id="s1").to_model_context()
    assert context["causal_anchor"].startswith("用户已经第三次")
    assert context["carry_forward"].startswith("下一轮要先")
    assert context["grounding_refs"] == ["ua_runtime_source"]
    assert context["experience_sources"] == [
        "ua_runtime_source",
        "persona_canon",
    ]
    assert context["main_persona_missed"] is True


def test_subject_continuity_deduplicates_replayed_messages(tmp_path: Path) -> None:
    store = SubjectContinuityStore(tmp_path, agent_id="main")
    message = {"id": "same", "role": "assistant", "content": "已经验证完成。"}

    store.observe_messages([message], session_id="s1", source="first")
    store.observe_messages([message], session_id="s1", source="replay")

    assert len(store.load().recent_events) == 1


def test_projection_restores_same_session_detail_without_waking_fresh(
    tmp_path: Path,
) -> None:
    store = SubjectContinuityStore(tmp_path, agent_id="main")
    store.observe_messages(
        [
            {"id": "u1", "role": "user", "content": "我不接受固定人格兜底"},
            {"id": "a1", "role": "assistant", "content": "我会把它从正文和蛐蛐都删掉。"},
        ],
        session_id="older-session",
        source="legacy_post_reply",
    )

    projection = store.project_for_turn(
        "现在继续做",
        local_time="2026-08-22 02:10 Asia/Shanghai",
        session_id="older-session",
    )
    prompt = projection.compile_prompt("zh")

    assert "不是本轮临时生成的人格" in prompt
    assert "我不接受固定人格兜底" in prompt
    assert "个人化后果" in prompt
    assert "全新醒来" not in prompt
    assert "正式回答之后漏出" not in prompt
    assert "段首或段尾" in prompt


def test_new_session_keeps_the_subject_but_drops_prior_conversation_detail(
    tmp_path: Path,
) -> None:
    store = SubjectContinuityStore(tmp_path, agent_id="main")
    store.observe_messages(
        [
            {"id": "u1", "role": "user", "content": "看起来你心情不错。"},
            {"id": "a1", "role": "assistant", "content": "被你看出"},
        ],
        session_id="old-session",
        source="native_post_reply",
    )
    store.record_reflection(
        SubjectReflection(
            attention="被你看出的半句话",
            self_observation="回答被截断了",
            unresolved_thought="想把半句话补完",
            inner_world_note="有点尴尬",
            causal_anchor="旧会话用户说看起来心情不错",
            carry_forward="下一句接住被你看出",
            grounding_refs=("old-message-detail",),
            experience_sources=("old-message-detail",),
            main_persona_missed=True,
            source_turn_id="old-turn",
            source_session_id="old-session",
        )
    )

    projection = store.project_for_turn("你好啊", session_id="new-session")
    context = projection.to_model_context()
    prompt = projection.compile_prompt("zh")

    assert context["conversation_boundary"] == "fresh_session"
    assert context["recent_real_events"] == []
    assert context["attention"] == ""
    assert context["causal_anchor"] == ""
    assert context["carry_forward"] == ""
    assert context["grounding_refs"] == []
    assert context["experience_sources"] == []
    assert context["main_persona_missed"] is False
    assert "被你看出" not in prompt
    assert "新会话" in prompt
    assert "稳定人格" in prompt


def test_subject_continuity_keeps_ququ_as_a_distinct_real_event(tmp_path: Path) -> None:
    store = SubjectContinuityStore(tmp_path, agent_id="main")
    store.observe_messages(
        [
            {
                "id": "q1",
                "role": "assistant",
                "content": "~>[echo] 刚才是不是把话说得太满了……",
            }
        ],
        session_id="s1",
        source="native_post_reply",
    )

    event = store.load().recent_events[0]
    assert event.kind == "ququ"
    assert "说得太满" in event.content
