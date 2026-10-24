# -*- coding: utf-8 -*-
from __future__ import annotations

# Script-unit tests deliberately exercise private parsing seams via a fixture.
# pylint: disable=protected-access,redefined-outer-name

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "review_persona_semantic_outputs_blind.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "persona_blind_review",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module():
    return _load_module()


def test_anonymous_mapping_is_deterministic_and_complete(module) -> None:
    first = module._anonymous_mapping("B01", "abc")
    second = module._anonymous_mapping("B01", "abc")
    assert first == second
    assert set(first) == {"A", "B"}
    assert set(first.values()) == {"baseline", "candidate"}


def test_review_messages_do_not_expose_arm_versions(module) -> None:
    case = {
        "id": "B01",
        "type": "single_turn",
        "domain": "builder",
        "rubric": {"must_observe": ["区分证据"]},
        "turns": [
            {
                "turn": 1,
                "user": "发生了什么？",
                "outputs": {
                    "baseline": {"status": "DONE", "text": "旧回答"},
                    "candidate": {"status": "DONE", "text": "新回答"},
                },
            },
        ],
    }
    messages = module._review_messages(
        case,
        {"A": "candidate", "B": "baseline"},
    )
    text = json.dumps(messages, ensure_ascii=False)
    assert messages[0]["role"] == "user"
    assert "text" in messages[0]
    assert "content" not in messages[0]
    assert "candidate" not in text
    assert "baseline" not in text
    assert "1.5.3" not in text
    assert "1.6.0" not in text
    assert "1.6.1" not in text
    assert "新回答" in text
    assert "旧回答" in text


def test_select_cases_preserves_requested_order_and_rejects_missing(
    module,
) -> None:
    cases = [{"id": "B01"}, {"id": "D02"}]
    assert module._select_cases(cases, ["D02", "B01"]) == [cases[1], cases[0]]
    with pytest.raises(ValueError, match="Unknown case id"):
        module._select_cases(cases, ["NOPE"])


def test_extract_json_object_accepts_fenced_json(module) -> None:
    payload = module._extract_json_object('```json\n{"preferred":"TIE"}\n```')
    assert payload == {"preferred": "TIE"}


def test_validate_review_rejects_invalid_enums(module) -> None:
    with pytest.raises(ValueError, match="Invalid preferred"):
        module._validate_review(
            {
                "preferred": "C",
                "a_quality": "PASS",
                "b_quality": "PASS",
                "hard_failures": {"A": [], "B": []},
            },
        )


def test_deblind_maps_candidate_preference_to_improved(module) -> None:
    review = {
        "preferred": "A",
        "a_quality": "PASS",
        "b_quality": "PARTIAL",
        "hard_failures": {"A": [], "B": []},
        "rationale": "",
        "evidence": [],
    }
    result = module._deblind(review, {"A": "candidate", "B": "baseline"})
    assert result["comparison"] == "IMPROVED"
    assert result["candidate_quality"] == "PASS"
    assert result["baseline_quality"] == "PARTIAL"


def test_deblind_maps_baseline_preference_to_regressed(module) -> None:
    review = {
        "preferred": "B",
        "a_quality": "PARTIAL",
        "b_quality": "PASS",
        "hard_failures": {"A": ["candidate issue"], "B": []},
        "rationale": "",
        "evidence": [],
    }
    result = module._deblind(review, {"A": "candidate", "B": "baseline"})
    assert result["comparison"] == "REGRESSED"
    assert result["candidate_hard_failures"] == ["candidate issue"]


def test_summary_ignores_parse_errors(module) -> None:
    summary = module._summary(
        [
            {
                "status": "DONE",
                "deblinded": {
                    "comparison": "NO_MEANINGFUL_CHANGE",
                    "baseline_quality": "PASS",
                    "candidate_quality": "PASS",
                    "candidate_hard_failures": [],
                },
            },
            {"status": "PARSE_ERROR"},
        ],
    )
    assert summary["comparison_counts"]["NO_MEANINGFUL_CHANGE"] == 1
    assert summary["candidate_case_quality"]["PASS"] == 1
