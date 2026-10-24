"""Parser-generated PDF page metadata cannot authorize invented chart data."""
import sys
from unittest.mock import MagicMock
import pytest
from reportlab.pdfgen import canvas
from pypdf import PdfReader
for dependency in ("aibot", "lark_oapi"):
    sys.modules.setdefault(dependency, MagicMock())
from lantai.presentation.models import InputMaterial, ChartObject, DeckPlanSlide, SlideIntent
from lantai.presentation.page_authoring import validate_planned_chart_sources
from lantai.presentation.planner import build_brief
from lantai.presentation.models import PresentationJobRequest

def request(materials):
    return PresentationJobRequest(conversation_id="pdf-metadata-lock", user_request="按材料如实做报告", slide_count=2, materials=materials)


def test_pdf_page_number_metadata_cannot_ground_an_invented_chart_value(tmp_path):
    path = tmp_path / "words.pdf"
    writer = canvas.Canvas(str(path), pageCompression=0)
    for text in ("The source contains words only.", "No numerical budget is provided."):
        writer.drawString(36, 756, text)
        writer.showPage()
    writer.save()
    extracted = "\n".join(page.extract_text() for page in PdfReader(str(path)).pages)
    assert not any(character.isdigit() for character in extracted)
    brief = build_brief(request([InputMaterial(id="pdf", name=path.name, path=str(path))]))
    assert all(not any(character.isdigit() for character in section.body) for section in brief.source_sections)
    slide = DeckPlanSlide(order=1, title="Budget", intent=SlideIntent(
        purpose="Report a factual budget", key_message="The budget is two dollars", source_refs=["pdf"],
    ), charts=[ChartObject(id="budget", chart_type="bar", categories=["Budget"],
        series={"USD": [2]}, source_refs=["pdf"], unit="USD", derivation="Budget stated by the document")])
    with pytest.raises(ValueError, match="uncited chart value"):
        validate_planned_chart_sources([slide], brief)


def test_literal_pdf_numeric_heading_remains_valid_source_evidence(tmp_path):
    path = tmp_path / "literal-heading.pdf"
    writer = canvas.Canvas(str(path), pageCompression=0)
    writer.drawString(36, 756, "## Page 2")
    writer.drawString(36, 736, "The source title explicitly specifies the count.")
    writer.showPage()
    writer.save()
    brief = build_brief(request([InputMaterial(id="pdf", name=path.name, path=str(path))]))
    assert any("Page 2" in section.grounded_text for section in brief.source_sections)
    slide = DeckPlanSlide(order=1, title="Count", intent=SlideIntent(
        purpose="Report the stated count", key_message="The source specifies two", source_refs=["pdf"],
    ), charts=[ChartObject(id="count", chart_type="bar", categories=["Count"],
        series={"Count": [2]}, source_refs=["pdf"], unit="count", derivation="Count explicitly written in the title")])
    validate_planned_chart_sources([slide], brief)
