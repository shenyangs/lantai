#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate Office real-file, renderer, safety and packaged-app evidence."""

from __future__ import annotations

# Reuse the harness format validators as the single evidence definition.
# pylint: disable=protected-access

import argparse
import hashlib
import json
import math
import plistlib
from pathlib import Path
from typing import Any

try:
    from packaged_office_support import (
        app_metadata as _app_metadata,
        require_visible_screenshot,
    )
except ModuleNotFoundError as import_error:
    if import_error.name != "packaged_office_support":
        raise
    from scripts.packaged_office_support import (
        app_metadata as _app_metadata,
        require_visible_screenshot,
    )


REQUIRED_EDITORS = {"docs", "sheets", "slides", "pdf"}
READBACK_KEYS = {"docs", "sheets_a1", "sheets_c1", "slides"}
EDITOR_BY_SUFFIX = {
    ".docx": "docs",
    ".xlsx": "sheets",
    ".pptx": "slides",
    ".pdf": "pdf",
}
KNOWN_ROUNDTRIP_WAIVERS = {"genoffice.xlsx.native_number_input"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _executable_hash(app: Path) -> str:
    with (app / "Contents" / "Info.plist").open("rb") as handle:
        executable = plistlib.load(handle)["CFBundleExecutable"]
    return _sha256(app / "Contents" / "MacOS" / str(executable))


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Office evidence is not an object: {path}")
    return payload


# Keep the ordered evidence rejection checks explicit and adjacent.
# pylint: disable=too-many-branches
def _check_corpus(path: Path) -> dict[str, Any]:
    corpus = _load(path.expanduser().resolve(strict=True))
    if (
        corpus.get("schema") != "com.lantai.office-release-corpus.v1"
        or corpus.get("ok") is not True
    ):
        raise AssertionError("Office release corpus is invalid")
    cases = corpus.get("cases") or []
    if corpus.get("case_count") != 5 or len(cases) != 5:
        raise AssertionError(
            "Office release corpus must contain exactly five cases",
        )
    case_ids: set[str] = set()
    hashes_by_editor: dict[str, set[str]] = {
        editor: set() for editor in REQUIRED_EDITORS
    }
    fixtures: dict[str, dict[str, str]] = {}
    for case in cases:
        case_id = str(case.get("case_id") or "")
        payload = case.get("payload") or {}
        evidence_path = Path(str(case.get("evidence") or "")).resolve(
            strict=True,
        )
        if _sha256(evidence_path) != case.get("evidence_sha256"):
            raise AssertionError(
                f"Office corpus evidence hash is stale: {case_id}",
            )
        if _load(evidence_path) != payload:
            raise AssertionError(
                f"Office corpus embedded payload differs from hashed receipt: {case_id}",
            )
        if (
            not case_id
            or case_id in case_ids
            or payload.get("case_id") != case_id
        ):
            raise AssertionError(
                "Office corpus case ids are missing or duplicated",
            )
        case_ids.add(case_id)
        files = payload.get("files") or {}
        captures = payload.get("captures") or {}
        if set(files) != REQUIRED_EDITORS or set(captures) != REQUIRED_EDITORS:
            raise AssertionError(
                f"Office corpus case is incomplete: {case_id}",
            )
        for editor in REQUIRED_EDITORS:
            file_path = Path(str(files[editor].get("path") or "")).resolve(
                strict=True,
            )
            screenshot = Path(
                str(captures[editor].get("screenshot") or ""),
            ).resolve(strict=True)
            if _sha256(file_path) != files[editor].get("sha256"):
                raise AssertionError(
                    f"Office corpus file hash is stale: {case_id}/{editor}",
                )
            if _sha256(screenshot) != captures[editor].get("sha256"):
                raise AssertionError(
                    f"Office corpus screenshot hash is stale: {case_id}/{editor}",
                )
            hashes_by_editor[editor].add(str(files[editor]["sha256"]))
            if str(file_path) in fixtures:
                raise AssertionError(
                    "Office corpus contains duplicate fixture paths",
                )
            fixtures[str(file_path)] = {
                "editor": editor,
                "sha256": str(files[editor]["sha256"]),
            }
        receipts = (payload.get("openxml") or {}).get("receipts") or []
        if len(receipts) < 6 or any(
            (item.get("receipt") or {}).get("atomic") is not True
            or not (item.get("receipt") or {}).get("after_sha256")
            for item in receipts
        ):
            raise AssertionError(
                f"Office corpus atomic receipts are incomplete: {case_id}",
            )
        if not READBACK_KEYS.issubset(
            (payload.get("openxml") or {}).get("readback") or {},
        ):
            raise AssertionError(
                f"Office corpus readback is incomplete: {case_id}",
            )
        pages = ((payload.get("pdf") or {}).get("ocr") or {}).get(
            "pages",
        ) or []
        recognized = str(
            (payload.get("pdf") or {}).get("recognized_text") or "",
        )
        if len(pages) != 2 or case_id not in recognized:
            raise AssertionError(
                f"Office corpus PDF readback is incomplete: {case_id}",
            )
    if any(len(hashes) != 5 for hashes in hashes_by_editor.values()):
        raise AssertionError(
            "Office corpus contains duplicate files instead of five real cases",
        )
    return {
        "case_ids": sorted(case_ids),
        "samples_per_type": 5,
        "distinct_hashes_per_type": {
            editor: len(hashes)
            for editor, hashes in sorted(hashes_by_editor.items())
        },
        "fixtures": fixtures,
    }


# pylint: enable=too-many-branches


def _check_app_binding(
    metadata: dict[str, Any],
    app: Path,
    current: dict[str, Any],
) -> None:
    if Path(str(metadata.get("path") or "")).resolve() != app:
        raise AssertionError("Office evidence is bound to another app")
    if metadata.get("executable_sha256") != current["executable_sha256"]:
        raise AssertionError("Office evidence is stale for this executable")
    if metadata.get("artifact_sha256") != current["artifact_sha256"]:
        raise AssertionError(
            "Office evidence contains stale app component artifact hashes",
        )


def _check_settlement(value: Any) -> None:
    if (
        not isinstance(value, dict)
        or value.get("ok") is not True
        or value.get("live") != []
        or value.get("unknown") != []
    ):
        raise AssertionError(
            "Office owned process settlement is incomplete or uncertain",
        )


def _readback_tools():
    try:
        import packaged_office_edit_roundtrip as tools
    except ModuleNotFoundError as exc:
        if exc.name != "packaged_office_edit_roundtrip":
            raise
        from scripts import packaged_office_edit_roundtrip as tools
    return tools


def _check_renderer_binding(value: Any, fixture: Path) -> None:
    tools = _readback_tools()
    suffix = fixture.suffix.lower()
    editor = EDITOR_BY_SUFFIX[suffix]
    title_attribute = (
        str(fixture) if suffix in {".docx", ".pdf"} else fixture.name
    )
    expected_workbench = hashlib.sha256(
        f"{editor}:{fixture}".encode(),
    ).hexdigest()[:20]
    # Each term is a distinct required observation.
    # pylint: disable=too-many-boolean-expressions
    if (
        not isinstance(value, dict)
        or value.get("editor") != editor
        or Path(str(value.get("document_path") or "")).resolve() != fixture
        or not str(value.get("url") or "").startswith("file://")
        or value.get("file_title") != fixture.name
        or value.get("file_title_attribute") != title_attribute
        or value.get("workbench_id") != expected_workbench
        or value.get("global_name") != tools.GLOBAL_BY_SUFFIX[suffix]
        or value.get("global_available") is not True
    ):
        raise AssertionError(
            "Office renderer identity does not prove its selected document",
        )
    # pylint: enable=too-many-boolean-expressions


def _check_pdf_pixels(
    result: dict[str, Any],
    renderer: dict[str, Any],
) -> None:
    from PIL import Image

    tools = _readback_tools()
    annotation = tools._pdf_saved_rectangle(
        result["before_state"],
        result["after_state"],
        result["interaction"],
    )
    readback = renderer.get("pdf_annotations") or {}
    if (
        readback.get("annotation") != annotation
        or readback.get("renderer_file_sha256") != result["after_sha256"]
    ):
        raise AssertionError(
            (
                "Fresh PDF rectangle or renderer file readback di"
                "ffers from the saved file"
            ),
        )
    canvas_path = Path(str(readback.get("canvas") or "")).resolve(strict=True)
    if _sha256(canvas_path) != readback.get("canvas_sha256"):
        raise AssertionError("Fresh PDF canvas hash is stale")
    geometry = readback.get("canvas_geometry") or {}
    bounds = geometry.get("rect") or {}
    viewport = geometry.get("viewport") or []
    # Each term is a distinct required observation.
    # pylint: disable=too-many-boolean-expressions
    if (
        not isinstance(viewport, list)
        or len(viewport) != 2
        or any(
            not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
            for v in viewport
        )
        or any(
            not isinstance(bounds.get(k), (int, float))
            or not math.isfinite(bounds[k])
            for k in ("left", "top", "width", "height")
        )
        or bounds["width"] <= 0
        or bounds["height"] <= 0
    ):
        raise AssertionError("Fresh PDF canvas geometry is incomplete")
    # pylint: enable=too-many-boolean-expressions
    normalized = annotation["normalized_rect"]
    color = [round(channel * 255) for channel in annotation["color"]]
    with Image.open(canvas_path) as image:
        canvas = image.convert("RGB")
        canvas_rect = [
            normalized[0] * canvas.width,
            normalized[1] * canvas.height,
            normalized[2] * canvas.width,
            normalized[3] * canvas.height,
        ]
        canvas_proof = tools._pdf_rectangle_pixel_proof(
            canvas,
            canvas_rect,
            color,
            annotation["normalized_line_width"] * canvas.width,
        )
    screenshot_path = Path(str(renderer["screenshot"])).resolve(strict=True)
    with Image.open(screenshot_path) as image:
        screenshot = image.convert("RGB")
        scale_x, scale_y = (
            screenshot.width / viewport[0],
            screenshot.height / viewport[1],
        )
        if abs(scale_x - scale_y) > 0.02:
            raise AssertionError(
                "Fresh PDF screenshot scale is inconsistent with its viewport",
            )
        screenshot_rect = [
            (bounds["left"] + normalized[0] * bounds["width"]) * scale_x,
            (bounds["top"] + normalized[1] * bounds["height"]) * scale_y,
            (bounds["left"] + normalized[2] * bounds["width"]) * scale_x,
            (bounds["top"] + normalized[3] * bounds["height"]) * scale_y,
        ]
        screenshot_proof = tools._pdf_rectangle_pixel_proof(
            screenshot,
            screenshot_rect,
            color,
            annotation["normalized_line_width"] * bounds["width"] * scale_x,
        )
    if (
        readback.get("canvas_pixel_proof") != canvas_proof
        or readback.get("screenshot_pixel_proof") != screenshot_proof
    ):
        raise AssertionError(
            "Fresh PDF rectangle pixel evidence differs from the actual images",
        )


def _native_click(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    rect = value.get("rect") or {}
    return (
        all(isinstance(value.get(key), (int, float)) for key in ("x", "y"))
        and float(rect.get("width") or 0) > 0
        and float(rect.get("height") or 0) > 0
        and bool(value.get("hitTag"))
    )


def _check_interaction(suffix: str, interaction: dict[str, Any]) -> None:
    required = {
        ".docx": (
            "temporary_visible",
            "undo_removed_temporary",
            "final_visible",
        ),
        ".xlsx": ("temporary_committed", "undo_clicked", "final_committed"),
        ".pdf": (
            "temporary_annotation",
            "undo_cleared_stack",
            "final_annotation",
        ),
    }
    valid = bool(interaction) and _native_click(interaction.get("save_click"))
    if suffix == ".docx":
        clicks = interaction.get("undo_clicks") or []
        valid = (
            valid
            and bool(clicks)
            and all(_native_click(item) for item in clicks)
        )
    else:
        valid = valid and _native_click(interaction.get("undo_click"))
    if suffix in required:
        valid = valid and all(
            interaction.get(key) is True for key in required[suffix]
        )
    if suffix == ".xlsx":
        valid = (
            valid
            and "post_undo_expected_a1" in interaction
            and interaction.get("post_undo_visible_a1")
            == interaction["post_undo_expected_a1"]
        )
    if suffix == ".pptx":
        initial = int(interaction.get("initial_slides") or 0)
        valid = (
            valid
            and initial > 0
            and interaction.get("temporary_slides") == initial + 1
            and interaction.get("undo_slides") == initial
            and interaction.get("final_slides") == initial + 1
        )
        valid = valid and all(
            _native_click((interaction.get(key) or {}).get("add_click"))
            for key in ("add_temporary_click", "add_final_click")
        )
    if suffix == ".pdf":
        temporary = interaction.get("temporary_rectangles")
        final = interaction.get("final_rectangles")
        valid = (
            valid
            and isinstance(temporary, list)
            and len(temporary) == 1
            and interaction.get("undo_rectangles") == []
            and isinstance(final, list)
            and len(final) == 1
        )
        if valid:
            assert isinstance(temporary, list)
            assert isinstance(final, list)
            for rectangle in [*temporary, *final]:
                if not isinstance(rectangle, dict):
                    valid = False
                    break
                rect = rectangle.get("normalized_rect")
                color = rectangle.get("color")
                width = rectangle.get("normalized_line_width")
                page = rectangle.get("page_index")
                valid = (
                    isinstance(rect, list)
                    and len(rect) == 4
                    and all(
                        isinstance(v, (int, float))
                        and math.isfinite(v)
                        and 0 <= v <= 1
                        for v in rect
                    )
                    and rect[0] < rect[2]
                    and rect[1] < rect[3]
                    and isinstance(color, list)
                    and len(color) == 3
                    and all(
                        isinstance(v, int) and 0 <= v <= 255 for v in color
                    )
                    and isinstance(width, (int, float))
                    and math.isfinite(width)
                    and 0 < width <= 0.1
                    and isinstance(page, int)
                    and page >= 0
                )
                if not valid:
                    break
    if not valid:
        raise AssertionError(
            f"Office native edit/undo/save interaction is incomplete: {suffix}",
        )


def _validate_roundtrip_waivers(
    evidence: dict[str, Any],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    waivers = evidence.get("waivers") or []
    if not isinstance(waivers, list):
        raise AssertionError("Office edit roundtrip waivers must be a list")
    expected_status = "passed_with_waiver" if waivers else "passed"
    if evidence.get("status", expected_status) != expected_status:
        raise AssertionError(
            "Office edit roundtrip status contradicts its waivers",
        )

    flattened: list[dict[str, Any]] = []
    waiver_ids: set[str] = set()
    for result in results:
        suffix = str(result.get("suffix") or "")
        result_waivers = result.get("waivers") or []
        if not isinstance(result_waivers, list):
            raise AssertionError(
                f"Office edit result waivers must be a list: {suffix}",
            )
        if result_waivers and suffix != ".xlsx":
            raise AssertionError(
                f"Office edit waiver is not allowed for: {suffix}",
            )
        for waiver in result_waivers:
            if not isinstance(waiver, dict):
                raise AssertionError(
                    "Office edit roundtrip waiver must be an object",
                )
            waiver_id = str(waiver.get("id") or "")
            reason = str(waiver.get("reason") or "").strip()
            if waiver_id not in KNOWN_ROUNDTRIP_WAIVERS or not reason:
                raise AssertionError(
                    (
                        "Office edit roundtrip contains an unknown or une"
                        "xplained waiver"
                    ),
                )
            if waiver_id in waiver_ids:
                raise AssertionError(
                    f"Office edit roundtrip duplicates waiver: {waiver_id}",
                )
            if (
                waiver.get("observed_value") != "123.5"
                or waiver.get("observed_type") != "s"
            ):
                raise AssertionError(
                    "Office edit roundtrip waiver observation is invalid",
                )
            after_state = result.get("after_state") or {}
            if (
                after_state.get("b2") != "123.5"
                or after_state.get("b2_type") != "s"
            ):
                raise AssertionError(
                    "Office edit roundtrip waiver contradicts disk readback",
                )
            waiver_ids.add(waiver_id)
            flattened.append(waiver)
    if waivers != flattened:
        raise AssertionError(
            "Office edit roundtrip waiver summary does not match its results",
        )
    return waivers


# Keep the ordered evidence rejection checks explicit and adjacent.
# pylint: disable=too-many-branches,too-many-statements
def _check_roundtrip(
    path: Path,
    *,
    app: Path,
    current: dict[str, Any],
    corpus_fixtures: dict[str, dict[str, str]],
) -> dict[str, Any]:
    evidence = _load(path.expanduser().resolve(strict=True))
    if (
        evidence.get("schema")
        != "com.lantai.packaged-office-edit-roundtrip.v1"
        or evidence.get("ok") is not True
    ):
        raise AssertionError(
            "Packaged Office edit roundtrip evidence is invalid",
        )
    metadata = evidence.get("app") or {}
    _check_app_binding(metadata, app, current)
    results = evidence.get("results") or []
    suffixes = {str(item.get("suffix") or "") for item in results}
    if len(results) != 4 or suffixes != {".docx", ".xlsx", ".pptx", ".pdf"}:
        raise AssertionError(
            "Office edit roundtrip must cover all four file types",
        )
    waivers = _validate_roundtrip_waivers(evidence, results)
    if waivers:
        raise AssertionError(
            "Office release requires native numeric semantics without waivers",
        )
    tools = _readback_tools()
    for result in results:
        suffix = str(result.get("suffix") or "")
        fixture = Path(str(result.get("fixture_copy") or "")).resolve(
            strict=True,
        )
        if result.get("hash_changed") is not True:
            raise AssertionError(
                f"Office edit did not change the file: {suffix}",
            )
        if _sha256(fixture) != result.get("after_sha256"):
            raise AssertionError(f"Office edit disk hash is stale: {suffix}")
        if not result.get("before_sha256") or result[
            "before_sha256"
        ] == result.get("after_sha256"):
            raise AssertionError(
                f"Office edit did not prove different before/after hashes: {suffix}",
            )
        source = Path(str(result.get("source") or "")).resolve(strict=True)
        expected_source = corpus_fixtures.get(str(source))
        # Each term is a distinct required observation.
        # pylint: disable=too-many-boolean-expressions
        if (
            source.suffix.lower() != suffix
            or fixture.suffix.lower() != suffix
            or expected_source is None
            or expected_source["editor"] != EDITOR_BY_SUFFIX[suffix]
            or _sha256(source) != result["before_sha256"]
            or result["before_sha256"] != expected_source["sha256"]
        ):
            raise AssertionError(
                f"Office before state is not bound to its source corpus file: {suffix}",
            )
        # pylint: enable=too-many-boolean-expressions
        if result.get("before_state") != tools._disk_state(source):
            raise AssertionError(
                f"Office before typed state differs from its source file: {suffix}",
            )
        if result.get("after_state") != tools._disk_state(fixture):
            raise AssertionError(
                f"Office saved typed disk state differs from its actual file: {suffix}",
            )
        marker = str(result.get("marker") or "")
        undo_marker = str(result.get("undo_marker") or "")
        if not marker or not undo_marker or marker == undo_marker:
            raise AssertionError(
                f"Office readback markers are missing or indistinguishable: {suffix}",
            )
        tools._assert_readback(
            suffix=suffix,
            before=result["before_state"],
            after=result["after_state"],
            marker=marker,
            undo_marker=undo_marker,
        )
        renderer = result.get("fresh_renderer") or {}
        _check_renderer_binding(result.get("renderer_identity"), fixture)
        _check_renderer_binding(renderer.get("renderer_identity"), fixture)
        screenshot = Path(str(renderer.get("screenshot") or "")).resolve(
            strict=True,
        )
        if not screenshot.is_file() or screenshot.stat().st_size < 1_000:
            raise AssertionError(
                f"Office edit reopen screenshot is missing: {suffix}",
            )
        if _sha256(screenshot) != renderer.get("screenshot_sha256"):
            raise AssertionError(
                f"Office edit reopen screenshot hash is stale: {suffix}",
            )
        if suffix != ".pdf":
            require_visible_screenshot(screenshot)
        interaction = result.get("interaction") or {}
        _check_interaction(suffix, interaction)
        if (
            suffix == ".docx"
            and renderer.get("docx_marker_visible") is not True
        ):
            raise AssertionError(
                "Fresh DOCX renderer did not show its saved marker",
            )
        if (
            suffix == ".xlsx"
            and renderer.get("xlsx_clipboard_readback") != marker
        ):
            raise AssertionError(
                "Fresh XLSX renderer clipboard readback differs from saved A1",
            )
        if suffix == ".pptx" and (
            renderer.get("pptx_slide_count")
            != result["after_state"]["slide_count"]
            or interaction.get("initial_slides")
            != result["before_state"]["slide_count"]
            or interaction.get("final_slides")
            != result["after_state"]["slide_count"]
        ):
            raise AssertionError(
                (
                    "Fresh PPTX renderer slide readback differs from "
                    "the saved file"
                ),
            )
        _check_settlement(result.get("edit_process_settlement"))
        _check_settlement(renderer.get("process_settlement"))
        # Each term is a distinct required observation.
        # pylint: disable=too-many-boolean-expressions
        if (
            result.get("fresh_process_is_distinct") is not True
            or result.get("fresh_user_data_is_distinct") is not True
            or not renderer.get("process_pid")
            or renderer["process_pid"] == result.get("edit_process_pid")
            or not renderer.get("user_data_dir")
            or renderer["user_data_dir"] == result.get("edit_user_data_dir")
        ):
            raise AssertionError(
                f"Office reopen did not use a fresh process and user data: {suffix}",
            )
        # pylint: enable=too-many-boolean-expressions
        if suffix == ".pdf":
            before = int(
                (result.get("before_state") or {}).get(
                    "square_annotation_count",
                    -1,
                ),
            )
            after = int(
                (result.get("after_state") or {}).get(
                    "square_annotation_count",
                    -1,
                ),
            )
            readback = renderer.get("pdf_annotations") or {}
            if (
                before < 0
                or after != before + 1
                or readback.get("file_sha256") != result.get("after_sha256")
                or readback.get("square_annotation_count") != after
                or readback.get("visible_square_count") != 1
            ):
                raise AssertionError(
                    (
                        "Fresh PDF annotation readback is incomplete or i"
                        "nconsistent"
                    ),
                )
            _check_pdf_pixels(result, renderer)
    return {
        "case_id": evidence.get("case_id"),
        "file_types": sorted(suffixes),
        "status": "passed_with_waiver" if waivers else "passed",
        "waivers": waivers,
    }


# pylint: enable=too-many-branches,too-many-statements


# Keep the ordered evidence rejection checks explicit and adjacent.
# pylint: disable=too-many-branches,too-many-statements
def check(
    *,
    app: Path,
    first_tier: Path,
    packaged: Path,
    corpus: Path,
    roundtrip: Path,
) -> dict[str, Any]:
    app = app.expanduser().resolve(strict=True)
    first = _load(first_tier.expanduser().resolve(strict=True))
    packed = _load(packaged.expanduser().resolve(strict=True))
    corpus_summary = _check_corpus(corpus)
    if first.get("ok") is not True or packed.get("ok") is not True:
        raise AssertionError("Office evidence did not pass")
    files = first.get("files") or {}
    captures = first.get("captures") or {}
    if set(files) != REQUIRED_EDITORS or set(captures) != REQUIRED_EDITORS:
        raise AssertionError(
            "Office first-tier evidence must cover all four editors",
        )
    verified_files: dict[str, dict[str, Any]] = {}
    for editor in sorted(REQUIRED_EDITORS):
        evidence = files[editor]
        path = Path(str(evidence.get("path") or "")).resolve(strict=True)
        if _sha256(path) != evidence.get("sha256"):
            raise AssertionError(f"Office file hash is stale: {editor}")
        screenshot = Path(
            str(captures[editor].get("screenshot") or ""),
        ).resolve(strict=True)
        if _sha256(screenshot) != captures[editor].get("sha256"):
            raise AssertionError(f"Office screenshot hash is stale: {editor}")
        verified_files[editor] = {
            "path": str(path),
            "sha256": evidence["sha256"],
            "screenshot": str(screenshot),
        }
    receipts = (first.get("openxml") or {}).get("receipts") or []
    if len(receipts) < 6:
        raise AssertionError(
            "Office evidence omitted transactional write receipts",
        )
    for receipt_evidence in receipts:
        receipt = receipt_evidence.get("receipt") or {}
        if receipt.get("atomic") is not True or not receipt.get(
            "after_sha256",
        ):
            raise AssertionError("Office write was not proven atomic")
    readback = (first.get("openxml") or {}).get("readback") or {}
    if not READBACK_KEYS.issubset(readback):
        raise AssertionError(
            "Office OpenXML fresh-process readback is incomplete",
        )
    pages = ((first.get("pdf") or {}).get("ocr") or {}).get("pages") or []
    if len(pages) != 2:
        raise AssertionError("PDF reopen/OCR evidence is incomplete")

    if Path(str(packed.get("app") or "")).resolve() != app:
        raise AssertionError(
            "Packaged Office evidence is bound to another app",
        )
    app_metadata = packed.get("app_metadata") or {}
    if Path(str(app_metadata.get("path") or "")).resolve() != app:
        raise AssertionError(
            "Packaged Office metadata is bound to another app",
        )
    current = _app_metadata(app)
    executable_sha256 = current["executable_sha256"]
    roundtrip_summary = _check_roundtrip(
        roundtrip,
        app=app,
        current=current,
        corpus_fixtures=corpus_summary["fixtures"],
    )
    _check_app_binding(app_metadata, app, current)
    packed_results = packed.get("results") or []
    suffixes = {
        Path(str(item.get("fixture") or "")).suffix.lower()
        for item in packed_results
    }
    if suffixes != {".docx", ".xlsx", ".pptx", ".pdf"}:
        raise AssertionError("Packaged Office evidence omitted a file type")
    packaged_counts = {
        suffix: sum(
            Path(str(item.get("fixture") or "")).suffix.lower() == suffix
            for item in packed_results
        )
        for suffix in suffixes
    }
    if len(packed_results) != 20 or any(
        count != 5 for count in packaged_counts.values()
    ):
        raise AssertionError(
            "Packaged Office evidence must open five files of every type",
        )
    observed: set[str] = set()
    for result in packed_results:
        fixture = Path(str(result.get("fixture") or "")).resolve(strict=True)
        key = str(fixture)
        expected = corpus_summary["fixtures"].get(key)
        if key in observed or expected is None:
            raise AssertionError(
                (
                    "Packaged Office contains duplicate files or file"
                    "s outside its corpus"
                ),
            )
        observed.add(key)
        if (
            result.get("fixture_sha256") != expected["sha256"]
            or _sha256(fixture) != expected["sha256"]
        ):
            raise AssertionError(
                "Packaged Office corpus fixture hash is stale",
            )
        visual = result.get("visual") or {}
        screenshot = Path(str(result.get("screenshot") or "")).resolve(
            strict=True,
        )
        if (
            not screenshot.is_file()
            or float(visual.get("luma_range") or 0) < 35
        ):
            raise AssertionError(
                "Packaged Office renderer is blank or unproven",
            )
        if _sha256(screenshot) != result.get("screenshot_sha256"):
            raise AssertionError("Packaged Office screenshot hash is stale")
        if float(visual.get("dark_ratio") or 0) < 0.01:
            raise AssertionError(
                "Packaged Office renderer contains no visible content",
            )
        actual_visual = require_visible_screenshot(screenshot)
        if visual != actual_visual:
            raise AssertionError(
                "Packaged Office visual receipt differs from actual pixels",
            )
        editor = result.get("editor") or {}
        bounds = editor.get("bounds") or {}
        renderer = result.get("renderer") or {}
        # Each term is a distinct required observation.
        # pylint: disable=too-many-boolean-expressions
        if (
            int(bounds.get("y") or 0) < 20
            or int(bounds.get("width") or 0) < 640
            or int(bounds.get("height") or 0) < 480
            or int(result.get("window_count") or 0) < 1
            or not str(renderer.get("url") or "").startswith("file://")
        ):
            raise AssertionError(
                "Packaged Office window geometry is unhealthy",
            )
        # pylint: enable=too-many-boolean-expressions
        _check_renderer_binding(renderer, fixture)
        _check_settlement(result.get("process_settlement"))
    if observed != set(corpus_summary["fixtures"]):
        raise AssertionError(
            "Packaged Office results do not cover the exact corpus",
        )
    return {
        "ok": True,
        "app": str(app),
        "executable_sha256": executable_sha256,
        "files": verified_files,
        "transaction_receipts": len(receipts),
        "packaged_file_types": sorted(suffixes),
        "packaged_samples_per_type": packaged_counts,
        "corpus": corpus_summary,
        "packaged_edit_undo_save_reopen": roundtrip_summary,
    }


# pylint: enable=too-many-branches,too-many-statements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--first-tier", type=Path, required=True)
    parser.add_argument("--packaged", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--roundtrip", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                check(
                    app=args.app,
                    first_tier=args.first_tier,
                    packaged=args.packaged,
                    corpus=args.corpus,
                    roundtrip=args.roundtrip,
                ),
                ensure_ascii=False,
            ),
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - release-gate CLI diagnostics
        print(
            f"Office release evidence failed: {exc}",
            file=__import__("sys").stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
