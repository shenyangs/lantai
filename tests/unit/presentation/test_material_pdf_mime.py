"""Declared PDF MIME must not certify a differently parsed text source."""
import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from lantai.presentation.material_extraction import PresentationMaterialError, read_material
from lantai.presentation.models import InputMaterial


def test_declared_pdf_mime_does_not_certify_plain_text_as_extracted_pdf(tmp_path):
    path = tmp_path / "scope.txt"
    path.write_text("预算上限27万元。禁止上传外部云盘。", encoding="utf-8")
    material = InputMaterial(id="scope", name="scope.pdf", path=str(path), media_type="application/pdf")
    with pytest.raises(PresentationMaterialError):
        read_material(material)


def test_real_pdf_declared_by_mime_reads_pdf_bytes_despite_storage_suffix(tmp_path):
    path = tmp_path / "scope.txt"
    writer = canvas.Canvas(str(path), pageCompression=0)
    writer.drawString(36, 756, "BUDGET_CEILING_27_USD")
    writer.showPage()
    writer.save()
    expected_text = PdfReader(str(path)).pages[0].extract_text().strip()
    material = InputMaterial(id="scope", name="scope.pdf", path=str(path), media_type="application/pdf")
    text, coverage = read_material(material)
    assert "BUDGET_CEILING_27_USD" in text
    assert text.strip().endswith(expected_text)
    assert "%PDF-" not in text
    assert coverage.method.startswith("pdf_")
