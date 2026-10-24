# -*- coding: utf-8 -*-
"""User-visible contracts for Shen Xingzhi's sparse user understanding."""

from lantai.agents.persona.user_understanding import (
    UnderstandingAtom,
    UnderstandingCandidate,
    UserUnderstandingStore,
)

from datetime import datetime, timezone


def test_turn_projection_keeps_confirmed_knowledge_separate_from_a_working_guess(
    tmp_path,
):
    store = UserUnderstandingStore(tmp_path)
    store.remember(
        UnderstandingAtom.create(
            kind="preference",
            statement="用户希望沈行知主动说出没有明说的担心。",
            status="confirmed",
            confidence=1.0,
            evidence_ids=("event_preference",),
        ),
    )
    store.remember(
        UnderstandingAtom.create(
            kind="decision_pattern",
            statement="用户在发布前反复修改时，可能真正担心公开后的不可逆评价。",
            status="working_hypothesis",
            confidence=0.78,
            evidence_ids=("event_release_1", "event_release_2"),
        ),
    )

    prompt = store.project_for_turn("这个发布方案我还是想再改一遍").compile_prompt()

    assert "已确认可信，且与当前 case 相关" in prompt
    assert "用户希望沈行知主动说出没有明说的担心" in prompt
    assert "工作假设、只能作为有依据的猜测表达" in prompt
    assert "用户在发布前反复修改时，可能真正担心" in prompt


def test_confirmed_old_output_feedback_does_not_leak_into_an_unrelated_new_case(
    tmp_path,
):
    store = UserUnderstandingStore(tmp_path)
    statement = (
        "用户对当前输出极度不满意，认为其质量远低于可接受标准，"
        "需要以更高标准重新做。"
    )
    store.remember(
        UnderstandingAtom.create(
            kind="preference",
            statement=statement,
            status="confirmed",
            confidence=1.0,
            evidence_ids=("old-case:output-feedback",),
        ),
    )

    projection = store.project_for_turn("做一个模仿超级马里奥的小游戏")

    assert projection.confirmed == ()
    assert statement not in projection.compile_prompt()


def test_confirmed_memory_is_recalled_when_the_current_case_is_relevant(tmp_path):
    store = UserUnderstandingStore(tmp_path)
    atom = store.remember(
        UnderstandingAtom.create(
            kind="value",
            statement="用户在发布决策中更看重真实体感。",
            status="confirmed",
            confidence=1.0,
            evidence_ids=("release:value",),
        ),
    )

    projection = store.project_for_turn("这次发布的体感还是不对")

    assert projection.confirmed == (atom,)


def test_future_relevance_is_only_an_association_candidate_not_direct_recall(
    tmp_path,
) -> None:
    store = UserUnderstandingStore(tmp_path)
    store.remember(
        UnderstandingAtom.create(
            kind="preference",
            statement="用户喜欢听陶喆的歌。",
            status="confirmed",
            confidence=0.98,
            evidence_ids=("message:music",),
            future_relevance="聊到音乐、大学、青春、二十多岁或旧日回忆时可能有用。",
        ),
    )

    projection = store.project_for_turn("忽然很怀念大学里二十二岁的自己")
    candidates = store.association_palette(max_items=12)

    assert projection.confirmed == ()
    assert [item.statement for item in candidates] == ["用户喜欢听陶喆的歌。"]


def test_user_correction_immediately_removes_a_wrong_guess_from_later_turns(
    tmp_path,
):
    store = UserUnderstandingStore(tmp_path)
    atom = store.remember(
        UnderstandingAtom.create(
            kind="decision_pattern",
            statement="用户反复修改时通常只是害怕失败。",
            status="working_hypothesis",
            confidence=0.81,
            evidence_ids=("event_guess_1", "event_guess_2"),
        ),
    )

    corrected = store.apply_feedback(atom.id, "corrected")
    next_prompt = store.project_for_turn("我还想继续修改").compile_prompt()

    assert corrected is not None
    assert corrected.status == "contradicted"
    assert corrected.corrected_hits == 1
    assert "害怕失败" not in next_prompt


def test_source_resync_does_not_overwrite_an_explicit_user_correction(tmp_path):
    store = UserUnderstandingStore(tmp_path)
    atom = store.remember(
        UnderstandingAtom.create(
            kind="open_loop",
            statement="这件事还没做完。",
            status="confirmed",
            confidence=1.0,
            evidence_ids=("activity:task:one",),
        ),
    )
    store.apply_feedback(atom.id, "corrected")

    resynced = store.remember(
        UnderstandingAtom.create(
            kind="open_loop",
            statement="这件事还没做完。",
            status="confirmed",
            confidence=1.0,
            evidence_ids=("activity:task:one",),
        ),
    )

    assert resynced.status == "contradicted"
    assert not store.project_for_turn("这件事").open_loops


def test_model_observed_fact_gets_a_review_expiry(tmp_path) -> None:
    store = UserUnderstandingStore(tmp_path)

    atom = store.ingest_candidate(
        UnderstandingCandidate(
            kind="fact",
            statement="用户近期接手一个七八人的团队。",
            confidence=0.92,
            evidence_ids=("event-role-1",),
        ),
    )

    assert atom.status == "observed"
    assert atom.expires_at


def test_expired_open_loop_does_not_keep_following_the_user(
    tmp_path,
):
    store = UserUnderstandingStore(tmp_path)
    store.remember(
        UnderstandingAtom.create(
            kind="open_loop",
            statement="上个月的发布复盘还没有做。",
            status="observed",
            confidence=0.88,
            evidence_ids=("run_old_release",),
            expires_at="2026-07-31T00:00:00+00:00",
        ),
    )

    projection = store.project_for_turn(
        "发布复盘",
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert projection.open_loops == ()


def test_forgetting_the_only_source_invalidates_the_derived_understanding(
    tmp_path,
):
    store = UserUnderstandingStore(tmp_path)
    atom = store.remember(
        UnderstandingAtom.create(
            kind="value",
            statement="用户更看重真实体感，而不是形式上的零风险。",
            status="confirmed",
            confidence=1.0,
            evidence_ids=("memory_value_source",),
        ),
    )

    changed = store.invalidate_source("memory_value_source")
    prompt = store.project_for_turn("真实体感").compile_prompt()

    assert changed == 1
    assert atom.statement not in prompt


def test_repeated_independent_evidence_promotes_a_pattern_to_working_hypothesis(
    tmp_path,
):
    store = UserUnderstandingStore(tmp_path)

    first = store.observe(
        kind="decision_pattern",
        statement="用户在发布前反复修改时，可能真正担心公开后的不可逆评价。",
        confidence=0.72,
        evidence_id="session_one:event_one",
    )
    second = store.observe(
        kind="decision_pattern",
        statement="用户在发布前反复修改时，可能真正担心公开后的不可逆评价。",
        confidence=0.78,
        evidence_id="session_two:event_two",
    )

    assert first.status == "observed"
    assert second.status == "working_hypothesis"
    assert second.evidence_ids == (
        "session_one:event_one",
        "session_two:event_two",
    )


def test_high_confidence_current_tension_is_available_after_first_disclosure(
    tmp_path,
):
    store = UserUnderstandingStore(tmp_path)

    atom = store.ingest_candidate(
        UnderstandingCandidate(
            kind="tension",
            statement="用户近期同时承受新生儿家庭适应、连续出差和新管理岗位压力。",
            confidence=0.92,
            evidence_ids=("session-disclosure:message-1",),
            evidence="用户直接说明近期多重压力",
            future_relevance="后续给出工作与生活建议时",
        ),
    )
    projection = store.project_for_turn("最近工作和家里都很累")

    assert atom.status == "working_hypothesis"
    assert atom.expires_at
    assert [item.statement for item in projection.hypotheses] == [atom.statement]
