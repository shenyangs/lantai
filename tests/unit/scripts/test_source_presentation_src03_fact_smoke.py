"""Behavior locks for the SRC-03 real-provider evidence harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from lantai.presentation.models import (
    ChartObject,
    DeckPlan,
    DeckPlanSlide,
    RelationshipType,
    SlideIntent,
    VisualDirection,
)


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "source_presentation_src03_fact_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "source_presentation_src03_fact_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plan(*, positive_profit_claim: bool = False, wrong_unit: bool = False) -> DeckPlan:
    source_ref = "d03-quarter-data"
    slides: list[DeckPlanSlide] = []
    for index in range(1, 11):
        title = f"第{index}页：收入增长必须与经营贡献分开判断"
        blocks = ["所有行动均为建议，成本变化原因仍待核实。"]
        charts: list[ChartObject] = []
        if index == 2:
            title = "Q4收入继续增长，但经营贡献回落"
            blocks = ["Q4收入较Q3增长9.375%（展示可四舍五入为9.4%）。"]
            charts.append(
                ChartObject(
                    chart_type="line",
                    categories=["2025Q1", "2025Q2", "2025Q3", "2025Q4"],
                    series={
                        "收入": [120, 145, 160, 175],
                        "成本": [95, 108, 126, 150],
                    },
                    unit="元" if wrong_unit else "万元",
                    derivation="直接摘录季度数据。",
                    source_refs=[source_ref],
                )
            )
        elif index == 3:
            title = "经营贡献在Q4较Q3减少9万元"
            blocks = ["经营贡献按收入减成本计算。"]
            charts.append(
                ChartObject(
                    chart_type="column",
                    categories=["2025Q1", "2025Q2", "2025Q3", "2025Q4"],
                    series={"经营贡献": [25, 37, 34, 25]},
                    unit="万元",
                    derivation="各季度收入减成本。",
                    source_refs=[source_ref],
                )
            )
        elif index == 4:
            blocks = [
                "全年收入600万元，全年成本479万元，经营贡献121万元；"
                "计算公式为600−479=121。"
            ]
        elif index == 5:
            blocks = [
                "客户数不能推出活跃人数、留存或LTV；成本变化原因待核实。"
            ]
        if positive_profit_claim and index == 6:
            title = "利润持续增长"
        intent = SlideIntent(
            purpose="管理判断",
            key_message=title,
            evidence=list(blocks),
            relationship_type=RelationshipType.DATA_TREND,
            source_refs=[source_ref],
        )
        slides.append(
            DeckPlanSlide(
                order=index,
                title=title,
                intent=intent,
                content_blocks=blocks,
                takeaway="建议先核实成本驱动项再决策。",
                charts=charts,
            )
        )
    return DeckPlan(
        title="2025年季度经营复盘",
        narrative_arc="收入增长与经营贡献回落的分层判断",
        visual_direction=VisualDirection(
            visual_concept="克制的经营复盘",
            palette=["112233", "D97706"],
            typography="中文无衬线",
            image_language="不使用装饰图片",
            chart_language="原生数据图表",
            shape_language="少量结构线",
            grid="非对称网格",
            recurring_motif="季度刻度",
        ),
        slides=slides,
    )


def test_src03_accepts_grounded_revenue_up_contribution_down_plan() -> None:
    module = _load_module()

    result = module._evaluate_src03_plan(_plan())

    assert result["ok"] is True
    assert result["failed_assertions"] == []
    assert result["metrics"] == {
        "slide_count": 10,
        "chart_count": 2,
        "grounded_slide_count": 10,
    }
    assert all(result["assertions"].values())


def test_src03_rejects_profit_growth_claim_and_wrong_monetary_unit() -> None:
    module = _load_module()

    result = module._evaluate_src03_plan(
        _plan(positive_profit_claim=True, wrong_unit=True)
    )

    assert result["ok"] is False
    assert result["assertions"]["no_positive_profit_growth_claim"] is False
    assert result["assertions"]["monetary_chart_units_preserved"] is False


def test_src03_customer_metric_check_allows_missing_but_rejects_claimed_values() -> None:
    module = _load_module()

    assert module._has_unsupported_customer_metric(
        "活跃人数、留存与LTV待补充数据后再判断。",
    ) is False
    assert module._has_unsupported_customer_metric("LTV为800元。") is True


def test_src03_safe_evidence_contains_no_prompt_or_plan_text() -> None:
    module = _load_module()
    raw_request = "请用这段不能落盘的原始材料生成管理层PPT"
    result = module._build_safe_evidence(
        source_head="a" * 40,
        provider_id="kimi-codingplan",
        model="kimi-for-coding",
        request_sha256="b" * 64,
        material_summaries=[
            {"name": "D03_brief.md", "sha256": "c" * 64, "bytes": 123},
        ],
        evaluation=module._evaluate_src03_plan(_plan()),
        elapsed_ms=4321,
        timeout_seconds=120,
        output_token_budget=8_048,
        stage_output_token_budgets={
            "outline": 2_824,
            "detail:001-004": 3_024,
            "detail:005-008": 2_200,
        },
        stage_timings_ms={
            "outline": 1_000,
            "detail:001-004": 2_000,
            "detail:005-008": 1_500,
        },
        stage_attempt_counts={
            "outline": 2,
            "detail:001-004": 1,
            "detail:005-008": 1,
        },
        utility_call_count=4,
        estimated_cost_units=60,
        validated_plan_json_bytes=12_345,
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert raw_request not in rendered
    assert "Q4收入继续增长" not in rendered
    assert result["input"]["persisted"] is False
    assert result["provider"]["route"] == "kimi-codingplan/kimi-for-coding"
    assert result["billing"]["basis"] == "user_subscription"
    assert result["timing_ms"] == {
        "semantic_plan_and_validation": 4321,
        "configured_timeout_per_call": 120000,
        "stages": {
            "outline": 1_000,
            "detail:001-004": 2_000,
            "detail:005-008": 1_500,
        },
    }
    assert result["generation_limits"] == {
        "max_output_tokens_total": 8_048,
        "stages": {
            "outline": 2_824,
            "detail:001-004": 3_024,
            "detail:005-008": 2_200,
        },
    }
    assert result["budget"] == {
        "estimated_existing_cost_units": 60,
        "actual_existing_cost_units": 40,
        "utility_call_count": 4,
        "stage_attempt_counts": {
            "outline": 2,
            "detail:001-004": 1,
            "detail:005-008": 1,
        },
    }
    assert result["evaluation"]["metrics"]["validated_plan_json_bytes"] == 12_345


def test_src03_validation_failure_keeps_only_field_locations_and_error_types() -> None:
    module = _load_module()

    class SecretFixture(BaseModel):
        values: list[int] = Field(min_length=2)

    try:
        SecretFixture.model_validate({"values": ["不能出现在证据里的模型原文"]})
    except ValidationError as exc:
        wrapped = ValueError("structured validation failed")
        wrapped.__cause__ = exc
        result = module._safe_failure(wrapped)
    else:  # pragma: no cover - the fixture is intentionally invalid
        raise AssertionError("invalid fixture unexpectedly validated")

    rendered = json.dumps(result, ensure_ascii=False)
    assert "不能出现在证据里的模型原文" not in rendered
    assert result["validation_issues"] == [
        {"loc": ["values", 0], "type": "int_parsing"},
    ]


def test_src03_known_policy_failure_uses_safe_code_without_message() -> None:
    module = _load_module()

    result = module._safe_failure(
        ValueError("semantic planner invented immutable data"),
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert "invented immutable data" not in rendered
    assert result["error_code"] == "semantic_plan_unknown_immutable_fact"


def test_src03_numeric_failure_keeps_only_location_hash_and_stage_metadata() -> None:
    module = _load_module()
    exc = ValueError("uncited storyboard number 777 in slide-004")
    exc.presentation_numeric_token = "777"
    exc.presentation_numeric_location = "slide-004"
    exc.presentation_stage_timings_ms = {
        "outline": 10_000,
        "detail:001-004": 20_000,
    }
    exc.presentation_stage_output_token_budgets = {
        "outline": 2_824,
        "detail:001-004": 3_024,
    }
    exc.presentation_stage_attempt_counts = {
        "outline": 3,
        "detail:001-004": 1,
    }
    exc.presentation_utility_call_count = 3

    result = module._safe_failure(exc)

    rendered = json.dumps(result, ensure_ascii=False)
    assert "777" not in rendered
    assert result["error_code"] == "semantic_plan_uncited_numeric_claim"
    assert result["numeric_location"] == "slide-004"
    assert result["numeric_token_sha256"] == module._sha256_bytes(b"777")
    assert result["stage_timings_ms"] == {
        "outline": 10_000,
        "detail:001-004": 20_000,
    }
    assert result["stage_output_token_budgets"] == {
        "outline": 2_824,
        "detail:001-004": 3_024,
    }
    assert result["stage_attempt_counts"] == {
        "outline": 3,
        "detail:001-004": 1,
    }
    assert result["utility_call_count"] == 3


def test_src03_claim_numeric_failure_keeps_only_safe_claim_id_and_hash() -> None:
    module = _load_module()
    exc = ValueError(
        "uncited storyboard number 100 in claim-ledger-claim-011",
    )
    exc.presentation_numeric_token = "100"
    exc.presentation_numeric_location = "claim-ledger-claim-011"

    result = module._safe_failure(exc)

    rendered = json.dumps(result, ensure_ascii=False)
    assert "uncited storyboard number" not in rendered
    assert '"100"' not in rendered
    assert result["error_code"] == "semantic_plan_uncited_numeric_claim"
    assert result["numeric_location"] == "claim-ledger-claim-011"
    assert result["numeric_token_sha256"] == module._sha256_bytes(b"100")


def test_src03_outline_numeric_failure_keeps_only_safe_slide_id_and_hash() -> None:
    module = _load_module()
    exc = ValueError(
        "uncited storyboard number 42 in outline-slide-006",
    )
    exc.presentation_numeric_token = "42"
    exc.presentation_numeric_location = "outline-slide-006"

    result = module._safe_failure(exc)

    rendered = json.dumps(result, ensure_ascii=False)
    assert "uncited storyboard number" not in rendered
    assert '"42"' not in rendered
    assert result["error_code"] == "semantic_plan_uncited_numeric_claim"
    assert result["numeric_location"] == "outline-slide-006"
    assert result["numeric_token_sha256"] == module._sha256_bytes(b"42")


def test_src03_detail_batch_failure_keeps_only_safe_page_orders() -> None:
    module = _load_module()
    exc = ValueError("semantic detail batch must return exact target orders")
    exc.presentation_detail_batch_stage = "detail:005-008"
    exc.presentation_detail_target_orders = [5, 6, 7, 8]
    exc.presentation_detail_returned_orders = [5, 6, 8, 8]

    result = module._safe_failure(exc)

    assert result["error_code"] == "semantic_plan_detail_order_set_mismatch"
    assert result["detail_batch_stage"] == "detail:005-008"
    assert result["detail_target_orders"] == [5, 6, 7, 8]
    assert result["detail_returned_orders"] == [5, 6, 8, 8]


def test_src03_detail_claim_failure_keeps_only_safe_batch_metadata() -> None:
    module = _load_module()
    exc = ValueError("semantic detail omitted required claim text")
    exc.presentation_detail_batch_stage = "detail:005-008"
    exc.presentation_detail_target_orders = [5, 6, 7, 8]
    exc.presentation_detail_returned_orders = [5, 6, 7, 8]

    result = module._safe_failure(exc)

    assert result["error_code"] == "semantic_plan_detail_claim_omitted"
    assert result["detail_batch_stage"] == "detail:005-008"
    assert result["detail_target_orders"] == [5, 6, 7, 8]
    assert result["detail_returned_orders"] == [5, 6, 7, 8]


def test_src03_delivery_contract_failure_uses_safe_code() -> None:
    module = _load_module()

    result = module._safe_failure(
        ValueError(
            "semantic claim ledger must cover every required delivery contract"
        ),
    )

    assert result["error_code"] == "semantic_plan_required_delivery_omitted"


def test_src03_delivery_semantics_failure_uses_safe_code() -> None:
    module = _load_module()

    result = module._safe_failure(
        ValueError(
            "semantic claim omitted required delivery semantics"
        ),
    )

    assert result["error_code"] == (
        "semantic_plan_required_delivery_semantics_omitted"
    )


def test_src03_outer_timeout_covers_all_bounded_retry_waves() -> None:
    module = _load_module()

    assert module._semantic_outer_timeout_seconds(
        per_call_timeout=120,
        slide_count=10,
        detail_batch_size=4,
        detail_concurrency=2,
        claim_attempt_limit=3,
        outline_attempt_limit=3,
        detail_attempt_limit=3,
    ) == 1470


def test_src03_safe_failure_reports_code_location_without_exception_text() -> None:
    module = _load_module()

    try:
        module._number_equal(["不能落盘的模型值"], [1.0])
    except ValueError as exc:
        result = module._safe_failure(exc)
    else:  # pragma: no cover - the fixture intentionally cannot parse
        raise AssertionError("invalid numeric fixture unexpectedly parsed")

    rendered = json.dumps(result, ensure_ascii=False)
    assert "不能落盘的模型值" not in rendered
    assert result["error_origin"]["file"].endswith(
        "scripts/source_presentation_src03_fact_smoke.py",
    )
    assert result["error_origin"]["function"] == "_number_equal"
    assert isinstance(result["error_origin"]["line"], int)
