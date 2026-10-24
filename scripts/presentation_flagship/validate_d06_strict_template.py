#!/usr/bin/env python3
"""Read-only OOXML check for the synthetic D06 strict-template artifact."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


def shape_map(xml: bytes) -> dict[str, ET.Element]:
    root = ET.fromstring(xml)
    result = {}
    for shape in root.findall(".//p:sp", NS):
        ident = shape.find("p:nvSpPr/p:cNvPr", NS)
        if ident is not None:
            result[ident.attrib["name"]] = shape
    return result


def text(shape: ET.Element) -> str:
    return "".join(node.text or "" for node in shape.findall(".//a:t", NS))


def geometry(shape: ET.Element) -> tuple[str, str, str, str]:
    xfrm = shape.find("p:spPr/a:xfrm", NS)
    if xfrm is None:
        raise AssertionError("shape has no geometry")
    off = xfrm.find("a:off", NS)
    ext = xfrm.find("a:ext", NS)
    return off.attrib["x"], off.attrib["y"], ext.attrib["cx"], ext.attrib["cy"]


def colors(shape: ET.Element) -> set[str]:
    return {node.attrib["val"].upper() for node in shape.findall(".//a:srgbClr", NS)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--edit-probe", type=Path)
    args = parser.parse_args()
    report = {"slide_count": 10, "footer": "青禾实验室 · 内部讨论", "findings": []}
    with zipfile.ZipFile(args.source) as src, zipfile.ZipFile(args.output) as out:
        source_shapes = [shape_map(src.read(f"ppt/slides/slide{i}.xml")) for i in range(1, 4)]
        output_slides = [shape_map(out.read(f"ppt/slides/slide{i}.xml")) for i in range(1, 11)]
        source_master = src.read("ppt/slideMasters/slideMaster1.xml")
        master_shape_count = len(ET.fromstring(source_master).findall(".//p:sp", NS))
        report["source_master_shape_count"] = master_shape_count
        source_layouts = sorted(name for name in src.namelist() if name.startswith("ppt/slideLayouts/slideLayout") and name.endswith(".xml"))
        report["source_layout_shape_counts"] = {
            name: len(ET.fromstring(src.read(name)).findall(".//p:sp", NS)) for name in source_layouts
        }
        logo_observed = master_shape_count > 0 or any(report["source_layout_shape_counts"].values())
        report["master_logo_claim"] = "inspect_required" if logo_observed else "not_observed_in_supplied_fixture"
        report["strict_logo_preservation_verifiable"] = logo_observed
        for number, shapes in enumerate(output_slides, start=1):
            reference = source_shapes[number - 1 if number <= 3 else 1]
            for name in ("Shape 0", "Text 1", "Shape 2", "Text 3", "Text 4"):
                if name not in shapes:
                    report["findings"].append(f"slide {number}: missing {name}")
                    continue
                if geometry(shapes[name]) != geometry(reference[name]):
                    report["findings"].append(f"slide {number}: {name} geometry changed")
            if "Text 5" in shapes:
                report["findings"].append(f"slide {number}: source placeholder remains")
            if text(shapes["Text 3"]) != report["footer"]:
                report["findings"].append(f"slide {number}: HARD footer differs")
            if text(shapes["Text 4"]) != str(number):
                report["findings"].append(f"slide {number}: page number differs")
            if "683FA5" not in colors(shapes["Shape 0"]):
                report["findings"].append(f"slide {number}: purple bar differs")
            if "supplemental-wordmark" not in shapes:
                report["findings"].append(f"slide {number}: synthetic wordmark missing")
            if len(shapes) < 10:
                report["findings"].append(f"slide {number}: content shape count unexpectedly low")
        report["native_shape_count"] = sum(len(shapes) for shapes in output_slides)
        notes = sorted(name for name in out.namelist() if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml"))
        report["notes_count"] = len(notes)
        if len(notes) != 10:
            report["findings"].append(f"expected 10 note parts; found {len(notes)}")
        for name in notes:
            note_text = "".join(node.text or "" for node in ET.fromstring(out.read(name)).findall(".//a:t", NS))
            if "SRC-B01" not in note_text or "合成开发材料" not in note_text:
                report["findings"].append(f"{name}: synthetic source note missing")
    if args.edit_probe:
        with zipfile.ZipFile(args.edit_probe) as probe:
            probe_slide_count = sum(
                name.startswith("ppt/slides/slide") and name.endswith(".xml")
                for name in probe.namelist()
            )
            probe_shapes = shape_map(probe.read("ppt/slides/slide4.xml"))
            report["edit_probe"] = {
                "slide_count": probe_slide_count,
                "slide_4_row_value_1": text(probe_shapes["row-value-1"]),
            }
            if probe_slide_count != 10 or report["edit_probe"]["slide_4_row_value_1"] != "文字、表格、图解可二次修改":
                report["findings"].append("edit/save/reopen probe did not retain the native text edit")
    report["structural_checks_passed"] = not report["findings"]
    report["strict_policy_fully_verifiable"] = report["structural_checks_passed"] and report["strict_logo_preservation_verifiable"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["structural_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
