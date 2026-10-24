# -*- coding: utf-8 -*-
"""Behavior locks for cross-channel memory usage tracing."""

from __future__ import annotations

import json

from lantai.agents.memory_usage_trace import (
    merge_last_recall_usage,
    record_delivered_persona_memory,
)
from lantai.agents.persona.perceptible_presence import (
    PersonaDeliveryReport,
    PersonaMemoryReference,
    PresenceState,
    plan_persona_turn,
)
from lantai.agents.persona.user_understanding import (
    TurnUnderstandingProjection,
    UnderstandingAtom,
)


def _write_trace(tmp_path, *, query: str, items: list[dict]) -> None:
    path = tmp_path / "memory-ledger" / "last-recall.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "query": query,
                "updated_at": "2026-08-13T00:00:00+00:00",
                "items": items,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _read_trace(tmp_path) -> dict:
    return json.loads(
        (tmp_path / "memory-ledger" / "last-recall.json").read_text(
            encoding="utf-8",
        ),
    )


def test_merge_last_recall_usage_keeps_canonical_items_and_deduplicates(tmp_path) -> None:
    canonical = {
        "source": "native-memory",
        "citation": "memory-ledger/facts.json#fact_1",
        "content": "用户长期从事品牌公关。",
        "score": 0.72,
    }
    _write_trace(tmp_path, query="DeepSeek 最近资讯", items=[canonical])
    reference = PersonaMemoryReference(
        source="user-understanding",
        citation="user-understanding/projection.json#ua_wps",
        content="用户负责 WPS C 端公关。",
        score=0.96,
    )

    assert merge_last_recall_usage(
        tmp_path,
        query="DeepSeek 最近资讯",
        references=(reference, reference),
    )

    assert _read_trace(tmp_path)["items"] == [canonical, reference.to_dict()]


def test_merge_last_recall_usage_rejects_a_late_response_for_a_newer_turn(tmp_path) -> None:
    _write_trace(tmp_path, query="更新的问题", items=[])
    reference = PersonaMemoryReference(
        source="user-understanding",
        citation="user-understanding/projection.json#ua_old",
        content="旧回答里的记忆。",
        score=0.9,
    )

    assert not merge_last_recall_usage(
        tmp_path,
        query="较早的问题",
        references=(reference,),
    )
    assert _read_trace(tmp_path)["items"] == []


def test_persona_memory_is_recorded_only_after_visible_grounding(tmp_path) -> None:
    atom = UnderstandingAtom.create(
        kind="fact",
        statement="用户负责 WPS C 端公关。",
        status="confirmed",
        confidence=0.96,
        evidence_ids=("message:wps",),
        future_relevance="分析 AI 产品与品牌公关时适用。",
    )
    query = "帮我从 WPS 公关视角看看 DeepSeek 最近的资讯"
    plan = plan_persona_turn(
        query,
        projection=TurnUnderstandingProjection(confirmed=(atom,)),
        state=PresenceState(),
        persona_mode="full",
    )
    _write_trace(tmp_path, query=query, items=[])

    assert not record_delivered_persona_memory(
        tmp_path,
        query=query,
        plan=plan,
        report=PersonaDeliveryReport(
            beat="relationship_echo",
            required=True,
            delivered=False,
            signal="missing_relationship_echo",
        ),
    )
    assert _read_trace(tmp_path)["items"] == []

    assert record_delivered_persona_memory(
        tmp_path,
        query=query,
        plan=plan,
        report=PersonaDeliveryReport(
            beat="relationship_echo",
            required=True,
            delivered=True,
            signal="grounded_memory_echo",
        ),
    )
    assert _read_trace(tmp_path)["items"][0]["citation"].endswith(atom.id)
