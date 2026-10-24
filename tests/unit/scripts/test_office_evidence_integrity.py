# -*- coding: utf-8 -*-
"""Office release receipts must reject incomplete or stale observations."""

from __future__ import annotations

# These behavior locks intentionally inspect private harness operations.
# pylint: disable=protected-access,redefined-outer-name

import copy
import hashlib
import importlib.util
import json
import plistlib
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from PIL import Image, ImageDraw, ImageStat


ROOT = Path(__file__).resolve().parents[3]
SUFFIXES = {
    "docs": ".docx",
    "sheets": ".xlsx",
    "slides": ".pptx",
    "pdf": ".pdf",
}


def _module(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "scripts" / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, value: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, value: dict) -> Path:
    return _write(path, json.dumps(value).encode())


def _renderer_identity(fixture: Path, editor: str) -> dict:
    names = {
        "docs": "__lantaiDocs",
        "sheets": "desktopApi",
        "slides": "slidesApi",
        "pdf": "pdfApi",
    }
    return {
        "url": f"file:///editor/{editor}/index.html",
        "editor": editor,
        "document_path": str(fixture),
        "file_title": fixture.name,
        "file_title_attribute": str(fixture)
        if editor in {"docs", "pdf"}
        else fixture.name,
        "workbench_id": hashlib.sha256(
            f"{editor}:{fixture}".encode(),
        ).hexdigest()[:20],
        "global_available": True,
        "global_name": names[editor],
    }


def _office_fixture(path: Path, text: str, *, edited=False) -> Path:
    """Synthetic typed containers exercise parsers, never an actual editor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".docx":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                f'<w:document xmlns:w="w"><w:t>{text}</w:t></w:document>',
            )
    elif path.suffix == ".xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.active["A1"] = text
        workbook.active["B2"] = 123.5 if edited else 1
        workbook.active["C2"] = "=B2*2"
        workbook.save(path)
        workbook.close()
    elif path.suffix == ".pptx":
        with zipfile.ZipFile(path, "w") as archive:
            for number in range(1, 3 if edited else 2):
                archive.writestr(
                    f"ppt/slides/slide{number}.xml",
                    f"<synthetic>{text}</synthetic>",
                )
    else:
        from pypdf import PdfWriter
        from pypdf.generic import (
            ArrayObject,
            DecodedStreamObject,
            DictionaryObject,
            FloatObject,
            NameObject,
            NumberObject,
        )

        writer = PdfWriter()
        page = writer.add_blank_page(width=600, height=800)
        writer.add_metadata({"/Title": text})
        if edited:
            rect = ArrayObject([FloatObject(v) for v in (97, 197, 223, 273)])
            stream = DecodedStreamObject()
            stream.set_data(b"1 w 1 J 1 j 1 0 0 RG 100 200 120 70 re S")
            stream.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Form"),
                    NameObject("/BBox"): rect,
                },
            )
            annotation = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Square"),
                    NameObject("/Rect"): rect,
                    NameObject("/F"): NumberObject(4),
                    NameObject("/C"): ArrayObject(
                        [NumberObject(v) for v in (1, 0, 0)],
                    ),
                    NameObject("/BS"): DictionaryObject(
                        {NameObject("/W"): NumberObject(1)},
                    ),
                    NameObject("/AP"): DictionaryObject(
                        {
                            NameObject("/N"): writer._add_object(
                                stream.flate_encode(),
                            ),
                        },
                    ),
                },
            )
            page[NameObject("/Annots")] = ArrayObject(
                [writer._add_object(annotation)],
            )
        writer.write(path)
    return path


def _synthetic_screenshot(path: Path) -> Path:
    """Draw a valid inert image with visible pixels, never actual Office."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (900, 650), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 340, 400), fill="black")
    draw.rectangle((20, 20, 880, 630), outline="black", width=3)
    draw.text((380, 90), "SYNTHETIC VISUAL CONTROL", fill="black")
    image.save(path)
    return path


def _synthetic_visual(path: Path) -> dict[str, float]:
    with Image.open(path) as source:
        grayscale = source.convert("RGB").resize((240, 160)).convert("L")
        stat = ImageStat.Stat(grayscale)
        extrema = grayscale.getextrema()
        histogram = grayscale.histogram()
    return {
        "mean_luma": round(float(stat.mean[0]), 2),
        "luma_range": float(extrema[1] - extrema[0]),
        "dark_ratio": round(sum(histogram[:238]) / sum(histogram), 4),
    }


def _pdf_pixel_receipt(
    module,
    fixture,
    before,
    after,
    interaction,
    screenshot,
):
    annotation = module._pdf_saved_rectangle(before, after, interaction)
    canvas_path = screenshot.with_name("synthetic-saved-page.png")
    canvas = Image.new("RGB", (600, 800), "white")
    ImageDraw.Draw(canvas).rectangle(
        (100, 530, 220, 600),
        outline="red",
        width=1,
    )
    canvas.save(canvas_path)
    viewport = Image.new("RGB", (900, 1000), "white")
    viewport.paste(canvas, (50, 60))
    viewport.save(screenshot)
    normalized = annotation["normalized_rect"]
    canvas_rect = [
        normalized[0] * 600,
        normalized[1] * 800,
        normalized[2] * 600,
        normalized[3] * 800,
    ]
    screenshot_rect = [
        50 + normalized[0] * 600,
        60 + normalized[1] * 800,
        50 + normalized[2] * 600,
        60 + normalized[3] * 800,
    ]
    return {
        "file_sha256": _sha(fixture),
        "renderer_file_sha256": _sha(fixture),
        "square_annotation_count": 1,
        "visible_square_count": 1,
        "annotation": annotation,
        "canvas": str(canvas_path),
        "canvas_sha256": _sha(canvas_path),
        "canvas_geometry": {
            "rect": {"left": 50, "top": 60, "width": 600, "height": 800},
            "viewport": [900, 1000],
        },
        "canvas_pixel_proof": module._pdf_rectangle_pixel_proof(
            canvas,
            canvas_rect,
            [255, 0, 0],
            1,
        ),
        "screenshot_pixel_proof": module._pdf_rectangle_pixel_proof(
            viewport,
            screenshot_rect,
            [255, 0, 0],
            1,
        ),
    }


@pytest.fixture
def evidence(tmp_path: Path):
    (
        "Inert files test the receipt validator; they are "
        "never Office evidence."
    )
    app = tmp_path / "Synthetic.app"
    _write(
        app / "Contents/Info.plist",
        plistlib.dumps({"CFBundleExecutable": "Synthetic"}),
    )
    artifacts: dict[str, Path] = {
        "main_executable": _write(
            app / "Contents/MacOS/Synthetic",
            b"inert executable",
        ),
        "app_asar": _write(
            app / "Contents/Resources/app.asar",
            b"inert renderer",
        ),
        "backend_executable": _write(
            app
            / (
                "Contents/Resources/lantai-server/Lantai "
                "Backend.app/Contents/MacOS/lantai-server"
            ),
            b"inert backend",
        ),
    }
    metadata: dict[str, Any] = {
        "path": str(app),
        "executable_sha256": _sha(artifacts["main_executable"]),
        "artifact_sha256": {
            key: _sha(path) for key, path in artifacts.items()
        },
    }
    cases: list[dict[str, Any]] = []
    packed_results: list[dict[str, Any]] = []
    for number in range(1, 6):
        case_id = f"case-{number:02d}"
        files, captures = {}, {}
        for editor, suffix in SUFFIXES.items():
            fixture = _office_fixture(
                tmp_path / case_id / f"sample{suffix}",
                f"synthetic {case_id} {editor}",
            )
            screenshot = _synthetic_screenshot(
                tmp_path / case_id / f"{editor}.png",
            )
            files[editor] = {"path": str(fixture), "sha256": _sha(fixture)}
            captures[editor] = {
                "screenshot": str(screenshot),
                "sha256": _sha(screenshot),
            }
            packed_results.append(
                {
                    "fixture": str(fixture),
                    "fixture_sha256": _sha(fixture),
                    "screenshot": str(screenshot),
                    "screenshot_sha256": _sha(screenshot),
                    "visual": _synthetic_visual(screenshot),
                    "editor": {
                        "bounds": {"y": 40, "width": 900, "height": 650},
                    },
                    "window_count": 1,
                    "renderer": _renderer_identity(fixture, editor),
                    "process_settlement": {
                        "ok": True,
                        "live": [],
                        "unknown": [],
                    },
                },
            )
        payload: dict[str, Any] = {
            "ok": True,
            "case_id": case_id,
            "files": files,
            "captures": captures,
            "openxml": {
                "receipts": [
                    {"receipt": {"atomic": True, "after_sha256": "receipt"}}
                    for _ in range(6)
                ],
                "readback": {
                    key: {}
                    for key in ("docs", "sheets_a1", "sheets_c1", "slides")
                },
            },
            "pdf": {"ocr": {"pages": [{}, {}]}, "recognized_text": case_id},
        }
        receipt = _json(tmp_path / case_id / "evidence.json", payload)
        cases.append(
            {
                "case_id": case_id,
                "evidence": str(receipt),
                "evidence_sha256": _sha(receipt),
                "payload": payload,
            },
        )
    corpus: dict[str, Any] = {
        "schema": "com.lantai.office-release-corpus.v1",
        "ok": True,
        "case_count": 5,
        "cases": cases,
    }
    packed: dict[str, Any] = {
        "ok": True,
        "app": str(app),
        "app_metadata": metadata,
        "results": packed_results,
    }
    native_click = {
        "x": 40,
        "y": 60,
        "rect": {"width": 30, "height": 20},
        "hitTag": "BUTTON",
    }
    interactions = {
        ".docx": {
            "temporary_visible": True,
            "undo_removed_temporary": True,
            "final_visible": True,
            "undo_clicks": [native_click],
            "save_click": native_click,
        },
        ".xlsx": {
            "temporary_committed": True,
            "undo_clicked": True,
            "final_committed": True,
            "undo_click": native_click,
            "save_click": native_click,
            "post_undo_visible_a1": "original",
            "post_undo_expected_a1": "original",
        },
        ".pptx": {
            "initial_slides": 1,
            "temporary_slides": 2,
            "undo_slides": 1,
            "final_slides": 2,
            "add_temporary_click": {"add_click": native_click},
            "add_final_click": {"add_click": native_click},
            "undo_click": native_click,
            "save_click": native_click,
        },
        ".pdf": {
            "temporary_annotation": True,
            "undo_cleared_stack": True,
            "final_annotation": True,
            "undo_click": native_click,
            "save_click": native_click,
        },
    }
    roundtrip_results = []
    roundtrip_module = _module("packaged_office_edit_roundtrip")
    for editor, suffix in SUFFIXES.items():
        source = Path(cases[0]["payload"]["files"][editor]["path"])
        fixture = _office_fixture(
            tmp_path / "roundtrip" / f"sample{suffix}",
            "FINAL",
            edited=True,
        )
        screenshot = _synthetic_screenshot(
            tmp_path / "roundtrip" / f"{editor}.png",
        )
        before = roundtrip_module._disk_state(source)
        after = roundtrip_module._disk_state(fixture)
        if suffix == ".pdf":
            rectangle = {
                "page_index": 0,
                "normalized_rect": [1 / 6, 0.6625, 11 / 30, 0.75],
                "color": [255, 0, 0],
                "normalized_line_width": 1 / 600,
            }
            interactions[suffix].update(
                temporary_rectangles=[rectangle],
                undo_rectangles=[],
                final_rectangles=[rectangle],
            )
            pdf_pixels = _pdf_pixel_receipt(
                roundtrip_module,
                fixture,
                before,
                after,
                interactions[suffix],
                screenshot,
            )
        else:
            pdf_pixels = None
        roundtrip_results.append(
            {
                "suffix": suffix,
                "source": str(source),
                "fixture_copy": str(fixture),
                "before_sha256": _sha(source),
                "after_sha256": _sha(fixture),
                "hash_changed": True,
                "marker": "FINAL",
                "undo_marker": "UNDO",
                "before_state": before,
                "after_state": after,
                "renderer_identity": _renderer_identity(fixture, editor),
                "edit_process_pid": 101,
                "edit_process_exit_code": 0,
                "edit_user_data_dir": "/edit",
                "fresh_process_is_distinct": True,
                "fresh_user_data_is_distinct": True,
                "edit_process_settlement": {
                    "ok": True,
                    "live": [],
                    "unknown": [],
                },
                "fresh_renderer": {
                    "screenshot": str(screenshot),
                    "screenshot_sha256": _sha(screenshot),
                    "process_pid": 102,
                    "user_data_dir": "/fresh",
                    "renderer_identity": _renderer_identity(fixture, editor),
                    "docx_marker_visible": True if suffix == ".docx" else None,
                    "xlsx_clipboard_readback": "FINAL"
                    if suffix == ".xlsx"
                    else None,
                    "pptx_slide_count": 2 if suffix == ".pptx" else None,
                    "process_settlement": {
                        "ok": True,
                        "live": [],
                        "unknown": [],
                    },
                    "pdf_annotations": pdf_pixels,
                },
                "interaction": interactions[suffix],
                "waivers": [],
            },
        )
    roundtrip: dict[str, Any] = {
        "schema": "com.lantai.packaged-office-edit-roundtrip.v1",
        "ok": True,
        "status": "passed",
        "app": metadata,
        "results": roundtrip_results,
        "waivers": [],
    }
    paths = {
        name: _json(tmp_path / f"{name}.json", value)
        for name, value in {
            "corpus": corpus,
            "first_tier": cases[0]["payload"],
            "packaged": packed,
            "roundtrip": roundtrip,
        }.items()
    }
    return SimpleNamespace(
        app=app,
        artifacts=artifacts,
        metadata=metadata,
        corpus=corpus,
        packed=packed,
        first=copy.deepcopy(cases[0]["payload"]),
        roundtrip=roundtrip,
        paths=paths,
    )


def _check(value):
    return _module("check_office_release_evidence").check(
        app=value.app,
        **value.paths,
    )


def test_complete_receipt_control(evidence):
    assert _check(evidence)["ok"] is True


@pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx", ".pdf"])
def test_saved_typed_state_must_match_the_actual_file(evidence, suffix):
    item = next(
        item
        for item in evidence.roundtrip["results"]
        if item["suffix"] == suffix
    )
    item["after_state"] = {
        "text": "FINAL",
        "a1": "FINAL",
        "b2": 123.5,
        "b2_type": "n",
        "c2": "=B2*2",
        "c2_type": "f",
        "slide_count": 2,
        "square_annotation_count": 1,
        "unrelated_fabricated_field": True,
    }
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError, match="(disk|typed|readback|state)"):
        _check(evidence)


@pytest.mark.parametrize(
    "suffix,key,value",
    [
        (".docx", "docx_marker_visible", False),
        (".xlsx", "xlsx_clipboard_readback", "another value"),
        (".pptx", "pptx_slide_count", 1),
    ],
)
def test_fresh_renderer_must_show_the_saved_content(
    evidence,
    suffix,
    key,
    value,
):
    item = next(
        item
        for item in evidence.roundtrip["results"]
        if item["suffix"] == suffix
    )
    item["fresh_renderer"][key] = value
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError, match="(fresh|Fresh|reopen|readback)"):
        _check(evidence)


@pytest.mark.parametrize(
    "key,value",
    [
        ("workbench_id", "another document"),
        ("file_title", "another.pdf"),
        ("global_available", False),
    ],
)
def test_entry_renderer_identity_cannot_be_replaced_by_matching_path(
    evidence,
    key,
    value,
):
    evidence.packed["results"][0]["renderer"][key] = value
    _json(evidence.paths["packaged"], evidence.packed)
    with pytest.raises(AssertionError, match="(document|renderer|identity)"):
        _check(evidence)


def test_fresh_renderer_identity_must_match_its_saved_document(evidence):
    evidence.roundtrip["results"][0]["fresh_renderer"]["renderer_identity"][
        "file_title"
    ] = "other.docx"
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError, match="(document|renderer|identity)"):
        _check(evidence)


def test_original_roundtrip_state_must_be_bound_to_the_source_file(evidence):
    evidence.roundtrip["results"][0]["before_state"] = {
        "text": "fabricated original",
    }
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError, match="(source|before|state)"):
        _check(evidence)


@pytest.mark.parametrize("target", ["canvas", "screenshot"])
def test_pdf_pixel_ratios_do_not_override_blank_observation(evidence, target):
    item = next(
        item
        for item in evidence.roundtrip["results"]
        if item["suffix"] == ".pdf"
    )
    renderer = item["fresh_renderer"]
    readback = renderer["pdf_annotations"]
    path = Path(
        readback["canvas"] if target == "canvas" else renderer["screenshot"],
    )
    with Image.open(path) as image:
        blank = Image.new("RGB", image.size, "white")
    blank.save(path)
    if target == "canvas":
        readback["canvas_sha256"] = _sha(path)
    else:
        renderer["screenshot_sha256"] = _sha(path)
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError, match="(PDF|rectangle|pixel)"):
        _check(evidence)


def test_pdf_save_must_match_the_final_native_rectangle(evidence):
    item = next(
        item
        for item in evidence.roundtrip["results"]
        if item["suffix"] == ".pdf"
    )
    item["interaction"]["final_rectangles"][0]["normalized_rect"] = [
        0.1,
        0.2,
        0.3,
        0.4,
    ]
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError, match="(PDF|rectangle|drawing)"):
        _check(evidence)


def test_pdf_undo_cannot_claim_empty_stack_with_a_visible_rectangle(evidence):
    item = next(
        item
        for item in evidence.roundtrip["results"]
        if item["suffix"] == ".pdf"
    )
    item["interaction"]["undo_rectangles"] = copy.deepcopy(
        item["interaction"]["final_rectangles"],
    )
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError, match="(PDF|rectangle|interaction)"):
        _check(evidence)


def test_missing_readback_is_rejected_even_when_extra_key_is_present(evidence):
    del evidence.first["openxml"]["readback"]["slides"]
    evidence.first["openxml"]["readback"]["unrelated"] = {}
    _json(evidence.paths["first_tier"], evidence.first)
    with pytest.raises(AssertionError, match="readback"):
        _check(evidence)


def test_twenty_opens_of_four_files_do_not_count_as_twenty_samples(evidence):
    evidence.packed["results"] = [
        copy.deepcopy(item)
        for _ in range(5)
        for item in evidence.packed["results"][:4]
    ]
    _json(evidence.paths["packaged"], evidence.packed)
    with pytest.raises(AssertionError, match="(duplicate|corpus|distinct)"):
        _check(evidence)


@pytest.mark.parametrize("artifact", ["app_asar", "backend_executable"])
def test_changed_app_component_invalidates_office_receipt(evidence, artifact):
    evidence.artifacts[artifact].write_bytes(b"changed component")
    with pytest.raises(AssertionError, match="(artifact|component|stale)"):
        _check(evidence)


def test_unknown_interaction_does_not_prove_edit_undo_save(evidence):
    for item in evidence.roundtrip["results"]:
        item["interaction"] = {"unrelated": True}
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError, match="interaction"):
        _check(evidence)


def test_fresh_pdf_window_without_annotation_readback_is_rejected(evidence):
    pdf = next(
        item
        for item in evidence.roundtrip["results"]
        if item["suffix"] == ".pdf"
    )
    del pdf["fresh_renderer"]["pdf_annotations"]
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError, match="(PDF|annotation)"):
        _check(evidence)


def test_screenshot_replacement_invalidates_receipt(evidence):
    Path(evidence.packed["results"][0]["screenshot"]).write_bytes(
        b"replaced screenshot" * 100,
    )
    with pytest.raises(AssertionError, match="screenshot"):
        _check(evidence)


def test_corpus_embedded_payload_must_match_hashed_receipt(evidence):
    evidence.corpus["cases"][0]["payload"]["case_id"] = "replaced"
    evidence.corpus["cases"][0]["case_id"] = "replaced"
    evidence.corpus["cases"][0]["payload"]["pdf"][
        "recognized_text"
    ] = "replaced"
    _json(evidence.paths["corpus"], evidence.corpus)
    with pytest.raises(AssertionError, match="(payload|receipt)"):
        _check(evidence)


def test_pdf_undo_must_leave_exactly_one_final_annotation():
    module = _module("packaged_office_edit_roundtrip")
    with pytest.raises(AssertionError, match="PDF annotation"):
        module._assert_readback(
            suffix=".pdf",
            before={"square_annotation_count": 0},
            after={"square_annotation_count": 2},
            marker="",
            undo_marker="",
        )


# Owned processes outlive setup and are settled by identity in finally.
# pylint: disable=consider-using-with
def test_cdp_close_failure_still_settles_the_owned_child(tmp_path):
    module = _module("packaged_office_edit_roundtrip")
    root = tmp_path / "isolated-user-data"
    root.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        start_new_session=True,
    )
    born = psutil.Process(process.pid).create_time()

    def broken_close():
        raise RuntimeError("synthetic CDP close failure")

    session = module.AppSession(
        process,
        root,
        0,
        SimpleNamespace(close=broken_close),
        tmp_path / "log",
        {},
    )
    try:
        with pytest.raises(RuntimeError, match="CDP close"):
            session.stop()
        assert process.poll() is not None
        assert not root.exists()
    finally:
        if (
            process.poll() is None
            and psutil.Process(process.pid).create_time() == born
        ):
            process.terminate()
            process.wait(timeout=5)


# pylint: enable=consider-using-with


# Owned processes outlive setup and are settled by identity in finally.
# pylint: disable=consider-using-with
def test_exited_leader_does_not_hide_live_office_child(tmp_path):
    module = _module("packaged_office_edit_roundtrip")
    root = tmp_path / "isolated-user-data"
    root.mkdir()
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import "
        "time; time.sleep(20)']); "
        "print(p.pid,flush=True); time.sleep(0.5)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline())
    child = psutil.Process(child_pid)
    born = child.create_time()
    session = module.AppSession(
        process,
        root,
        0,
        SimpleNamespace(close=lambda: None),
        tmp_path / "log",
        {},
    )
    try:
        process.wait(timeout=5)
        session.stop()
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        assert not root.exists()
    finally:
        try:
            if (
                child.create_time() == born
                and child.is_running()
                and child.status() != psutil.STATUS_ZOMBIE
            ):
                child.terminate()
                child.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        process.stdout.close()


# pylint: enable=consider-using-with


# Owned processes outlive setup and are settled by identity in finally.
# pylint: disable=consider-using-with
def test_unknown_owned_identity_never_counts_as_settled(monkeypatch):
    support = _module("packaged_office_support")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        start_new_session=True,
    )
    born = psutil.Process(process.pid).create_time()
    try:
        cohort = support.OwnedProcessCohort(process)
        real_create_time = psutil.Process.create_time

        def denied(owner):
            if owner.pid == process.pid:
                raise psutil.AccessDenied(owner.pid)
            return real_create_time(owner)

        monkeypatch.setattr(psutil.Process, "create_time", denied)
        receipt = cohort.settle(term_timeout=0.1, kill_timeout=0.1)
        assert receipt["ok"] is False
        assert receipt["unknown"]
        assert process.poll() is None
        assert receipt["signals"] == []
    finally:
        monkeypatch.undo()
        if (
            process.poll() is None
            and psutil.Process(process.pid).create_time() == born
        ):
            process.terminate()
            process.wait(timeout=5)


# pylint: enable=consider-using-with


# Owned processes outlive setup and are settled by identity in finally.
# pylint: disable=consider-using-with
def test_owned_cleanup_does_not_signal_another_session():
    support = _module("packaged_office_support")
    owned = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        start_new_session=True,
    )
    foreign = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        start_new_session=True,
    )
    born = {
        p.pid: psutil.Process(p.pid).create_time() for p in (owned, foreign)
    }
    try:
        cohort = support.OwnedProcessCohort(owned)
        receipt = cohort.settle(term_timeout=2, kill_timeout=1)
        assert receipt["ok"] is True
        assert owned.poll() is not None
        assert foreign.poll() is None
        assert all(item["pid"] != foreign.pid for item in receipt["signals"])
    finally:
        for process in (owned, foreign):
            if (
                process.poll() is None
                and psutil.Process(process.pid).create_time()
                == born[process.pid]
            ):
                process.terminate()
                process.wait(timeout=5)


# pylint: enable=consider-using-with


def test_reused_session_leader_cannot_adopt_foreign_processes(monkeypatch):
    support = _module("packaged_office_support")
    cohort = support.OwnedProcessCohort.__new__(support.OwnedProcessCohort)
    cohort.process = SimpleNamespace(pid=101)
    cohort.born = 100.0
    cohort.sid = 101
    cohort._owners = {101: 100.0}
    cohort._lock = threading.RLock()
    cohort._monitor_error = None
    births = {101: 200.0, 102: 201.0}
    monkeypatch.setattr(support.psutil, "pids", lambda: list(births))
    monkeypatch.setattr(support.os, "getsid", lambda _pid: 101)
    monkeypatch.setattr(
        support.psutil,
        "Process",
        lambda pid: SimpleNamespace(
            pid=pid,
            create_time=lambda: births[pid],
            status=lambda: "running",
            children=lambda recursive: [],
        ),
    )
    snapshot = cohort._snapshot()
    assert snapshot["live"] == []
    assert cohort._owners == {101: 100.0}


def test_ownership_monitor_failure_blocks_cleanup(monkeypatch):
    support = _module("packaged_office_support")
    cohort = support.OwnedProcessCohort.__new__(support.OwnedProcessCohort)
    cohort.process = SimpleNamespace(pid=101, poll=lambda: 0)
    cohort.born = 100.0
    cohort.sid = 101
    cohort._owners = {101: 100.0}
    cohort._lock = threading.RLock()
    cohort._stop = threading.Event()
    cohort._signals = []
    cohort._monitor_error = None
    cohort._thread = SimpleNamespace(
        join=lambda timeout: None,
        is_alive=lambda: False,
    )

    def failed_snapshot():
        raise OSError("synthetic inventory failure")

    monkeypatch.setattr(cohort, "_snapshot", failed_snapshot)
    cohort._monitor()
    monkeypatch.setattr(
        cohort,
        "_snapshot",
        lambda: {"live": [], "unknown": []},
    )
    receipt = cohort.settle(term_timeout=0.01, kill_timeout=0.01)
    assert receipt["ok"] is False
    assert any("monitor" in item["reason"] for item in receipt["unknown"])


def test_cohort_constructor_rejects_an_already_exited_popen(monkeypatch):
    support = _module("packaged_office_support")
    process = SimpleNamespace(pid=101, poll=lambda: 0)
    monkeypatch.setattr(
        support.psutil,
        "Process",
        lambda _pid: SimpleNamespace(create_time=lambda: 200.0),
    )
    monkeypatch.setattr(support.os, "getsid", lambda _pid: 101)
    monkeypatch.setattr(
        support.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: None),
    )
    with pytest.raises(AssertionError, match="(exited|identity)"):
        support.OwnedProcessCohort(process)


def test_monitor_inventory_gap_is_not_erased_by_a_later_empty_scan(
    monkeypatch,
):
    support = _module("packaged_office_support")
    cohort = support.OwnedProcessCohort.__new__(support.OwnedProcessCohort)
    cohort.process = SimpleNamespace(pid=101, poll=lambda: 0)
    cohort.born = 100.0
    cohort.sid = 101
    cohort._owners = {101: 100.0}
    cohort._lock = threading.RLock()
    cohort._stop = threading.Event()
    cohort._signals = []
    cohort._monitor_error = None
    cohort._thread = SimpleNamespace(
        join=lambda timeout: None,
        is_alive=lambda: False,
    )
    calls = 0

    def gap():
        nonlocal calls
        calls += 1
        cohort._stop.set()
        return {
            "live": [],
            "unknown": [
                {"pid": 101, "reason": "descendant inventory unavailable"},
            ],
        }

    monkeypatch.setattr(cohort, "_snapshot", gap)
    cohort._monitor()
    monkeypatch.setattr(
        cohort,
        "_snapshot",
        lambda: {"live": [], "unknown": []},
    )
    receipt = cohort.settle(term_timeout=0.01, kill_timeout=0.01)
    assert calls == 1
    assert receipt["ok"] is False
    assert receipt["unknown"]


def test_cdp_close_interrupt_still_settles_and_preserves_interrupt(tmp_path):
    module = _module("packaged_office_edit_roundtrip")
    root = tmp_path / "isolated-user-data"
    root.mkdir()
    settled = []

    def interrupt():
        raise KeyboardInterrupt("CDP close interrupt")

    session = module.AppSession(
        SimpleNamespace(pid=101),
        root,
        0,
        SimpleNamespace(close=interrupt),
        tmp_path / "log",
        {},
        ownership=SimpleNamespace(
            settle=lambda: settled.append(True)
            or {"ok": True, "live": [], "unknown": []},
        ),
    )
    with pytest.raises(KeyboardInterrupt, match="CDP close interrupt"):
        session.stop()
    assert settled == [True]
    assert not root.exists()


def test_fresh_readback_preserves_primary_failure_when_cleanup_also_fails(
    monkeypatch,
    tmp_path,
):
    module = _module("packaged_office_edit_roundtrip")

    def fail_cleanup():
        raise RuntimeError("cleanup failure")

    session = SimpleNamespace(
        process=SimpleNamespace(pid=101),
        root=tmp_path,
        cdp=object(),
        stop=fail_cleanup,
    )
    monkeypatch.setattr(module, "_launch", lambda *_args: session)
    monkeypatch.setattr(
        module,
        "_body_text",
        lambda _cdp: "missing saved marker",
    )
    with pytest.raises(AssertionError, match="fresh DOCX") as caught:
        module._fresh_renderer_readback(
            app=tmp_path,
            fixture=tmp_path / "file.docx",
            marker="required marker",
            output=tmp_path / "out.png",
            timeout=1,
        )
    assert isinstance(caught.value.__cause__, RuntimeError)


def _refresh_capture_receipts(value) -> None:
    """Rebind all references so stale hashes cannot mask a pixel failure."""
    for case in value.corpus["cases"]:
        for capture in case["payload"]["captures"].values():
            capture["sha256"] = _sha(Path(capture["screenshot"]))
        _json(Path(case["evidence"]), case["payload"])
        case["evidence_sha256"] = _sha(Path(case["evidence"]))
    value.first = copy.deepcopy(value.corpus["cases"][0]["payload"])
    for name, payload in (
        ("corpus", value.corpus),
        ("first_tier", value.first),
        ("packaged", value.packed),
        ("roundtrip", value.roundtrip),
    ):
        _json(value.paths[name], payload)


@pytest.mark.parametrize("replacement", ["blank", "invalid", "tiny"])
def test_packaged_pixels_override_claimed_visual(evidence, replacement):
    item = evidence.packed["results"][0]
    screenshot = Path(item["screenshot"])
    if replacement == "invalid":
        screenshot.write_bytes(b"NOT AN IMAGE" * 200)
    else:
        size = (10, 10) if replacement == "tiny" else (900, 650)
        Image.new("RGB", size, "white").save(screenshot)
    item["screenshot_sha256"] = _sha(screenshot)
    _refresh_capture_receipts(evidence)
    with pytest.raises(AssertionError):
        _check(evidence)


@pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx"])
def test_fresh_saved_content_requires_visible_pixels(evidence, suffix):
    item = next(
        value
        for value in evidence.roundtrip["results"]
        if value["suffix"] == suffix
    )
    renderer = item["fresh_renderer"]
    screenshot = Path(renderer["screenshot"])
    Image.new("RGB", (900, 650), "white").save(screenshot)
    renderer["screenshot_sha256"] = _sha(screenshot)
    _json(evidence.paths["roundtrip"], evidence.roundtrip)
    with pytest.raises(AssertionError):
        _check(evidence)


def test_packaged_visual_receipt_must_match_actual_pixels(evidence):
    evidence.packed["results"][0]["visual"]["dark_ratio"] = 0.99
    _json(evidence.paths["packaged"], evidence.packed)
    with pytest.raises(AssertionError):
        _check(evidence)
