"""Caller authority may correct generated locks, never erase source evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lantai.app.routers import presentations as api
from lantai.presentation import models
from lantai.presentation import natural_edit
from lantai.presentation.models import (
    DeckIR, DeckPlan, DeckPlanSlide, DeckSlide, FactRecord, PresentationBrief,
    PresentationObjectEditPlan, PresentationSourceCoverage,
    PresentationSourceSection, SlideIntent, TextObject, VisualDirection,
)
from lantai.presentation.versioning import deck_ir_matches_version, deck_ir_version


OLD = "负责人选择方案B。"
NEW = "负责人倾向方案B，但尚未批准。"
REF = "source-workshop"
BODY = NEW + "\n投入为两周。"
SHA = hashlib.sha256(BODY.encode()).hexdigest()


def _fixture():
    coverage = PresentationSourceCoverage(
        material_id=REF, source_name="Synthetic workshop", source_sha256=SHA,
        method="inline", status="supported_content_extracted", extracted_chars=len(BODY),
    )
    brief = PresentationBrief(
        objective="来源限定", audience="团队", narrative="合成资料核对",
        required_facts=["投入为两周。"], immutable_data=["两周"], source_refs=[REF],
        source_sections=[PresentationSourceSection(title="讨论", body=BODY, source_ref=REF)],
        source_coverage=[coverage],
    )
    deck = DeckIR(
        id="source-lock", title="来源限定", source_refs=[REF], source_coverage=[coverage],
        required_facts=[OLD, "投入为两周。"], immutable_data=["两周"],
        facts=[FactRecord(fact_id="claim", text=OLD, source_refs=[REF]),
               FactRecord(fact_id="compound", text="原讨论：" + OLD, source_refs=[REF]),
               FactRecord(fact_id="fixed", text="投入为两周。", source_refs=[REF], immutable=True)],
        slides=[DeckSlide(id="page-a", intent_id="intent-a", layout_id="freeform",
                          source_fact_ids=["claim", "compound"],
                          objects=[TextObject(id="title-a", role="title", text=OLD)]),
                DeckSlide(id="page-b", intent_id="intent-b", layout_id="freeform",
                          source_fact_ids=["fixed"],
                          objects=[TextObject(id="title-b", role="title", text="投入为两周。")])],
    )
    plan = DeckPlan(
        title=deck.title, narrative_arc="核对原材料", source_coverage=[coverage],
        visual_direction=VisualDirection(visual_concept="核对", palette=["111111", "FFFFFF"],
            typography="清晰", image_language="无", chart_language="原生", shape_language="克制",
            grid="十二列", recurring_motif="细线"),
        slides=[DeckPlanSlide(order=1, title="讨论", content_blocks=[OLD],
            intent=SlideIntent(id="intent-a", purpose="核对", key_message="讨论方案B",
                evidence=["原讨论：" + OLD], required_facts=[OLD], source_refs=[REF])),
                DeckPlanSlide(order=2, title="投入",
            intent=SlideIntent(id="intent-b", purpose="核对", key_message="两周",
                required_facts=["投入为两周。"], immutable_facts=["两周"], source_refs=[REF]))],
    )
    return deck, plan, brief


def _request(**updates):
    return {"original_fact": OLD, "corrected_fact": NEW, "source_ref": REF,
            "source_sha256": SHA, "source_section_sha256": SHA, **updates}


def _apply(deck, plan, brief, requests, scope=None):
    return natural_edit.apply_source_fact_corrections(
        deck, plan, brief, requests, allowed_slide_ids=scope or ["page-a"],
    )


def test_caller_source_correction_preserves_every_lock_and_stable_identity():
    deck, plan, brief = _fixture()
    before = deck.model_dump(mode="json"), plan.model_dump(mode="json"), brief.model_dump(mode="json")
    changed, changed_plan = _apply(deck, plan, brief, [_request()])
    assert changed.required_facts == [NEW, "投入为两周。"]
    assert len(changed.required_facts) == len(deck.required_facts)
    assert changed.immutable_data == deck.immutable_data
    assert [fact.fact_id for fact in changed.facts] == [fact.fact_id for fact in deck.facts]
    assert [fact.source_refs for fact in changed.facts] == [fact.source_refs for fact in deck.facts]
    assert [fact.text for fact in changed.facts] == [NEW, "原讨论：" + NEW, "投入为两周。"]
    assert changed.slides == deck.slides  # Provenance is not a fake native edit.
    assert changed_plan.slides[0].intent.required_facts == [NEW]
    assert changed_plan.slides[0].intent.evidence == ["原讨论：" + NEW]
    assert changed_plan.slides[0].content_blocks == [NEW]
    assert changed_plan.slides[0].intent.key_message == plan.slides[0].intent.key_message
    assert changed_plan.slides[1] == plan.slides[1]
    assert len(changed.source_fact_corrections) == 1
    record = changed.source_fact_corrections[0]
    assert record.correction.original_fact == OLD and record.correction.corrected_fact == NEW
    assert record.original_fact_texts == {"claim": OLD, "compound": "原讨论：" + OLD}
    assert record.slide_ids == ["page-a"]
    natural_edit.validate_source_fact_correction_history(changed, brief)
    assert before == (deck.model_dump(mode="json"), plan.model_dump(mode="json"), brief.model_dump(mode="json"))


@pytest.mark.parametrize("updates", [
    {"original_fact": "未登记的事实"}, {"corrected_fact": "已经批准方案B。"},
    {"corrected_fact": OLD}, {"source_ref": "invented-ref"},
    {"source_sha256": "b" * 64}, {"source_section_sha256": "c" * 64},
])
def test_correction_rejects_unknown_lock_invented_source_or_stale_hash(updates):
    deck, plan, brief = _fixture()
    before = deck.model_dump_json(), plan.model_dump_json(), brief.model_dump_json()
    with pytest.raises(ValueError, match="source_fact_correction"):
        _apply(deck, plan, brief, [_request(**updates)])
    assert before == (deck.model_dump_json(), plan.model_dump_json(), brief.model_dump_json())


@pytest.mark.parametrize("protected", ["brief", "immutable_fact", "immutable_intent", "immutable_data"])
def test_correction_cannot_override_user_or_immutable_constraints(protected):
    deck, plan, brief = _fixture()
    if protected == "brief":
        brief.required_facts.append(OLD)
    elif protected == "immutable_fact":
        deck.facts[0].immutable = True
    elif protected == "immutable_intent":
        plan.slides[0].intent.immutable_facts.append(OLD)
    else:
        deck.immutable_data.append(OLD)
    with pytest.raises(ValueError, match="source_fact_correction"):
        _apply(deck, plan, brief, [_request()])


@pytest.mark.parametrize("failure", ["scope", "duplicate", "eleven", "converted", "empty_catalog", "foreign_catalog_ref"])
def test_correction_is_bounded_source_bound_and_caller_scoped(failure):
    deck, plan, brief = _fixture()
    requests, scope = [_request()], ["page-a"]
    if failure == "scope":
        deck.slides[1].source_fact_ids.append("claim")
    elif failure == "duplicate":
        requests *= 2
    elif failure == "eleven":
        requests *= 11
    elif failure == "converted":
        brief.source_coverage[0].status = "converted_doc_unverified"
    elif failure == "empty_catalog":
        deck.facts[0].text = deck.facts[1].text = "无关证据"
    else:
        deck.facts[1].source_refs = ["foreign"]
    with pytest.raises(ValueError, match="source_fact_correction"):
        _apply(deck, plan, brief, requests, scope)


@pytest.mark.parametrize("tamper", [
    "replacement", "index", "catalog", "scope", "pin", "original",
    "omitted_catalog", "other_lock", "catalog_identity", "immutable",
])
def test_persisted_correction_is_rechecked_not_trusted_by_flag(tamper):
    deck, plan, brief = _fixture()
    changed, _ = _apply(deck, plan, brief, [_request()])
    data = changed.model_dump(mode="json")
    record = data["source_fact_corrections"][0]
    if tamper == "replacement":
        data["required_facts"][0] = "模型自行放宽约束"
    elif tamper == "index":
        record["fact_index"] = 1
    elif tamper == "catalog":
        data["facts"][1]["text"] = "被擦除的上下文"
    elif tamper == "scope":
        record["slide_ids"] = ["missing-page"]
    elif tamper == "pin":
        record["correction"]["source_sha256"] = "e" * 64
    elif tamper == "original":
        record["original_fact_texts"]["claim"] = "伪造的旧事实"
    elif tamper == "omitted_catalog":
        record["original_fact_texts"].pop("compound")
    elif tamper == "other_lock":
        data["required_facts"].pop()
    elif tamper == "catalog_identity":
        data["facts"][1]["source_refs"] = ["foreign"]
    else:
        data["facts"][0]["immutable"] = True
    with pytest.raises(ValueError, match="source_fact_correction"):
        natural_edit.validate_source_fact_correction_history(DeckIR.model_validate(data), brief)


def test_model_cannot_self_authorize_source_lock_changes():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PresentationObjectEditPlan.model_validate({
            "slide_id": "page-a", "actions": [{"kind": "replace_text", "object_id": "title-a", "text": NEW}],
            "source_fact_corrections": [_request()],
        })


@pytest.mark.parametrize("alias", [False, True])
def test_explicit_correction_survives_api_boundary_without_model_authority(monkeypatch, alias):
    calls = []

    async def runtime(_request):
        return SimpleNamespace(redo_slide=lambda *a, **k: calls.append((a, k)) or {"status": "completed"})

    monkeypatch.setattr(api, "_runtime", runtime)
    payload = api.SlideRedo(conversation_id="owner", slide_id="page-a", instruction="按原资料修订",
        base_output_sha256="a" * 64, source_fact_corrections=[_request()])
    assert asyncio.run((api.edit_slide if alias else api.redo_slide)("job", payload, SimpleNamespace())) == {"status": "completed"}
    assert [item.model_dump(mode="json") for item in calls[0][1]["source_fact_corrections"]] == [_request()]


def test_correction_schema_cannot_delete_facts_or_overrun_api_limit():
    with pytest.raises(ValidationError):
        models.SourceFactCorrection.model_validate(_request(corrected_fact=""))
    with pytest.raises(ValidationError):
        api.SlideRedo(conversation_id="owner", slide_id="page-a", instruction="过宽",
            base_output_sha256="a" * 64, source_fact_corrections=[_request()] * 11)


def test_empty_correction_default_preserves_historical_version_fingerprint():
    deck, _, _ = _fixture()
    # Reconstruct the exact pre-field canonical payload, not a changed golden fixture.
    payload = deck.model_dump(mode="json")
    payload.pop("source_fact_corrections", None)
    original = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    assert deck_ir_version(deck) == original
    assert deck_ir_matches_version(deck, original)
    changed, _ = _apply(deck, None, _fixture()[2], [_request()])
    assert deck_ir_version(changed) != original
    assert not deck_ir_matches_version(changed, original)


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_neutral_correction_field_is_absent_from_all_persisted_deck_serializations(explicit_empty):
    deck, _, brief = _fixture()
    if explicit_empty:
        deck.source_fact_corrections = []
    for candidate in (deck, deck.model_copy(deep=True),
                      DeckIR.model_validate_json(deck.model_dump_json())):
        assert "source_fact_corrections" not in candidate.model_dump(mode="python")
        assert "source_fact_corrections" not in candidate.model_dump(mode="json")
        assert "source_fact_corrections" not in json.loads(candidate.model_dump_json())
        assert candidate.source_fact_corrections == []
    changed, _ = _apply(deck, None, brief, [_request()])
    for candidate in (changed, DeckIR.model_validate_json(changed.model_dump_json())):
        assert len(candidate.model_dump(mode="json")["source_fact_corrections"]) == 1
        assert candidate.source_fact_corrections == changed.source_fact_corrections
        natural_edit.validate_source_fact_correction_history(candidate, brief)
        assert deck_ir_version(candidate) == deck_ir_version(changed)


@pytest.mark.parametrize("field", ["original_fact", "corrected_fact", "source_ref"])
@pytest.mark.parametrize("value", [" ", "\t\n"])
def test_caller_correction_cannot_replace_evidence_with_whitespace(field, value):
    with pytest.raises(ValidationError):
        models.SourceFactCorrection.model_validate(_request(**{field: value}))


def test_correction_chain_replays_without_losing_any_original_evidence():
    deck, plan, brief = _fixture()
    next_fact = "负责人倾向方案B，决策仍然待定。"
    brief.source_sections[0].body += "\n" + next_fact
    digest = hashlib.sha256(brief.source_sections[0].body.encode()).hexdigest()
    deck.source_coverage[0].source_sha256 = brief.source_coverage[0].source_sha256 = digest
    first = _request(source_sha256=digest, source_section_sha256=digest)
    second = _request(original_fact=NEW, corrected_fact=next_fact,
                      source_sha256=digest, source_section_sha256=digest)
    changed, changed_plan = _apply(deck, plan, brief, [first, second])
    assert changed.required_facts == [next_fact, "投入为两周。"]
    assert len(changed.facts) == 3 and len(changed.source_fact_corrections) == 2
    assert changed.source_fact_corrections[0].original_fact_texts["claim"] == OLD
    assert changed.source_fact_corrections[1].original_fact_texts["claim"] == NEW
    assert changed_plan.slides[0].intent.required_facts == [next_fact]
    natural_edit.validate_source_fact_correction_history(changed, brief)
