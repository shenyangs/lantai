"""No numeric material fact can be derived from runtime-generated page ordinals."""
import pytest
from reportlab.pdfgen import canvas
from pypdf import PdfReader
from lantai.presentation.models import DeckPlanSlide, InputMaterial, SlideIntent
from lantai.presentation.planner import build_brief
from lantai.presentation.page_authoring import grounded_source_text, validate_grounded_numeric_text, validate_planned_text_sources
from tests.unit.presentation.test_material_conservation import capture, payload_for, request

@pytest.mark.parametrize("boundary", ["model_request", "planned_text", "authored_page_source"])
def test_actual_numeric_consumers_ignore_parser_generated_pdf_pagination(tmp_path, boundary, capture):
    path = tmp_path / "words.pdf"
    writer = canvas.Canvas(str(path), pageCompression=0)
    for text in ("The source contains words only.", "No numerical budget is provided."):
        writer.drawString(36, 756, text)
        writer.showPage()
    writer.save()
    extracted = "\n".join(page.extract_text() for page in PdfReader(str(path)).pages)
    assert not any(character.isdigit() for character in extracted)
    req = request([InputMaterial(id="pdf", name=path.name, path=str(path))])
    brief = build_brief(req)
    if boundary == "model_request":
        payload = payload_for(req, brief, capture)
        assert payload["allowed_numeric_tokens_by_source"]["pdf"] == []
    elif boundary == "planned_text":
        slide = DeckPlanSlide(order=1, title="Budget", intent=SlideIntent(
            purpose="Report the budget", key_message="Budget is 2 USD", source_refs=["pdf"]
        ), content_blocks=["Budget is 2 USD"])
        with pytest.raises(ValueError, match="uncited storyboard number"):
            validate_planned_text_sources([slide], brief)
    else:
        with pytest.raises(ValueError, match="uncited storyboard number"):
            validate_grounded_numeric_text("Budget is 2 USD", grounded_source_text(req, brief), location="isolated-page")
