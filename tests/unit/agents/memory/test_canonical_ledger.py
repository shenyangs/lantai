# -*- coding: utf-8 -*-

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.reme_light_memory_manager import ReMeLightMemoryManager
from lantai.agents.memory.structured_user_memory import StructuredUserMemoryStore
from lantai.agents.memory.layered_memory import AtomCandidate, LayeredMemoryStore
from lantai.agents.memory.policy import MemoryPolicyResult
from lantai.runtime.converters import LegacyMsg as Msg
from dataclasses import replace
from types import SimpleNamespace
import json


def test_clear_auto_preference_is_pending_and_explicit_memory_is_confirmed(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("用户说默认用中文")

    pending = ledger.capture_fact(
        "用户偏好中文", memory_type="style", event=event,
    )
    confirmed = ledger.capture_fact(
        "默认用中文回答", memory_type="instruction", event=event,
        explicit_user_request=True,
    )

    assert pending.status == "pending"
    assert confirmed.status == "confirmed"
    assert ledger.rebuild_projections() == [{
        "fact_id": confirmed.id,
        "type": "instruction",
        "content": "默认用中文回答",
        "scope": "global",
        "scope_id": "",
    }]


def test_forget_cascades_projection_and_index_and_cannot_revive(tmp_path):
    deleted = []
    ledger = CanonicalMemoryLedger(tmp_path, index_delete=deleted.append)
    event = ledger.record_event("记住默认用 pytest")
    fact = ledger.capture_fact(
        "默认使用 pytest", memory_type="workflow", event=event,
        explicit_user_request=True,
    )

    assert ledger.forget(fact.id) is True
    assert ledger.rebuild_projections() == []
    assert deleted == [f"memory:{fact.id}"]
    assert ledger.forget(fact.id) is False


def test_forgotten_fact_cannot_be_recaptured_after_restart(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("记住默认用 pytest")
    fact = ledger.capture_fact(
        "默认使用 pytest", memory_type="workflow", event=event,
        explicit_user_request=True,
    )
    assert ledger.forget(fact.id)
    restarted = CanonicalMemoryLedger(tmp_path)
    later = restarted.record_event("我以前说过默认使用 pytest", session_id="later")
    assert restarted.capture_fact(
        "默认使用 pytest", memory_type="workflow", event=later,
        explicit_user_request=True,
    ) is None
    assert restarted.confirmed_facts() == []


def test_manual_readd_after_forget_accepts_new_evidence(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    first = ledger.record_event("以前默认用 pytest", source_role="user")
    forgotten = ledger.capture_fact("默认用 pytest", memory_type="workflow", event=first)
    assert ledger.forget(forgotten.id)

    manual = ledger.record_event("默认用 pytest", source_role="manual")
    restored = ledger.capture_fact(
        "默认用 pytest", memory_type="workflow", event=manual,
        explicit_user_request=True,
    )
    assert restored is not None and restored.id != forgotten.id
    recent = ledger.record_event("这次也默认用 pytest", source_role="user")
    merged = ledger.capture_fact(
        "默认用 pytest", memory_type="workflow", event=recent,
        evidence_quote="默认用 pytest",
        support_status="supported",
    )
    assert merged is not None and merged.id == restored.id
    assert recent.id in merged.event_ids


def test_original_message_timestamp_is_distinct_from_capture_time(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.layered_memory = LayeredMemoryStore(tmp_path, "agent")
    original_time = "2026-08-01 09:30:00.000"
    record = manager.layered_memory.record_messages(
        [Msg(name="user", role="user", content="记住用中文", timestamp=original_time)],
        session_id="historical",
    )[0]
    event = manager._record_ledger_events([record])[record.id]
    fact = manager.memory_ledger.capture_fact(
        "用中文", memory_type="instruction", event=event,
        evidence_quote="用中文",
    )
    source = manager.memory_ledger.open_fact_sources(fact.id)["sources"][0]
    assert source["source_created_at"] == original_time
    assert source["created_at"] != original_time


def test_runtime_and_sensitive_data_are_not_promoted_to_memory(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("event")

    assert ledger.capture_fact("下一步 todo 是发布", memory_type="episodic", event=event) is None
    assert ledger.capture_fact("api_key=secret", memory_type="instruction", event=event) is None


def test_events_are_idempotent_and_exclude_runtime_and_sensitive_content(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)

    first = ledger.record_event("用户偏好简洁回答", event_id="event_l0_1")
    second = ledger.record_event("用户偏好简洁回答", event_id="event_l0_1")

    assert first is not None
    assert second == first
    assert ledger.record_event("下一步 todo 是发布") is None
    assert ledger.record_event("api_key=secret") is None
    assert len(ledger._load_events()) == 1


def test_source_quote_is_exact_and_resolves_original_unicode_offsets(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    original = "我喜欢简短回答。\n🙂 但这个项目的报告必须完整。"
    event = ledger.record_event(
        original,
        source_message_id="message-1",
        session_id="session-1",
        source_role="user",
    )
    fact = ledger.capture_fact(
        "这个项目的报告必须完整",
        memory_type="workflow",
        event=event,
        evidence_quote="这个项目的报告必须完整",
    )

    source = ledger.open_fact_sources(fact.id)
    assert source["sources"][0]["quote"] == "这个项目的报告必须完整"
    assert source["sources"][0]["content"] == original
    start = source["sources"][0]["start_codepoint"]
    end = source["sources"][0]["end_codepoint"]
    assert original[start:end] == "这个项目的报告必须完整"


def test_two_source_atom_keeps_each_exact_original_quote(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(tmp_path, "agent", manager.user_memory_store)
    records = manager.layered_memory.record_messages([
        Msg(name="user", role="user", content="我负责北辰项目的测试。"),
        Msg(name="user", role="user", content="北辰项目的自动化也由我维护。"),
    ], session_id="two-sources")
    events = manager._record_ledger_events(records)
    quotes = {
        records[0].id: "负责北辰项目的测试",
        records[1].id: "北辰项目的自动化也由我维护",
    }
    atom = manager.layered_memory.ingest_atom_candidates([
        AtomCandidate(
            type="workflow", content="用户负责北辰项目测试及自动化维护",
            confidence=0.9, source_kind="direct_user", temporal_scope="durable",
            subject="user", salience="core", support_status="supported",
            support_model="utility/reviewer", source_record_ids=list(quotes),
            evidence_quotes=quotes,
        ),
    ], records=records)[0]
    assert manager._record_pending_ledger_facts([atom], events) == 0
    fact = manager.memory_ledger.list_facts()[0]
    assert fact.status == "confirmed"
    sources = manager.memory_ledger.open_fact_sources(fact.id)["sources"]
    assert len(sources) == 2
    assert {source["quote"] for source in sources} == set(quotes.values())
    assert all(source["status"] == "located" for source in sources)


def test_derived_source_cannot_claim_user_original_review_grade(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("模型改写的摘要", source_role="derived")
    fact = ledger.capture_fact(
        "模型改写的摘要", memory_type="style", event=event,
        evidence_quote="模型改写的摘要", support_status="supported",
    )
    assert fact.support_status == "derived_unverified"
    assert ledger.open_fact_sources(fact.id)["sources"][0]["support_status"] == "derived_unverified"


def test_persisted_derived_role_blocks_forged_automatic_confirmation(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    derived = ledger.record_event("模型改写的摘要", source_role="derived")
    forged = replace(derived, source_role="user")

    fact = ledger.capture_fact(
        "用户偏好简洁回答", memory_type="style", event=forged,
        evidence_quote="模型改写的摘要", support_status="supported",
        policy_result=MemoryPolicyResult(
            decision="confirm", scope="global", subject="user",
            utility_score=0.9, reason="自动识别的核心事实",
        ),
    )

    assert fact is not None
    assert fact.status != "confirmed"
    assert fact.support_status == "derived_unverified"
    assert ledger.confirmed_facts() == []


def test_delete_source_removes_original_and_prevents_async_recapture(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("以后默认用中文", event_id="event_l0_deleted", source_role="user")
    fact = ledger.capture_fact("默认用中文", memory_type="style", event=event,
                               evidence_quote="默认用中文", support_status="supported")
    assert ledger.confirm(fact.id)
    before = ledger.correction_fingerprint()

    result = ledger.delete_source_event(event.id)

    assert result["deleted"] is True
    assert ledger.list_events() == []
    assert ledger.get_fact(fact.id).status == "forgotten"
    assert ledger.confirmed_facts() == []
    assert ledger.open_fact_sources(fact.id)["sources"] == []
    assert ledger.correction_fingerprint() != before
    assert CanonicalMemoryLedger(tmp_path).record_event(
        event.content, event_id=event.id, source_role="user",
    ) is None
    assert ledger.capture_fact("默认用中文", memory_type="style", event=event,
                               support_status="supported") is None


def test_delete_one_source_retains_independently_supported_fact(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    first = ledger.record_event("本项目用 pytest", event_id="event_l0_first", source_role="user")
    second = ledger.record_event("本项目仍用 pytest", event_id="event_l0_second", source_role="user")
    fact = ledger.capture_fact("本项目用 pytest", memory_type="workflow", event=first,
                               evidence_quote="本项目用 pytest", support_status="supported")
    ledger.confirm(fact.id)
    ledger.capture_fact("本项目用 pytest", memory_type="workflow", event=second,
                        evidence_quote="本项目仍用 pytest", support_status="supported")

    ledger.delete_source_event(first.id)

    surviving = ledger.get_fact(fact.id)
    assert surviving.status == "confirmed"
    assert surviving.event_ids == [second.id]
    assert [source["event_id"] for source in ledger.open_fact_sources(fact.id)["sources"]] == [second.id]


def test_delete_joint_source_demotes_remaining_joint_evidence(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    first = ledger.record_event("我负责北辰项目测试", event_id="event_l0_joint_first", source_role="user")
    second = ledger.record_event("我也维护自动化", event_id="event_l0_joint_second", source_role="user")
    fact = ledger.capture_fact(
        "用户负责北辰项目测试及自动化", memory_type="workflow", event=first,
        evidence_quote="负责北辰项目测试", support_status="supported",
        support_group="joint-review-1",
    )
    ledger.capture_fact(
        fact.content, memory_type="workflow", event=second,
        evidence_quote="维护自动化", support_status="supported",
        support_group="joint-review-1",
    )
    ledger.confirm(fact.id)

    ledger.delete_source_event(first.id)

    surviving = ledger.get_fact(fact.id)
    assert surviving.status == "pending"
    assert surviving.support_status == "unreviewed"
    assert surviving.event_ids == [second.id]
    assert surviving.evidence_refs[0]["support_status"] == "unreviewed"
    assert ledger.confirmed_facts() == []


def test_joint_review_is_not_injectable_before_every_source_is_bound(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(tmp_path, "agent", manager.user_memory_store)
    records = manager.layered_memory.record_messages([
        Msg(name="user", role="user", content="我负责北辰项目测试。"),
        Msg(name="user", role="user", content="北辰项目自动化也由我维护。"),
    ], session_id="joint")
    events = manager._record_ledger_events(records)
    atom = manager.layered_memory.ingest_atom_candidates([AtomCandidate(
        type="workflow", content="用户负责北辰项目测试及自动化维护",
        confidence=0.93, source_kind="direct_user", temporal_scope="durable",
        subject="user", salience="core", support_status="supported",
        source_record_ids=[record.id for record in records],
        evidence_quotes={
            records[0].id: "负责北辰项目测试",
            records[1].id: "自动化也由我维护",
        },
    )], records=records)[0]
    intermediate_statuses = []
    capture = manager.memory_ledger.capture_fact

    def recording_capture(*args, **kwargs):
        result = capture(*args, **kwargs)
        intermediate_statuses.append(result.status if result else None)
        return result

    manager.memory_ledger.capture_fact = recording_capture
    manager._record_pending_ledger_facts([atom], events)

    assert intermediate_statuses == ["pending", "pending"]
    fact = manager.memory_ledger.confirmed_facts()[0]
    assert len(fact.evidence_refs) == 2
    assert {ref["support_group"] for ref in fact.evidence_refs} == {
        fact.evidence_refs[0]["support_group"]
    }


def test_joint_review_across_turns_requires_the_earlier_source(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(tmp_path, "agent", manager.user_memory_store)
    first = manager.layered_memory.record_messages(
        [Msg(name="user", role="user", content="我负责北辰项目测试。")],
        session_id="first",
    )[0]
    manager._record_ledger_events([first])
    second = manager.layered_memory.record_messages(
        [Msg(name="user", role="user", content="自动化也由我维护。")],
        session_id="second",
    )[0]
    current_events = manager._record_ledger_events([second])
    atom = manager.layered_memory.ingest_atom_candidates([AtomCandidate(
        type="workflow", content="用户负责北辰项目测试及自动化维护",
        confidence=0.93, source_kind="direct_user", temporal_scope="durable",
        subject="user", salience="core", support_status="supported",
        source_record_ids=[first.id, second.id],
        evidence_quotes={
            first.id: "负责北辰项目测试",
            second.id: "自动化也由我维护",
        },
    )], records=[first, second])[0]

    manager._record_pending_ledger_facts([atom], current_events)

    fact = manager.memory_ledger.confirmed_facts()[0]
    assert set(fact.event_ids) == {f"event_{first.id}", f"event_{second.id}"}
    assert len({ref["support_group"] for ref in fact.evidence_refs}) == 1
    manager.memory_ledger.delete_source_event(f"event_{first.id}")
    assert manager.memory_ledger.get_fact(fact.id).status == "pending"


def test_independent_reviews_across_turns_survive_one_source_deletion(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(tmp_path, "agent", manager.user_memory_store)
    for session_id, content, quote in (
        ("first", "我负责北辰项目测试。", "负责北辰项目测试"),
        ("second", "北辰项目测试仍由我负责。", "北辰项目测试仍由我负责"),
    ):
        record = manager.layered_memory.record_messages(
            [Msg(name="user", role="user", content=content)],
            session_id=session_id,
        )[0]
        events = manager._record_ledger_events([record])
        atom = manager.layered_memory.ingest_atom_candidates([AtomCandidate(
            type="workflow", content="用户负责北辰项目测试",
            confidence=0.93, source_kind="direct_user", temporal_scope="durable",
            subject="user", salience="core", support_status="supported",
            source_record_ids=[record.id], evidence_quotes={record.id: quote},
        )], records=[record])[0]
        manager._record_pending_ledger_facts([atom], events)
        if session_id == "first":
            first_event_id = events[record.id].id

    fact = manager.memory_ledger.confirmed_facts()[0]
    assert len(fact.event_ids) == 2
    assert all(not ref.get("support_group") for ref in fact.evidence_refs)
    manager.memory_ledger.delete_source_event(first_event_id)
    assert manager.memory_ledger.get_fact(fact.id).status == "confirmed"


def test_candidate_never_binds_to_unrelated_last_message(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(tmp_path, "agent", manager.user_memory_store)
    records = manager.layered_memory.record_messages(
        [
            Msg(name="user", role="user", content="本项目默认用 pytest。"),
            Msg(name="user", role="user", content="明天中午见。"),
        ],
        session_id="source-binding",
    )
    events = manager._record_ledger_events(records)
    atom = manager.layered_memory.ingest_atom_candidates(
        [AtomCandidate(type="workflow", content="本项目默认用 pytest", evidence="本项目默认用 pytest")],
        records=records,
    )[0]

    manager._record_pending_ledger_facts([atom], events)

    facts = manager.memory_ledger.list_facts()
    assert len(facts) == 1
    assert facts[0].event_ids == [events[records[0].id].id]


def test_candidate_with_missing_source_does_not_keep_partial_evidence(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    event = manager.memory_ledger.record_event(
        "本项目默认用 pytest。", source_role="user",
    )
    atom = SimpleNamespace(
        type="workflow", content="本项目默认用 pytest", confidence=0.9,
        source_record_ids=["record_present", "record_deleted"],
        metadata={"support_status": "supported", "source_kind": "direct_user"},
    )

    assert manager._record_pending_ledger_facts(
        [atom], {"record_present": event},
    ) == 0
    assert manager.memory_ledger.list_facts() == []


def test_new_unreviewed_source_does_not_inherit_old_supported_grade(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(
        tmp_path, "agent", manager.user_memory_store,
    )
    first_records = manager.layered_memory.record_messages(
        [Msg(name="user", role="user", content="我负责北辰项目测试。")],
        session_id="first",
    )
    first_events = manager._record_ledger_events(first_records)
    first_atoms = manager.layered_memory.ingest_atom_candidates([
        AtomCandidate(
            type="workflow", content="用户负责北辰项目测试", confidence=0.91,
            source_kind="direct_user", temporal_scope="durable",
            subject="user", salience="core", support_status="supported",
            evidence_quote="负责北辰项目测试",
        ),
    ], records=first_records)
    manager._record_pending_ledger_facts(first_atoms, first_events)
    fact = manager.memory_ledger.confirmed_facts()[0]

    second_records = manager.layered_memory.record_messages(
        [Msg(name="user", role="user", content="北辰项目测试仍由我负责。")],
        session_id="second",
    )
    second_events = manager._record_ledger_events(second_records)
    second_atoms = manager.layered_memory.ingest_atom_candidates([
        AtomCandidate(
            type="workflow", content="用户负责北辰项目测试", confidence=0.91,
            source_kind="direct_user", temporal_scope="durable",
            subject="user", salience="core", support_status="unreviewed",
            evidence_quote="北辰项目测试仍由我负责",
        ),
    ], records=second_records)
    manager._record_pending_ledger_facts(second_atoms, second_events)

    assert manager.memory_ledger.get_fact(fact.id).event_ids == [
        first_events[first_records[0].id].id,
    ]


def test_unified_memory_index_excludes_unconfirmed_atoms_and_raw_conversation(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.working_dir = str(tmp_path)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(tmp_path, "agent", manager.user_memory_store)
    event = manager.memory_ledger.record_event("用户默认用 pytest", event_id="event_memory_fts")
    manager.memory_ledger.capture_fact(
        "默认用 pytest 跑单测",
        memory_type="workflow",
        event=event,
        explicit_user_request=True,
    )
    manager.layered_memory.ingest_atom_candidates(
        [
            AtomCandidate(
                type="workflow", content="项目测试使用 pytest", priority=2,
                confidence=0.8, evidence="test",
            ),
        ],
    )
    manager.layered_memory.record_messages(
        [Msg(name="user", role="user", content="pytest 的结果要保留引用")],
        session_id="memory-index",
    )

    candidates = manager._unified_memory_candidates("pytest 引用", max_results=8)

    assert candidates
    assert all(candidate.citation for candidate in candidates)
    assert any("memory-ledger/facts.json#" in candidate.citation for candidate in candidates)
    assert not any("layered-memory/" in candidate.citation for candidate in candidates)

    context = manager._format_unified_memory_recall_context(
        query="pytest 引用",
        max_results=8,
        max_chars=4000,
    )
    trace = json.loads(
        (tmp_path / "memory-ledger" / "last-recall.json").read_text(
            encoding="utf-8",
        ),
    )
    assert "<unified-memory-context>" in context
    assert "never creates a task, artifact, or permission" in context
    assert trace["items"]
    assert all(item["citation"] for item in trace["items"])


async def test_explicit_tool_memory_confirms_but_auto_preference_stays_pending(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)

    response = await manager.memory_store("以后默认用中文回答", memory_type="instruction")
    payload = json.loads(response.content[0]["text"])
    assert payload["status"] == "confirmed"
    source = manager.memory_ledger.open_fact_sources(payload["ledger_fact_id"])
    assert source["sources"][0]["source_role"] == "derived"
    assert source["sources"][0]["status"] == "source_only"
    assert source["sources"][0]["support_status"] == "derived_unverified"
    assert source["fact"]["support_status"] == "derived_unverified"

    event = manager.memory_ledger.record_event("用户偏好简洁", event_id="event_l0_2")
    pending = manager._record_pending_ledger_facts(
        [SimpleNamespace(type="style", content="用户偏好简洁", confidence=0.7)],
        {"l0_2": event},
    )

    assert pending == 1
    statuses = {fact.status for fact in manager.memory_ledger._load_facts()}
    assert statuses == {"confirmed", "pending"}


def test_fabricated_or_stale_event_cannot_bind_a_fact(tmp_path):
    from dataclasses import replace

    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("原句", source_role="user")
    assert ledger.capture_fact(
        "伪造的事实", memory_type="style", event=replace(event, content="伪造来源"),
        explicit_user_request=True,
    ) is None
    assert ledger.list_facts() == []


def test_pending_fact_never_enters_projection_until_confirmed(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("以后回答默认更简洁", session_id="s1")
    fact = ledger.capture_fact("以后回答默认更简洁", memory_type="style", event=event)

    assert fact.status == "pending"
    assert ledger.list_pending()[0].id == fact.id
    assert ledger.confirmed_facts() == []
    assert ledger.rebuild_projections() == []

    confirmed = ledger.confirm(fact.id, scope="global")
    assert confirmed.status == "confirmed"
    assert ledger.rebuild_projections()[0]["fact_id"] == fact.id


def test_recurrence_across_sessions_promotes_observation_to_suggestion(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    first_event = ledger.record_event("回复时保留来源链接", session_id="s1")
    second_event = ledger.record_event("回复时保留来源链接", session_id="s2")

    first = ledger.capture_fact(
        "回复时保留来源链接", memory_type="instruction", event=first_event,
    )
    second = ledger.capture_fact(
        "回复时保留来源链接", memory_type="instruction", event=second_event,
    )

    assert first.id == second.id
    assert second.status == "pending"
    assert second.occurrence_count == 2
    assert second.session_ids == ["s1", "s2"]


def test_layered_recurrence_promotes_only_after_two_distinct_sessions(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(
        tmp_path,
        "agent",
        manager.user_memory_store,
    )

    for session_id in ("s1", "s2"):
        messages = [
            Msg(
                name="user",
                role="user",
                content="我希望回复时保留来源链接",
            ),
        ]
        records = manager.layered_memory.record_messages(
            messages,
            session_id=session_id,
        )
        events = manager._record_ledger_events(records)
        atoms = manager.layered_memory.extract_atoms(messages, records)
        manager._record_pending_ledger_facts(atoms, events)

    suggestions = manager.memory_ledger.list_pending()
    assert len(suggestions) == 1
    assert suggestions[0].session_ids == ["s1", "s2"]
    atom = manager.layered_memory.load_atoms()[0]
    assert atom.status == "pending"
    assert manager.layered_memory.search_atoms("来源链接") == []


def test_llm_grounded_core_self_disclosure_becomes_confirmed_memory(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(
        tmp_path,
        "agent",
        manager.user_memory_store,
    )
    records = manager.layered_memory.record_messages(
        [
            Msg(
                name="user",
                role="user",
                content="我刚接手 C 端公关一号位，现在管理七八个人。",
            ),
        ],
        session_id="disclosure-1",
    )
    events = manager._record_ledger_events(records)
    atoms = manager.layered_memory.ingest_atom_candidates(
        [
            AtomCandidate(
                type="identity",
                content="用户负责 C 端公关并担任一号位，管理约七八人团队",
                priority=3,
                confidence=0.94,
                evidence="用户直接自述",
                source_kind="direct_user",
                temporal_scope="durable",
                subject="user",
                    salience="core",
                    evidence_quote="我刚接手 C 端公关一号位",
                    support_status="supported",
            ),
        ],
        records=records,
    )

    pending = manager._record_pending_ledger_facts(atoms, events)

    assert pending == 0
    facts = manager.memory_ledger.confirmed_facts()
    assert [fact.content for fact in facts] == [
        "用户负责 C 端公关并担任一号位，管理约七八人团队",
    ]
    projection = manager.user_memory_store.load_entries()[0]
    assert projection.status == "active"
    assert projection.confirmed is True


def test_high_confidence_project_fact_uses_user_material_as_evidence(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(
        tmp_path,
        "agent",
        manager.user_memory_store,
    )
    records = manager.layered_memory.record_messages(
        [
            Msg(name="user", role="user", content="请看一下我提供的项目说明。"),
            Msg(
                name="read_document",
                role="assistant",
                content=[
                    {
                        "type": "tool_result",
                        "name": "read_document",
                        "output": "项目只维护官网发行版，App Store 版已归档。",
                    },
                ],
                metadata={"memory_source": "tool_result"},
            ),
        ],
        session_id="material-1",
    )
    events = manager._record_ledger_events(records)
    atoms = manager.layered_memory.ingest_atom_candidates(
        [
            AtomCandidate(
                type="workflow",
                content="项目只维护官网发行版，App Store 版已归档",
                priority=3,
                confidence=0.96,
                evidence="用户提供的项目说明",
                source_kind="user_provided_material",
                temporal_scope="durable",
                subject="project",
                    salience="core",
                    evidence_quote="项目只维护官网发行版",
                    support_status="supported",
            ),
        ],
        records=records,
    )

    assert records[1].metadata["memory_source"] == "tool_result"
    assert {event.source_role for event in events.values()} == {
        "user",
        "tool_result",
    }
    assert manager._record_pending_ledger_facts(atoms, events) == 0
    fact = manager.memory_ledger.confirmed_facts()[0]
    material_event = next(
        event for event in events.values() if event.source_role == "tool_result"
    )
    assert fact.event_ids == [material_event.id]
    assert fact.scope == "project"
    assert fact.subject == "project"


def test_reject_and_supersede_rebuild_projections(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("记住默认使用中文", session_id="s1")
    first = ledger.capture_fact(
        "默认使用中文", memory_type="instruction", event=event,
        explicit_user_request=True,
    )
    second = ledger.capture_fact(
        "默认使用简体中文", memory_type="instruction", event=event,
        explicit_user_request=True,
    )

    assert ledger.supersede(first.id, second.id)
    assert [item["fact_id"] for item in ledger.rebuild_projections()] == [second.id]
    assert ledger.reject(second.id, reason="不准确").status == "rejected"
    assert ledger.rebuild_projections() == []


async def test_forget_archives_legacy_projection_and_canonical_fact(tmp_path):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.memory_ledger = CanonicalMemoryLedger(tmp_path)
    manager.user_memory_store = StructuredUserMemoryStore(tmp_path)
    manager.layered_memory = LayeredMemoryStore(tmp_path, "agent", manager.user_memory_store)

    stored = await manager.memory_store("默认用 pytest 跑单测", memory_type="workflow")
    entry_id = json.loads(stored.content[0]["text"])["entry"]["id"]
    manager.layered_memory.ingest_atom_candidates(
        [
            AtomCandidate(
                type="workflow",
                content="默认用 pytest 跑单测",
                priority=2,
                confidence=0.9,
                evidence="explicit memory",
            ),
        ],
    )
    manager.layered_memory.record_messages(
        [Msg(name="user", role="user", content="默认用 pytest 跑单测")],
        session_id="forgotten-memory",
    )
    forgotten = await manager.memory_forget(entry_id)
    payload = json.loads(forgotten.content[0]["text"])

    assert payload["archived"] is True
    assert payload["ledger_facts_forgotten"] == 1
    assert payload["layered_projections_forgotten"]["atoms"] == 1
    assert manager.memory_ledger.rebuild_projections() == []
    assert manager.layered_memory.search_atoms("pytest") == []
    assert manager.layered_memory.search_conversations("pytest") == []
    assert manager.layered_memory.hybrid_recall("pytest") == []
