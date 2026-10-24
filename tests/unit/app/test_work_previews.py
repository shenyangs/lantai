from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from lantai.app.work_previews import get_or_create_work_preview
from lantai.app.works import add_work


def _add_pdf_work(workspace: Path, source: Path):
    return add_work(
        workspace,
        source_path=source,
        allowed_roots=[workspace],
        source_chat_id="chat-1",
        source_run_id="run-1",
    )


def test_pdf_preview_is_bound_to_managed_work_digest(tmp_path, monkeypatch):
    source = tmp_path / "deck.pdf"
    source.write_bytes(b"%PDF-1.4 fixture")
    item = _add_pdf_work(tmp_path, source)
    renders: list[Path] = []

    def fake_render(pdf_path, output_dir, **_kwargs):
        renders.append(Path(pdf_path))
        page = Path(output_dir) / "page-1.png"
        page.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), "white").save(page)
        return (page,)

    monkeypatch.setattr("lantai.app.work_previews.render_pdf_pages", fake_render)

    preview = get_or_create_work_preview(tmp_path, item.id)
    again = get_or_create_work_preview(tmp_path, item.id)

    assert preview.work_id == item.id
    assert preview.source_digest == item.source_digest
    assert preview.page_count == 1
    assert preview.pages[0].width == 640
    assert renders == [Path(item.path)]
    assert again == preview


def test_preview_cache_is_also_bound_to_renderer_version(tmp_path, monkeypatch):
    source = tmp_path / "deck.pdf"
    source.write_bytes(b"%PDF-1.4 fixture")
    item = _add_pdf_work(tmp_path, source)
    renders: list[str] = []
    renderer_version = ["pdfium fixture; profile=v1"]

    monkeypatch.setattr(
        "lantai.app.work_previews._preview_renderer_identity",
        lambda _kind: ("pdfium", renderer_version[0]),
    )

    def fake_render(_pdf_path, output_dir, **_kwargs):
        renders.append(renderer_version[0])
        page = Path(output_dir) / "page-1.png"
        page.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), "white").save(page)
        return (page,)

    monkeypatch.setattr("lantai.app.work_previews.render_pdf_pages", fake_render)

    first = get_or_create_work_preview(tmp_path, item.id)
    assert get_or_create_work_preview(tmp_path, item.id) == first
    renderer_version[0] = "pdfium fixture; profile=v2"
    second = get_or_create_work_preview(tmp_path, item.id)

    assert first.renderer_version.endswith("v1")
    assert second.renderer_version.endswith("v2")
    assert renders == [
        "pdfium fixture; profile=v1",
        "pdfium fixture; profile=v2",
    ]


def test_preview_refuses_modified_managed_bytes(tmp_path):
    source = tmp_path / "deck.pdf"
    source.write_bytes(b"%PDF-1.4 fixture")
    item = _add_pdf_work(tmp_path, source)
    Path(item.path).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="modified"):
        get_or_create_work_preview(tmp_path, item.id)


def test_preview_rejects_non_previewable_work_without_executing_it(tmp_path):
    source = tmp_path / "build-deck.cjs"
    source.write_text("throw new Error('must not execute')", encoding="utf-8")
    item = add_work(
        tmp_path,
        source_path=source,
        allowed_roots=[tmp_path],
        source_chat_id="chat-1",
        source_run_id="run-1",
    )

    with pytest.raises(ValueError, match="not previewable"):
        get_or_create_work_preview(tmp_path, item.id)
