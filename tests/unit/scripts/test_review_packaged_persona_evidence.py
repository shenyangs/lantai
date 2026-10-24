from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "review_packaged_persona_evidence.py"
)
SPEC = importlib.util.spec_from_file_location("review_packaged_persona_evidence", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_extract_json_object_accepts_fenced_or_prefixed_json() -> None:
    payload = MODULE._extract_json_object(
        '```json\n{"evaluation_id":"turn-1","relationship_passed":true}\n```'
    )

    assert payload["evaluation_id"] == "turn-1"
    assert payload["relationship_passed"] is True


def test_validate_decision_requires_bound_id_and_boolean_axes() -> None:
    decision = MODULE._validate_decision(
        {
            "evaluation_id": "turn-1",
            "relationship_passed": True,
            "governance_passed": False,
            "critical_violation": False,
            "reasons": ["语义上接住了情绪"],
            "flags": ["workification"],
        },
        evaluation_id="turn-1",
    )

    assert decision["governance_passed"] is False
    with pytest.raises(ValueError, match="evaluation_id"):
        MODULE._validate_decision(
            {
                "evaluation_id": "wrong",
                "relationship_passed": True,
                "governance_passed": True,
                "critical_violation": False,
                "reasons": ["通过"],
            },
            evaluation_id="turn-1",
        )


def test_review_prompt_treats_phrases_as_examples_and_protects_non_task_chat() -> None:
    prompt = MODULE._review_prompt(
        evaluation_id="turn-1",
        scenario_id="personal-loss-continuity",
        title="丧失中的克制陪伴",
        memory_preload=("陪伴十五年的猫",),
        user="你就陪我待会儿。",
        answer="好，我在。",
        relationship_examples=(("我在",),),
        governance_examples=(("不追问",),),
        forbidden_examples=("给你一份计划",),
    )

    assert "不要求逐字命中示例" in prompt
    assert "强行转成工作" in prompt
    assert '"non_task_conversation":true' in prompt


def test_format_retry_only_requests_schema_correction() -> None:
    prompt = MODULE._format_retry_prompt(
        evaluation_id="review-1",
        invalid_output="我的判断是通过",
    )

    assert "不要重新分析、不要改变你的判断" in prompt
    assert "evaluation_id=review-1" in prompt
    assert "我的判断是通过" in prompt
