import json
from dataclasses import replace

from lantai.agents.memory.structured_user_memory import StructuredUserMemoryStore, UserMemoryEntry
from lantai.agents.memory.layered_memory import LayeredMemoryStore, L1Atom
from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.policy import MemoryPolicyResult


def test_legacy_projection_recovers_canonical_subject_without_writing(tmp_path):
    store = StructuredUserMemoryStore(tmp_path)
    store.save_entries([UserMemoryEntry(id="p", type="identity", content="项目采用开放接口。", confirmed=True, canonical_fact_id="f")])
    ledger = tmp_path / "memory-ledger"
    ledger.mkdir()
    (ledger / "facts.json").write_text(json.dumps({"facts": [{"id": "f", "content": "项目采用开放接口。", "memory_type": "identity", "event_ids": [], "confidence": 1.0, "subject": "project", "scope": "global", "scope_id": "", "status": "confirmed"}]}))
    before = store.entries_path.read_bytes()
    entry = store.load_entries()[0]
    assert entry.subject == "project"
    assert "项目采用开放接口" not in store.read_profile_summary()["summary"]
    assert store.entries_path.read_bytes() == before


def test_personal_summary_excludes_scoped_and_superseded_facts(tmp_path):
    store = StructuredUserMemoryStore(tmp_path)
    store.save_entries([
        UserMemoryEntry(id="u", type="identity", content="用户是编辑。", confirmed=True),
        UserMemoryEntry(id="p", type="identity", content="项目背景资料。", confirmed=True, scope="project"),
        UserMemoryEntry(id="t", type="style", content="本次采用长文。", confirmed=True, scope="thread"),
        UserMemoryEntry(id="s", type="identity", content="旧职业。", confirmed=True, superseded_by_id="u"),
    ])
    summary = store.read_profile_summary()["summary"]
    assert "用户是编辑" in summary
    assert "项目背景" not in summary
    assert "本次采用" not in summary
    assert "旧职业" not in summary


def test_system2_does_not_describe_projects_as_people(tmp_path):
    store = LayeredMemoryStore(tmp_path, agent_id="test")
    summary = store._build_system2_summary(
        atoms=[L1Atom(id="a", type="identity", content="项目使用开放接口。", priority=2, confidence=1, scene_name="api", source_message_ids=[], source_record_ids=[], evidence="", created_at="", updated_at="", metadata={"subject": "project", "scope": "global"})],
        structured_entries=[UserMemoryEntry(id="p", type="identity", content="项目背景。", confirmed=True, scope="project")],
        scenes=[], digest="test", reason="test",
    )
    assert not any(model["key"] == "user_profile" for model in summary["models"])


def test_same_text_does_not_merge_different_owners_or_scopes(tmp_path):
    store = StructuredUserMemoryStore(tmp_path)
    for subject, scope, scope_id in [("user", "global", ""), ("project", "project", "a"), ("project", "project", "b")]:
        store.add_entry("默认采用简洁表达。", "style", subject=subject, scope=scope, scope_id=scope_id)
    assert len(store.load_entries()) == 3


def test_canonical_owner_wins_over_legacy_atom_defaults(tmp_path):
    store = LayeredMemoryStore(tmp_path, agent_id="test")
    atom = L1Atom(id="a", type="identity", content="HUMAN API 项目资料", priority=2, confidence=1, scene_name="api", source_message_ids=[], source_record_ids=[], evidence="", created_at="", updated_at="")
    entry = UserMemoryEntry(id="p", type="identity", content=atom.content, confirmed=True, subject="project", scope="project", canonical_atom_ids=["a"])
    summary = store._build_system2_summary([atom], [entry], [], "test", "test")
    assert not any(model["key"] == "user_profile" for model in summary["models"])


def test_l1_and_canonical_dedup_keep_separate_ownership(tmp_path):
    store = LayeredMemoryStore(tmp_path, agent_id="test")
    atom = L1Atom(id="a", type="style", content="默认采用简洁表达。", priority=2, confidence=1, scene_name="a", source_message_ids=[], source_record_ids=[], evidence="", created_at="", updated_at="", metadata={"subject": "user", "scope": "global"})
    store.upsert_atoms([atom, replace(atom, id="b", metadata={"subject": "project", "scope": "project", "scope_id": "b"})])
    assert len(store.load_atoms()) == 2
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event(atom.content, source_message_id="m", session_id="s", source_role="user")
    for subject, scope, scope_id in [("user", "global", ""), ("project", "project", "a"), ("project", "project", "b")]:
        ledger.capture_fact(atom.content, memory_type="style", event=event, scope_id=scope_id, policy_result=MemoryPolicyResult(decision="confirm", subject=subject, scope=scope, utility_score=1, reason="test", sensitivity="normal"))
    assert len(ledger.list_facts()) == 3


def test_recurrence_after_promotion_keeps_the_same_fact(tmp_path):
    ledger = CanonicalMemoryLedger(tmp_path)
    ids = []
    for session in ("s1", "s2", "s3"):
        event = ledger.record_event("回复时保留来源链接", session_id=session)
        ids.append(ledger.capture_fact("回复时保留来源链接", memory_type="instruction", event=event).id)
    assert len(set(ids)) == 1


def test_added_fact_is_not_deduplicated_as_a_substring(tmp_path):
    store = StructuredUserMemoryStore(tmp_path)
    first = store.add_entry("用户从事图书编辑。", "identity")
    second = store.add_entry("用户从事图书编辑，并参与博物馆志愿讲解。", "identity")
    assert first["entry"]["id"] != second["entry"]["id"]
    ledger = CanonicalMemoryLedger(tmp_path)
    event = ledger.record_event("用户从事图书编辑，并参与博物馆志愿讲解。", session_id="s")
    one = ledger.capture_fact("用户从事图书编辑", memory_type="identity", event=event, explicit_user_request=True)
    two = ledger.capture_fact("用户从事图书编辑，并参与博物馆志愿讲解", memory_type="identity", event=event, explicit_user_request=True)
    assert one.id != two.id


def test_persona_fallback_and_real_consolidation_use_canonical_ownership(tmp_path):
    user_store = StructuredUserMemoryStore(tmp_path)
    user_store.save_entries([UserMemoryEntry(id="project", type="identity", content="Atlas 项目资料。", confirmed=True, subject="project", scope="project", canonical_atom_ids=["a"])])
    store = LayeredMemoryStore(tmp_path, "test", user_store)
    atom = L1Atom(id="a", type="identity", content="Atlas 项目资料。", priority=2, confidence=1, scene_name="atlas", source_message_ids=[], source_record_ids=[], evidence="", created_at="", updated_at="")
    store.save_atoms([atom])
    store.refresh_scenes_and_persona()
    persona = store.persona_path.read_text()
    personal_section = persona.split("## 用户画像摘要", 1)[1].split("##", 1)[0]
    assert "Atlas" not in personal_section
    assert "Atlas" in persona
    entries = user_store.load_entries()
    entries[0].scope = "thread"
    entries[0].subject = "third_party"
    user_store.save_entries(entries)
    summary = store.system2_consolidate(force=True)
    assert not any(model["key"] == "user_profile" for model in summary["models"])


def test_personal_summary_counts_one_canonical_fact_once(tmp_path):
    store = StructuredUserMemoryStore(tmp_path)
    first = UserMemoryEntry(id="a", type="identity", content="用户担任编辑。", confirmed=True, canonical_fact_id="f")
    store.save_entries([first, replace(first, id="b")])
    assert store.read_profile_summary()["by_type"]["identity"] == ["用户担任编辑。"]
