# -*- coding: utf-8 -*-
"""Versioned golden behaviour for canonical Memory lifecycle invariants."""

from __future__ import annotations

import json
from pathlib import Path

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.layered_memory import AtomCandidate, LayeredMemoryStore


FIXTURE_PATH = Path(__file__).resolve().parents[3] / "fixtures" / "memory" / "memory_ledger_v1.json"


def test_memory_ledger_golden_confirm_pending_conflict_and_forget(tmp_path):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["suite_id"] == "memory_ledger_v1"
    deleted: list[str] = []
    ledger = CanonicalMemoryLedger(tmp_path, index_delete=deleted.append)
    event = ledger.record_event("用户说偏好")
    assert event is not None
    confirmed = ledger.capture_fact(fixture["confirmed"], memory_type="workflow", event=event, explicit_user_request=True)
    pending = ledger.capture_fact(fixture["pending"], memory_type="style", event=event)
    assert confirmed is not None and confirmed.status == "confirmed"
    assert pending is not None and pending.status == "pending"
    assert ledger.capture_fact(fixture["sensitive"], memory_type="instruction", event=event) is None
    store = LayeredMemoryStore(tmp_path, "agent")
    first, second = [
        store.ingest_atom_candidates([AtomCandidate(type="instruction", content=value, priority=2, confidence=.8)])[0]
        for value in fixture["conflict"]
    ]
    assert second.status == "conflict" and second.metadata["related_ids"] == [first.id]
    assert ledger.forget(confirmed.id) is True
    assert ledger.rebuild_projections() == []
    assert deleted == [f"memory:{confirmed.id}"]
    assert ledger.forget(confirmed.id) is False


def test_memory_ledger_is_provider_neutral_and_forget_survives_reopen(tmp_path):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    case = fixture["cross_provider"]
    before = CanonicalMemoryLedger(tmp_path)
    event = before.record_event(
        case["confirmed"],
        session_id=case["provider_before"],
        source_role="user",
    )
    assert event is not None
    confirmed = before.capture_fact(
        case["confirmed"],
        memory_type="workflow",
        event=event,
        explicit_user_request=True,
    )
    pending = before.capture_fact(
        case["pending"],
        memory_type="style",
        event=event,
    )
    assert confirmed is not None and pending is not None

    after_provider_switch = CanonicalMemoryLedger(tmp_path)
    assert case["provider_after"] != case["provider_before"]
    assert [item.content for item in after_provider_switch.confirmed_facts()] == [
        case["confirmed"]
    ]
    assert [item["content"] for item in after_provider_switch.rebuild_projections()] == [
        case["confirmed"]
    ]
    assert case["pending"] not in json.dumps(
        after_provider_switch.rebuild_projections(),
        ensure_ascii=False,
    )

    assert after_provider_switch.forget(confirmed.id) is True
    after_forget_reopen = CanonicalMemoryLedger(tmp_path)
    assert after_forget_reopen.confirmed_facts() == []
    assert after_forget_reopen.rebuild_projections() == []
