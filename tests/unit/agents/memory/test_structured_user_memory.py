# -*- coding: utf-8 -*-
# pylint: disable=unsubscriptable-object
"""Tests for structured user memory natural-language normalization."""

from pathlib import Path
from unittest.mock import AsyncMock

from lantai.agents.memory import structured_user_memory
from lantai.agents.memory.structured_user_memory import (
    MemoryCandidate,
    StructuredUserMemoryStore,
    UserMemoryEntry,
    build_memory_recall_plan,
    extract_structured_user_memory,
)
from lantai.app.utility_model import UtilityTextResult
from lantai.runtime.converters import LegacyMsg as Msg


def test_unconfirmed_compatibility_entry_never_enters_system_prompt(tmp_path):
    store = StructuredUserMemoryStore(tmp_path)
    automatic = store.add_entry(
        "以后回答默认更简洁",
        memory_type="style",
        source="agent",
        confidence=0.9,
    )["entry"]

    assert store.format_for_system_prompt() == ""
    store.mark_confirmed(automatic["id"], canonical_fact_id="fact_1")
    assert "以后回答默认更简洁" in store.format_for_system_prompt()


def test_search_relevant_does_not_return_confirmed_but_unrelated_memory(
    tmp_path,
):
    store = StructuredUserMemoryStore(tmp_path)
    decision = store.add_entry(
        "用户对当前输出极度不满意，需要以更高标准重新做。",
        memory_type="style",
        source="manual",
    )
    assert decision["entry"]["confirmed"] is True
    assert decision["entry"]["scope"] == "thread"

    assert store.search_relevant("做一个模仿超级马里奥的小游戏") == []


def test_canonical_scope_survives_the_structured_projection_round_trip(
    tmp_path,
):
    store = StructuredUserMemoryStore(tmp_path)
    decision = store.add_entry(
        "项目默认使用 pytest 跑单测。",
        memory_type="workflow",
        source="agent",
        scope="project",
        scope_id="project-lantai3",
    )
    entry_id = decision["entry"]["id"]

    confirmed = store.mark_confirmed(
        entry_id,
        canonical_fact_id="fact-project",
        scope="project",
        scope_id="project-lantai3",
    )
    reloaded = store.load_entries()[0]

    assert confirmed is not None
    assert (reloaded.scope, reloaded.scope_id) == (
        "project",
        "project-lantai3",
    )


def test_confirmed_preferences_add_non_emotional_collaboration_calibration(
    tmp_path,
):
    """Confirmed memory lowers friction without implying affinity."""
    store = StructuredUserMemoryStore(tmp_path)
    store.add_entry(
        "默认先看结论，再看必要理由。",
        memory_type="style",
        source="manual",
    )

    prompt = store.format_for_system_prompt()

    assert '<collaboration-calibration stage="calibrating">' in prompt
    assert "这不是好感度或亲密度" in prompt
    assert "不能放宽权限、事实标准或关系边界" in prompt


def test_multiple_confirmed_memory_types_establish_collaboration_without_trust(
    tmp_path,
):
    store = StructuredUserMemoryStore(tmp_path)
    store.add_entry("默认结论先行。", "style", source="manual")
    store.add_entry("默认先运行最小测试。", "instruction", source="manual")
    store.add_entry("默认项目改动只落在 lantai3。", "workflow", source="manual")

    prompt = store.format_for_system_prompt()

    assert '<collaboration-calibration stage="established">' in prompt
    assert "不是好感度或亲密度" in prompt
    assert "当前消息优先于历史偏好" in prompt
    assert "已确认历史的回声" in prompt
    assert "可以自然说出‘又是……’" in prompt
    assert "只能引用本提示中真实存在的记忆" in prompt
    assert "不要每轮翻旧账" in prompt


async def test_structured_extraction_uses_the_isolated_utility_task(
    tmp_path,
    monkeypatch,
) -> None:
    run_task = AsyncMock(
        return_value=UtilityTextResult(
            text=(
                '{"memories":[{"type":"style","content":"用户偏好简洁回答",'
                '"confidence":0.9,"reason":"长期表达偏好",'
                '"evidence":"以后都简洁一点"}]}'
            ),
            model_label="utility/test",
        ),
    )
    monkeypatch.setattr(
        structured_user_memory,
        "run_memory_text_task",
        run_task,
    )

    candidates = await extract_structured_user_memory(
        agent_id="agent",
        messages=[
            Msg(
                name="user",
                role="user",
                content="以后回答都简洁一点。",
            ),
        ],
        store=StructuredUserMemoryStore(tmp_path),
    )

    assert [candidate.content for candidate in candidates] == [
        "用户偏好简洁回答。",
    ]
    run_task.assert_awaited_once()
    assert (
        run_task.await_args.kwargs["purpose"]
        == "structured_user_memory_extraction"
    )


async def test_structured_extraction_keeps_only_l0_when_semantic_model_fails(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        structured_user_memory,
        "run_memory_text_task",
        AsyncMock(return_value=None),
    )

    candidates = await extract_structured_user_memory(
        agent_id="agent",
        messages=[
            Msg(
                name="user",
                role="user",
                content="以后所有报告都先给摘要。",
            ),
        ],
        store=StructuredUserMemoryStore(tmp_path),
    )

    assert candidates == []


def test_memory_candidate_rewrites_closing_phrase_complaint() -> None:
    candidate = MemoryCandidate.from_dict(
        {
            "type": "style",
            "content": "你怎么这么喜欢说“有事直接说”，好像我很啰嗦似的",
            "confidence": 0.9,
        },
    )

    assert candidate is not None
    assert candidate.content == "用户不喜欢用“有事直接说”这类话收尾，希望语气更自然、克制。"


def test_memory_candidate_rewrites_context_bloat_complaint() -> None:
    candidate = MemoryCandidate.from_dict(
        {
            "type": "workflow",
            "content": "为啥我们才说了这几句，就24.1K上下文了",
            "confidence": 0.8,
        },
    )

    assert candidate is not None
    assert candidate.content == "用户对无效的上下文膨胀很敏感，偏好更精简的信息组织。"


def test_memory_candidate_rejects_assistant_meta_identity() -> None:
    candidate = MemoryCandidate.from_dict(
        {
            "type": "identity",
            "content": "沈行知是澜台的默认工作伙伴身份名，职责是陪伴用户判断、推进和交付任务。",
            "confidence": 0.92,
        },
    )

    assert candidate is None


def test_store_add_entry_naturalizes_workdir_path(tmp_path) -> None:
    store = StructuredUserMemoryStore(tmp_path)
    source_path = Path.home() / "Downloads/未命名文件夹"

    decision = store.add_entry(
        content=(
            f"**工作区状态**：当前工作目录为 `{source_path}`。"
            "该目录下已存在 `memory.md` 与 `MEMORY.md`。"
        ),
        memory_type="workflow",
        source="agent",
    )

    assert decision["decision"] == "new"
    snapshot = store.snapshot()
    assert snapshot["entries"][0]["content"] == (
        "当前工作目录是 ~/Downloads/未命名文件夹，目录里已有 memory.md / MEMORY.md。"
    )


def test_store_add_entry_naturalizes_uploaded_file_path(tmp_path) -> None:
    store = StructuredUserMemoryStore(tmp_path)
    source_path = (
        Path.home()
        / "Downloads/未命名文件夹/media"
        / "6f9cc2ac584d457583d710b3a337b9be_image.png"
    )

    decision = store.add_entry(
        content=f"翻译一下 用户上传文件，已经下载到 {source_path}",
        memory_type="workflow",
        source="agent",
    )

    assert decision["decision"] == "new"
    snapshot = store.snapshot()
    assert snapshot["entries"][0]["content"] == (
        "处理上传文件时，文件会先落到 ~/Downloads/未命名文件夹/media/"
        "6f9cc2ac584d457583d710b3a337b9be_image.png。"
    )


def test_manual_memory_update_creates_evolution_chain(tmp_path) -> None:
    store = StructuredUserMemoryStore(tmp_path)

    first = store.add_entry(
        content="默认提醒时间是每天 09:00。",
        memory_type="instruction",
        source="manual",
        reason="用户手动设置默认提醒时间",
    )
    second = store.add_entry(
        content="默认提醒时间改为每天 15:00。",
        memory_type="instruction",
        source="manual",
        reason="用户更新默认提醒时间",
    )

    assert first["decision"] == "new"
    assert second["decision"] == "evolve"

    snapshot = store.snapshot(include_archived=True)
    entries = {entry["id"]: entry for entry in snapshot["entries"]}
    new_entry = second["entry"]
    old_entry = entries[first["entry"]["id"]]

    assert new_entry["supersedes_id"] == old_entry["id"]
    assert new_entry["chain_id"] == old_entry["chain_id"]
    assert new_entry["evolution_status"] == "current"
    assert old_entry["status"] == "archived"
    assert old_entry["evolution_status"] == "superseded"
    assert old_entry["superseded_by_id"] == new_entry["id"]
    assert old_entry["valid_to"]

    prompt = store.format_for_system_prompt()
    assert "15" in prompt
    assert "09" not in prompt


def test_retrieval_context_includes_evolution_history(tmp_path) -> None:
    store = StructuredUserMemoryStore(tmp_path)

    store.add_entry(
        content="默认提醒时间是每天 09:00。",
        memory_type="instruction",
        source="manual",
    )
    store.add_entry(
        content="默认提醒时间改为每天 15:00。",
        memory_type="instruction",
        source="manual",
    )

    context = store.format_retrieval_context("默认提醒时间")

    assert "默认提醒时间改为每天 15" in context
    assert "演化历史" in context
    assert "默认提醒时间是每天 09" in context


def test_recall_plan_routes_evolution_query_to_chain_history(tmp_path) -> None:
    store = StructuredUserMemoryStore(tmp_path)
    store.add_entry(
        content="默认提醒时间是每天 09:00。",
        memory_type="instruction",
        source="manual",
    )
    store.add_entry(
        content="默认提醒时间改为每天 15:00。",
        memory_type="instruction",
        source="manual",
    )
    plan = build_memory_recall_plan("之前为什么把默认提醒时间改了？")

    context = store.format_retrieval_context(
        "之前为什么把默认提醒时间改了？",
        recall_plan=plan,
    )

    assert plan.intent == "evolution"
    assert plan.include_evolution_history is True
    assert "Recall route: evolution" in context
    assert "默认提醒时间改为每天 15" in context
    assert "默认提醒时间是每天 09" in context


def test_search_relevant_can_filter_memory_layers(tmp_path) -> None:
    store = StructuredUserMemoryStore(tmp_path)
    store.add_entry(
        content="用户是一名日报分析师。",
        memory_type="identity",
        source="manual",
    )
    store.add_entry(
        content="默认用 pytest 跑单测。",
        memory_type="workflow",
        source="manual",
    )

    results = store.search_relevant("默认 pytest 用户", layers=["workflow"])

    assert results
    assert all(item.memory_layer == "workflow" for item in results)


def test_search_and_summary_ignore_assistant_meta_memories(tmp_path) -> None:
    store = StructuredUserMemoryStore(tmp_path)
    good = store.add_entry(
        content="用户长期负责美股收盘复盘和科技成长股研究。",
        memory_type="identity",
        source="manual",
    )
    assert good["decision"] == "new"

    entries = store.load_entries()
    entries.append(
        UserMemoryEntry.from_dict(
            {
                "id": "assistant-meta",
                "type": "identity",
                "content": "沈行知是澜台的默认工作伙伴身份名，职责是陪伴用户判断、推进和交付任务。",
                "source": "auto",
                "status": "active",
                "confidence": 0.93,
            },
        ),
    )
    store.save_entries(entries)

    summary = store.read_profile_summary()
    summary_text = " ".join(summary.get("by_type", {}).get("identity", []))
    results = store.search_relevant("用户负责什么")

    assert "沈行知" not in summary_text
    assert results
    assert all("沈行知" not in item.content for item in results)


def test_update_entry_can_confirm_low_confidence_memory(tmp_path) -> None:
    store = StructuredUserMemoryStore(tmp_path)
    decision = store.add_entry(
        content="默认用中文回答。",
        memory_type="instruction",
        source="manual",
        confidence=0.6,
    )

    updated = store.update_entry(
        decision["entry"]["id"],
        status="active",
        confidence=0.88,
    )

    assert updated is not None
    assert updated.status == "active"
    assert updated.confidence == 0.88
