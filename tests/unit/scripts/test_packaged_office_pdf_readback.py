# -*- coding: utf-8 -*-
from __future__ import annotations

# These behavior locks intentionally inspect private harness operations.
# pylint: disable=protected-access

import importlib.util
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)


def _module():
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "packaged_office_edit_roundtrip",
        root / "scripts/packaged_office_edit_roundtrip.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pdf(
    path: Path,
    *,
    active: bool,
    appearance: bool = True,
    hidden: bool = False,
    matrix: bool = False,
):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Square"),
            NameObject("/Rect"): ArrayObject(
                [FloatObject(v) for v in (97, 197, 223, 273)],
            ),
            NameObject("/C"): ArrayObject([FloatObject(v) for v in (1, 0, 0)]),
            NameObject("/BS"): DictionaryObject(
                {NameObject("/W"): NumberObject(1)},
            ),
        },
    )
    if appearance:
        stream = DecodedStreamObject()
        stream.set_data(b"1 w 1 J 1 j 1 0 0 RG 100 200 120 70 re S")
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): annotation["/Rect"],
            },
        )
        if matrix:
            stream[NameObject("/Matrix")] = ArrayObject(
                [NumberObject(v) for v in (1, 0, 0, 1, 300, 400)],
            )
        annotation[NameObject("/AP")] = DictionaryObject(
            {NameObject("/N"): writer._add_object(stream.flate_encode())},
        )
    if hidden:
        annotation[NameObject("/F")] = NumberObject(2)
    reference = writer._add_object(annotation)
    if active:
        page[NameObject("/Annots")] = ArrayObject([reference])
    writer.write(path)


def test_pdf_count_ignores_unreachable_annotation_objects(tmp_path):
    path = tmp_path / "unreachable.pdf"
    _pdf(path, active=False)
    assert _module()._pdf_square_annotation_count(path) == 0


def test_active_compressed_appearance_proves_drawn_rectangle(tmp_path):
    path = tmp_path / "active.pdf"
    _pdf(path, active=True)
    result = _module()._pdf_annotation_readback(path)
    assert result["square_annotation_count"] == 1
    annotation = result["annotations"][0]
    assert annotation["drawn_rect"] == [100.0, 200.0, 220.0, 270.0]
    assert annotation["color"] == [1.0, 0.0, 0.0]
    assert annotation["page_index"] == 0
    assert annotation["page_cropbox"] == [0.0, 0.0, 600.0, 800.0]
    assert annotation["appearance_sha256"]


def test_square_without_saved_appearance_is_not_visible_annotation_proof(
    tmp_path,
):
    path = tmp_path / "no-appearance.pdf"
    _pdf(path, active=True, appearance=False)
    with pytest.raises(AssertionError, match="appearance"):
        _module()._pdf_annotation_readback(path)


@pytest.mark.parametrize("unrelated", [False, True])
def test_blank_or_unrelated_pixels_do_not_prove_saved_pdf_rectangle(unrelated):
    image = Image.new("RGB", (400, 300), "white")
    if unrelated:
        ImageDraw.Draw(image).rectangle(
            (200, 180, 300, 250),
            outline="red",
            width=2,
        )
    with pytest.raises(AssertionError, match="rectangle"):
        _module()._pdf_rectangle_pixel_proof(
            image,
            [40, 50, 160, 120],
            [255, 0, 0],
            2,
        )


def test_matching_visible_rectangle_pixels_are_accepted():
    image = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(image).rectangle((40, 50, 160, 120), outline="red", width=2)
    result = _module()._pdf_rectangle_pixel_proof(
        image,
        [40, 50, 160, 120],
        [255, 0, 0],
        2,
    )
    assert len(result["edge_match_ratios"]) == 4
    assert min(result["edge_match_ratios"]) >= 0.8


@pytest.mark.parametrize("invalid_stage", [1, 2, 3])
def test_pdf_undo_state_requires_observed_rectangles_not_button_state(
    monkeypatch,
    invalid_stage,
):
    module = _module()
    stage = 0

    def draw(*_args, **_kwargs):
        nonlocal stage
        stage = 1 if stage == 0 else 3

    def click(*_args, label):
        nonlocal stage
        if label == "PDF undo":
            stage = 2
        return {"x": 1, "y": 1}

    def rectangles(*_args):
        expected = 0 if stage in {0, 2} else 1
        count = 1 - expected if stage == invalid_stage else expected
        return [
            {
                "normalized_rect": [0.1, 0.2, 0.3, 0.4],
                "color": [255, 0, 0],
                "normalized_line_width": 0.002,
            },
        ] * count

    def wait(predicate, *, label, **_kwargs):
        if not predicate():
            raise AssertionError(label)

    monkeypatch.setattr(module, "_draw_pdf_rect", draw)
    monkeypatch.setattr(module, "_native_click", click)
    monkeypatch.setattr(
        module,
        "_button_disabled",
        lambda cdp, title: stage == 2 if title == "撤销" else False,
    )
    monkeypatch.setattr(
        module,
        "_pdf_visible_rectangles",
        rectangles,
        raising=False,
    )
    monkeypatch.setattr(module, "_wait", wait)
    with pytest.raises(AssertionError, match="PDF.*rectangle"):
        module._edit_pdf(SimpleNamespace(cdp=object()), 1)


def test_saved_appearance_geometry_uses_crop_and_page_rotation():
    module = _module()
    annotation = {
        "page_cropbox": [20, 30, 620, 830],
        "drawn_rect": [120, 230, 240, 300],
        "page_rotation": 90,
        "line_width": 2,
    }
    assert module._pdf_normalized_rectangle(annotation) == pytest.approx(
        [0.25, 1 / 6, 0.3375, 11 / 30],
    )


@pytest.mark.parametrize("invalid", ["hidden", "matrix"])
def test_hidden_or_transformed_appearance_does_not_prove_visible_geometry(
    tmp_path,
    invalid,
):
    path = tmp_path / "unsupported.pdf"
    _pdf(
        path,
        active=True,
        hidden=invalid == "hidden",
        matrix=invalid == "matrix",
    )
    with pytest.raises(AssertionError, match="appearance"):
        _module()._pdf_annotation_readback(path)


@pytest.mark.parametrize("suffix", [".pdf", ".pptx"])
def test_fresh_renderer_needs_saved_content_proof_beyond_a_window(
    tmp_path,
    monkeypatch,
    suffix,
):
    module = _module()
    fixture = tmp_path / f"fixture{suffix}"
    if suffix == ".pdf":
        _pdf(fixture, active=True)
    else:
        with zipfile.ZipFile(fixture, "w") as archive:
            for index in range(1, 4):
                archive.writestr(f"ppt/slides/slide{index}.xml", "<slide/>")
    stopped = []

    class Cdp:
        def evaluate(self, _expression, **_kwargs):
            return "Title"

        def screenshot(self, path):
            Image.new("RGB", (400, 300), "white").save(path)

    session = SimpleNamespace(
        cdp=Cdp(),
        process=SimpleNamespace(pid=101),
        root=tmp_path,
        workbench={},
        renderer_identity={},
        process_settlement={"ok": True},
        stop=lambda: stopped.append(True),
    )
    monkeypatch.setattr(module, "_launch", lambda *args: session)
    monkeypatch.setattr(module, "_body_text", lambda cdp: "Office content")
    monkeypatch.setattr(module, "_slide_status", lambda cdp: (1, 2))
    with pytest.raises(AssertionError, match=suffix[1:].upper()):
        module._fresh_renderer_readback(
            app=tmp_path,
            fixture=fixture,
            marker="FINAL",
            output=tmp_path / "fresh.png",
            timeout=1,
        )
    assert stopped == [True]
