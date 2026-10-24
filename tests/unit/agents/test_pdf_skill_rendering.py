from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from PIL import Image
from reportlab.pdfgen import canvas

from lantai.presentation.pdf_render import render_pdf_pages

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "src" / "lantai" / "agents" / "skills"


def _load_renderer(locale: str):
    path = SKILLS / locale / "scripts" / "render_pages.py"
    spec = importlib.util.spec_from_file_location(
        f"{locale.replace('-', '_')}_pdf_converter",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_pdf_skill_rendering_uses_bundled_pdfium_without_system_poppler(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "one-page.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(72, 720, "Lantai bundled PDFium")
    pdf.showPage()
    pdf.drawString(72, 720, "Lantai bundled PDFium page two")
    pdf.save()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    for locale in ("pdf-zh", "pdf-en"):
        output = tmp_path / locale
        output.mkdir()
        renderer = _load_renderer(locale)

        converted = renderer.render(source, output)

        assert len(converted) == 2
        for page in converted:
            with Image.open(page) as image:
                assert image.format == "PNG"
                assert image.width > 0
                assert image.height > 0


def test_shared_pdfium_renderer_is_the_single_behavioral_seam(
    tmp_path: Path,
) -> None:
    source = tmp_path / "shared.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(72, 720, "shared renderer")
    pdf.save()

    rendered = render_pdf_pages(
        source,
        tmp_path / "shared",
        dpi=144,
        prefix="slide-",
        zero_pad=2,
        image_format="JPEG",
    )

    assert [path.name for path in rendered] == ["slide-01.jpg"]
    with Image.open(rendered[0]) as image:
        assert image.format == "JPEG"


def test_pptx_visual_qa_uses_centralized_bundled_pdfium() -> None:
    for locale in ("pptx-zh", "pptx-en"):
        source = (SKILLS / locale / "references" / "runtime-boundary.md").read_text(
            encoding="utf-8",
        )
        assert "lantai.presentation.render" in source
        assert not (SKILLS / locale / "scripts" / "thumbnail.py").exists()

    stable_render = (ROOT / "src" / "lantai" / "presentation" / "render.py").read_text(
        encoding="utf-8",
    )
    assert "pdf2image" not in stable_render
    assert "pdftoppm" not in stable_render
    assert "render_pdf_pages" in stable_render
    shared_render = (
        ROOT / "src" / "lantai" / "presentation" / "pdf_render.py"
    ).read_text(encoding="utf-8")
    assert "pypdfium2" in shared_render
