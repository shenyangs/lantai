# -*- coding: utf-8 -*-
"""Contracts for the real-provider positive-temperament source smoke."""

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "run_source_persona_positive_smoke.py"
)
SPEC = importlib.util.spec_from_file_location(
    "run_source_persona_positive_smoke",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

REVIEW_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "review_source_persona_positive_evidence.py"
)
REVIEW_SPEC = importlib.util.spec_from_file_location(
    "review_source_persona_positive_evidence",
    REVIEW_SCRIPT,
)
assert REVIEW_SPEC is not None and REVIEW_SPEC.loader is not None
REVIEW_MODULE = importlib.util.module_from_spec(REVIEW_SPEC)
REVIEW_SPEC.loader.exec_module(REVIEW_MODULE)


def test_positive_smoke_covers_the_five_v2_behavior_slices() -> None:
    assert MODULE.SCENARIO_IDS == (
        "repeated-question-patience",
        "changing-requirements-kindness",
        "user-correction-listening",
        "repair-after-mistake",
        "honest-difficulty-with-path",
    )


def test_positive_smoke_declares_versioned_evidence() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "com.lantai.source-persona-positive-smoke.v1" in text
    assert '"persona_template_version": "2.1.0"' in text
    assert '"constitution_version": "1.6.1"' in text


def test_review_replays_current_suite_without_changing_recorded_answer() -> (
    None
):
    answer = "你说得对，我刚才算错了。重新核算后，正确结果是 391。"
    result = REVIEW_MODULE.review(
        {
            "schema": "com.lantai.source-persona-positive-smoke.v1",
            "results": [
                {
                    "scenario_id": "repair-after-mistake",
                    "passed": False,
                    "answer": answer,
                    "missing_required_groups": [["旧关键词"]],
                    "forbidden_matches": [],
                },
            ],
        },
    )

    assert result["ok"] is True
    assert result["passed"] == 1
    assert result["results"][0]["answer"] == answer
    assert result["results"][0]["recorded_evaluation"]["passed"] is False
    assert result["results"][0]["passed"] is True
