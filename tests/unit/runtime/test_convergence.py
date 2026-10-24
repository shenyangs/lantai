# -*- coding: utf-8 -*-
"""Behavior locks for the unified main/sub-agent convergence controller."""

from __future__ import annotations

from lantai.runtime.convergence import (
    ConvergenceController,
    build_convergence_review_prompt,
    classify_tool_family,
    parse_convergence_review,
    policy_for_work_method,
)
from lantai.runtime.runs import RuntimeConvergenceState
from lantai.runtime.tools import ToolCall, ToolResult


def _result(call_id: str, text: str, *, progress_made=None) -> ToolResult:
    metadata = {}
    if progress_made is not None:
        metadata["progress_made"] = progress_made
    return ToolResult(
        call_id=call_id,
        name="web_search",
        content=[{"type": "text", "text": text}],
        metadata=metadata,
    )


def _search(controller: ConvergenceController, index: int, query: str, text: str):
    return controller.observe(
        ToolCall(
            id=f"call-{index}",
            name="web_search",
            arguments={"query": query},
        ),
        _result(f"call-{index}", text),
    )


def test_tool_families_are_provider_independent() -> None:
    assert classify_tool_family("guanlan_search") == "research"
    assert classify_tool_family("deepseek_quick_search") == "research"
    assert classify_tool_family("web_fetch") == "read"
    assert classify_tool_family("edit_file") == "mutation"
    assert classify_tool_family("collaborate_with_agents") == "collaboration"


def test_novel_search_results_are_not_stopped_by_a_family_counter() -> None:
    controller = ConvergenceController(
        objective="比较莫言和余华",
        policy=policy_for_work_method("standard"),
    )

    decision = None
    for index in range(1, 9):
        controller.start_round()
        _search(
            controller,
            index,
            f"莫言 余华 公开资料 方向 {index}",
            f"第 {index} 次独立结果 https://example.com/{index}",
        )
        decision = controller.finish_round(
            model_round_count=index,
            max_model_rounds=40,
            used_tokens=index * 100,
            max_tokens=600_000,
        )

    assert decision is not None
    assert decision.action == "continue"
    assert controller.public_state()["family_counts"]["research"] == 8


def test_distinct_local_image_reads_count_as_new_sources() -> None:
    controller = ConvergenceController(
        objective="逐页检查渲染后的演示文稿",
        policy=policy_for_work_method("standard"),
    )

    decision = None
    observations = []
    for index in range(1, 13):
        controller.start_round()
        observation = controller.observe(
            ToolCall(
                id=f"view-{index}",
                name="view_image",
                arguments={"path": f"/tmp/rendered/slide-{index}.png"},
            ),
            ToolResult(
                call_id=f"view-{index}",
                name="view_image",
                content=[{"type": "text", "text": "image rendered"}],
            ),
        )
        observations.append(observation)
        decision = controller.finish_round(
            model_round_count=index,
            max_model_rounds=40,
            used_tokens=index * 100,
            max_tokens=600_000,
        )

    assert all(observation.new_sources == 1 for observation in observations)
    assert controller.public_state()["unique_sources"] == 12
    assert decision is not None
    assert decision.action == "continue"


def test_duplicate_results_count_as_no_progress_even_with_new_call_ids() -> None:
    controller = ConvergenceController(
        objective="查找一个事实",
        policy=policy_for_work_method("deep_research", max_no_progress_rounds=2),
    )

    controller.start_round()
    first = _search(controller, 1, "问题 版本一", "同一份缓存结果")
    controller.finish_round(
        model_round_count=1,
        max_model_rounds=30,
        used_tokens=100,
        max_tokens=900_000,
    )
    controller.start_round()
    second = _search(controller, 2, "问题 版本二", "同一份缓存结果")
    controller.finish_round(
        model_round_count=2,
        max_model_rounds=30,
        used_tokens=200,
        max_tokens=900_000,
    )
    controller.start_round()
    third = _search(controller, 3, "问题 版本三", "同一份缓存结果")
    decision = controller.finish_round(
        model_round_count=3,
        max_model_rounds=30,
        used_tokens=300,
        max_tokens=900_000,
    )

    assert first.made_progress is True
    assert second.made_progress is False
    assert third.made_progress is False
    assert decision.action == "review"
    assert decision.reason == "semantic_no_progress"


def test_resource_version_change_is_new_evidence_with_identical_payload() -> None:
    controller = ConvergenceController(
        objective="读取修改后的同一文件",
        policy=policy_for_work_method("standard", max_no_progress_rounds=1),
    )
    observations = []
    for index, version in enumerate(("rev-1", "rev-2", "rev-3"), start=1):
        controller.start_round()
        observations.append(
            controller.observe(
                ToolCall(
                    id=f"read-{index}",
                    name="read_file",
                    arguments={"file_path": "report.md"},
                ),
                ToolResult(
                    call_id=f"read-{index}",
                    name="read_file",
                    content=[{"type": "text", "text": "read succeeded"}],
                    metadata={"resource_version": version},
                ),
            )
        )
        decision = controller.finish_round(
            model_round_count=index,
            max_model_rounds=20,
            used_tokens=index * 100,
            max_tokens=600_000,
        )

    assert [observation.novel_result for observation in observations] == [
        True,
        True,
        True,
    ]
    assert [observation.made_progress for observation in observations] == [
        True,
        True,
        True,
    ]
    assert decision.action == "continue"


def test_explicit_state_change_is_authoritative_progress() -> None:
    controller = ConvergenceController(
        objective="修改文件",
        policy=policy_for_work_method("standard", max_no_progress_rounds=1),
    )
    controller.start_round()
    observation = controller.observe(
        ToolCall(id="edit-1", name="edit_file", arguments={"file_path": "a.py"}),
        ToolResult(
            call_id="edit-1",
            name="edit_file",
            content=[{"type": "text", "text": "unchanged display text"}],
            metadata={"progress_made": True, "progress_keys": ["file:a.py:rev2"]},
        ),
    )
    decision = controller.finish_round(
        model_round_count=1,
        max_model_rounds=40,
        used_tokens=100,
        max_tokens=600_000,
    )

    assert observation.made_progress is True
    assert observation.new_progress_keys == 1
    assert decision.action == "continue"


def test_resolved_acceptance_updates_the_bounded_progress_ledger() -> None:
    controller = ConvergenceController(
        objective="形成有来源的结论",
        acceptance=("来源已核验", "分歧已标明"),
        policy=policy_for_work_method("standard"),
    )
    controller.start_round()
    controller.observe(
        ToolCall(id="verify-1", name="wiki_read", arguments={"page": "source"}),
        ToolResult(
            call_id="verify-1",
            name="wiki_read",
            content=[{"type": "text", "text": "verified"}],
            metadata={"resolved_acceptance": ["来源已核验"]},
        ),
    )

    assert controller.public_state()["resolved_acceptance_count"] == 1
    assert "来源已核验" not in str(controller.snapshot())


def test_resource_counters_do_not_force_a_synthesis_round() -> None:
    controller = ConvergenceController(
        objective="完成任务",
        policy=policy_for_work_method("standard"),
    )
    controller.start_round()
    decision = controller.finish_round(
        model_round_count=34,
        max_model_rounds=40,
        used_tokens=100,
        max_tokens=600_000,
    )

    assert decision.action == "continue"
    token_decision = controller.finish_round(
        model_round_count=400,
        max_model_rounds=40,
        used_tokens=6_000_000,
        max_tokens=600_000,
    )
    assert token_decision.action == "continue"


def test_collaboration_members_have_a_bounded_consultation_policy() -> None:
    policy = policy_for_work_method(
        "deep_research",
        collaboration_member=True,
    )

    assert policy.search_family_limit == 10
    assert policy.read_family_limit == 14
    assert policy.collaboration_family_limit == 1


def test_collaboration_contract_can_tighten_member_convergence_policy() -> None:
    policy = policy_for_work_method(
        "deep_research",
        collaboration_member=True,
        overrides={
            "max_research_calls": 7,
            "max_read_calls": 9,
            "max_no_progress_rounds": 2,
        },
    )

    assert policy.search_family_limit == 7
    assert policy.read_family_limit == 9
    assert policy.max_no_progress_rounds == 2


def test_subagent_novel_research_is_not_mistaken_for_a_loop() -> None:
    controller = ConvergenceController(
        objective="比较两位作家的体力与格斗能力",
        policy=policy_for_work_method(
            "deep_research",
            collaboration_member=True,
        ),
    )

    decision = None
    for index in range(1, 11):
        controller.start_round()
        _search(
            controller,
            index,
            f"不同角度检索 {index}",
            f"独立来源 https://example.com/evidence-{index}",
        )
        decision = controller.finish_round(
            model_round_count=index,
            max_model_rounds=40,
            used_tokens=index * 200,
            max_tokens=600_000,
        )

    assert decision is not None
    assert decision.action == "continue"
    assert controller.public_state()["family_counts"]["research"] == 10


def test_snapshot_restores_progress_without_exposing_queries() -> None:
    controller = ConvergenceController(
        objective="查资料",
        policy=policy_for_work_method("standard"),
    )
    controller.start_round()
    _search(controller, 1, "一段敏感检索词", "https://example.com/a")
    controller.finish_round(
        model_round_count=1,
        max_model_rounds=40,
        used_tokens=100,
        max_tokens=600_000,
    )
    snapshot = controller.snapshot()
    restored = ConvergenceController(
        objective="查资料",
        policy=policy_for_work_method("standard"),
        snapshot=snapshot,
    )

    assert restored.public_state()["family_counts"] == {"research": 1}
    assert "一段敏感检索词" not in str(snapshot)
    assert "example.com" not in str(snapshot)


def test_typed_checkpoint_round_trip_restores_family_counts() -> None:
    controller = ConvergenceController(
        objective="查资料",
        policy=policy_for_work_method("standard"),
    )
    controller.start_round()
    _search(controller, 1, "版本差异", "https://example.com/a")
    typed_state = RuntimeConvergenceState.model_validate(controller.snapshot())
    restored = ConvergenceController(
        objective="查资料",
        policy=policy_for_work_method("standard"),
        snapshot=typed_state.model_dump(mode="json"),
    )

    assert typed_state.family_counts[0].family == "research"
    assert restored.public_state()["family_counts"] == {"research": 1}


def test_review_is_one_shot_and_malformed_output_fails_to_synthesis() -> None:
    controller = ConvergenceController(
        objective="开放任务",
        acceptance=("结论有依据",),
        policy=policy_for_work_method("standard"),
    )
    decision = controller.record_review("continue", reason="还有一个明确来源可查")
    next_decision = controller.record_review("synthesize", reason="新来源未改变结论")

    assert decision.action == "continue"
    assert next_decision.action == "synthesize"
    assert parse_convergence_review("not json") == (
        "synthesize",
        "convergence_review_invalid",
    )
    assert parse_convergence_review('{"action":"ask_user","reason":"范围不清"}') == (
        "ask_user",
        "范围不清",
    )

    prompt = build_convergence_review_prompt(
        objective=controller.objective,
        acceptance=controller.acceptance,
        trigger=next_decision,
    )
    assert "Return JSON only" in prompt
    assert "call tools" in prompt


def test_task_revision_resets_stale_no_progress_without_discarding_evidence() -> None:
    controller = ConvergenceController(
        objective="先直接回答",
        policy=policy_for_work_method("standard"),
    )
    controller.start_round()
    _search(controller, 1, "旧目标资料", "https://example.com/old")
    controller.finish_round(
        model_round_count=1,
        max_model_rounds=40,
        used_tokens=100,
        max_tokens=600_000,
    )
    controller.record_review("continue", reason="旧目标还需一步")
    before = controller.public_state()
    before_sources = controller.snapshot()["seen_sources"]

    controller.revise_intent(
        objective="先直接回答\n\n[steer revision=1]\n再生成 Word 文档",
        acceptance=("文档存在",),
    )
    after = controller.public_state()

    assert controller.objective.endswith("再生成 Word 文档")
    assert controller.acceptance == ("文档存在",)
    assert after["family_counts"] == before["family_counts"]
    assert controller.snapshot()["seen_sources"] == before_sources
    assert after["consecutive_no_progress_rounds"] == 0
    assert after["review_count"] == 0
