# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from lantai.app.evaluator_fixtures import (
    GoldenFixtureCase,
    evaluate_golden_fixture,
    load_golden_fixture_cases,
    run_golden_fixture_suite,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "runtime_evaluator"
    / "golden_cases.json"
)


def test_runtime_evaluator_golden_fixtures_match_expected() -> None:
    cases = load_golden_fixture_cases(FIXTURE_PATH)
    suite = run_golden_fixture_suite(cases)

    assert suite.total == 9
    assert suite.failed == 0
    assert suite.summary.conclusion == "pass"

    by_id = {result.case_id: result for result in suite.results}
    assert by_id["preview-unreachable"].actual_conclusion == "needs_revision"
    assert by_id["preview-unreachable"].actual_category == "availability"
    assert by_id["runtime-build-error"].actual_category == "build"
    assert by_id["runtime-test-failure"].actual_category == "test"
    assert by_id["runtime-dependency-missing"].actual_category == "dependency"
    assert by_id["runtime-policy-blocked"].actual_conclusion == "blocked"
    assert by_id["runtime-stalled"].actual_category == "timeout"


def test_runtime_evaluator_golden_fixture_reports_expectation_mismatch() -> None:
    case = GoldenFixtureCase.model_validate(
        {
            "id": "mismatched-build",
            "title": "Mismatched build classification",
            "kind": "runtime_event",
            "input": {
                "event": {
                    "type": "tool.completed",
                    "status": "failed",
                    "summary": "构建失败",
                    "payload": {
                        "command": "npm run build",
                        "stderr": "error TS2304: Cannot find name 'missingSymbol'.",
                        "exit_code": 2,
                    },
                },
            },
            "expect": {
                "conclusion": "pass",
                "category": "test",
            },
        },
    )

    result = evaluate_golden_fixture(case)

    assert result.passed is False
    assert result.actual_conclusion == "needs_revision"
    assert result.actual_category == "build"
    assert "conclusion expected 'pass'" in result.mismatches[0]
    assert "category expected 'test'" in result.mismatches[1]
