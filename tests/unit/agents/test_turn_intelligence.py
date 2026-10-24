# -*- coding: utf-8 -*-
"""Behavior locks for model-owned turn intent decisions."""

from __future__ import annotations

import json

import pytest

from lantai.agents import turn_intelligence
from lantai.agents.turn_intelligence import classify_turn_intent
from lantai.app.utility_model import UtilityTaskTimeoutError, UtilityTextResult


@pytest.mark.asyncio
async def test_plain_guide_request_stays_chat_text_when_model_says_answer_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def classify(**_kwargs):
        return UtilityTextResult(
            text=json.dumps(
                {
                    "interaction_mode": "answer",
                    "workspace_intent": "none",
                    "durable_artifact_intent": "none",
                    "external_action_intent": "none",
                    "conversation_mode": "work",
                    "work_state": "steady",
                    "persona_beat": "professional_judgment",
                    "persona_required": True,
                    "subjective_signals": [],
                    "confidence": 0.96,
                }
            ),
            model_label="test/model",
        )

    monkeypatch.setattr(
        turn_intelligence,
        "run_utility_text_task",
        classify,
    )

    decision = await classify_turn_intent(
        "请写一份一个半月宝宝居家带娃指南。",
        agent_id="default",
    )

    assert decision.status == "classified"
    assert decision.interaction_mode == "answer"
    assert decision.allows_mutating_tools is False
    assert decision.durable_artifact_intent == "none"
    assert "write_file" not in decision.compile_prompt()


@pytest.mark.asyncio
async def test_explicit_meeting_request_can_select_collaboration_semantically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def classify(**_kwargs):
        return UtilityTextResult(
            text=json.dumps(
                {
                    "interaction_mode": "answer",
                    "workspace_intent": "none",
                    "durable_artifact_intent": "none",
                    "external_action_intent": "none",
                    "requested_work_method_id": "collaboration",
                    "conversation_mode": "work",
                    "work_state": "focused",
                    "persona_beat": "professional_judgment",
                    "persona_required": True,
                    "subjective_signals": [],
                    "confidence": 0.96,
                }
            ),
            model_label="test/model",
        )

    monkeypatch.setattr(turn_intelligence, "run_utility_text_task", classify)

    decision = await classify_turn_intent(
        "用会议厅形式分析一下先有鸡还是先有蛋。",
        agent_id="default",
    )

    assert decision.requested_work_method_id == "collaboration"


@pytest.mark.asyncio
async def test_explicit_workspace_edit_allows_mutating_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def classify(**_kwargs):
        return UtilityTextResult(
            text=json.dumps(
                {
                    "interaction_mode": "action",
                    "workspace_intent": "modify",
                    "durable_artifact_intent": "none",
                    "external_action_intent": "none",
                    "conversation_mode": "work",
                    "work_state": "focused",
                    "persona_beat": "professional_judgment",
                    "persona_required": True,
                    "subjective_signals": [],
                    "confidence": 0.94,
                }
            ),
            model_label="test/model",
        )

    monkeypatch.setattr(
        turn_intelligence,
        "run_utility_text_task",
        classify,
    )

    decision = await classify_turn_intent(
        "把这个项目里的重复兜底删掉并跑测试。",
        agent_id="default",
    )

    assert decision.status == "classified"
    assert decision.interaction_mode == "action"
    assert decision.workspace_intent == "modify"
    assert decision.allows_mutating_tools is True


@pytest.mark.asyncio
async def test_unavailable_intelligence_fails_open_to_primary_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(**_kwargs):
        return None

    monkeypatch.setattr(
        turn_intelligence,
        "run_utility_text_task",
        unavailable,
    )

    decision = await classify_turn_intent("帮我处理一下。", agent_id="default")

    assert decision.status == "unavailable"
    assert decision.enforces_tool_ceiling is False
    assert decision.compile_prompt() == ""


@pytest.mark.asyncio
async def test_turn_intelligence_without_dedicated_attempt_stays_primary_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    async def unavailable(**kwargs):
        observed.update(kwargs)
        return None

    monkeypatch.setattr(
        turn_intelligence,
        "run_utility_text_task",
        unavailable,
    )

    decision = await classify_turn_intent("继续处理。", agent_id="default")

    assert decision.status == "unavailable"
    assert observed["allow_follow_active"] is False


@pytest.mark.asyncio
async def test_dedicated_timeout_defers_to_the_real_primary_turn_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def timed_out(**_kwargs):
        raise UtilityTaskTimeoutError(phase="total", timeout_seconds=12)

    monkeypatch.setattr(
        turn_intelligence,
        "run_utility_text_task",
        timed_out,
    )

    decision = await classify_turn_intent(
        "把孙宇晨的事情经过，做成一份ppt吧",
        agent_id="default",
    )

    assert decision.status == "failed"
    assert decision.interaction_mode == "unknown"
    assert decision.decision_origin == "utility"
    assert decision.failure_reason == "timeout:total:12s"


@pytest.mark.asyncio
async def test_invalid_model_json_never_falls_back_to_keyword_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def invalid(**_kwargs):
        return UtilityTextResult(text="这是一个文件任务", model_label="test/model")

    monkeypatch.setattr(
        turn_intelligence,
        "run_utility_text_task",
        invalid,
    )
    decision = await classify_turn_intent("写一份指南。", agent_id="default")

    assert decision.status == "failed"
    assert decision.decision_origin == "utility"
    assert decision.failure_reason == "invalid dedicated turn-intelligence JSON"
    assert decision.enforces_tool_ceiling is False
    assert decision.workspace_intent == "unknown"


@pytest.mark.asyncio
async def test_explicit_user_correction_is_an_allowed_subjective_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def classify(**_kwargs):
        return UtilityTextResult(
            text=json.dumps(
                {
                    "interaction_mode": "answer",
                    "workspace_intent": "none",
                    "durable_artifact_intent": "none",
                    "external_action_intent": "none",
                    "conversation_mode": "mixed",
                    "work_state": "steady",
                    "persona_beat": "subjective_notice",
                    "persona_required": True,
                    "subjective_signals": ["user-correction"],
                    "confidence": 0.96,
                },
            ),
            model_label="test/model",
        )

    monkeypatch.setattr(turn_intelligence, "run_utility_text_task", classify)

    decision = await classify_turn_intent(
        "你理解反了，我说的是澜台。",
        agent_id="default",
    )

    assert decision.subjective_signals == ("user-correction",)


@pytest.mark.asyncio
async def test_subject_continuity_changes_the_model_owned_personalization_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    async def classify(**kwargs):
        observed.update(kwargs)
        return UtilityTextResult(
            text=json.dumps(
                {
                    "interaction_mode": "conversation",
                    "workspace_intent": "none",
                    "durable_artifact_intent": "none",
                    "external_action_intent": "none",
                    "conversation_mode": "casual",
                    "work_state": "gentle",
                    "persona_beat": "subjective_notice",
                    "persona_required": True,
                    "latent_need": "他不是在要一句休息建议，是希望疲惫被看见。",
                    "subjective_attention": "他把累说得很轻。",
                    "relationship_posture": "quiet_company",
                    "personalization_consequence": "先问累的来源，不立刻给通用建议。",
                    "causal_anchor": "用户把累说得很轻。",
                    "selected_memory_ids": ["ua_valid", "ua_invented"],
                    "expression_surface": "relationship_reply",
                    "self_implication": "我不想把这句话用建议打发掉。",
                    "carry_forward": "等他愿意时再问累从哪里来。",
                    "experience_sources": ["ua_valid", "made_up_source"],
                    "ququ_bypass_worthy": True,
                    "subjective_signals": ["user-overload"],
                    "confidence": 0.97,
                },
                ensure_ascii=False,
            ),
            model_label="test/model",
        )

    monkeypatch.setattr(turn_intelligence, "run_utility_text_task", classify)
    continuity = {
        "self_observation": "上次我太快给了建议，没有先听他说完。",
        "unresolved_thought": "他最近是不是一直在赶。",
    }
    understanding = {
        "relevant": [
            {
                "id": "ua_valid",
                "statement": "用户疲惫时希望先被听见，不要立刻收到通用建议。",
            },
        ],
        "association_palette": [],
    }

    decision = await classify_turn_intent(
        "我今天有点累。",
        agent_id="default",
        continuity_context=continuity,
        understanding_context=understanding,
    )

    payload = json.loads(str(observed["user_prompt"]))
    assert payload["subject_continuity"] == continuity
    assert payload["user_understanding"] == understanding
    assert decision.latent_need.startswith("他不是在要")
    assert decision.personalization_consequence.startswith("先问累的来源")
    assert decision.ququ_bypass_worthy is True
    assert decision.selected_memory_ids == ("ua_valid",)
    assert decision.experience_sources == ("ua_valid",)
    assert decision.expression_surface == "relationship_reply"
