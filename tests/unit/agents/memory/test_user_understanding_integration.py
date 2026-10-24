# -*- coding: utf-8 -*-
"""Memory-manager behavior for sparse user-understanding projection."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from lantai.agents.memory.reme_light_memory_manager import (
    ReMeLightMemoryManager,
)
from lantai.agents.memory.layered_memory import AtomCandidate
from lantai.agents.memory.structured_user_memory import StructuredUserMemoryStore
from lantai.agents.persona.user_understanding import (
    UnderstandingAtom,
    UnderstandingCandidate,
)
from lantai.runtime.converters import LegacyMsg


def test_confirmed_memory_is_available_as_a_turn_specific_understanding(
    tmp_path,
):
    manager = ReMeLightMemoryManager(str(tmp_path), "default")
    manager.user_memory_store.add_entry(
        "用户希望沈行知主动说出没有明说的担心。",
        memory_type="style",
        source="manual",
    )

    prompt = manager.project_user_understanding_prompt("这份方案我还是不放心")

    assert '<user-understanding source="evidence-projection">' in prompt
    assert "已确认可信，且与当前 case 相关" in prompt
    assert "主动说出没有明说的担心" in prompt


def test_confirmed_old_case_feedback_stays_known_but_out_of_unrelated_turns(
    tmp_path,
):
    manager = ReMeLightMemoryManager(str(tmp_path), "default")
    statement = "用户对当前输出极度不满意，需要以更高标准重新做。"
    decision = manager.user_memory_store.add_entry(
        statement,
        memory_type="style",
        source="manual",
    )

    prompt = manager.project_user_understanding_prompt(
        "做一个模仿超级马里奥的小游戏",
    )

    assert decision["entry"]["confirmed"] is True
    assert manager.user_memory_store.load_entries()[0].content == statement
    assert statement not in prompt


def test_manager_startup_migrates_preexisting_confirmed_one_case_memory(
    tmp_path,
):
    statement = "用户对当前输出极度不满意，需要以更高标准重新做。"
    store = StructuredUserMemoryStore(tmp_path)
    created = store.add_entry(
        statement,
        memory_type="style",
        source="manual",
    )["entry"]
    entries = store.load_entries()
    entries[0].scope = "global"  # Simulate data from the installed 1.0.45 app.
    entries[0].scope_id = ""
    store.save_entries(entries)
    governance_marker = tmp_path / "memory-ledger" / "migration-v2.json"
    governance_marker.parent.mkdir(parents=True, exist_ok=True)
    governance_marker.write_text(
        '{"version":"memory-governance-v2"}',
        encoding="utf-8",
    )

    manager = ReMeLightMemoryManager(str(tmp_path), "default")

    migrated = manager.user_memory_store.load_entries()[0]
    assert migrated.id == created["id"]
    assert migrated.confirmed is True
    assert migrated.scope == "thread"
    assert (
        tmp_path / "memory-ledger" / "recall-safety-v1.json"
    ).is_file()
    assert statement not in manager.project_user_understanding_prompt(
        "做一个模仿超级马里奥的小游戏",
    )


def test_relevant_working_guess_stays_out_of_hidden_turn_context(
    tmp_path,
):
    manager = ReMeLightMemoryManager(str(tmp_path), "default")
    manager.user_understanding_store.remember(
        UnderstandingAtom.create(
            kind="decision_pattern",
            statement="用户在发布前反复修改时，可能真正担心公开后的不可逆评价。",
            status="working_hypothesis",
            confidence=0.82,
            evidence_ids=("event_release_1", "event_release_2"),
        ),
    )

    prompt = manager.project_user_understanding_prompt(
        "这个发布方案我还是想再改一遍",
    )

    assert prompt == ""


def test_operational_activity_never_becomes_hidden_turn_instruction(tmp_path):
    task_path = tmp_path / "capabilities" / "task-runs.json"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "id": "run-persona",
                        "title": "做完人格化优化",
                        "status": "running",
                        "artifacts": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reminder_path = tmp_path / "reminders" / "reminders.json"
    reminder_path.parent.mkdir(parents=True)
    reminder_path.write_text(
        json.dumps(
            {
                "reminders": [
                    {"id": "reminder-eval", "title": "晚上重看测评", "status": "new"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manager = ReMeLightMemoryManager(str(tmp_path), "default")
    manager.layered_memory.system2_summary_path.parent.mkdir(parents=True, exist_ok=True)
    manager.layered_memory.system2_summary_path.write_text(
        json.dumps(
            {
                "open_intentions": [
                    {"content": "之后还要验证情感连续性", "source_id": "s2-eval"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    prompt = manager.project_user_understanding_prompt("人格化优化还差什么")

    assert "做完人格化优化" not in prompt
    assert "晚上重看测评" not in prompt
    assert "之后还要验证情感连续性" not in prompt


def test_generic_whats_left_query_does_not_pull_other_case_open_loops(tmp_path):
    manager = ReMeLightMemoryManager(str(tmp_path), "default")
    manager.user_understanding_store.remember(
        UnderstandingAtom.create(
            kind="open_loop",
            statement="上一个 case 还要验证情感连续性。",
            status="confirmed",
            confidence=1.0,
            evidence_ids=("task:old-case",),
            scope="task",
            scope_id="old-case",
        ),
    )

    prompt = manager.project_user_understanding_prompt("这个马里奥游戏还差什么？")

    assert "情感连续性" not in prompt


@pytest.mark.asyncio
async def test_completed_turn_feeds_isolated_understanding_back_into_later_turns(
    tmp_path,
    monkeypatch,
):
    async def no_atoms(**_kwargs):
        return []

    async def extracted_understanding(**kwargs):
        return [
            UnderstandingCandidate(
                kind="relationship_protocol",
                statement="用户希望沈行知主动说出没有明说的担心。",
                confidence=0.93,
                evidence_ids=tuple(kwargs["source_ids"]),
                evidence="希望有人比我先说出心里的话",
                future_relevance="以后理解含蓄请求时",
            ),
        ]

    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_llm_atom_candidates",
        no_atoms,
    )
    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_user_understanding_candidates",
        extracted_understanding,
    )
    manager = ReMeLightMemoryManager(str(tmp_path), "default")

    await manager.capture_turn(
        [
            LegacyMsg("用户", "我希望有人比我先说出心里的话。", "user"),
            LegacyMsg("沈行知", "我明白了。", "assistant"),
        ],
        session_id="session-feedback",
    )
    prompt = manager.project_user_understanding_prompt("我今天只想说半句话")

    assert prompt == ""


@pytest.mark.asyncio
async def test_deep_personal_disclosure_keeps_core_facts_and_current_context(
    tmp_path,
    monkeypatch,
):
    async def extracted_atoms(**_kwargs):
        return [
            AtomCandidate(
                type="identity",
                content="用户近期成为父亲，家庭进入新生儿适应阶段",
                priority=3,
                confidence=0.93,
                evidence="用户直接自述刚有宝宝",
                source_kind="direct_user",
                temporal_scope="durable",
                subject="user",
                salience="core",
                evidence_quote="家里刚有了宝宝",
            ),
            AtomCandidate(
                type="identity",
                content="用户负责 C 端公关并担任一号位，管理约七八人团队",
                priority=3,
                confidence=0.95,
                evidence="用户直接自述工作职责",
                source_kind="direct_user",
                temporal_scope="durable",
                subject="user",
                salience="core",
                evidence_quote="最近接手 C 端公关一号位，现在管理七八个人",
            ),
            AtomCandidate(
                type="episodic",
                content="用户近期连续出差并承受工作与家庭的多重压力",
                priority=2,
                confidence=0.92,
                evidence="用户直接说明近期处境",
                source_kind="direct_user",
                temporal_scope="current",
                subject="user",
                salience="core",
            ),
        ]

    async def extracted_understanding(**kwargs):
        return [
            UnderstandingCandidate(
                kind="tension",
                statement="用户近期同时承受新生儿家庭适应、连续出差和新管理岗位压力。",
                confidence=0.92,
                evidence_ids=tuple(kwargs["source_ids"]),
                evidence="用户直接说明近期多重压力",
                future_relevance="后续给出工作与生活建议时",
            ),
        ]

    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_llm_atom_candidates",
        extracted_atoms,
    )
    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_user_understanding_candidates",
        extracted_understanding,
    )

    async def reviewed_sources(**_kwargs):
        review_items = json.loads(_kwargs["user_prompt"])["candidates"]
        return SimpleNamespace(
            text=json.dumps({"verdicts": [{
                "index": item["index"], "status": "supported",
                "supporting_source_record_ids": list(item["evidence_quotes"]),
                "reason": "用户直接说明这件事",
            } for item in review_items]}, ensure_ascii=False),
            model_label="test-reviewer",
        )

    monkeypatch.setattr(
        "lantai.agents.memory.layered_memory.run_memory_text_task",
        reviewed_sources,
    )
    manager = ReMeLightMemoryManager(str(tmp_path), "default")

    result = await manager.capture_turn(
        [
            LegacyMsg(
                "用户",
                (
                    "我最近很累，家里刚有了宝宝，之后又连续出差十几天。"
                    "最近接手 C 端公关一号位，现在管理七八个人，"
                    "家里也在磨合，我对未来有些迷茫。"
                ),
                "user",
            ),
            LegacyMsg("沈行知", "我先陪你把这些压力拆开。", "assistant"),
        ],
        session_id="session-deep-disclosure",
        source="manual_reindex",
    )

    confirmed = manager.memory_ledger.confirmed_facts()
    assert result["l0_records"] == 2
    assert result["understanding_atoms"] == 1
    assert {fact.content for fact in confirmed} == {
        "用户近期成为父亲，家庭进入新生儿适应阶段",
        "用户负责 C 端公关并担任一号位，管理约七八人团队",
    }
    assert all(fact.session_ids == ["session-deep-disclosure"] for fact in confirmed)
    assert all(
        manager.memory_ledger.open_fact_sources(fact.id)["sources"][0]["status"] == "located"
        for fact in confirmed
    )
    projection = manager.user_understanding_store.project_for_turn(
        "最近工作和家里都很累",
    )
    assert [item.kind for item in projection.hypotheses] == ["tension"]


@pytest.mark.asyncio
async def test_turn_runs_fact_and_current_context_understanding_concurrently(
    tmp_path,
    monkeypatch,
):
    both_started = asyncio.Event()
    started: set[str] = set()
    completed: set[str] = set()

    async def wait_for_other_extractor(name: str) -> None:
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.2)
        completed.add(name)

    async def extracted_atoms(**_kwargs):
        await wait_for_other_extractor("facts")
        return []

    async def extracted_understanding(**_kwargs):
        await wait_for_other_extractor("context")
        return []

    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_llm_atom_candidates",
        extracted_atoms,
    )
    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_user_understanding_candidates",
        extracted_understanding,
    )
    manager = ReMeLightMemoryManager(str(tmp_path), "default")

    await manager.capture_turn(
        [LegacyMsg("用户", "我刚接手一个新团队，最近也很疲惫。", "user")],
        session_id="session-concurrent-understanding",
        source="manual_reindex",
    )

    assert completed == {"facts", "context"}


@pytest.mark.asyncio
async def test_close_drains_native_background_memory_capture(tmp_path) -> None:
    manager = ReMeLightMemoryManager(str(tmp_path), "default")
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def capture_turn(**_kwargs):
        started.set()
        await release.wait()
        completed.set()
        return {"maintenance_required": False}

    async def close_backend():
        return True

    manager.capture_turn = capture_turn
    manager._reme = SimpleNamespace(close=close_backend)
    manager.schedule_capture_turn(
        messages=[LegacyMsg("用户", "这是本轮重要信息。", "user")],
        session_id="session-background-capture",
        source="native_post_reply",
    )
    await started.wait()

    close_task = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    assert close_task.done() is False

    release.set()
    assert await close_task is True
    assert completed.is_set()


@pytest.mark.asyncio
async def test_slow_semantic_capture_queues_and_eventually_enriches_later_turns(
    tmp_path,
    monkeypatch,
) -> None:
    """Accepted semantic work is delayed rather than silently discarded."""

    semantic_calls: list[str] = []
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def hold_first_pair(name: str) -> None:
        semantic_calls.append(name)
        if len(semantic_calls) == 2:
            both_started.set()
        await release.wait()

    async def slow_atoms(**_kwargs):
        await hold_first_pair("facts")
        return []

    async def no_understanding(**_kwargs):
        await hold_first_pair("understanding")
        return []

    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.load_config",
        lambda: SimpleNamespace(
            utility_intelligence=SimpleNamespace(mode="dedicated"),
        ),
    )
    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_llm_atom_candidates",
        slow_atoms,
    )
    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_user_understanding_candidates",
        no_understanding,
    )
    manager = ReMeLightMemoryManager(str(tmp_path), "default")

    first = manager.schedule_capture_turn(
        messages=[LegacyMsg("用户", "第一轮证据。", "user")],
        session_id="session-first",
        source="native_post_reply",
    )
    await asyncio.wait_for(both_started.wait(), timeout=0.5)
    second = manager.schedule_capture_turn(
        messages=[LegacyMsg("用户", "第二轮证据。", "user")],
        session_id="session-second",
        source="native_post_reply",
    )
    await asyncio.sleep(0.05)
    assert second.done() is False
    release.set()
    await asyncio.wait_for(first, timeout=0.5)
    await asyncio.wait_for(second, timeout=0.5)

    assert semantic_calls.count("facts") == 2
    assert semantic_calls.count("understanding") == 2
    assert {
        event.content for event in manager.memory_ledger.list_events()
    } >= {"第一轮证据。", "第二轮证据。"}


@pytest.mark.asyncio
async def test_clear_quiesce_drains_active_capture_and_rejects_late_write(
    tmp_path,
) -> None:
    manager = ReMeLightMemoryManager(str(tmp_path), "default")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def capture_turn(**_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"maintenance_required": False}

    manager.capture_turn = capture_turn
    first = manager.schedule_capture_turn(
        messages=[LegacyMsg("用户", "这是清空前已接收的信息。", "user")],
        session_id="session-before-clear",
    )
    await started.wait()

    quiesce = asyncio.create_task(manager.quiesce_capture_for_clear())
    await asyncio.sleep(0)
    assert quiesce.done() is False

    late = manager.schedule_capture_turn(
        messages=[LegacyMsg("用户", "这条不应再进入记忆。", "user")],
        session_id="session-during-clear",
    )
    await late
    assert calls == 1

    release.set()
    await first
    await quiesce
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_semantic_extraction_keeps_only_raw_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    async def no_candidates(**_kwargs):
        return []

    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_llm_atom_candidates",
        no_candidates,
    )
    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_user_understanding_candidates",
        no_candidates,
    )
    manager = ReMeLightMemoryManager(str(tmp_path), "default")

    result = await manager.capture_turn(
        [LegacyMsg("用户", "我是某项目负责人，管理七八个人。", "user")],
        session_id="session-no-semantic-result",
    )

    assert result["l0_records"] == 1
    assert result["l1_atoms"] == 0
    assert manager.memory_ledger.list_events()
    assert manager.memory_ledger._load_facts() == []


@pytest.mark.asyncio
async def test_memory_never_turns_future_language_into_a_task(
    tmp_path,
    monkeypatch,
):
    calls: list[str] = []

    async def no_atoms(**_kwargs):
        calls.append("facts")
        return []

    async def extracted_understanding(**kwargs):
        calls.append("understanding")
        return [
            UnderstandingCandidate(
                kind="prospective_intent",
                statement="用户希望人格化优化完成后再做一轮真实模型测评。",
                confidence=0.94,
                evidence_ids=tuple(kwargs["source_ids"]),
                evidence="做完后再测一轮",
                future_relevance="优化完成时",
            ),
        ]

    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_llm_atom_candidates",
        no_atoms,
    )
    monkeypatch.setattr(
        "lantai.agents.memory.reme_light_memory_manager.extract_user_understanding_candidates",
        extracted_understanding,
    )
    manager = ReMeLightMemoryManager(str(tmp_path), "default")

    await manager.capture_turn(
        [
            LegacyMsg("用户", "这轮做完后再做一轮真实模型测评。", "user"),
            LegacyMsg("沈行知", "好。", "assistant"),
        ],
        session_id="session-prospective",
    )

    assert calls == []
    assert manager.opportunity_store.next_eligible() is None
