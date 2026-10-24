# -*- coding: utf-8 -*-
"""Long-source claims must fit without dropping facts or multiplying retries."""

import json

import pytest
from pydantic import ValidationError

from lantai.app.structured_utility import (
    StructuredTaskValidationError,
    StructuredUtilityResult,
)
from lantai.app.utility_model import UtilityTaskIncompleteError
from lantai.presentation.intelligence import (
    UtilityPresentationSemanticPlanner,
    _semantic_claim_source_batches,
)
from lantai.presentation.models import (
    PresentationBrief,
    PresentationJobRequest,
    PresentationSourceSection,
)


class OutlineReached(RuntimeError):
    """Stop the fake model at the real merged-ledger/outline boundary."""


def _long_case():
    sections = [
        PresentationSourceSection(
            title="收入材料",
            source_ref=f"source-{index}",
            body=json.dumps({
                "unit": "万元",
                "expected_checks": {"revenue": 42},
                "categories": ["甲", "乙"],
                "series": {"收入": [42, 38]},
                "background": "有据可查的背景材料。" * 700,
            }, ensure_ascii=False),
        )
        for index in range(3)
    ]
    brief = PresentationBrief(
        objective="完整展示收入事实",
        audience="报告读者",
        narrative="事实、比较与结论",
        required_facts=["收入为42万元", "乙为38万元", "保持完整口径"],
        immutable_data=["42", "38"],
        source_refs=[section.source_ref for section in sections],
        source_sections=sections,
        prohibited_changes=["不得发明事实"],
    )
    request = PresentationJobRequest(
        conversation_id="long-claim-batching-test",
        user_request="把完整报告做成PPT",
        slide_count=26,
    )
    return request, brief


def _ledger(kwargs, payload):
    contracts = payload["required_delivery_contracts"]
    sections = payload["brief"]["source_sections"]
    global_contracts = kwargs["purpose"] == "presentation-semantic-global-contracts"
    return kwargs["result_model"].model_validate({
        "claims": [{
            "claim_id": f"claim-{index:03d}",
            "statement": "收入为42万元；乙为38万元。",
            "source_refs": [section["source_ref"]],
            "delivery_contract_ids": [contract["contract_id"]],
        } for index, (section, contract) in enumerate(
            zip(sections, contracts), start=1,
        )] if not global_contracts else [],
        "chart_contracts": [{
            "contract_id": f"chart-{index:03d}",
            "chart": {
                "chart_type": "bar",
                "categories": ["甲", "乙"],
                "series": {"收入": [42, 38]},
                "unit": "万元",
                "source_refs": [section["source_ref"]],
            },
        } for index, section in enumerate(sections, start=1)]
        if "source_batch" not in payload else [],
    })


def test_long_claim_stage_preserves_sections_contracts_and_charts(monkeypatch):
    request, brief = _long_case()
    claim_payloads = []
    global_payloads = []
    outline_payloads = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            outline_payloads.append(payload)
            raise OutlineReached
        if kwargs["purpose"] == "presentation-semantic-global-contracts":
            global_payloads.append(payload)
        else:
            claim_payloads.append(payload)
        assert kwargs["max_output_tokens"] == 8192
        return StructuredUtilityResult(
            value=_ledger(kwargs, payload), model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)

    assert len(claim_payloads) == 3
    assert len(global_payloads) == 1
    assert global_payloads[0]["brief"] == brief.model_dump(mode="json")
    assert len(global_payloads[0]["approved_claim_ledger"]["claims"]) == 3
    assert [section for payload in claim_payloads
            for section in payload["brief"]["source_sections"]] == [
        section.model_dump(mode="json") for section in brief.source_sections
    ]
    for payload in claim_payloads:
        assert payload["claim_count_target"] <= 40
        assert payload["claim_count_ceiling"] == 118
        assert payload["global_required_facts"] == brief.required_facts
        assert payload["brief"]["prohibited_changes"] == brief.prohibited_changes
    contracts = [contract for payload in claim_payloads
                 for contract in payload["required_delivery_contracts"]]
    assert [contract["contract_id"] for contract in contracts] == [
        "delivery-001", "delivery-002", "delivery-003",
    ]
    merged = outline_payloads[0]["claim_ledger"]
    assert [claim["claim_id"] for claim in merged["claims"]] == [
        "claim-001", "claim-002", "claim-003",
    ]
    assert [chart["contract_id"] for chart in merged["chart_contracts"]] == [
        "chart-001", "chart-002", "chart-003",
    ]
    assert [claim["delivery_contract_ids"] for claim in merged["claims"]] == [
        ["delivery-001"], ["delivery-002"], ["delivery-003"],
    ]
    assert all(chart["chart"]["series"] == {"收入": [42, 38]}
               for chart in merged["chart_contracts"])
    assert planner.estimated_cost_units(request) == 1400


def test_calendar_source_reaches_real_claim_utility_boundary_without_retry(
    monkeypatch,
):
    request, brief = _long_case()
    brief = brief.model_copy(
        update={
            "source_sections": [
                section.model_copy(
                    update={
                        "title": f"{section.title}\n资料发布于2026-10-06。",
                    },
                )
                for section in brief.source_sections
            ],
        },
    )
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert kwargs["max_output_tokens"] == 8192
        for tokens in payload.get(
            "allowed_numeric_tokens_by_source", {},
        ).values():
            assert {"2026", "10", "06"}.issubset(tokens)
            assert not {"-10", "-6"}.intersection(tokens)
        value = _ledger(kwargs, payload)
        value = value.model_copy(
            update={
                "claims": [
                    claim.model_copy(
                        update={
                            "statement": (
                                "收入为42万元；乙为38万元。"
                                "资料发布于2026-10-06。"
                            ),
                        },
                    )
                    for claim in value.claims
                ],
            },
        )
        return StructuredUtilityResult(
            value=value,
            model_label="test/model",
            repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task",
        fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert not planner.last_stage_contract_failures
    assert planner.estimated_cost_units(request) == 1400


def test_claim_batches_share_existing_five_attempt_allowance(monkeypatch):
    request, brief = _long_case()
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        if claim_calls == 1:
            raise StructuredTaskValidationError("failed schema validation")
        payload = json.loads(kwargs["user_prompt"])
        return StructuredUtilityResult(
            value=_ledger(kwargs, payload), model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 5
    assert {key: count for key, count in planner.last_stage_attempt_counts.items()
            if key.startswith("claims")} == {
        "claims:001": 2, "claims:002": 1, "claims:003": 1, "claims:004": 1,
    }


def test_dense_source_numeric_retry_keeps_the_complete_signed_whitelist(monkeypatch):
    request, brief = _long_case()
    sections = []
    for section in brief.source_sections:
        data = json.loads(section.body)
        data.update({
            "source_values": list(range(1, 90)),
            "reported_signed_value": -7,
            "reported_percentage": "17%",
        })
        sections.append(section.model_copy(update={
            "body": json.dumps(data, ensure_ascii=False),
        }))
    brief = brief.model_copy(update={"source_sections": sections})
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        value = _ledger(kwargs, payload)
        if kwargs["purpose"] == "presentation-semantic-claims":
            claim_calls += 1
            if claim_calls == 1:
                value = value.model_copy(update={"claims": [
                    value.claims[0].model_copy(update={
                        "statement": "收入为42万元；乙为38万元；对比值为-89万元。",
                    }),
                ]})
            elif claim_calls == 2:
                directive = payload["retry_directive"]
                # A late source value and its original percent/sign forms must
                # not disappear behind a supposedly authoritative 64-item cap.
                assert "89" in directive
                assert "17%" in directive
                assert "-7" in directive
                assert "正负号" in directive
                assert "-89" not in directive
                value = value.model_copy(update={"claims": [
                    value.claims[0].model_copy(update={
                        "statement": (
                            "收入为42万元；乙为38万元；对比值为89万元；"
                            "原报数为-7；比例为17%。"
                        ),
                    }),
                ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert sum(count for stage, count in planner.last_stage_attempt_counts.items()
               if stage.startswith("claims")) == 5
    assert planner.last_stage_contract_failures == [{
        "stage": "claims:001", "attempt": 1,
        "violation_code": "unsupported_numeric_claim",
    }]


def test_numeric_retry_never_repairs_an_unsupported_sign_locally(monkeypatch):
    request, brief = _long_case()
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        claim_calls += 1
        payload = json.loads(kwargs["user_prompt"])
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            value.claims[0].model_copy(update={
                "statement": "收入为42万元；乙为38万元；乙的原报数为-38万元。",
            }),
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(ValueError, match="uncited storyboard number -38"):
        planner.plan(request, brief)
    assert claim_calls == 2
    assert planner.last_stage_attempt_counts == {"claims:001": 2}
    assert len(planner.last_stage_contract_failures) == 2


@pytest.mark.parametrize("repair", [False, True])
def test_claim_ledger_rejects_percent_rescaling_before_outline(monkeypatch, repair):
    request, brief = _long_case()
    sections = []
    for section in brief.source_sections:
        data = json.loads(section.body)
        data["reported_percentage"] = "17%"
        sections.append(section.model_copy(update={
            "body": json.dumps(data, ensure_ascii=False),
        }))
    brief = brief.model_copy(update={"source_sections": sections})
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        value = _ledger(kwargs, payload)
        if kwargs["purpose"] == "presentation-semantic-claims":
            claim_calls += 1
            if claim_calls == 1 or not repair:
                percentage = "0.17%"
            else:
                percentage = "17%"
                if claim_calls == 2:
                    assert "17%" in payload["retry_directive"]
                    assert "0.17%" not in payload["retry_directive"]
            value = value.model_copy(update={"claims": [
                value.claims[0].model_copy(update={
                    "statement": f"收入为42万元；乙为38万元；比例为{percentage}。",
                }),
            ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    if repair:
        with pytest.raises(OutlineReached):
            planner.plan(request, brief)
        assert claim_calls == 4
        assert sum(count for stage, count in planner.last_stage_attempt_counts.items()
                   if stage.startswith("claims")) == 5
        assert len(planner.last_stage_contract_failures) == 1
    else:
        with pytest.raises(ValueError, match="uncited storyboard number 0.17"):
            planner.plan(request, brief)
        assert claim_calls == 2
        assert planner.last_stage_attempt_counts == {"claims:001": 2}
        assert len(planner.last_stage_contract_failures) == 2


@pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029"])
def test_claim_planner_accepts_cross_line_negative_source_without_retry(monkeypatch, line_break):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": line_break.join([section.title, "7", "−17%"]),
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert "retry_directive" not in payload
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": "收入为42万元；乙为38万元；比例为-17%。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert sum(count for stage, count in planner.last_stage_attempt_counts.items()
               if stage.startswith("claims")) == 4
    assert not planner.last_stage_contract_failures


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("prefix", ["年份\t增长率\n2025\t", "2025年 "])
def test_claim_planner_accepts_adjacent_negative_table_and_year_values_without_retry(
    monkeypatch, minus, prefix,
):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{prefix}{minus}17%",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert "retry_directive" not in payload
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": "收入为42万元；乙为38万元；2025年的增长率为-17%。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert sum(count for stage, count in planner.last_stage_attempt_counts.items()
               if stage.startswith("claims")) == 4
    assert not planner.last_stage_contract_failures


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("equals", ["=", "＝"])
@pytest.mark.parametrize(("last_operator", "result"), [("-", 20), ("+", 30)])
def test_claim_planner_accepts_explicit_arithmetic_chains_without_retry(
    monkeypatch, minus, equals, last_operator, result,
):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": (
                f"{section.title}\n收入120万元；成本95万元；费用5万元；贡献{result}万元。"
            ),
        }) for section in brief.source_sections],
    })
    last_operator = minus if last_operator == "-" else last_operator
    equation = f"120万元 {minus}95万元 {last_operator}5万元 {equals} {result}万元"
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert "retry_directive" not in payload
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元；{equation}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert sum(count for stage, count in planner.last_stage_attempt_counts.items()
               if stage.startswith("claims")) == 4
    assert not planner.last_stage_contract_failures


@pytest.mark.parametrize("minus", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("equals", ["=", "＝"])
@pytest.mark.parametrize(("equation", "source"), [
    (
        "120万元 {minus}95万元 {minus} {minus}5万元 {equals}30万元",
        "收入120万元；成本95万元；调整-5万元；贡献30万元。",
    ),
    (
        "120万元 {minus}95万元 ×(2) {equals}{minus}70万元",
        "收入120万元；成本95万元；系数2；贡献-70万元。",
    ),
    (
        "(120万元 {minus}95万元) {minus}5万元 {equals}20万元",
        "收入120万元；成本95万元；费用5万元；贡献20万元。",
    ),
    (
        "120万元 {minus}95万元 {minus}({minus}5万元) {equals}30万元",
        "收入120万元；成本95万元；调整-5万元；贡献30万元。",
    ),
    (
        "25万元 {equals}120万元 {minus}95万元",
        "收入120万元；成本95万元；贡献25万元。",
    ),
    (
        "（120万元 {minus}95万元） {minus}5万元 {equals}20万元",
        "收入120万元；成本95万元；费用5万元；贡献20万元。",
    ),
    (
        "120万元 {minus}95万元 + {minus}5万元 {equals}20万元",
        "收入120万元；成本95万元；调整-5万元；贡献20万元。",
    ),
])
def test_claim_planner_accepts_signed_and_parenthesized_equations_without_retry(
    monkeypatch, minus, equals, equation, source,
):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    equation = equation.format(minus=minus, equals=equals)
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert "retry_directive" not in payload
        assert kwargs["max_output_tokens"] == 8192
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元；{equation}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert sum(count for stage, count in planner.last_stage_attempt_counts.items()
               if stage.startswith("claims")) == 4
    assert not planner.last_stage_contract_failures
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize("unit", [
    "元", "万元", "千元", "百元", "亿元", "人", "个", "家", "次",
    "天", "月", "年", "季度", "%", "％",
])
@pytest.mark.parametrize(("equation", "adjustment", "result"), [
    ("(120 -95){unit} =25{unit}", False, 25),
    ("25{unit} =(120 -95){unit}", False, 25),
    ("（(120 -95){unit}） =25{unit}", False, 25),
    ("25{unit} =(120 -95){unit} =25{unit}", False, 25),
    ("（120 -95）{unit} -（−5）{unit} =30{unit}", True, 30),
])
def test_claim_planner_accepts_postfix_parenthesized_quantities_without_retry(
    monkeypatch, unit, equation, adjustment, result,
):
    equation = equation.format(unit=unit)
    source = f"收入120{unit}；成本95{unit}；贡献{result}{unit}。"
    if adjustment:
        source += f"调整-5{unit}。"
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert "retry_directive" not in payload
        assert kwargs["max_output_tokens"] == 8192
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元；{equation}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert sum(count for stage, count in planner.last_stage_attempt_counts.items()
               if stage.startswith("claims")) == 4
    assert not planner.last_stage_contract_failures
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize(("source", "authored", "accepted"), [
    ("原报数95万元", "原报数为-(95)万元", False),
    ("原报数95万元", "原报数为−（95）万元", False),
    ("原报数95万元", "原报数为−（（95万元））", False),
    ("原报数95%", "原报数为-(95)%", False),
    ("原报数95%", "原报数为-(95%)", False),
    ("原报数95%", "原报数为−（（95）％）", False),
    ("原报数-95万元", "原报数为-(95)万元", True),
    ("原报数-(95)万元", "原报数为-95万元", True),
    ("原报数-95%", "原报数为−（95）％", True),
    ("原报数-(95)%", "原报数为-95%", True),
    ("原报数17%", "原报数为（17）％", True),
    ("原报数（17）％", "原报数为17%", True),
    ("原报数95万元", "原报数为-(-95)万元", True),
    ("原报数95%", "原报数为-(-(95))%", True),
    ("原报数-(-95)万元", "原报数为-95万元", False),
    ("原报数（17）％", "原报数为1700%", False),
    ("原报数（17）％", "原报数为(0.17)%", False),
    ("收入120万元；成本95万元；贡献25万元", "120万元 -(95万元) =25万元", True),
    ("收入120万元；成本95万元；贡献-25万元", "-(120万元 -95万元) =-25万元", True),
    ("收入120万元；成本95万元；贡献-25万元", "-(120 -95)万元 =-25万元", True),
    ("今年120万元 -95万元 =25万元", "收入120万元；成本95万元；贡献25万元", True),
])
def test_claim_planner_preserves_atomic_parenthesized_sign_and_percent_notation(
    monkeypatch, source, authored, accepted,
):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert kwargs["max_output_tokens"] == 8192
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元；{authored}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    if accepted:
        with pytest.raises(OutlineReached):
            planner.plan(request, brief)
        assert claim_calls == 4
        assert not planner.last_stage_contract_failures
    else:
        with pytest.raises(ValueError, match="uncited storyboard number"):
            planner.plan(request, brief)
        assert claim_calls == 2
        assert planner.last_stage_contract_failures == [
            {
                "stage": "claims:001", "attempt": attempt,
                "violation_code": "unsupported_numeric_claim",
            }
            for attempt in (1, 2)
        ]
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize("authored", [
    "-(95万元)万元", "−（（95万元））万元", "-(17%)%",
    "−（17％）%", "(17%)%", "(95万元)万元",
])
def test_claim_planner_rejects_repeated_atomic_quantity_units(monkeypatch, authored):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n原报数-95万元；比例-17%",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert kwargs["max_output_tokens"] == 8192
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元；{authored}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(ValueError, match="unsupported numeric notation"):
        planner.plan(request, brief)
    assert claim_calls == 2
    assert planner.last_stage_attempt_counts == {"claims:001": 2}
    assert len(planner.last_stage_contract_failures) == 2
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize(("source", "authored", "accepted"), [
    ("原报数95万元", "原报数为--(95)万元", True),
    ("原报数95万元", "原报数为--95万元", True),
    ("原报数95万元", "原报数为-(-(+95))万元", True),
    ("原报数95万元", "原报数为−+（−＋95）万元", True),
    ("原报数-95万元", "原报数为-(+95)万元", True),
    ("原报数-95万元", "原报数为-+(95)万元", True),
    ("原报数-95万元", "原报数为-+95万元", True),
    ("原报数-95万元", "原报数为--(-95)万元", True),
    ("原报数-95万元", "原报数为−＋（＋95）万元", True),
    ("原报数-95万元", "原报数为＋（−95）万元", True),
    ("原报数95万元", "原报数为-(+95)万元", False),
    ("原报数95万元", "原报数为-+(95)万元", False),
    ("原报数95万元", "原报数为-+95万元", False),
    ("原报数95%", "原报数为--(95)%", True),
    ("原报数-95%", "原报数为-(+95)%", True),
    ("原报数95%", "原报数为-(+95)%", False),
    ("原报数--(95)%", "原报数为95%", True),
    ("原报数--(95)%", "原报数为-95%", False),
    ("原报数-(+95)%", "原报数为-95%", True),
    ("原报数− +\t（＋ 95）万元", "原报数为-95万元", True),
    ("原报数95万元", "- --(95)万元", True),
    ("原报数-95万元", "- -(+95)万元", True),
    ("收入120万元；调整-95万元；贡献215万元", "120万元 --(95万元)=215万元", True),
    ("收入120万元；成本95万元；贡献25万元", "120万元 -+(95万元)=25万元", True),
    ("收入120万元；成本95万元；贡献-25万元", "-(120万元 -+(95万元))=-25万元", True),
    ("收入120万元；调整-95万元；贡献215万元", "120万元 - -(95万元)=215万元", True),
    ("收入120万元；成本95万元；贡献25万元", "120万元 -(+95万元)=25万元", True),
    ("原报数95万元", "- +(95)万元", True),
])
def test_claim_planner_preserves_complete_unary_sign_chain_on_one_quantity(
    monkeypatch, source, authored, accepted,
):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert kwargs["max_output_tokens"] == 8192
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元\n{authored}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    if accepted:
        with pytest.raises(OutlineReached):
            planner.plan(request, brief)
        assert claim_calls == 4
        assert not planner.last_stage_contract_failures
    else:
        with pytest.raises(ValueError, match="uncited storyboard number"):
            planner.plan(request, brief)
        assert claim_calls == 2
        assert planner.last_stage_contract_failures == [
            {
                "stage": "claims:001", "attempt": attempt,
                "violation_code": "unsupported_numeric_claim",
            }
            for attempt in (1, 2)
        ]
    assert planner.estimated_cost_units(request) == 1400




@pytest.mark.parametrize("authored", [
    "120 --95",
    "120万元 --95万元",
    "(120万元) --(95万元)",
    "(120) - -95",
    "120 --95 =",
    "120 --95 =215 +",
    "120 --95 =(215",
    "120万元 − −（95万元）＝215万元＋",
    "120 + - + -95 =",
    "(120) -(+(-95)) =",
    "-(95万元元)",
    "−（95万元元）",
    "-(95万元万元)",
    "-(17%%)",
    "-(17％%)",
    "-(17%％)",
    "-(95万元 元)",
    "-(+(17%%))",
    "(95万元元)",
    "-(95",
    "-（95)",
    "--(95",
    "-+(95",
    "−（95万元",
    "-(17%",
    "-(95万元）",
    "-((95)",
    "120--95",
    "120-95 =",
    "120-95 =215 +",
    "(120-95",
    "120-95)",
    "120-95 +",
    "120-95-",
    "120-95=(215",
    "-(95人次次)",
    "(95人天天)",
    "-(95人月月)",
    "-(95人年年)",
    "-(95人次)人次",
    "(95人天)天",
    "120-95 +(17)",
    "120-95 =(215",
    "120--95(17)",
    "120-95-17",
    "(120-95 +(17))",
    "(120-95))",
    "((120-95)",
    "120-95万元万元",
    "(120-95万元)万元",
    "(120-95)万元万元",
    "(120-95)%％",
    "120-95 =215 + (17",
])
def test_claim_planner_rejects_unproven_or_malformed_quantity_notation(
    monkeypatch, authored,
):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n收入120万元；成本95万元；贡献215万元；比例17%",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert kwargs["max_output_tokens"] == 8192
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元\n{authored}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(ValueError, match="unsupported numeric notation"):
        planner.plan(request, brief)
    assert claim_calls == 2
    assert planner.last_stage_attempt_counts == {"claims:001": 2}
    assert planner.last_stage_contract_failures == [
        {"stage": "claims:001", "attempt": attempt,
         "violation_code": "unsupported_numeric_claim"}
        for attempt in (1, 2)
    ]
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize(("source", "authored"), [
    *[
        (source.format(unit=unit), authored.format(unit=unit))
        for unit in ("人次", "人天", "人月", "人年")
        for source, authored in (
            ("累计95{unit}", "累计(95{unit})"),
            ("累计(95{unit})", "累计95{unit}"),
            ("调整-95{unit}", "调整-(95{unit})"),
            ("调整-(95{unit})", "调整-95{unit}"),
        )
    ],
    *[
        (source.format(sign=sign), authored.format(sign=sign))
        for sign in ("-", "−", "－", "﹣")
        for source, authored in (
            ("区间2025年到2026年", "区间2025{sign}2026年"),
            ("区间2025{sign}2026年", "区间2025年到2026年"),
            ("区间2025年到2026年", "区间(2025{sign}2026年)"),
            ("区间(2025{sign}2026年)", "区间2025年到2026年"),
        )
    ],
    ("区间5.0到6.5", "区间5.0-6.5"),
    ("区间5.0-6.5", "区间5.0到6.5"),
    ("区间2025年到2026年", "区间2025–2026年"),
    ("区间2025年到2026年", "区间2025—2026年"),
])
def test_claim_planner_preserves_compound_units_and_compact_ranges(
    monkeypatch, source, authored,
):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert kwargs["max_output_tokens"] == 8192
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元\n{authored}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert not planner.last_stage_contract_failures
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize(("source", "authored"), [
    (left, right)
    for sign in ("-", "−", "－", "﹣")
    for source, authored in (
        ("区间2025年到2026年，人数95人", "区间2025{sign}2026年(95人)"),
        ("区间2025年到2026年，人数95人", "区间2025{sign}2026年（共95人）"),
        ("区间2025年到2026年、2027年到2028年", "区间2025{sign}2026年 2027{sign}2028年"),
        ("区间2025年到2026年", "区间2025年{sign}2026年"),
        ("区间5元到17元", "区间5元{sign}17元"),
        ("区间2025年到2026年，人数95人", "区间(2025{sign}2026年)(95人)"),
        ("区间2025年到2026年，人数95人", "区间2025{sign}2026年 95人"),
        ("人数95人，区间2025年到2026年", "(95人)2025{sign}2026年"),
        ("区间2025年到2026年，人数95人", "区间(2025{sign}2026年（共95人）)"),
        ("区间2025年到2026年，人数95人", "区间(2025{sign}2026)年(95人)"),
    )
    for left, right in (
        (source, authored.format(sign=sign)),
        (authored.format(sign=sign), source),
    )
])
def test_claim_planner_proves_compact_range_atoms_with_adjacent_content(
    monkeypatch, source, authored,
):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert kwargs["max_output_tokens"] == 8192
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元\n{authored}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(
            value=value, model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert not planner.last_stage_contract_failures
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize(("source", "authored"), [
    (left, right)
    for sign in ("-", "−", "－", "﹣")
    for source, authored in (
        ("区间2025年到2026年", "- 2025{sign}2026年"),
        ("区间2025年到2026年", "* 2025{sign}2026年"),
        ("区间2025年到2026年", "  +\t2025{sign}2026年"),
        ("区间5到17", "区间(5){sign}(17)"),
        ("区间5元到17元", "区间(5元){sign}(17元)"),
        ("区间5元到17元", "区间5元{sign}(17元)"),
        ("区间5元到17元", "区间(5元){sign}17元"),
        ("区间5元到17元", "区间（5元）{sign}（17元）"),
        ("区间5元到17元", "区间((5)元){sign}((17)元)"),
        ("区间5元到17元", "区间((5元)){sign}((17元))"),
    )
    for left, right in (
        (source, authored.format(sign=sign)),
        (authored.format(sign=sign), source),
    )
])
def test_claim_planner_proves_wrapped_and_list_range_quantities(monkeypatch, source, authored):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert kwargs["max_output_tokens"] == 8192
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元\n{authored}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(value=value, model_label="test/model", repaired=False)

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_model)
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert not planner.last_stage_contract_failures
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize("sign", ["-", "−", "－", "﹣"])
@pytest.mark.parametrize("notation", ["(5{sign}17)%", "（5{sign}17）％", "5{sign}17%"])
@pytest.mark.parametrize("authored", ["比例5%到17%", "比例1700%", "比例0.17%"])
def test_claim_planner_keeps_shared_range_percent_dimension(monkeypatch, sign, notation, authored):
    request, brief = _long_case()
    source = f"比例{notation.format(sign=sign)}"
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元\n{authored}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(value=value, model_label="test/model", repaired=False)

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_model)
    planner = UtilityPresentationSemanticPlanner("agent-1")
    if authored == "比例5%到17%":
        with pytest.raises(OutlineReached):
            planner.plan(request, brief)
        assert claim_calls == 4
        assert not planner.last_stage_contract_failures
    else:
        with pytest.raises(ValueError, match="uncited storyboard number"):
            planner.plan(request, brief)
        assert claim_calls == 2
        assert planner.last_stage_attempt_counts == {"claims:001": 2}
        assert len(planner.last_stage_contract_failures) == 2


def test_truncated_claim_batch_stops_without_identical_retry(monkeypatch):
    request, brief = _long_case()
    calls = 0

    async def truncated(**kwargs):
        nonlocal calls
        calls += 1
        raise UtilityTaskIncompleteError(finish_reason="length")

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", truncated,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(UtilityTaskIncompleteError):
        planner.plan(request, brief)
    assert calls == 1
    assert planner.last_stage_contract_failures == [{
        "stage": "claims:001", "attempt": 1,
        "violation_code": "output_truncated",
    }]


def test_outline_prompt_does_not_request_fields_its_schema_forbids():
    assert "6. relationship_type 可从" not in (
        UtilityPresentationSemanticPlanner.OUTLINE_SYSTEM_PROMPT
    )


def test_two_batches_preserve_the_existing_total_claim_capacity(monkeypatch):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": brief.source_sections[:2],
        "source_refs": brief.source_refs[:2],
    })
    payloads = []
    global_payloads = []

    async def fake_model(**kwargs):
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-global-contracts":
            global_payloads.append(payload)
        else:
            payloads.append(payload)
        return StructuredUtilityResult(
            value=_ledger(kwargs, payload), model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    with pytest.raises(OutlineReached):
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)
    assert len(payloads) == 2
    assert len(global_payloads) == 1
    assert [payload["claim_count_target"] for payload in payloads] == [60, 60]
    assert [payload["claim_count_ceiling"] for payload in payloads] == [119, 119]


@pytest.mark.parametrize("counts", [(41, 1, 1), (100, 1)])
def test_skewed_fact_density_can_use_remaining_global_capacity(monkeypatch, counts):
    request, brief = _long_case()
    sections = []
    for section, count in zip(brief.source_sections, counts):
        body = json.loads(section.body)
        body["qualitative_facts"] = ["背景事实" + chr(0x4E00 + index)
                                     for index in range(count - 1)]
        sections.append(section.model_copy(update={
            "body": json.dumps(body, ensure_ascii=False),
        }))
    brief = brief.model_copy(update={
        "source_sections": sections,
        "source_refs": [section.source_ref for section in sections],
    })
    claim_calls = []
    outlines = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            outlines.append(payload)
            raise OutlineReached
        if kwargs["purpose"] == "presentation-semantic-global-contracts":
            return StructuredUtilityResult(
                value=_ledger(kwargs, payload), model_label="test/model", repaired=False,
            )
        claim_calls.append(payload)
        ledger = _ledger(kwargs, payload)
        source = payload["brief"]["source_sections"][0]
        facts = json.loads(source["body"])["qualitative_facts"]
        claims = ledger.claims + [ledger.claims[0].model_copy(update={
            "claim_id": f"claim-{index + 2:03d}",
            "statement": statement,
            "delivery_contract_ids": [],
        }) for index, statement in enumerate(facts)]
        return StructuredUtilityResult(
            value=ledger.model_copy(update={"claims": claims}),
            model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    merged = outlines[0]["claim_ledger"]
    assert len(merged["claims"]) == sum(counts) <= 120
    assert [claim["claim_id"] for claim in merged["claims"]] == [
        f"claim-{index:03d}" for index in range(1, sum(counts) + 1)
    ]
    assert [claim["statement"] for claim in merged["claims"]
            if claim["statement"].startswith("背景事实")] == [
        "背景事实" + chr(0x4E00 + index) for index in range(counts[0] - 1)
    ]
    assert len(claim_calls) == len(counts) <= 5


@pytest.mark.parametrize("section_count", [0, 1, 2, 3, 16, 60])
def test_transport_partition_keeps_every_section_once_and_in_order(section_count):
    _, brief = _long_case()
    sections = [brief.source_sections[0].model_copy(update={
        "body": "材料" * (index + 1) * 500,
    }) for index in range(section_count)]
    brief = brief.model_copy(update={"source_sections": sections})
    batches = _semantic_claim_source_batches(brief)
    assert [index for batch in batches for index in batch] == list(
        range(section_count),
    )
    assert 1 <= len(batches) <= 3
    if section_count:
        assert all(batches)


def test_shared_file_reference_does_not_allow_numbers_from_other_batches(monkeypatch):
    request, brief = _long_case()
    sections = []
    for index, section in enumerate(brief.source_sections):
        body = json.loads(section.body)
        body["expected_checks"] = {}
        body["categories"] = []
        body["series"] = {}
        body["numeric_fact"] = 100 + index
        sections.append(section.model_copy(update={
            "source_ref": "source-0", "body": json.dumps(body, ensure_ascii=False),
        }))
    brief = brief.model_copy(update={
        "source_sections": sections, "source_refs": ["source-0"],
    })
    calls = 0

    async def wrong_scope(**kwargs):
        nonlocal calls
        calls += 1
        return StructuredUtilityResult(
            value=kwargs["result_model"].model_validate({
                "claims": [{
                    "claim_id": "claim-001", "statement": "收入为101万元。",
                    "source_refs": ["source-0"],
                    "delivery_contract_ids": [],
                }], "chart_contracts": [],
            }), model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", wrong_scope,
    )
    with pytest.raises(ValueError, match="uncited storyboard number 101"):
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)
    # Three source batches and their global contracts share five calls: this
    # first batch can retry once while preserving all remaining initial calls.
    assert calls == 2


def test_short_claim_stage_keeps_single_call_payload_and_full_capacity(monkeypatch):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "body": section.body.replace("有据可查的背景材料。" * 700, "背景材料"),
        }) for section in brief.source_sections],
    })
    payloads = []

    async def fake_model(**kwargs):
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        payload = json.loads(kwargs["user_prompt"])
        payloads.append(payload)
        return StructuredUtilityResult(
            value=_ledger(kwargs, payload), model_label="test/model", repaired=False,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert len(payloads) == 1
    assert payloads[0]["claim_count_ceiling"] == 120
    assert "source_batch" not in payloads[0]
    assert payloads[0]["brief"] == brief.model_dump(mode="json")
    assert planner.last_stage_attempt_counts["claims"] == 1


@pytest.mark.parametrize(("source", "authored"), [
    *[
        (left, right)
        for minus in ("-", "−", "－", "﹣")
        for source, authored in (
            ("120万元{minus}95万元=25万元", "120万元{minus}95万元=25万元(结余)"),
            ("120万元{minus}95万元＝25万元", "120万元{minus}95万元＝25万元（结余）"),
            ("120万元{minus}95万元=25万元", "120万元{minus}95万元=25万元 \t(结余)"),
            ("120万元{minus}95万元=25万元", "(起点)120万元{minus}95万元=25万元"),
            ("120万元{minus}95万元=25万元；人数17人", "120万元{minus}95万元=25万元(17人)"),
            ("120万元{minus}95万元=25万元；人数17人", "(17人)120万元{minus}95万元=25万元"),
        )
        for left, right in (
            (source.format(minus=minus), authored.format(minus=minus)),
            (authored.format(minus=minus), source.format(minus=minus)),
        )
    ],
    *[
        (left, right)
        for unit in ("公斤", "千克", "克")
        for low, high in (("5", "17"), ("5.0", "17.5"))
        for source, authored in (
            (f"区间{low}{unit}到{high}{unit}", f"区间({low}{unit})-({high}{unit})"),
            (f"区间{low}{unit}到{high}{unit}", f"区间（{low}{unit}）−（{high}{unit}）"),
            (f"区间{low}{unit}到{high}{unit}", f"区间(({low}){unit})－(({high}){unit})"),
        )
        for left, right in ((source, authored), (authored, source))
    ],
])
def test_claim_planner_keeps_annotation_and_standard_weight_boundaries(monkeypatch, source, authored):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    claim_calls = 0

    async def fake_model(**kwargs):
        nonlocal claim_calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        claim_calls += 1
        assert kwargs["max_output_tokens"] == 8192
        assert not payload.get("retry_directive")
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元\n{authored}。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(value=value, model_label="test/model", repaired=False)

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_model)
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert claim_calls == 4
    assert not planner.last_stage_contract_failures
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize(("source", "authored"), [
    (left, right)
    for unit in ("吨", "米", "件", "台", "份", "小时", "未知后缀", "kg")
    for low, high in (("5", "17"), ("5.0", "17.5"))
    for source, authored in (
        (f"区间{low}{unit}到{high}{unit}", f"区间({low}{unit})-({high}{unit})"),
        (f"区间{low}{unit}到{high}{unit}", f"区间（{low}{unit}）−（{high}{unit}）"),
        (f"区间{low}{unit}到{high}{unit}", f"区间(({low}){unit})－(({high}){unit})"),
    )
    for left, right in ((source, authored), (authored, source))
])
def test_claim_planner_keeps_generic_single_quantity_range_suffixes(monkeypatch, source, authored):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n{source}",
        }) for section in brief.source_sections],
    })
    calls = 0

    async def fake_model(**kwargs):
        nonlocal calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        calls += 1
        assert kwargs["max_output_tokens"] == 8192
        assert not payload.get("retry_directive")
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={"statement": f"收入为42万元；乙为38万元\n{authored}。"})
            for claim in value.claims
        ]})
        return StructuredUtilityResult(value=value, model_label="test/model", repaired=False)

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_model)
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert calls == 4
    assert not planner.last_stage_contract_failures
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize("suffix", [
    "年的增长率为", "人的变化率为", "未知后缀的变化率为", "kg的变化率为",
])
def test_claim_planner_keeps_negative_rate_after_opaque_prose_suffix(monkeypatch, suffix):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n年份2025；变化率-17%",
        }) for section in brief.source_sections],
    })
    calls = 0

    async def fake_model(**kwargs):
        nonlocal calls
        payload = json.loads(kwargs["user_prompt"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached
        calls += 1
        assert kwargs["max_output_tokens"] == 8192
        assert not payload.get("retry_directive")
        value = _ledger(kwargs, payload)
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={
                "statement": f"收入为42万元；乙为38万元\n2025{suffix}-17%。",
            }) for claim in value.claims
        ]})
        return StructuredUtilityResult(value=value, model_label="test/model", repaired=False)

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_model)
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert calls == 4
    assert not planner.last_stage_contract_failures
    assert planner.estimated_cost_units(request) == 1400


@pytest.mark.parametrize("malformed", [
    "120万元-95万元理由=25万元",
    "120万元-95万元理由＝25万元",
    "120万元-95万元理由 =25万元",
    "120万元-95万元理由+5万元=30万元",
])
def test_claim_planner_rejects_prose_interrupted_equation(monkeypatch, malformed):
    request, brief = _long_case()
    brief = brief.model_copy(update={
        "source_sections": [section.model_copy(update={
            "title": f"{section.title}\n收入120万元；成本95万元；贡献25万元；费用5万元；贡献30万元",
        }) for section in brief.source_sections],
    })
    calls = 0

    async def fake_model(**kwargs):
        nonlocal calls
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached("prose-interrupted equation reached outline")
        calls += 1
        value = _ledger(kwargs, json.loads(kwargs["user_prompt"]))
        value = value.model_copy(update={"claims": [
            claim.model_copy(update={"statement": f"收入为42万元；乙为38万元\n{malformed}。"})
            for claim in value.claims
        ]})
        return StructuredUtilityResult(value=value, model_label="test/model", repaired=False)

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_model)
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(ValueError, match="uncited storyboard number -95"):
        planner.plan(request, brief)
    assert calls == 2
    assert len(planner.last_stage_contract_failures) == 2


def _cross_source_case(counts=(1, 1, 1)):
    request, brief = _long_case()
    source_text = [
        "甲收入42万元，预算40万元。材料给出总收入80万元，公式42+38=80万元。",
        "乙收入38万元，预算35万元。",
        "比较采用同一口径，不得估算缺失指标。",
    ]
    first_facts = ["甲收入42万元。", "乙收入38万元。", "比较采用同一口径。"]
    sections = []
    for index, (text, count) in enumerate(zip(source_text, counts)):
        facts = [first_facts[index]] + [
            "甲的独立事实" + chr(0x4E00 + offset)
            for offset in range(count - 1)
        ]
        sections.append(PresentationSourceSection(
            title="比较依据", source_ref=f"source-{index}",
            body=json.dumps({
                "material": text, "facts": facts,
                "background": "完整保留的定性背景。" * 700,
            }, ensure_ascii=False),
        ))
    return request, brief.model_copy(update={
        "source_sections": sections, "source_refs": brief.source_refs[:len(sections)],
        "required_facts": ["完整保留同一图表的收入与预算比较和已有合计公式"],
        "immutable_data": ["42", "38", "40", "35", "80"],
    })


def _cross_source_result(kwargs, payload, *, formula="42+38=80万元", chart_refs=None):
    if kwargs["purpose"] == "presentation-semantic-global-contracts":
        data = {
            "claims": [{
                "claim_id": "claim-001", "kind": "calculation",
                "statement": "甲乙收入合计80万元。", "calculation": formula,
                "source_refs": ["source-0", "source-1"],
                "delivery_contract_ids": [],
            }],
            "chart_contracts": [{
                "contract_id": "chart-001", "chart": {
                    "chart_type": "column", "categories": ["甲", "乙"],
                    "series": {"收入": [42, 38], "预算": [40, 35]},
                    "unit": "万元", "source_refs": chart_refs or ["source-0", "source-1"],
                },
            }],
        }
    else:
        section = payload["brief"]["source_sections"][0]
        data = {
            "claims": [{
                "claim_id": f"claim-{index:03d}", "statement": statement,
                "source_refs": [section["source_ref"]], "delivery_contract_ids": [],
            } for index, statement in enumerate(json.loads(section["body"])["facts"], 1)],
            "chart_contracts": [],
        }
    return StructuredUtilityResult(
        value=kwargs["result_model"].model_validate(data),
        model_label="test/model", repaired=False,
    )


@pytest.mark.parametrize("counts", [(1, 1, 1), (117, 1, 1)])
def test_global_contracts_preserve_multisource_chart_calculation_and_dense_facts(
    monkeypatch, counts,
):
    request, brief = _cross_source_case(counts)
    calls = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            ledger = payload["claim_ledger"]
            expected = [statement for section in brief.source_sections
                        for statement in json.loads(section.body)["facts"]]
            assert [claim["statement"] for claim in ledger["claims"][:-1]] == expected
            assert len(ledger["claims"]) == sum(counts) + 1 <= 120
            assert ledger["claims"][-1]["kind"] == "calculation"
            assert ledger["claims"][-1]["calculation"] == "42+38=80万元"
            assert ledger["chart_contracts"][0]["chart"]["series"] == {
                "收入": [42, 38], "预算": [40, 35],
            }
            assert ledger["chart_contracts"][0]["chart"]["source_refs"] == [
                "source-0", "source-1",
            ]
            raise OutlineReached
        if kwargs["purpose"] == "presentation-semantic-global-contracts":
            assert payload["brief"] == brief.model_dump(mode="json")
            assert payload["claim_count_ceiling"] == 120 - sum(counts)
            assert len(payload["approved_claim_ledger"]["claims"]) == sum(counts)
        return _cross_source_result(kwargs, payload)

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(OutlineReached):
        planner.plan(request, brief)
    assert calls[:-1] == ["presentation-semantic-claims"] * 3 + [
        "presentation-semantic-global-contracts",
    ]
    assert sum(count for key, count in planner.last_stage_attempt_counts.items()
               if key.startswith("claims")) == 4


@pytest.mark.parametrize("invalid", ["chart_scope", "invented_formula"])
def test_global_contracts_reject_uncited_chart_values_and_invented_formulas(
    monkeypatch, invalid,
):
    request, brief = _cross_source_case()
    calls = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached("invalid global evidence reached outline")
        return _cross_source_result(
            kwargs, payload,
            formula="42-38=80万元" if invalid == "invented_formula" else "42+38=80万元",
            chart_refs=["source-0"] if invalid == "chart_scope" else None,
        )

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    message = "uncited chart value 35" if invalid == "chart_scope" else "material-given formula"
    with pytest.raises(ValueError, match=message):
        planner.plan(request, brief)
    assert len(calls) == 5
    assert calls[-2:] == ["presentation-semantic-global-contracts"] * 2


def test_global_contracts_recompute_explicitly_authorized_cross_source_sum(monkeypatch):
    request, brief = _cross_source_case()
    authorization = "预算合计按甲预算加乙预算计算，不得估算缺失指标。"
    body = json.loads(brief.source_sections[2].body)
    body["material"] += authorization
    brief.source_sections[2].body = json.dumps(body, ensure_ascii=False)
    proof = {
        "terms": [
            {"source_ref": "source-0", "source_excerpt": "甲收入42万元，预算40万元。",
             "value": "40", "unit": "万元", "coefficient": 1},
            {"source_ref": "source-1", "source_excerpt": "乙收入38万元，预算35万元。",
             "value": "35", "unit": "万元", "coefficient": 1},
        ],
        "result": "75", "unit": "万元",
        "authorization_source_ref": "source-2", "authorization_excerpt": authorization,
    }
    calls = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            claim = payload["claim_ledger"]["claims"][-1]
            assert claim["statement"] == "甲乙预算合计75万元。"
            assert claim["calculation_proofs"] == [proof]
            assert claim["source_refs"] == ["source-0", "source-1", "source-2"]
            raise OutlineReached
        result = _cross_source_result(kwargs, payload)
        if kwargs["purpose"] == "presentation-semantic-global-contracts":
            data = result.value.model_dump(mode="json")
            data["claims"][0].update({
                "statement": "甲乙预算合计75万元。", "calculation": "40万元+35万元=75万元",
                "calculation_proofs": [proof],
                "source_refs": ["source-0", "source-1", "source-2"],
            })
            result = StructuredUtilityResult(
                value=kwargs["result_model"].model_validate(data), model_label="test/model", repaired=False,
            )
        return result

    monkeypatch.setattr("lantai.presentation.intelligence.run_structured_utility_task", fake_model)
    with pytest.raises(OutlineReached):
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)
    assert calls == ["presentation-semantic-claims"] * 3 + [
        "presentation-semantic-global-contracts", "presentation-semantic-outline",
    ]


def test_global_contract_truncation_does_not_replay_partial_output(monkeypatch):
    request, brief = _cross_source_case()
    calls = []

    async def fake_model(**kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached("truncated global output reached outline")
        if kwargs["purpose"] == "presentation-semantic-global-contracts":
            raise UtilityTaskIncompleteError(finish_reason="length")
        return _cross_source_result(kwargs, json.loads(kwargs["user_prompt"]))

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    with pytest.raises(UtilityTaskIncompleteError):
        planner.plan(request, brief)
    assert len(calls) == 4
    assert planner.last_stage_contract_failures == [{
        "stage": "claims:004", "attempt": 1, "violation_code": "output_truncated",
    }]


def test_full_claim_capacity_still_preserves_complete_global_charts(monkeypatch):
    request, brief = _cross_source_case((118, 1, 1))
    calls = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            ledger = payload["claim_ledger"]
            assert len(ledger["claims"]) == 120
            assert [claim["statement"] for claim in ledger["claims"]] == [
                statement for section in brief.source_sections
                for statement in json.loads(section.body)["facts"]
            ]
            assert ledger["chart_contracts"][0]["chart"]["series"] == {
                "收入": [42, 38], "预算": [40, 35],
            }
            raise OutlineReached
        result = _cross_source_result(kwargs, payload)
        if kwargs["purpose"] == "presentation-semantic-global-contracts":
            assert payload["claim_count_ceiling"] == 0
            data = result.value.model_dump(mode="json")
            data["claims"] = []
            result = StructuredUtilityResult(
                value=kwargs["result_model"].model_validate(data),
                model_label="test/model", repaired=False,
            )
        return result

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    with pytest.raises(OutlineReached):
        UtilityPresentationSemanticPlanner("agent-1").plan(request, brief)
    assert calls[:-1] == ["presentation-semantic-claims"] * 3 + [
        "presentation-semantic-global-contracts",
    ]


@pytest.mark.parametrize("invalid", ["capacity", "non_calculation", "unknown_field", "bad_id"])
def test_global_contract_delta_cannot_bypass_normal_ledger_contract(monkeypatch, invalid):
    request, brief = _cross_source_case((117, 1, 1))
    calls = []

    async def fake_model(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "presentation-semantic-outline":
            raise OutlineReached("invalid global delta reached outline")
        result = _cross_source_result(kwargs, payload)
        if kwargs["purpose"] == "presentation-semantic-global-contracts":
            data = result.value.model_dump(mode="json")
            if invalid == "capacity":
                data["claims"].append({**data["claims"][0], "claim_id": "claim-002"})
            elif invalid == "non_calculation":
                data["claims"][0]["kind"] = "fact"
            elif invalid == "unknown_field":
                data["unreviewed_fact"] = "不能绕过严格 schema"
            else:
                data["claims"][0]["claim_id"] = "claim-002"
            try:
                value = kwargs["result_model"].model_validate(data)
            except ValidationError as exc:
                raise StructuredTaskValidationError("failed schema validation") from exc
            result = StructuredUtilityResult(
                value=value, model_label="test/model", repaired=False,
            )
        return result

    monkeypatch.setattr(
        "lantai.presentation.intelligence.run_structured_utility_task", fake_model,
    )
    planner = UtilityPresentationSemanticPlanner("agent-1")
    expected_error = StructuredTaskValidationError if invalid == "unknown_field" else ValueError
    with pytest.raises(expected_error):
        planner.plan(request, brief)
    assert len(calls) == 5
    assert calls[-2:] == ["presentation-semantic-global-contracts"] * 2
