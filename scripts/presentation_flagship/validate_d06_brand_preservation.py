#!/usr/bin/env python3
"""Read-only D06 authorized-brand gate. Package checks are not visual acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
}
TEMPLATE_SHA256 = "b8de6d7bbced3b3b8c8870e47332c732cee5f0277dece1e8823f74f9c86d2cf4"
LOGO_SHA256 = "78f6545a82343399917c3aa16cfac9f5bcbf47811b51ed9f23009520795f6625"
LOGO_FRAME_EMU = (11106150, 123825, 400050, 400050)


def _xml(package: zipfile.ZipFile, part: str) -> ET.Element:
    return ET.fromstring(package.read(part))


def _relationships(package: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    folder, name = posixpath.split(part)
    rel_part = posixpath.join(folder, "_rels", name + ".rels")
    root = _xml(package, rel_part)
    result = {}
    for rel in root.findall("pkg:Relationship", NS):
        if rel.get("TargetMode", "Internal") != "Internal":
            continue
        raw = rel.get("Target", "")
        target = posixpath.normpath(raw.lstrip("/") if raw.startswith("/") else posixpath.join(folder, raw))
        if not target.startswith("ppt/") or "\\" in target:
            raise ValueError("invalid_package_relationship_target")
        ident = rel.get("Id", "")
        if not ident or ident in result:
            raise ValueError("invalid_package_relationship_id")
        result[ident] = (rel.get("Type", "").rsplit("/", 1)[-1], target)
    return result


def _single_link(package: zipfile.ZipFile, part: str, kind: str) -> str:
    links = [target for rel_kind, target in _relationships(package, part).values() if rel_kind == kind]
    if len(links) != 1:
        raise ValueError("missing_or_ambiguous_" + kind)
    return links[0]


def _slides(package: zipfile.ZipFile) -> list[str]:
    rels = _relationships(package, "ppt/presentation.xml")
    result = []
    for entry in _xml(package, "ppt/presentation.xml").findall("p:sldIdLst/p:sldId", NS):
        kind, target = rels[entry.attrib[f"{{{NS['r']}}}id"]]
        if kind != "slide" or target in result:
            raise ValueError("invalid_slide_order_relationship")
        result.append(target)
    return result


def _frame(shape: ET.Element) -> tuple[int, int, int, int]:
    transform = shape.find("p:spPr/a:xfrm", NS)
    if transform is None:
        raise ValueError("missing_native_geometry")
    if int(transform.get("rot", "0")) or transform.get("flipH", "0") not in {"0", "false"} or transform.get("flipV", "0") not in {"0", "false"}:
        raise ValueError("transformed_native_geometry")
    offset, extent = transform.find("a:off", NS), transform.find("a:ext", NS)
    if offset is None or extent is None:
        raise ValueError("missing_native_geometry")
    return int(offset.attrib["x"]), int(offset.attrib["y"]), int(extent.attrib["cx"]), int(extent.attrib["cy"])


def _visible(root: ET.Element) -> bool:
    return root.get("showMasterSp", "1") not in {"0", "false"}


def inspect_logo_inheritance(
    pptx: Path, *, logo_sha256: str, frame_emu: tuple[int, int, int, int],
    expected_slide_count: int,
) -> dict:
    """Require the active slide → layout → master → native picture → PNG chain.

    Unused media, an unrelated master, changed/cropped/hidden pictures and disabled
    master shapes cannot pass. Pixel visibility still needs exact-package review.
    """
    report: dict = {"passed": False, "findings": [], "slides": [], "visual_review": "REQUIRED_SEPARATE"}
    try:
        with zipfile.ZipFile(pptx) as package:
            parts = _slides(package)
            report["slide_count"] = len(parts)
            if len(parts) != expected_slide_count:
                report["findings"].append("expected_slide_count_mismatch")
            for number, part in enumerate(parts, 1):
                item = {"slide_number": number, "slide_part": part, "passed": False}
                report["slides"].append(item)
                try:
                    slide = _xml(package, part)
                    layout_part = _single_link(package, part, "slideLayout")
                    layout = _xml(package, layout_part)
                    master_part = _single_link(package, layout_part, "slideMaster")
                    master = _xml(package, master_part)
                    item.update(layout_part=layout_part, master_part=master_part)
                    if not _visible(slide) or not _visible(layout):
                        raise ValueError("master_shapes_hidden")
                    rels = _relationships(package, master_part)
                    matches = []
                    for picture in master.findall("p:cSld/p:spTree/p:pic", NS):
                        ident = picture.find("p:nvPicPr/p:cNvPr", NS)
                        blip = picture.find("p:blipFill/a:blip", NS)
                        if blip is None:
                            continue
                        relation = rels.get(blip.get(f"{{{NS['r']}}}embed", ""))
                        if relation is None or relation[0] != "image":
                            continue
                        image_part = relation[1]
                        if hashlib.sha256(package.read(image_part)).hexdigest() != logo_sha256:
                            continue
                        if ident is None or ident.get("hidden", "0") in {"1", "true"}:
                            continue
                        if _frame(picture) != frame_emu:
                            continue
                        crop = picture.find("p:blipFill/a:srcRect", NS)
                        if crop is not None and any(int(value) != 0 for value in crop.attrib.values()):
                            continue
                        if blip.findall("a:alphaModFix", NS) or list(blip):
                            continue
                        locks = picture.find("p:nvPicPr/p:cNvPicPr/a:picLocks", NS)
                        if locks is None or locks.get("noChangeAspect") not in {"1", "true"}:
                            continue
                        matches.append(image_part)
                    if len(matches) != 1:
                        raise ValueError("exact_visible_native_master_logo_missing_or_ambiguous")
                    item.update(passed=True, logo_part=matches[0])
                except (KeyError, ValueError, ET.ParseError) as exc:
                    # Never emit source/model-owned XML or arbitrary values.
                    code = str(exc) if isinstance(exc, ValueError) else type(exc).__name__.lower()
                    if not code.replace("_", "").isalnum() or len(code) > 96:
                        code = "invalid_logo_chain"
                    report["findings"].append(f"slide_{number}:{code}")
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, ET.ParseError):
        report["findings"].append("invalid_presentation_package")
    report["passed"] = bool(report["slides"]) and not report["findings"]
    return report


def _shapes(root: ET.Element) -> list[ET.Element]:
    return root.findall("p:cSld/p:spTree/p:sp", NS)


def _text(shape: ET.Element) -> str:
    return "".join(node.text or "" for node in shape.findall(".//a:t", NS))


def validate_brand_preservation(template: Path, output: Path, *, expected_slide_count: int = 10) -> dict:
    report = inspect_logo_inheritance(
        output, logo_sha256=LOGO_SHA256, frame_emu=LOGO_FRAME_EMU,
        expected_slide_count=expected_slide_count,
    )
    report["case"] = "D06-authorized-synthetic-brand-replacement-v4"
    report["original_case_status"] = "UNVERIFIABLE_NO_LOGO"
    report["template_sha256"] = hashlib.sha256(template.read_bytes()).hexdigest()
    report["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    report["product_acceptance"] = "REQUIRES_GENERATION_FACT_VISUAL_EDIT_AND_OFFICE_EVIDENCE"
    if report["template_sha256"] != TEMPLATE_SHA256:
        report["findings"].append("authorized_template_pin_mismatch")
    with zipfile.ZipFile(template) as source, zipfile.ZipFile(output) as target:
        source_parts, output_parts = _slides(source), _slides(target)
        if len(source_parts) != 3:
            report["findings"].append("source_template_slide_count_mismatch")
        expected_size = _xml(source, "ppt/presentation.xml").find("p:sldSz", NS)
        actual_size = _xml(target, "ppt/presentation.xml").find("p:sldSz", NS)
        if expected_size is None or actual_size is None or any(expected_size.get(key) != actual_size.get(key) for key in ("cx", "cy")):
            report["findings"].append("template_slide_size_changed")
        reference = {
            shape.find("p:nvSpPr/p:cNvPr", NS).get("name"): shape
            for shape in _shapes(_xml(source, source_parts[1]))
            if shape.find("p:nvSpPr/p:cNvPr", NS) is not None
        }
        for number, part in enumerate(output_parts, 1):
            shapes = _shapes(_xml(target, part))
            for name in ("Shape 0", "Text 1", "Shape 2", "Text 3", "Text 4"):
                candidates = [shape for shape in shapes if _frame(shape) == _frame(reference[name])]
                matched = False
                for shape in candidates:
                    if name == "Text 3" and _text(shape) != "青禾实验室 · 内部讨论":
                        continue
                    if name == "Text 4" and _text(shape) != str(number):
                        continue
                    if name == "Text 1" and not _text(shape).strip():
                        continue
                    if name == "Shape 0" and not any(node.get("val", "").upper() == "683FA5" for node in shape.findall("p:spPr/a:solidFill/a:srgbClr", NS)):
                        continue
                    matched = True
                if not matched:
                    report["findings"].append(f"slide_{number}:strict_anchor_{name.replace(' ', '_')}_changed")
    report["passed"] = bool(report["slides"]) and not report["findings"]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-slide-count", type=int, default=10)
    args = parser.parse_args()
    report = validate_brand_preservation(args.template, args.output, expected_slide_count=args.expected_slide_count)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
