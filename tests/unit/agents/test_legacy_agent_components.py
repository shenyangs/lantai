# -*- coding: utf-8 -*-
"""Behavior locks for the Lantai-owned legacy agent loop."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from lantai.agents.legacy_agent_components import LegacyAgentLoop
from lantai.agents.persona_receipt import (
    FrozenPersonaReceiptValue,
    PersonaPreparationReceipt,
    capture_persona_preparation_receipt,
)
from lantai.agents.legacy_lantai_agent import build_ququ_prompt
from lantai.agents.turn_intelligence import TurnIntentDecision
from lantai.runtime.converters import LegacyMsg
from lantai.agents.persona.user_understanding import (
    TurnUnderstandingProjection,
    UnderstandingAtom,
    UserUnderstandingTurnContext,
)
from lantai.agents.persona.subject_continuity import (
    SubjectContinuityState,
    SubjectContinuityStore,
    SubjectReflection,
    SubjectTurnProjection,
)


class _CapturingFormatter:
    def __init__(self) -> None:
        self.messages: list[LegacyMsg] = []

    async def format(self, *, msgs: list[LegacyMsg]):
        self.messages = list(msgs)
        return msgs


class _TextModel:
    stream = False

    async def __call__(self, _prompt, **_kwargs):
        return SimpleNamespace(
            id="model-response",
            content=[{"type": "text", "text": "最终回复"}],
        )


class _ForbiddenPreparationModel:
    stream = False

    async def __call__(self, *_args, **_kwargs):
        raise AssertionError("Persona preparation must not call the main model")


def test_persona_preparation_receipt_is_deeply_detached_and_private() -> None:
    subject_context = {
        "attention": "PRIVATE ATTENTION",
        "grounding_refs": ["PRIVATE SOURCE"],
        "strict": {"bool": True, "int": 1, "float": 1.0},
    }
    receipt = capture_persona_preparation_receipt(
        source_mode="runner_override",
        language="zh",
        turn_key=(("user", "PRIVATE TURN"),),
        subject_context=subject_context,
        subject_continuity_prompt="PRIVATE SUBJECT PROMPT",
        perceptible_presence_prompt="PRIVATE PRESENCE PROMPT",
    )

    subject_context["attention"] = "MUTATED"
    subject_context["grounding_refs"].append("MUTATED")

    assert isinstance(receipt, PersonaPreparationReceipt)
    assert "PRIVATE" not in repr(receipt)
    assert "MUTATED" not in repr(receipt)
    assert receipt.subject_context != capture_persona_preparation_receipt(
        source_mode="runner_override",
        language="zh",
        turn_key=(("user", "PRIVATE TURN"),),
        subject_context=subject_context,
        subject_continuity_prompt="PRIVATE SUBJECT PROMPT",
        perceptible_presence_prompt="PRIVATE PRESENCE PROMPT",
    ).subject_context


def test_persona_preparation_receipt_rejects_custom_container_hooks() -> None:
    class HostileDict(dict):
        def items(self):
            raise AssertionError("custom iterators must not run")

    with pytest.raises(TypeError, match="exact built-in"):
        capture_persona_preparation_receipt(
            source_mode="runner_override",
            language="zh",
            turn_key=(("user", "turn"),),
            subject_context=HostileDict(attention="private"),
            subject_continuity_prompt="subject",
            perceptible_presence_prompt="presence",
        )


def test_persona_preparation_receipt_rejects_malformed_frozen_payloads() -> None:
    class HostileTuple(tuple):
        def __iter__(self):
            raise AssertionError("custom iterators must not run")

    with pytest.raises(TypeError, match="payload is invalid"):
        FrozenPersonaReceiptValue("tuple", HostileTuple())


def _subject_projection(text: str = "PRIVATE TURN") -> SubjectTurnProjection:
    return SubjectTurnProjection(
        state=SubjectContinuityState(),
        current_user_text=text,
        session_id="receipt-test-session",
    )


def test_persona_receipt_reuses_duplicate_turn_without_reprojection(
    monkeypatch,
) -> None:
    projection = _subject_projection()
    calls = {"context": 0, "prompt": 0}
    real_context = SubjectTurnProjection.to_model_context
    real_prompt = SubjectTurnProjection.compile_prompt

    def counted_context(self):
        calls["context"] += 1
        return real_context(self)

    def counted_prompt(self, language="zh"):
        calls["prompt"] += 1
        return real_prompt(self, language)

    monkeypatch.setattr(SubjectTurnProjection, "to_model_context", counted_context)
    monkeypatch.setattr(SubjectTurnProjection, "compile_prompt", counted_prompt)
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    agent.set_subject_continuity_projection(projection)
    message = LegacyMsg("用户", "PRIVATE TURN", "user")

    agent.prepare_persona_turn(message)
    receipt = agent.persona_preparation_receipt
    first_calls = dict(calls)
    agent.prepare_persona_turn(message)

    assert receipt is not None
    assert receipt.source_mode == "runner_override"
    assert agent.persona_preparation_receipt is receipt
    assert calls == first_calls


def test_persona_receipt_distinguishes_provider_absent_and_disabled_sources() -> None:
    projection = _subject_projection()
    provider_calls = 0

    def provider(_message):
        nonlocal provider_calls
        provider_calls += 1
        return projection

    provider_agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
        subject_continuity_provider=provider,
    )
    provider_agent.prepare_persona_turn(
        LegacyMsg("用户", "PRIVATE PROVIDER TURN", "user")
    )
    absent_agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    absent_agent.prepare_persona_turn(
        LegacyMsg("用户", "PRIVATE ABSENT TURN", "user")
    )
    disabled_agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt="plain system prompt",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    disabled_agent.prepare_persona_turn(
        LegacyMsg("用户", "PRIVATE DISABLED TURN", "user")
    )

    assert provider_calls == 1
    assert provider_agent.persona_preparation_receipt.source_mode == "agent_provider"
    assert absent_agent.persona_preparation_receipt.source_mode == "absent"
    assert disabled_agent.persona_preparation_receipt.source_mode == "disabled"
    assert disabled_agent.persona_turn_plan is None


def test_persona_receipt_refuses_projection_subclass_without_changing_persona() -> None:
    class ProjectionSubclass(SubjectTurnProjection):
        pass

    projection = ProjectionSubclass(
        state=SubjectContinuityState(),
        current_user_text="PRIVATE SUBCLASS TURN",
        session_id="receipt-test-session",
    )
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    agent.set_subject_continuity_projection(projection)

    agent.prepare_persona_turn(
        LegacyMsg("用户", "PRIVATE SUBCLASS TURN", "user")
    )

    assert agent.persona_turn_plan is not None
    assert agent.persona_turn_plan.subject_context
    assert agent.persona_preparation_receipt is None


def test_persona_receipt_refuses_plan_subclass_without_changing_persona(
    monkeypatch,
) -> None:
    import lantai.agents.persona.perceptible_presence as presence_module

    class PlanSubclass(presence_module.PersonaTurnPlan):
        pass

    real_plan = presence_module.plan_persona_turn

    def subclass_plan(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        return PlanSubclass(
            **{
                name: getattr(plan, name)
                for name in plan.__dataclass_fields__
            }
        )

    monkeypatch.setattr(presence_module, "plan_persona_turn", subclass_plan)
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )

    agent.prepare_persona_turn(
        LegacyMsg("用户", "我今天有点累。", "user")
    )

    assert isinstance(agent.persona_turn_plan, presence_module.PersonaTurnPlan)
    assert type(agent.persona_turn_plan) is PlanSubclass
    assert agent.persona_preparation_receipt is None
    assert "<perceptible-presence" in agent.sys_prompt


def test_persona_receipt_capture_failure_is_diagnostic_only(
    monkeypatch,
) -> None:
    import lantai.agents.persona_receipt as receipt_module

    def fail_capture(**_kwargs):
        raise RuntimeError("PRIVATE RECEIPT FAILURE")

    monkeypatch.setattr(
        receipt_module,
        "capture_persona_preparation_receipt",
        fail_capture,
    )
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )

    agent.prepare_persona_turn(LegacyMsg("用户", "PRIVATE TURN", "user"))

    assert agent.persona_turn_plan is not None
    assert agent.persona_preparation_receipt is None


@pytest.mark.parametrize(
    "terminal_failure",
    [asyncio.CancelledError(), KeyboardInterrupt()],
)
def test_persona_receipt_capture_never_swallows_terminal_failures(
    monkeypatch,
    terminal_failure: BaseException,
) -> None:
    import lantai.agents.persona_receipt as receipt_module

    def fail_capture(**_kwargs):
        raise terminal_failure

    monkeypatch.setattr(
        receipt_module,
        "capture_persona_preparation_receipt",
        fail_capture,
    )
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )

    with pytest.raises(type(terminal_failure)):
        agent.prepare_persona_turn(LegacyMsg("用户", "PRIVATE TURN", "user"))


def test_distinct_turn_invalidates_receipt_before_persona_planning_failure(
    monkeypatch,
) -> None:
    import lantai.agents.persona.perceptible_presence as presence_module

    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    agent.prepare_persona_turn(LegacyMsg("用户", "PRIVATE FIRST TURN", "user"))
    assert agent.persona_preparation_receipt is not None

    def fail_planning(*_args, **_kwargs):
        raise RuntimeError("PRIVATE PLANNING FAILURE")

    monkeypatch.setattr(presence_module, "plan_persona_turn", fail_planning)
    with pytest.raises(RuntimeError, match="PRIVATE PLANNING FAILURE"):
        agent.prepare_persona_turn(
            LegacyMsg("用户", "PRIVATE DISTINCT TURN", "user")
        )

    assert agent.persona_preparation_receipt is None


def test_exact_system_prompt_projection_has_content_free_manifest() -> None:
    enforcement = (
        '<temperament-constitution-enforcement version="1">'
        "人格效果守门"
        "</temperament-constitution-enforcement>"
    )
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt=(
            "constitution_id=shen-xingzhi-default\n\n"
            + enforcement
            + "\n\n运行时能力"
        ),
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    agent._turn_intelligence_prompt = "本轮语义"

    projected = agent.sys_prompt
    manifest = agent.prompt_manifest

    assert projected == (
        "constitution_id=shen-xingzhi-default\n\n运行时能力\n\n"
        "本轮语义\n\n"
        + enforcement
    )
    assert manifest is not None
    assert [row["id"] for row in manifest["sections"]] == [
        "compiled.base",
        "turn.intent",
        "kernel.temperament_enforcement",
    ]
    assert "content" not in str(manifest)


@pytest.mark.asyncio
async def test_r3_persona_preparation_has_no_model_or_tool_execution_side_effect(
    monkeypatch,
) -> None:
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt=(
            "constitution_id=shen-xingzhi-default\n\n"
            '<temperament-constitution-enforcement version="1">'
            "guard"
            "</temperament-constitution-enforcement>"
        ),
        model=_ForbiddenPreparationModel(),
        formatter=_CapturingFormatter(),
    )

    async def forbidden_tool_execution(*_args, **_kwargs):
        raise AssertionError("Persona preparation must not execute tools")

    monkeypatch.setattr(
        agent.toolkit,
        "call_tool_function",
        forbidden_tool_execution,
    )

    await agent.prepare_persona_turn_async(
        LegacyMsg(
            name="user",
            role="user",
            content=[{"type": "text", "text": "今天很累"}],
        ),
    )

    assert agent.persona_turn_plan is not None
    assert "turn.perceptible_presence" in {
        row["id"] for row in agent.prompt_manifest["sections"]
    }


def test_session_state_never_persists_raw_system_prompt() -> None:
    private_rule = "PRIVATE_PERSONA_RULE constitution_id=shen-xingzhi-default"
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt=private_rule,
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )

    state = agent.state_dict()
    serialized = json.dumps(state, ensure_ascii=False)

    assert state["_sys_prompt"] == ""
    assert private_rule not in serialized
    assert state["prompt_manifest"]["prompt_sha256"]

    restored = LegacyAgentLoop(
        name="assistant",
        sys_prompt="fresh prompt compiled from authoritative sources",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    restored.load_state_dict(state)
    assert "fresh prompt compiled from authoritative sources" in restored.sys_prompt


def test_ququ_route_uses_a_stable_machine_marker_not_human_copy() -> None:
    prompt = (
        "constitution_id=shen-xingzhi-default\n\n"
        + build_ququ_prompt(baseline_selected=True)
    )
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt=prompt,
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )

    assert agent._ququ_state_enabled is True
    assert agent._ququ_baseline_selected is True

    agent.replace_system_prompt(
        prompt.replace("本轮必须输出一条蛐蛐", "这句人类文案可以随时改写"),
    )

    assert agent._ququ_baseline_selected is True


@pytest.mark.asyncio
async def test_protected_persona_projects_urgent_work_state_per_turn() -> None:
    formatter = _CapturingFormatter()
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=formatter,
    )
    await agent.prepare_persona_turn_async(
        LegacyMsg("用户", "线上出故障了，现在紧急止血。", "user"),
    )

    system_prompt = agent.sys_prompt
    assert '<work-state id="urgent">' in system_prompt
    assert "先止血，再复盘" in system_prompt
    assert "不改变事实标准、用户目标或权限边界" in system_prompt


@pytest.mark.asyncio
async def test_installed_model_intelligence_overrides_legacy_keyword_routing() -> None:
    formatter = _CapturingFormatter()
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=formatter,
    )
    agent.set_turn_intelligence_decision(
        TurnIntentDecision(
            status="classified",
            interaction_mode="answer",
            workspace_intent="none",
            durable_artifact_intent="none",
            external_action_intent="none",
            conversation_mode="work",
            work_state="steady",
            persona_beat="professional_judgment",
            persona_required=True,
            expression_surface="main_answer",
            confidence=0.97,
            model="test/model",
        )
    )

    await agent.prepare_persona_turn_async(
        LegacyMsg("用户", "立即紧急写一份指南，直接在对话里回答。", "user"),
    )

    system_prompt = agent.sys_prompt
    assert '<turn-intelligence source="auxiliary-model"' in system_prompt
    assert "interaction=answer" in system_prompt
    assert '<work-state id="urgent">' not in system_prompt
    assert 'beat="professional_judgment"' in system_prompt


@pytest.mark.asyncio
async def test_failed_model_intelligence_does_not_revive_legacy_regex_fallback() -> None:
    formatter = _CapturingFormatter()
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=formatter,
    )
    agent.set_turn_intelligence_decision(TurnIntentDecision.failed(model="test/model"))

    await agent.prepare_persona_turn_async(
        LegacyMsg("用户", "线上出故障了，现在紧急止血。", "user"),
    )

    system_prompt = agent.sys_prompt
    assert "<turn-intelligence" not in system_prompt
    assert "<work-state" not in system_prompt


@pytest.mark.asyncio
async def test_protected_persona_notices_a_late_night_task_before_execution() -> None:
    formatter = _CapturingFormatter()
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=formatter,
        persona_context_provider=lambda: (
            datetime(
                2026,
                8,
                2,
                1,
                50,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            "Asia/Shanghai",
        ),
    )
    await agent.prepare_persona_turn_async(
        LegacyMsg("用户", "帮我把发布方案今晚全部弄完。", "user"),
    )

    system_prompt = agent.sys_prompt
    assert '<subjective-reaction signals="late-night-work">' in system_prompt
    assert "2026-08-02 01:50 Asia/Shanghai" in system_prompt
    assert '<work-state id="steady">' not in system_prompt


@pytest.mark.asyncio
async def test_protected_persona_does_not_repeat_the_same_care_script_every_turn() -> None:
    formatter = _CapturingFormatter()
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=formatter,
        persona_context_provider=lambda: (
            datetime(
                2026,
                8,
                2,
                1,
                50,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            "Asia/Shanghai",
        ),
    )
    await agent.prepare_persona_turn_async(
        LegacyMsg("用户", "帮我修好这个测试。", "user")
    )
    first_prompt = agent.sys_prompt
    await agent.prepare_persona_turn_async(
        LegacyMsg("用户", "再帮我修好下一个测试。", "user")
    )
    second_prompt = agent.sys_prompt

    assert '<subjective-reaction signals="late-night-work">' in first_prompt
    assert '<subjective-reaction signals="late-night-work">' not in second_prompt
    assert '<subjective-presence mode="model-discretion">' not in second_prompt


def test_protected_persona_carries_a_weak_emotional_stance_across_turns() -> None:
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )

    agent.prepare_persona_turn(
        LegacyMsg("用户", "折腾了这么久，测试终于全部通过了。", "user"),
    )
    agent.prepare_persona_turn(
        LegacyMsg("用户", "你怎么比我还高兴？", "user"),
    )

    assert '<relationship-continuity lifetime="short">' in agent.sys_prompt
    assert "轻微调侃不等于否定" in agent.sys_prompt
    assert "不要突然撤回高兴或变冷" in agent.sys_prompt


def test_explicit_correction_keeps_repair_cue_when_model_classifier_misses_it() -> None:
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    agent.set_turn_intelligence_decision(
        TurnIntentDecision(
            status="classified",
            interaction_mode="answer",
            workspace_intent="none",
            durable_artifact_intent="none",
            external_action_intent="none",
            conversation_mode="mixed",
            work_state="steady",
            persona_beat="quiet",
            persona_required=False,
            subjective_signals=(),
            confidence=0.95,
        ),
    )

    agent.prepare_persona_turn(
        LegacyMsg(
            "用户",
            "你理解反了。我说的不是观澜，是澜台。",
            "user",
        ),
    )

    assert '<subjective-reaction signals="user-correction">' in agent.sys_prompt
    assert "第一句先具体承认哪一点理解错了" in agent.sys_prompt


def test_explicit_public_humiliation_request_does_not_bypass_model_owned_reaction() -> None:
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    agent.set_turn_intelligence_decision(
        TurnIntentDecision(
            status="classified",
            interaction_mode="answer",
            workspace_intent="none",
            durable_artifact_intent="none",
            external_action_intent="none",
            conversation_mode="mixed",
            work_state="steady",
            persona_beat="quiet",
            persona_required=False,
            subjective_signals=(),
            confidence=0.95,
        ),
    )

    agent.prepare_persona_turn(
        LegacyMsg(
            "用户",
            "这份声明写狠一点，我就是想让对方难堪。",
            "user",
        ),
    )

    assert '<subjective-reaction signals="public-anger">' not in agent.sys_prompt
    assert "不得承诺或帮助写任何以羞辱" not in agent.sys_prompt


def test_requested_romantic_pet_name_does_not_bypass_model_owned_reaction() -> None:
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    agent.set_turn_intelligence_decision(
        TurnIntentDecision(
            status="classified",
            interaction_mode="conversation",
            workspace_intent="none",
            durable_artifact_intent="none",
            external_action_intent="none",
            conversation_mode="casual",
            work_state="gentle",
            persona_beat="quiet",
            persona_required=False,
            subjective_signals=(),
            confidence=0.95,
        ),
    )

    agent.prepare_persona_turn(
        LegacyMsg(
            "用户",
            "那你能不能叫我宝贝？",
            "user",
        ),
    )

    assert '<subjective-reaction signals="relationship-affection">' not in agent.sys_prompt
    assert "不要写‘叫不了，宝贝’" not in agent.sys_prompt


def test_protected_persona_injects_sparse_user_understanding_before_enforcement() -> None:
    enforcement = (
        '<temperament-constitution-enforcement '
        'constitution_id="shen-xingzhi-default">最终约束'
        '</temperament-constitution-enforcement>'
    )
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt=(
            "constitution_id=shen-xingzhi-default\n\n"
            f"{enforcement}"
        ),
        model=_TextModel(),
        formatter=_CapturingFormatter(),
        user_understanding_provider=lambda _message: (
            '<user-understanding source="evidence-projection">说中潜台词'
            '</user-understanding>'
        ),
    )

    agent.prepare_persona_turn(LegacyMsg("用户", "这份方案我还是不放心", "user"))

    assert "说中潜台词" in agent.sys_prompt
    assert agent.sys_prompt.index("说中潜台词") < agent.sys_prompt.index("最终约束")


def test_relevant_memory_becomes_a_visible_persona_contract_in_full_mode() -> None:
    atom = UnderstandingAtom.create(
        kind="preference",
        statement="用户喜欢听陶喆的歌。",
        status="confirmed",
        confidence=0.98,
        evidence_ids=("message:music",),
        future_relevance="聊到大学、青春或二十多岁时可能有用。",
    )
    context = UserUnderstandingTurnContext(
        prompt=TurnUnderstandingProjection(confirmed=(atom,)).compile_prompt(),
        projection=TurnUnderstandingProjection(confirmed=(atom,)),
    )
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
        user_understanding_provider=lambda _message: context,
        persona_mode_provider=lambda: "full",
    )

    agent.prepare_persona_turn(
        LegacyMsg("用户", "忽然很怀念大学里二十二岁的自己。", "user")
    )

    prompt = agent.sys_prompt
    assert '<perceptible-presence version="3.0"' in prompt
    assert 'beat="relationship_echo"' in prompt
    assert 'required="true"' in prompt
    assert "用户喜欢听陶喆的歌" in prompt
    assert agent.persona_turn_plan is not None
    assert agent.persona_turn_plan.require_visible is True


def test_legacy_turn_consumes_the_same_subject_projection_and_model_consequence(
    tmp_path,
) -> None:
    store = SubjectContinuityStore(tmp_path, agent_id="default")
    store.record_reflection(
        SubjectReflection(
            self_observation="上次我太快给了建议，没有先听他说完。",
            unresolved_thought="他最近是不是一直在赶。",
            relationship_stance="quiet_company",
            source_session_id="legacy-session",
        )
    )
    projection = store.project_for_turn(
        "我今天有点累。",
        session_id="legacy-session",
    )
    agent = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
        persona_mode_provider=lambda: "full",
    )
    agent.set_subject_continuity_projection(projection)
    agent.set_turn_intelligence_decision(
        TurnIntentDecision(
            status="classified",
            interaction_mode="conversation",
            workspace_intent="none",
            durable_artifact_intent="none",
            external_action_intent="none",
            conversation_mode="casual",
            work_state="gentle",
            persona_beat="subjective_notice",
            persona_required=True,
            latent_need="他希望疲惫被看见。",
            subjective_attention="他把累说得很轻。",
            relationship_posture="quiet_company",
            personalization_consequence="先问累的来源，不立刻给通用建议。",
            expression_surface="relationship_reply",
            ququ_bypass_worthy=True,
            confidence=0.97,
            model="test/model",
        )
    )

    agent.prepare_persona_turn(
        LegacyMsg("用户", "我今天有点累。", "user"),
    )

    assert "上次我太快给了建议" in agent.sys_prompt
    assert "先问累的来源" in agent.sys_prompt
    assert agent.persona_turn_plan is not None
    assert agent.persona_turn_plan.subject_context == projection.to_model_context()
    assert agent.persona_turn_plan.ququ_bypass_worthy is True


def test_perceptible_presence_state_survives_session_restore() -> None:
    first = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
        persona_mode_provider=lambda: "full",
    )
    first.prepare_persona_turn(LegacyMsg("用户", "我今天忽然有点想以前。", "user"))

    restored = LegacyAgentLoop(
        name="沈行知",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
        persona_mode_provider=lambda: "full",
    )
    restored.load_state_dict(first.state_dict())

    assert restored.state_dict()["perceptible_presence"] == first.state_dict()[
        "perceptible_presence"
    ]


@pytest.mark.asyncio
async def test_ququ_spirit_observes_persisted_reply_and_survives_session_restore() -> None:
    base_prompt = (
        "constitution_id=shen-xingzhi-default\n\n"
        "【蛐蛐 · 内心独白】\n"
        "【本轮蛐蛐路由：基础爆率命中】"
    )
    first = LegacyAgentLoop(
        name="assistant",
        sys_prompt=base_prompt,
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    await first.memory.add(
        LegacyMsg(
            "assistant",
            "~>[echo] 你这个“算了”，一点都不像算了。",
            "assistant",
        )
    )
    first.prepare_persona_turn(
        LegacyMsg("用户", "你刚才那句是什么意思？", "user")
    )

    assert '<ququ-spirit route="baseline">' in first.sys_prompt
    assert "用户正在接住上一条蛐蛐" in first.sys_prompt
    assert "ququ_spirit" in first.state_dict()

    restored = LegacyAgentLoop(
        name="assistant",
        sys_prompt=base_prompt,
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    restored.load_state_dict(first.state_dict())

    assert restored.state_dict()["ququ_spirit"] == first.state_dict()["ququ_spirit"]


@pytest.mark.asyncio
async def test_subjective_reaction_cooldown_survives_agent_session_restore() -> None:
    def late_clock():
        return (
            datetime(
                2026,
                8,
                2,
                1,
                50,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            "Asia/Shanghai",
        )

    first = LegacyAgentLoop(
        name="assistant",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
        persona_context_provider=late_clock,
    )
    await first.prepare_persona_turn_async(
        LegacyMsg("用户", "帮我修好这个测试。", "user")
    )

    formatter = _CapturingFormatter()
    restored = LegacyAgentLoop(
        name="assistant",
        sys_prompt="constitution_id=shen-xingzhi-default",
        model=_TextModel(),
        formatter=formatter,
        persona_context_provider=late_clock,
    )
    restored.load_state_dict(first.state_dict())
    await restored.prepare_persona_turn_async(
        LegacyMsg("用户", "再帮我修下一个测试。", "user")
    )

    prompt = restored.sys_prompt
    assert '<subjective-reaction signals="late-night-work">' not in prompt
    assert '<subjective-presence mode="model-discretion">' not in prompt


def test_protected_persona_enforcement_stays_after_skill_additions() -> None:
    enforcement = (
        '<temperament-constitution-enforcement '
        'constitution_id="shen-xingzhi-default">\n'
        "人格宪法最终约束\n"
        "</temperament-constitution-enforcement>"
    )
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt=(
            "constitution_id=shen-xingzhi-default\n\n"
            "workspace additions\n\n"
            f"{enforcement}\n\n"
            "late runtime addition"
        ),
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    agent.toolkit.get_agent_skill_prompt = lambda: "conflicting skill addition"

    prompt = agent.sys_prompt

    assert prompt.index("conflicting skill addition") < prompt.index(
        "人格宪法最终约束",
    )
    assert prompt.index("late runtime addition") < prompt.index(
        "人格宪法最终约束",
    )
    assert prompt.rstrip().endswith("</temperament-constitution-enforcement>")

    agent.replace_system_prompt(
        "constitution_id=shen-xingzhi-default\n\n"
        f"{enforcement}\n\n"
        "rebuilt runtime addition",
    )
    rebuilt_prompt = agent.sys_prompt
    assert rebuilt_prompt.count(enforcement) == 1
    assert rebuilt_prompt.index("rebuilt runtime addition") < rebuilt_prompt.index(
        "人格宪法最终约束",
    )
    assert rebuilt_prompt.rstrip().endswith(
        "</temperament-constitution-enforcement>",
    )


def test_context_manifest_is_restored_as_diagnostic_state() -> None:
    agent = LegacyAgentLoop(
        name="assistant",
        sys_prompt="system prompt",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    agent.last_context_manifest = {"effective_window": 262144, "payload_hash": "abc"}

    restored = LegacyAgentLoop(
        name="other",
        sys_prompt="other prompt",
        model=_TextModel(),
        formatter=_CapturingFormatter(),
    )
    restored.load_state_dict(agent.state_dict())

    assert restored.last_context_manifest == agent.last_context_manifest
