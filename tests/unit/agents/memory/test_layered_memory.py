# -*- coding: utf-8 -*-
"""Tests for the layered memory store."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lantai.agents.memory import layered_memory
from lantai.agents.memory.layered_memory import (
    OFFLOAD_MARKER,
    AtomCandidate,
    L1Atom,
    LayeredMemoryStore,
    extract_llm_atom_candidates,
    review_atom_candidates,
)
from lantai.agents.memory.structured_user_memory import StructuredUserMemoryStore
from lantai.app.utility_model import UtilityTextResult
from lantai.runtime.converters import LegacyMsg as Msg


def test_local_capture_keeps_raw_l0_and_unreviewed_l1_out_of_persona(tmp_path):
    user_store = StructuredUserMemoryStore(tmp_path)
    store = LayeredMemoryStore(
        working_dir=tmp_path,
        agent_id="agent",
        user_memory_store=user_store,
    )
    messages = [
        Msg(
            name="user",
            role="user",
            content="记住：以后这个项目默认用 pytest 跑单测。",
        ),
        Msg(
            name="assistant",
            role="assistant",
            content="收到，我会默认使用 pytest。",
        ),
    ]

    result = store.capture_turn(messages, session_id="s1")

    assert result["l0_records"] == 2
    assert result["l1_atoms"] >= 1
    snapshot = store.snapshot()
    assert snapshot["stats"]["l0_conversations"] == 2
    assert snapshot["stats"]["l1_atoms"] >= 1
    assert snapshot["stats"]["l2_scenes"] == 0
    assert snapshot["engine_status"]["structured_memory"]["status"] == "已就绪"
    assert snapshot["engine_status"]["layered_memory"]["l1"] >= 1
    assert "pytest" not in snapshot["persona"]
    assert store.search_atoms("pytest") == []
    assert user_store.load_entries() == []


def test_l0_keeps_exact_original_beyond_extraction_limit(tmp_path):
    store = LayeredMemoryStore(tmp_path, "agent")
    original = "开头 🙂\n" + "甲" * 20_100 + "\n结尾：仅限本项目。"
    record = store.record_messages(
        [Msg(name="user", role="user", content=original)],
        session_id="long-message",
    )[0]

    opened = store.open_source(record.id)
    assert opened["record"]["content"] == original
    assert opened["record"]["content_hash"] == layered_memory._sha(original)
    extraction_input = layered_memory._format_messages_for_atom_extraction([], [record])
    assert record.id in extraction_input
    assert "结尾：仅限本项目。" in extraction_input


def test_deleted_l0_source_cannot_reappear_from_same_message(tmp_path):
    store = LayeredMemoryStore(tmp_path, "agent")
    message = Msg(name="user", role="user", content="请记住仅限本项目使用 pytest")
    record = store.record_messages([message], session_id="s1")[0]
    assert store.purge_source_record(record.id) is True
    assert store.open_source(record.id) is None
    assert store.record_messages([message], session_id="s1") == []
    assert LayeredMemoryStore(tmp_path, "agent").open_source(record.id) is None


def test_purging_one_source_removes_cached_quote_and_demotes_joint_atom(tmp_path):
    store = LayeredMemoryStore(tmp_path, "agent")
    first, second = store.record_messages([
        Msg(name="user", role="user", content="我负责北辰项目测试。"),
        Msg(name="user", role="user", content="我也维护自动化。"),
    ], session_id="joint")
    store.upsert_atoms([L1Atom(
        id="joint-atom", type="workflow", content="负责北辰测试和自动化",
        priority=2, confidence=0.9, scene_name="general",
        source_message_ids=[], source_record_ids=[first.id, second.id],
        evidence="我负责北辰项目测试。", created_at="2026-09-27T00:00:00Z",
        updated_at="2026-09-27T00:00:00Z", status="active",
        metadata={
            "evidence_quote": "我负责北辰项目测试。",
            "evidence_quotes": {
                first.id: "我负责北辰项目测试。",
                second.id: "我也维护自动化。",
            },
            "support_source_record_ids": [first.id, second.id],
            "support_review_groups": [[first.id, second.id]],
            "support_status": "supported",
            "support_reason": "我负责北辰项目测试。与第二条共同支持",
        },
    )])

    assert store.purge_source_record(first.id)

    atom = store.load_atoms(include_archived=True)[0]
    assert atom.status == "pending"
    assert atom.source_record_ids == [second.id]
    assert atom.metadata["support_source_record_ids"] == []
    assert atom.metadata["evidence_quotes"] == {second.id: "我也维护自动化。"}
    assert first.id not in json.dumps(atom.to_dict(), ensure_ascii=False)
    assert "我负责北辰项目测试。" not in json.dumps(atom.to_dict(), ensure_ascii=False)


def test_injected_memory_context_is_not_saved_as_new_source(tmp_path):
    store = LayeredMemoryStore(tmp_path, "agent")
    records = store.record_messages(
        [Msg(name="system", role="system", content="<memory-context>old fact</memory-context>")],
        session_id="s1",
    )
    assert records == []


def test_system_instructions_are_not_archived_as_memory_sources(tmp_path):
    store = LayeredMemoryStore(tmp_path, "agent")
    records = store.record_messages([
        Msg(name="system", role="system", content="系统内部指令，不应成为长期原文"),
        Msg(name="user", role="user", content="请记住本项目用 pytest。"),
    ], session_id="system-filter")

    assert len(records) == 1
    assert records[0].role == "user"
    assert "系统内部指令" not in json.dumps(store.snapshot(), ensure_ascii=False)


async def test_l1_extraction_uses_the_isolated_utility_task(monkeypatch) -> None:
    run_task = AsyncMock(
        return_value=UtilityTextResult(
            text=(
                '{"atoms":[{"type":"workflow","content":"默认使用 pytest",'
                '"priority":2,"confidence":0.9,"evidence":"用户明确要求",'
                '"source_kind":"direct_user","temporal_scope":"durable",'
                '"subject":"project","salience":"core"}]}'
            ),
            model_label="utility/test",
        ),
    )
    monkeypatch.setattr(layered_memory, "run_memory_text_task", run_task)

    candidates = await extract_llm_atom_candidates(
        agent_id="agent",
        messages=[
            Msg(
                name="user",
                role="user",
                content="以后这个项目默认使用 pytest。",
            ),
        ],
        existing_atoms=[
            SimpleNamespace(
                type="workflow",
                content="以后这个项目默认使用 pytest",
                status="observed",
                confidence=0.68,
            ),
        ],
    )

    assert [candidate.content for candidate in candidates] == ["默认使用 pytest"]
    assert candidates[0].source_kind == "direct_user"
    assert candidates[0].temporal_scope == "durable"
    assert candidates[0].subject == "project"
    assert candidates[0].salience == "core"
    run_task.assert_awaited_once()
    assert (
        run_task.await_args.kwargs["purpose"]
        == "layered_memory_atom_extraction"
    )
    assert "status=observed; confidence=0.68" in run_task.await_args.kwargs[
        "user_prompt"
    ]


async def test_semantic_review_rejects_overgeneralization_and_preserves_source(monkeypatch):
    run_task = AsyncMock(return_value=UtilityTextResult(
        text='{"verdicts":[{"index":0,"status":"unsupported","reason":"只限本周周报，不能概括成不喜欢所有提醒"}]}',
        model_label="utility/reviewer",
    ))
    monkeypatch.setattr(layered_memory, "run_memory_text_task", run_task)
    candidate = AtomCandidate(
        type="style", content="用户不喜欢提醒", source_kind="direct_user",
        temporal_scope="durable", subject="user", salience="core",
        source_record_ids=["source-one"], evidence_quote="这周先别提醒我周报",
    )
    reviewed = await review_atom_candidates(
        agent_id="agent", candidates=[candidate],
        records=[SimpleNamespace(
            id="source-one", role="user", metadata={},
            content="这周先别提醒我周报，下周恢复提醒。",
        )],
        corrections=[],
    )
    assert reviewed[0].support_status == "unsupported"
    assert reviewed[0].support_model == "utility/reviewer"
    assert "下周恢复提醒" in run_task.await_args.kwargs["user_prompt"]
    assert run_task.await_args.kwargs["purpose"] == "memory_evidence_support_review"


async def test_semantic_review_accepts_distinct_quotes_from_two_original_messages(monkeypatch, tmp_path):
    store = LayeredMemoryStore(tmp_path, "agent")
    records = store.record_messages([
        Msg(name="user", role="user", content="我负责北辰项目的测试。"),
        Msg(name="user", role="user", content="北辰项目的自动化也由我维护。"),
    ], session_id="two-sources")
    run_task = AsyncMock(return_value=UtilityTextResult(
        text=json.dumps({"verdicts": [{
            "index": 0, "status": "supported", "reason": "两条原话共同支持",
            "supporting_source_record_ids": [record.id for record in records],
        }]}),
        model_label="utility/reviewer",
    ))
    monkeypatch.setattr(layered_memory, "run_memory_text_task", run_task)
    candidate = AtomCandidate(
        type="workflow", content="用户负责北辰项目测试及自动化维护",
        confidence=0.9, source_kind="direct_user", temporal_scope="durable",
        subject="user", salience="core",
        source_record_ids=[record.id for record in records],
        evidence_quotes={
            records[0].id: "负责北辰项目的测试",
            records[1].id: "北辰项目的自动化也由我维护",
        },
    )
    reviewed = await review_atom_candidates(
        agent_id="agent", candidates=[candidate], records=records, corrections=[],
    )
    atom = store.ingest_atom_candidates(reviewed, records=records)[0]
    assert reviewed[0].support_status == "supported"
    assert atom.source_record_ids == [record.id for record in records]
    assert atom.metadata["evidence_quotes"] == candidate.evidence_quotes
    prompt = run_task.await_args.kwargs["user_prompt"]
    assert "负责北辰项目的测试" in prompt
    assert "北辰项目的自动化也由我维护" in prompt


async def test_semantic_review_does_not_trust_unlisted_source(monkeypatch, tmp_path):
    store = LayeredMemoryStore(tmp_path, "agent")
    records = store.record_messages([
        Msg(name="user", role="user", content="我负责北辰项目测试。"),
        Msg(name="user", role="user", content="下午记得买咖啡。"),
    ], session_id="two-sources")
    monkeypatch.setattr(layered_memory, "run_memory_text_task", AsyncMock(
        return_value=UtilityTextResult(
            text=json.dumps({"verdicts": [{
                "index": 0, "status": "supported", "reason": "只有第一条相关",
                "supporting_source_record_ids": [records[0].id],
            }]}),
            model_label="utility/reviewer",
        ),
    ))
    candidate = AtomCandidate(
        type="identity", content="用户负责北辰项目测试", confidence=0.9,
        source_kind="direct_user", temporal_scope="durable", subject="user",
        salience="core", source_record_ids=[record.id for record in records],
        evidence_quotes={records[0].id: "负责北辰项目测试", records[1].id: "买咖啡"},
    )

    reviewed = await review_atom_candidates(
        agent_id="agent", candidates=[candidate], records=records, corrections=[],
    )

    assert reviewed[0].support_status == "uncertain"
    assert store.ingest_atom_candidates(reviewed, records=records)[0].status != "active"


async def test_review_failure_cannot_activate_automatic_core_fact(monkeypatch, tmp_path):
    monkeypatch.setattr(layered_memory, "run_memory_text_task", AsyncMock(return_value=None))
    store = LayeredMemoryStore(tmp_path, "agent")
    record = store.record_messages(
        [Msg(name="user", role="user", content="我长期负责测试平台。")],
        session_id="s1",
    )[0]
    candidate = AtomCandidate(
        type="identity", content="用户长期负责测试平台", confidence=0.95,
        source_kind="direct_user", temporal_scope="durable", subject="user",
        salience="core", source_record_ids=[record.id], evidence_quote="长期负责测试平台",
    )
    reviewed = await review_atom_candidates(
        agent_id="agent", candidates=[candidate], records=[record], corrections=[],
    )
    atom = store.ingest_atom_candidates(reviewed, records=[record])[0]
    assert reviewed[0].support_status == "unreviewed"
    assert atom.status != "active"
    assert atom.metadata["support_status"] == "unreviewed"


def test_pending_and_observed_atoms_never_enter_stable_context_or_recall(tmp_path):
    store = LayeredMemoryStore(working_dir=tmp_path, agent_id="agent")
    pending = store.ingest_atom_candidates(
        [
            AtomCandidate(
                type="style",
                content="以后回答默认更自然、克制",
                confidence=0.82,
            ),
        ],
    )[0]
    observed = store.ingest_atom_candidates(
        [
            AtomCandidate(
                type="workflow",
                content="这次请先生成日报模板",
                confidence=0.7,
            ),
        ],
    )[0]

    assert pending.status == "pending"
    assert observed.status == "observed"
    store.refresh_scenes_and_persona()
    assert store.search_atoms("自然 克制 日报") == []
    assert "自然" not in store.format_stable_context()
    assert "日报" not in store.format_stable_context()


def test_active_l1_atom_does_not_recall_into_an_unrelated_new_case(tmp_path):
    store = LayeredMemoryStore(working_dir=tmp_path, agent_id="agent")
    atom = store.ingest_atom_candidates(
        [
            AtomCandidate(
                type="style",
                content="用户对当前输出极度不满意，需要以更高标准重新做。",
                confidence=1.0,
                explicit_user_request=True,
            ),
        ],
    )[0]
    assert atom.status == "active"

    assert store.search_atoms("做一个模仿超级马里奥的小游戏") == []


def test_system2_prior_cannot_bypass_current_query_relevance(tmp_path):
    store = LayeredMemoryStore(working_dir=tmp_path, agent_id="agent")
    store.system2_summary_path.parent.mkdir(parents=True, exist_ok=True)
    store.system2_summary_path.write_text(
        '{"updated_at":"2026-08-09T00:00:00+00:00","models":['
        '{"key":"communication_model","summary":"用户对旧输出极度不满意",'
        '"sample":["用户对旧输出极度不满意"]}]}',
        encoding="utf-8",
    )

    assert store.hybrid_recall("做一个模仿超级马里奥的小游戏") == []


def test_parallel_l0_writes_do_not_lose_records_or_corrupt_state(tmp_path):
    """Concurrent post-reply capture must preserve both JSONL and dedupe state."""
    first = LayeredMemoryStore(working_dir=tmp_path, agent_id="agent")
    second = LayeredMemoryStore(working_dir=tmp_path, agent_id="agent")

    def record(store, text):
        return store.record_messages(
            [Msg(name="user", role="user", content=text)],
            session_id="same-session",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda pair: record(*pair),
                [(first, "第一条并发记忆"), (second, "第二条并发记忆")],
            ),
        )

    assert sum(len(result) for result in results) == 2
    snapshot = first.snapshot()
    assert snapshot["stats"]["l0_conversations"] == 2


def test_offload_tool_results_writes_ref_and_replaces_output(tmp_path):
    store = LayeredMemoryStore(working_dir=tmp_path, agent_id="agent")
    large_text = "line\n" * 2000
    messages = [
        Msg(
            name="assistant",
            role="assistant",
            content=[
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read_file",
                    "input": {"path": "big.log"},
                },
            ],
        ),
        Msg(
            name="assistant",
            role="system",
            content=[
                {
                    "type": "tool_result",
                    "id": "call_1",
                    "name": "read_file",
                    "output": [{"type": "text", "text": large_text}],
                },
            ],
        ),
    ]

    result = store.offload_tool_results(messages, min_bytes=1000)

    assert result["created"] == 1
    assert result["replaced"] == 1
    output = messages[1].content[0]["output"][0]["text"]
    assert OFFLOAD_MARKER in output
    snapshot = store.snapshot()
    assert snapshot["stats"]["offload_entries"] == 1
    node_id = snapshot["offload"][0]["node_id"]
    source = store.open_source(node_id)
    assert source is not None
    assert source["kind"] == "offload_ref"


def test_atom_upsert_marks_conflicts(tmp_path):
    store = LayeredMemoryStore(working_dir=tmp_path, agent_id="agent")

    first = store.ingest_atom_candidates(
        [
            AtomCandidate(
                type="instruction",
                content="以后项目默认 pytest 超时 10 秒。",
                priority=2,
                confidence=0.8,
            ),
        ],
    )
    second = store.ingest_atom_candidates(
        [
            AtomCandidate(
                type="instruction",
                content="以后项目默认 pytest 超时 20 秒。",
                priority=2,
                confidence=0.8,
            ),
        ],
    )

    assert first
    assert second
    assert second[0].status == "conflict"
    assert second[0].metadata["related_ids"] == [first[0].id]


def test_atom_upsert_keeps_old_source_reviews_when_status_advances(tmp_path):
    store = LayeredMemoryStore(working_dir=tmp_path, agent_id="agent")
    common = dict(
        id="same-atom", type="workflow", content="用户默认使用 pytest。",
        priority=2, confidence=0.9, scene_name="", source_message_ids=[],
        evidence="", created_at="2026-09-27T00:00:00Z",
        updated_at="2026-09-27T00:00:00Z",
    )
    store.upsert_atoms([L1Atom(
        **common, source_record_ids=["old"], status="pending",
        metadata={"support_source_record_ids": ["old"],
                  "evidence_quotes": {"old": "默认使用 pytest"}},
    )])

    merged = store.upsert_atoms([L1Atom(
        **common, source_record_ids=["new"], status="active",
        metadata={"support_source_record_ids": ["new"],
                  "evidence_quotes": {"new": "默认使用 pytest。"}},
    )])[0]

    assert merged.source_record_ids == ["old", "new"]
    assert merged.metadata["support_source_record_ids"] == ["old", "new"]
    assert merged.metadata["evidence_quotes"] == {
        "old": "默认使用 pytest", "new": "默认使用 pytest。",
    }


def test_purging_one_source_keeps_an_independently_reviewed_atom(tmp_path):
    store = LayeredMemoryStore(tmp_path, "agent")
    first, second = store.record_messages([
        Msg(name="user", role="user", content="本项目默认使用 pytest。"),
        Msg(name="user", role="user", content="再确认本项目默认使用 pytest。"),
    ], session_id="independent")
    common = dict(
        id="independent-atom", type="workflow", content="本项目默认使用 pytest",
        priority=2, confidence=0.9, scene_name="general", source_message_ids=[],
        evidence="", created_at="2026-09-27T00:00:00Z",
        updated_at="2026-09-27T00:00:00Z", status="active",
    )
    for record in (first, second):
        store.upsert_atoms([L1Atom(
            **common, source_record_ids=[record.id], metadata={
                "support_status": "supported",
                "support_source_record_ids": [record.id],
                "support_review_groups": [[record.id]],
                "evidence_quotes": {record.id: "本项目默认使用 pytest"},
            },
        )])

    store.purge_source_record(first.id)

    atom = store.load_atoms()[0]
    assert atom.status == "active"
    assert atom.source_record_ids == [second.id]
    assert atom.metadata["support_source_record_ids"] == [second.id]
    assert atom.metadata["support_review_groups"] == [[second.id]]


def test_hybrid_recall_fuses_structured_atoms_and_l0(tmp_path):
    user_store = StructuredUserMemoryStore(tmp_path)
    user_store.add_entry(
        content="用户要求默认用 pytest 跑单测。",
        memory_type="workflow",
        source="manual",
    )
    store = LayeredMemoryStore(
        working_dir=tmp_path,
        agent_id="agent",
        user_memory_store=user_store,
    )
    store.capture_turn(
        [
            Msg(
                name="user",
                role="user",
                content="记住：这个仓库默认用 pytest。",
            ),
            Msg(name="assistant", role="assistant", content="已记录。"),
        ],
    )

    results = store.hybrid_recall(
        query="pytest 单测",
        structured_entries=user_store.search_relevant("pytest 单测"),
    )
    sources = {item["source"] for item in results}

    assert "structured" in sources
    assert "l1" in sources or "l0" in sources


def test_system2_consolidate_writes_cognitive_summary(tmp_path):
    user_store = StructuredUserMemoryStore(tmp_path)
    user_store.add_entry(
        content="用户是一名美股市场日报分析师。",
        memory_type="identity",
        source="manual",
    )
    user_store.add_entry(
        content="默认提醒时间是每天 09:00。",
        memory_type="instruction",
        source="manual",
    )
    user_store.add_entry(
        content="默认提醒时间改为每天 15:00。",
        memory_type="instruction",
        source="manual",
    )
    store = LayeredMemoryStore(
        working_dir=tmp_path,
        agent_id="agent",
        user_memory_store=user_store,
    )
    store.ingest_atom_candidates(
        [
            AtomCandidate(
                type="episodic",
                content="用户任务：继续完成记忆小闭环；下一步需要验证双版本。",
                priority=2,
                confidence=0.8,
            ),
        ],
    )

    summary = store.system2_consolidate(reason="test")

    assert summary["models"]
    assert summary["open_intentions"] == []
    assert summary["evolution_chains"]
    snapshot = store.snapshot()
    assert snapshot["stats"]["system2_models"] >= 1
    assert snapshot["stats"]["open_intentions"] == 0
    assert "system2" in snapshot
    assert "System2" in store.system2_markdown_path.read_text(encoding="utf-8")


def test_snapshot_does_not_infer_pending_state_from_confidence(tmp_path):
    user_store = StructuredUserMemoryStore(tmp_path)
    user_store.add_entry(
        content="默认用中文回答。",
        memory_type="instruction",
        source="agent",
        confidence=0.55,
    )
    store = LayeredMemoryStore(
        working_dir=tmp_path,
        agent_id="agent",
        user_memory_store=user_store,
    )

    snapshot = store.snapshot()

    assert snapshot["engine_status"]["structured_memory"]["visible_entries"] == 1
    assert snapshot["engine_status"]["structured_memory"]["pending_entries"] == 0


def test_stable_context_includes_system2_summary(tmp_path):
    store = LayeredMemoryStore(working_dir=tmp_path, agent_id="agent")
    store.ingest_atom_candidates(
        [
            AtomCandidate(
                type="workflow",
                content="项目默认先在 lantai3 验证，再同步到 App Store 版。",
                priority=2,
                confidence=0.85,
                explicit_user_request=True,
            ),
        ],
    )
    store.system2_consolidate(reason="test")

    context = store.format_stable_context(include_audit_profile=True)

    assert "<system2-memory" in context
    assert "lantai3" in context
