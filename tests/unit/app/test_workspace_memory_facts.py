# -*- coding: utf-8 -*-

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.dreaming import MemoryDreamingService
from lantai.agents.memory.policy import MemoryPolicyResult
from lantai.agents.persona.intervention_router import InterventionCandidate
from lantai.agents.persona.opportunity_queue import OpportunityStore
from lantai.agents.persona.user_understanding import (
    UnderstandingAtom,
    UserUnderstandingStore,
)
from lantai.app.routers import workspace as workspace_router
from lantai.app.reminders.models import RemindersFile, WatchTopic
from lantai.app.reminders.repo import ReminderRepository
from lantai.config.config import ReMeLightMemoryConfig


async def test_memory_fact_api_lists_and_confirms_real_pending_facts(
    tmp_path,
    monkeypatch,
):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("以后回答默认更简洁", session_id="s1")
    fact = ledger.capture_fact(
        "以后回答默认更简洁",
        memory_type="style",
        event=event,
    )

    snapshot = await workspace_router.list_memory_facts(
        request=None,
        status="pending",
        limit=5,
    )
    assert [item["id"] for item in snapshot["facts"]] == [fact.id]

    result = await workspace_router.confirm_memory_fact(
        fact.id,
        workspace_router.MemoryFactAction(scope="global"),
        request=None,
    )
    assert result["confirmed"] is True
    assert CanonicalMemoryLedger(tmp_path).confirmed_facts()[0].id == fact.id
    projection = workspace_router._get_user_memory_store(workspace).load_entries()[0]
    assert projection.confirmed is True
    assert projection.canonical_fact_id == fact.id


async def test_memory_fact_source_api_resolves_only_current_workspace(
    tmp_path, monkeypatch,
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    workspace = SimpleNamespace(workspace_dir=first, agent_id="agent", memory_manager=None)

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    ledger = CanonicalMemoryLedger(first)
    event = ledger.record_event(
        "请保留原话：\n周报只在这个项目发送 🙂",
        source_message_id="message-1",
        session_id="session-1",
        source_role="user",
    )
    fact = ledger.capture_fact(
        "这个项目发送周报",
        memory_type="workflow",
        event=event,
        evidence_quote="周报只在这个项目发送 🙂",
    )
    result = await workspace_router.open_memory_fact_sources(fact.id, request=None)
    assert result["sources"][0]["status"] == "located"
    assert result["sources"][0]["quote"] == "周报只在这个项目发送 🙂"
    assert result["sources"][0]["content"] == event.content

    workspace.workspace_dir = second
    with pytest.raises(HTTPException) as exc_info:
        await workspace_router.open_memory_fact_sources(fact.id, request=None)
    assert exc_info.value.status_code == 404


async def test_delete_memory_fact_source_removes_l0_and_fact_projection(tmp_path, monkeypatch):
    workspace = SimpleNamespace(workspace_dir=tmp_path, agent_id="agent", memory_manager=None)

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    layered = workspace_router._get_layered_memory_store(workspace)
    from lantai.runtime.converters import LegacyMsg as Msg
    record = layered.record_messages(
        [Msg(name="user", role="user", content="这个项目默认用 pytest")], session_id="session-1",
    )[0]
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event(record.content, event_id=f"event_{record.id}",
                                source_role="user", source_message_id=record.id)
    fact = ledger.capture_fact("这个项目用 pytest", memory_type="workflow", event=event,
                               evidence_quote="默认用 pytest", support_status="supported")
    ledger.confirm(fact.id)

    result = await workspace_router.delete_memory_fact_source(fact.id, event.id, request=None)

    assert result["deleted"] is True
    assert layered.open_source(record.id) is None
    assert ledger.get_fact(fact.id).status == "forgotten"
    assert ledger.open_fact_sources(fact.id)["sources"] == []
    with pytest.raises(HTTPException) as exc_info:
        await workspace_router.delete_memory_fact_source(fact.id, "event_other", request=None)
    assert exc_info.value.status_code == 404


async def test_project_confirmation_gets_a_real_workspace_scope_id(
    tmp_path,
    monkeypatch,
):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent-project",
        memory_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("项目默认使用 pytest", session_id="s1")
    fact = ledger.capture_fact(
        "项目默认使用 pytest",
        memory_type="workflow",
        event=event,
    )

    result = await workspace_router.confirm_memory_fact(
        fact.id,
        workspace_router.MemoryFactAction(scope="project"),
        request=None,
    )

    assert result["fact"]["scope_id"] == "workspace:agent-project"
    projection = workspace_router._get_user_memory_store(workspace).load_entries()[0]
    assert (projection.scope, projection.scope_id) == (
        "project",
        "workspace:agent-project",
    )


async def test_memory_fact_reject_and_pause_preferences(tmp_path, monkeypatch):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("以后回答默认更自然", session_id="s1")
    fact = ledger.capture_fact(
        "以后回答默认更自然",
        memory_type="style",
        event=event,
    )

    rejected = await workspace_router.reject_memory_fact(
        fact.id,
        workspace_router.MemoryFactAction(reason="用户忽略"),
        request=None,
    )
    assert rejected["fact"]["status"] == "rejected"
    preferences = await workspace_router.update_memory_preferences(
        workspace_router.MemoryPreferencesUpdate(
            runtime_enabled=False,
            capture_enabled=False,
        ),
        request=None,
    )
    assert preferences == {
        "runtime_enabled": False,
        "capture_enabled": False,
    }

    ledger._write_atomic(
        ledger.root / "last-recall.json",
        {
            "query": "pytest",
            "updated_at": "2026-07-27T00:00:00+00:00",
            "items": [
                {
                    "source": "memory",
                    "citation": "memory-ledger/facts.json#fact_1",
                    "content": "默认使用 pytest",
                    "score": 0.9,
                },
            ],
        },
    )
    trace = await workspace_router.get_last_memory_recall(request=None)
    assert trace["status"] == "ready"
    assert trace["reason"] == ""
    assert trace["items"][0]["citation"].startswith("memory-ledger/")


async def test_memory_recall_trace_distinguishes_unknown_error_and_real_zero(
    tmp_path,
    monkeypatch,
):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    ledger = CanonicalMemoryLedger(tmp_path)
    trace_path = ledger.root / "last-recall.json"

    missing = await workspace_router.get_last_memory_recall(request=None)
    assert missing == {
        "query": "",
        "updated_at": "",
        "items": [],
        "status": "unavailable",
        "reason": "no_record",
    }

    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("not-json", encoding="utf-8")
    broken = await workspace_router.get_last_memory_recall(request=None)
    assert broken["status"] == "error"
    assert broken["reason"] == "read_failed"

    trace_path.write_text(
        json.dumps(
            {
                "query": "这次没有相关记忆吗？",
                "updated_at": "2026-09-05T12:00:00+00:00",
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    empty = await workspace_router.get_last_memory_recall(request=None)
    assert empty["status"] == "empty"
    assert empty["reason"] == ""
    assert empty["query"] == "这次没有相关记忆吗？"


async def test_user_understanding_api_is_sparse_auditable_and_correctable(
    tmp_path,
    monkeypatch,
):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    store = UserUnderstandingStore(tmp_path)
    confirmed = store.remember(
        UnderstandingAtom.create(
            kind="preference",
            statement="用户偏好有人情味的主动提醒。",
            status="confirmed",
            confidence=0.98,
            evidence_ids=("chat:s1:m1",),
        ),
    )
    hypothesis = store.remember(
        UnderstandingAtom.create(
            kind="decision_pattern",
            statement="用户可能更看重真实体感而非高分。",
            status="working_hypothesis",
            confidence=0.82,
            evidence_ids=("eval:persona-1",),
        ),
    )
    store.remember(
        UnderstandingAtom.create(
            kind="open_loop",
            statement="还要补真实模型烟测。",
            status="confirmed",
            confidence=1.0,
            evidence_ids=("task:smoke",),
        ),
    )
    await ReminderRepository(tmp_path).save(
        RemindersFile(
            watch_topics=[
                WatchTopic(
                    id="corrected-topic",
                    title="真实体感",
                    brief_status="confirmed",
                    brief_evidence_refs=[f"understanding:{hypothesis.id}"],
                ),
                WatchTopic(
                    id="deleted-topic",
                    title="主动提醒",
                    brief_status="confirmed",
                    brief_evidence_refs=[f"understanding:{confirmed.id}"],
                ),
            ],
        ),
    )

    snapshot = await workspace_router.list_user_understanding(request=None)

    assert snapshot["confirmed"][0]["id"] == confirmed.id
    assert snapshot["hypotheses"][0]["evidence_ids"] == ["eval:persona-1"]
    assert snapshot["open_loops"][0]["statement"] == "还要补真实模型烟测。"

    corrected = await workspace_router.feedback_user_understanding(
        hypothesis.id,
        workspace_router.UserUnderstandingFeedback(verdict="corrected"),
        request=None,
    )
    assert corrected["atom"]["status"] == "contradicted"
    reminders = await ReminderRepository(tmp_path).load()
    corrected_topic = next(
        item for item in reminders.watch_topics if item.id == "corrected-topic"
    )
    assert corrected_topic.brief_status == "paused"
    deleted = await workspace_router.delete_user_understanding(
        confirmed.id,
        request=None,
    )
    assert deleted == {"deleted": True}
    reminders = await ReminderRepository(tmp_path).load()
    deleted_topic = next(
        item for item in reminders.watch_topics if item.id == "deleted-topic"
    )
    assert deleted_topic.brief_status == "paused"


async def test_memory_source_retirement_pauses_dependent_brief_topic(tmp_path):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
    )
    atom = UserUnderstandingStore(tmp_path).remember(
        UnderstandingAtom.create(
            kind="value",
            statement="用户重视来源可追溯的判断。",
            status="confirmed",
            confidence=0.98,
            evidence_ids=("memory-1",),
        ),
    )
    await ReminderRepository(tmp_path).save(
        RemindersFile(
            watch_topics=[
                WatchTopic(
                    id="memory-topic",
                    title="可信信息源",
                    brief_status="confirmed",
                    brief_evidence_refs=[f"understanding:{atom.id}"],
                ),
            ],
        ),
    )

    retired = await workspace_router._invalidate_user_understanding_sources(
        workspace,
        ["memory-1"],
    )

    saved_atom = UserUnderstandingStore(tmp_path).get_atom(atom.id)
    reminders = await ReminderRepository(tmp_path).load()
    assert retired == [atom.id]
    assert saved_atom is not None and saved_atom.status == "superseded"
    assert reminders.watch_topics[0].brief_status == "paused"


async def test_proactive_opportunity_api_exposes_sources_and_feedback(
    tmp_path,
    monkeypatch,
):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    item = OpportunityStore(tmp_path).enqueue_candidate(
        InterventionCandidate(
            kind="future_follow_up",
            content="跟进这一轮人格化的真实测评",
            relevance=0.9,
            urgency=0.2,
            expected_utility=0.9,
            confidence=0.9,
            relationship_resonance=0.8,
            novelty=0.8,
            intrusion_cost=0.35,
        ),
        source_ids=("task:persona",),
    )
    assert item is not None

    snapshot = await workspace_router.list_proactive_opportunities(request=None)
    assert snapshot["opportunities"][0]["source_ids"] == ["task:persona"]
    dismissed = await workspace_router.feedback_proactive_opportunity(
        item.id,
        workspace_router.ProactiveOpportunityFeedback(verdict="dismissed"),
        request=None,
    )
    assert dismissed["opportunity"]["status"] == "dismissed"


async def test_memory_clear_api_previews_and_preserves_chat_history(
    tmp_path,
    monkeypatch,
):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    reloads = []
    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    monkeypatch.setattr(
        workspace_router,
        "schedule_agent_reload",
        lambda _request, agent_id: reloads.append(agent_id),
    )
    (tmp_path / "MEMORY.md").write_text("- 需要被清理的记忆", encoding="utf-8")
    UserUnderstandingStore(tmp_path).remember(
        UnderstandingAtom.create(
            kind="preference",
            statement="这条用户理解也应该被清理。",
            status="confirmed",
            confidence=1.0,
            evidence_ids=("chat:clear",),
        ),
    )
    OpportunityStore(tmp_path).enqueue_candidate(
        InterventionCandidate(
            kind="future_follow_up",
            content="这条主动机会也应该被清理",
            relevance=0.9,
            urgency=0.2,
            expected_utility=0.9,
            confidence=0.9,
            relationship_resonance=0.8,
            novelty=0.8,
            intrusion_cost=0.35,
        ),
        source_ids=("task:clear",),
    )
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "chat.json").write_text("聊天原文", encoding="utf-8")

    preview = await workspace_router.preview_memory_clear(request=None)
    assert preview["counts"]["markdown_files"] == 1
    assert preview["counts"]["user_understanding_atoms"] == 1
    assert preview["counts"]["proactive_opportunities"] == 1
    assert preview["chat_history_preserved"] is True

    result = await workspace_router.clear_memory(
        workspace_router.MemoryClearRequest(
            confirmation="删除全部记忆",
            create_backup=False,
        ),
        request=None,
    )
    assert result["cleared"] is True
    assert not (tmp_path / "MEMORY.md").exists()
    assert not (tmp_path / "user-understanding").exists()
    assert (tmp_path / "sessions" / "chat.json").read_text(encoding="utf-8") == "聊天原文"
    assert reloads == ["agent"]


async def test_memory_clear_api_requires_exact_confirmation(tmp_path, monkeypatch):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    (tmp_path / "MEMORY.md").write_text("仍应保留", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        await workspace_router.clear_memory(
            workspace_router.MemoryClearRequest(
                confirmation="删除",
                create_backup=False,
            ),
            request=None,
        )

    assert exc_info.value.status_code == 400
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "仍应保留"


async def test_memory_clear_quiesces_background_capture_before_deleting(
    tmp_path,
    monkeypatch,
):
    manager = SimpleNamespace(
        quiesce_capture_for_clear=AsyncMock(),
        resume_capture_after_failed_clear=MagicMock(),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=manager,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    monkeypatch.setattr(workspace_router, "schedule_agent_reload", lambda *_args: None)
    (tmp_path / "MEMORY.md").write_text("待清空", encoding="utf-8")

    result = await workspace_router.clear_memory(
        workspace_router.MemoryClearRequest(
            confirmation="删除全部记忆",
            create_backup=False,
        ),
        request=None,
    )

    assert result["cleared"] is True
    manager.quiesce_capture_for_clear.assert_awaited_once()
    manager.resume_capture_after_failed_clear.assert_not_called()


async def test_memory_dreaming_api_reports_and_runs_the_governed_pipeline(
    tmp_path,
    monkeypatch,
):
    config = SimpleNamespace(
        running=SimpleNamespace(
            reme_light_memory_config=ReMeLightMemoryConfig(),
        ),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
        cron_manager=None,
        config=config,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    monkeypatch.setattr(
        workspace_router,
        "load_agent_config",
        lambda _agent_id: config,
    )

    status = await workspace_router.get_memory_dreaming(request=None)
    assert status["enabled"] is False
    assert status["schedule"] == "0 3 * * *"
    assert status["latest_run"] is None

    result = await workspace_router.run_memory_dreaming(
        workspace_router.MemoryDreamingRunRequest(shadow=True),
        request=None,
    )
    assert result["status"] == "completed"
    assert result["shadow"] is True

    refreshed = await workspace_router.get_memory_dreaming(request=None)
    assert refreshed["latest_run"]["run_id"] == result["run_id"]
    assert refreshed["runs"][0]["run_id"] == result["run_id"]


async def test_memory_dreaming_preferences_hot_reload_the_opt_in_schedule(
    tmp_path,
    monkeypatch,
):
    class FakeCronManager:
        def __init__(self):
            self.rescheduled = 0

        async def reschedule_dream(self):
            self.rescheduled += 1

    config = SimpleNamespace(
        running=SimpleNamespace(
            reme_light_memory_config=ReMeLightMemoryConfig(),
        ),
    )
    cron_manager = FakeCronManager()
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
        cron_manager=cron_manager,
        config=config,
    )

    async def fake_workspace(_request):
        return workspace

    saved = []
    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    monkeypatch.setattr(
        workspace_router,
        "load_agent_config",
        lambda _agent_id: config,
    )
    monkeypatch.setattr(
        workspace_router,
        "save_agent_config",
        lambda agent_id, value: saved.append((agent_id, value)),
    )

    result = await workspace_router.update_memory_dreaming(
        workspace_router.MemoryDreamingPreferencesUpdate(
            enabled=True,
            schedule="30 2 * * *",
            rem_enabled=False,
            min_score=0.74,
        ),
        request=None,
    )

    assert result["enabled"] is True
    assert result["schedule"] == "30 2 * * *"
    assert result["rem_enabled"] is False
    assert result["min_score"] == 0.74
    assert saved == [("agent", config)]
    assert cron_manager.rescheduled == 1


async def test_memory_dreaming_api_rolls_back_only_untouched_staging(
    tmp_path,
    monkeypatch,
):
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent",
        memory_manager=None,
        cron_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(workspace_router, "get_agent_for_request", fake_workspace)
    ledger = CanonicalMemoryLedger(tmp_path)
    observe = MemoryPolicyResult(
        decision="observe",
        scope="global",
        subject="user",
        utility_score=0.9,
        reason="等待后台复核",
    )
    for index in (1, 2):
        event = ledger.record_event(
            "长期研究报告都要保留原始资料链接。",
            session_id=f"session-{index}",
            source_role="user",
        )
        fact = ledger.capture_fact(
            "用户希望长期研究报告保留原始资料链接。",
            memory_type="workflow",
            event=event,
            policy_result=observe,
            confidence=0.9,
            evidence_quote="长期研究报告都要保留原始资料链接。",
            support_status="supported",
            support_model="test-reviewer",
        )
    run = MemoryDreamingService(
        tmp_path,
        agent_id="agent",
        ledger=ledger,
    ).run(
        shadow=False,
        min_score=0.55,
    )
    assert ledger.get_fact(fact.id).status == "pending"

    result = await workspace_router.rollback_memory_dreaming(
        run["run_id"],
        request=None,
    )

    assert result["rolled_back"] is True
    assert result["restored_count"] == 1
    assert CanonicalMemoryLedger(tmp_path).get_fact(fact.id).status == "observed"
