"""Material conservation through the actual semantic planner request boundary.

Only model resolution is mocked; fixtures never call a real provider.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pydantic import ValidationError

from lantai.app import utility_model
from lantai.app.utility_model import ResolvedUtilityModel
from lantai.config.config import UtilityIntelligenceConfig
from lantai.presentation.intelligence import UtilityPresentationSemanticPlanner
from lantai.presentation.material_extraction import PresentationMaterialError, read_material
from lantai.presentation.models import InputMaterial, PresentationJobRequest
from lantai.presentation.planner import build_brief, build_deterministic_fixture_plan


class RequestCaptured(Exception):
    pass


@pytest.fixture
def model_capture(monkeypatch):
    calls = []

    async def capture(messages, **kwargs):
        calls.append((messages, kwargs))
        raise RequestCaptured("synthetic model boundary reached")

    monkeypatch.setattr(utility_model, "load_config", lambda: SimpleNamespace(
        utility_intelligence=UtilityIntelligenceConfig(mode="dedicated"),
    ))
    monkeypatch.setattr(utility_model, "resolve_utility_model", lambda config, agent_id:
        ResolvedUtilityModel(model=capture, provider_id="synthetic-capture", model_id="capture"),
    )
    return calls


def _request(materials, count=2):
    return PresentationJobRequest(
        conversation_id="private-lta013-014-request-capture",
        user_request="根据所有材料做决策汇报，保留预算及禁止事项。",
        slide_count=count,
        materials=materials,
    )


def _capture_payload(request, brief, calls):
    with pytest.raises(RequestCaptured):
        UtilityPresentationSemanticPlanner("synthetic-test-agent").plan(request, brief)
    assert len(calls) == 1
    messages, kwargs = calls[0]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert kwargs["max_tokens"] > 0
    payload = json.loads(messages[1]["content"])
    assert payload["brief"] == brief.model_dump(mode="json")
    assert payload["allowed_source_refs"] == brief.source_refs
    return payload


def _section_text(sections):
    return "\n".join(f"{s.title}\n{s.body}" for s in sections)


@pytest.mark.parametrize("text", [
    "范围只限现有客户。\n# 项目汇报\n预算上限27万元。禁止上传外部云盘。\n## 实施\n只做本地试点。",
    "预算上限27万元。禁止上传外部云盘。\n## 实施\n只做本地试点。",
    "# 项目汇报\n预算上限27万元。禁止上传外部云盘。\n### 实施\n试点证据。\n#### 边界\n只做本地试点。",
    "# 项目汇报\n预算上限27万元。禁止上传外部云盘。\n## \n只做本地试点。\n## 实施\n试点证据。",
    "预算上限27万元。禁止上传外部云盘。\n只做本地试点。",
], ids=["before-and-after-h1", "without-h1", "nested-headings", "empty-heading", "no-headings"])
def test_preamble_constraints_reach_production_model_call_and_fixture_plan(text, model_capture):
    material = InputMaterial(id="scope", name="scope.md", content=text)
    request = _request([material])
    brief = build_brief(request)
    payload = _capture_payload(request, brief, model_capture)
    raw_source = _section_text(brief.source_sections)
    for constraint in ["预算上限27万元", "禁止上传外部云盘", "只做本地试点"]:
        assert constraint in raw_source
        assert constraint in json.dumps(payload["brief"], ensure_ascii=False)
    assert payload["brief"]["source_coverage"][0]["extracted_chars"] == len(text)
    plan = build_deterministic_fixture_plan(request, brief)
    final_text = "\n".join(slide.title + "\n" + "\n".join(slide.content_blocks) for slide in plan.slides)
    for constraint in ["预算上限27万元", "禁止上传外部云盘", "只做本地试点"]:
        assert constraint in final_text
    assert all(slide.intent.source_refs == ["scope"] for slide in plan.slides)


@pytest.mark.parametrize("ceiling", [1, 2, 10])
def test_multiple_sources_keep_preambles_and_provenance_when_page_budget_changes(ceiling, model_capture):
    materials = [
        InputMaterial(id="budget", name="budget.md", content="预算上限27万元。\n# 预算依据\n## 范围\n只做本地试点。"),
        InputMaterial(id="privacy", name="privacy.md", content="禁止上传外部云盘。\n## 范围\n客户仅授权本地读取。"),
    ]
    request = _request(materials, count=ceiling)
    brief = build_brief(request)
    payload = _capture_payload(request, brief, model_capture)
    sections = payload["brief"]["source_sections"]
    assert any("预算上限27万元" in s["body"] and s["source_ref"] == "budget" for s in sections)
    assert any("禁止上传外部云盘" in s["body"] and s["source_ref"] == "privacy" for s in sections)
    plan = build_deterministic_fixture_plan(request, brief)
    assert len(plan.slides) <= ceiling
    assert {ref for slide in plan.slides for ref in slide.intent.source_refs} == {"budget", "privacy"}
    combined = "\n".join(block for slide in plan.slides for block in slide.content_blocks)
    assert "预算上限27万元" in combined
    assert "禁止上传外部云盘" in combined
    assert combined.count("预算上限27万元") == 1
    assert combined.count("禁止上传外部云盘") == 1


@pytest.mark.parametrize("text, constraints", [
    ("# 项目汇报\n## 预算上限27万元\n## 禁止上传外部云盘\n## 实施\n只做本地试点。", ["预算上限27万元", "禁止上传外部云盘"]),
    ("# 项目汇报\n## " + "范围说明" * 20 + "预算上限27万元\n只做本地试点。", ["预算上限27万元"]),
], ids=["heading-only-constraints", "long-heading-constraint-tail"])
def test_constraints_in_headings_are_not_erased_before_model_request(text, constraints, model_capture):
    request = _request([InputMaterial(id="headings", name="constraints.md", content=text)])
    brief = build_brief(request)
    payload = _capture_payload(request, brief, model_capture)
    transmitted = json.dumps(payload["brief"], ensure_ascii=False)
    for constraint in constraints:
        assert constraint in transmitted, "source constraint absent from actual model request"


@pytest.mark.parametrize("count", [1, 2, 10])
def test_each_material_keeps_document_heading_constraints(count, model_capture):
    materials = [
        InputMaterial(id="budget", name="budget.md", content="# 预算上限27万元\n## 实施\n先做试点。"),
        InputMaterial(id="privacy", name="privacy.md", content="# 禁止上传外部云盘\n## 实施\n本地读取。"),
    ]
    request = _request(materials, count=count)
    brief = build_brief(request)
    payload = _capture_payload(request, brief, model_capture)
    for source, constraint in [("budget", "预算上限27万元"), ("privacy", "禁止上传外部云盘")]:
        sections = [section for section in payload["brief"]["source_sections"] if section["source_ref"] == source]
        assert constraint in json.dumps(sections, ensure_ascii=False), "document heading lost from its source context"


def test_consecutive_document_headings_keep_both_constraints(model_capture):
    request = _request([InputMaterial(id="scope", name="scope.md", content="# 预算上限27万元\n# 禁止上传外部云盘\n## 实施\n本地读取。")])
    brief = build_brief(request)
    payload = _capture_payload(request, brief, model_capture)
    transmitted = json.dumps(payload["brief"], ensure_ascii=False)
    assert "预算上限27万元" in transmitted
    assert "禁止上传外部云盘" in transmitted


@pytest.mark.parametrize("count", [1, 2, 10])
@pytest.mark.parametrize("storage_suffix", [".pdf", ".txt"])
def test_pdf_hash_table_headers_keep_source_role_and_order(
    tmp_path, count, storage_suffix, model_capture,
):
    from reportlab.pdfgen import canvas

    path = tmp_path / ("decision-report" + storage_suffix)
    writer = canvas.Canvas(str(path), pageCompression=0)
    for index, line in enumerate([
        "Decision report: compare alternatives before authorization.",
        "# Section Section Description",
        "1 Scope Applies to the current decision only.",
        "## Reference Code",
        "2 Evidence Confirm assumptions before baselining.",
        "### Required Conditions",
        "3 Approval A small project is not automatically exempt.",
    ]):
        writer.drawString(36, 756 - index * 24, line)
    writer.showPage()
    writer.save()
    material = InputMaterial(
        id="decision-report", name="decision-report.pdf", path=str(path),
        media_type="application/pdf",
    )
    extracted, coverage = read_material(material)
    assert coverage.method.startswith("pdf_")
    assert "# Section Section Description" in extracted
    request = _request([material], count=count)
    brief = build_brief(request)
    assert brief.source_title == "", "PDF table columns are not document titles"
    assert len(brief.source_sections) == 1
    section = brief.source_sections[0]
    assert section.body == extracted
    assert section.document_title == ""
    assert section.source_ref == material.id
    payload = _capture_payload(request, brief, model_capture)
    assert payload["brief"]["source_sections"][0]["body"] == extracted
    assert payload["brief"]["source_coverage"][0] == coverage.model_dump(mode="json")


@pytest.mark.parametrize("format", ["markdown", "docx"])
def test_authored_document_headings_keep_existing_meaning(tmp_path, format, model_capture):
    if format == "markdown":
        material = InputMaterial(
            id="authored", name="decision.md",
            content="# Decision Report\n## Scope\nConfirm assumptions before baselining.",
        )
    else:
        path = tmp_path / "decision.docx"
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                '<w:r><w:t>Decision Report</w:t></w:r></w:p>'
                '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
                '<w:r><w:t>Scope</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>Confirm assumptions before baselining.</w:t></w:r></w:p>'
                '</w:body></w:document>'
            ))
        material = InputMaterial(id="authored", name=path.name, path=str(path))
    request = _request([material])
    brief = build_brief(request)
    assert brief.source_title == "Decision Report"
    assert len(brief.source_sections) == 1
    assert brief.source_sections[0].title == "Scope"
    assert brief.source_sections[0].document_title == "Decision Report"
    assert brief.source_sections[0].body == "Confirm assumptions before baselining."
    payload = _capture_payload(request, brief, model_capture)
    assert payload["brief"]["source_title"] == "Decision Report"


def _docx(path: Path, text: str, xml_length: int | None = None):
    prefix = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    paragraph = "<w:p><w:r><w:t>" + escape(text) + "</w:t></w:r></w:p>"
    suffix = "</w:body></w:document>"
    document = (prefix + paragraph + suffix).encode("utf-8")
    if xml_length is not None:
        padding = xml_length - len(document) - len(b"<!---->")
        assert padding >= 0
        document = (prefix + paragraph).encode("utf-8") + b"<!--" + b"x" * padding + b"-->" + suffix.encode("utf-8")
        assert len(document) == xml_length
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)


@pytest.mark.parametrize("kind", ["inline", "text", "docx", "pdf"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_character_boundary_keeps_tail_or_explicitly_rejects_before_model(kind, delta, tmp_path, monkeypatch, model_capture):
    cap = 1_000_000
    tail = "预算上限27万元。禁止上传外部云盘。"
    text = "a" * (cap + delta - len(tail)) + tail
    assert len(text) == cap + delta
    if kind == "inline":
        if delta > 0:
            with pytest.raises(ValidationError, match="string_too_long"):
                InputMaterial(id="boundary", name="inline.md", content=text)
            assert model_capture == []
            return
        material = InputMaterial(id="boundary", name="inline.md", content=text)
    else:
        path = tmp_path / ("boundary." + {"text": "txt", "docx": "docx", "pdf": "pdf"}[kind])
        if kind == "text":
            path.write_text(text, encoding="utf-8")
        elif kind == "docx":
            _docx(path, text)
        else:
            path.write_bytes(b"%PDF-synthetic-reader-boundary\n")
            monkeypatch.setattr("lantai.app.wiki.core.extract_source_text_detail", lambda path, **kwargs: {
                "status": "text_extracted", "text": text, "method": "synthetic_reader", "page_count": 1,
                "text_complete": True, "extracted_pages": 1,
            })
        material = InputMaterial(id="boundary", name=path.name, path=str(path))
    request = _request([material], count=1)
    if delta > 0:
        with pytest.raises(PresentationMaterialError, match="extraction limit"):
            build_brief(request)
        assert model_capture == []
        return
    extracted, coverage = read_material(material)
    assert extracted == text
    assert coverage.extracted_chars == len(text)
    if kind == "inline":
        assert coverage.source_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    else:
        assert coverage.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    brief = build_brief(request)
    payload = _capture_payload(request, brief, model_capture)
    assert payload["brief"]["source_sections"][0]["body"] == text
    assert payload["brief"]["source_sections"][0]["body"].endswith(tail)
    assert payload["brief"]["source_coverage"][0]["extracted_chars"] == len(text)


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_docx_xml_byte_boundary_is_explicit(tmp_path, delta, model_capture):
    text = "预算上限27万元。禁止上传外部云盘。"
    path = tmp_path / "xml-boundary.docx"
    _docx(path, text, xml_length=16_000_000 + delta)
    material = InputMaterial(id="xml-boundary", name=path.name, path=str(path))
    request = _request([material], count=1)
    if delta > 0:
        with pytest.raises(PresentationMaterialError, match="exceeds"):
            build_brief(request)
        assert model_capture == []
    else:
        brief = build_brief(request)
        payload = _capture_payload(request, brief, model_capture)
        assert payload["brief"]["source_sections"][0]["body"] == text


@pytest.mark.parametrize("kind", ["invalid-utf8", "bad-docx-xml", "duplicate-docx-member", "bad-pdf-status"])
def test_bad_formats_fail_before_model_call(tmp_path, monkeypatch, kind, model_capture):
    if kind == "invalid-utf8":
        path = tmp_path / "bad.txt"
        path.write_bytes(b"\xff\xfe")
    elif kind == "bad-docx-xml":
        path = tmp_path / "bad.docx"
        with ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", b"<invalid")
    elif kind == "duplicate-docx-member":
        path = tmp_path / "duplicate.docx"
        with pytest.warns(UserWarning, match="Duplicate name"):
            with ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", b"<invalid")
                archive.writestr("word/document.xml", b"<invalid")
    else:
        path = tmp_path / "bad.pdf"
        path.write_bytes(b"%PDF-synthetic-reader-boundary\n")
        monkeypatch.setattr("lantai.app.wiki.core.extract_source_text_detail", lambda path, **kwargs: {
            "status": "ocr_extracted", "text": "unverified synthetic text", "method": "synthetic",
        })
    with pytest.raises(PresentationMaterialError):
        build_brief(_request([InputMaterial(id="bad-format", name=path.name, path=str(path))]))
    assert model_capture == []
