#!/usr/bin/env python3
"""Check source-sensitive claims in the D01 synthetic development deck.

This is an artifact-level guard, not a claim that the whole deck is factual or
visually fit for use. The expected wording follows SRC-M01 in the supplied
D01_workshop_notes.md; it must not be inferred from the authoring script.
"""

import argparse
import posixpath
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile


def slide_text(archive: ZipFile, number: int) -> str:
    root = ET.fromstring(archive.read(f"ppt/slides/slide{number}.xml"))
    return " ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))


def slide_chart_values(archive: ZipFile, number: int) -> str:
    rels = ET.fromstring(archive.read(f"ppt/slides/_rels/slide{number}.xml.rels"))
    chart_paths = []
    for relation in rels:
        if not relation.attrib.get("Type", "").endswith("/chart"):
            continue
        target = relation.attrib["Target"]
        chart_paths.append(
            target.lstrip("/") if target.startswith("/")
            else posixpath.normpath(posixpath.join("ppt/slides", target))
        )
    return " ".join(
        node.text or ""
        for chart_path in chart_paths
        for node in ET.fromstring(archive.read(chart_path)).iter()
        if node.tag.endswith("}v")
    )


def check(path: str) -> list[str]:
    with ZipFile(path) as archive:
        slides = {number: slide_text(archive, number) for number in (3, 4, 5, 11)}
        chart_text = slide_chart_values(archive, 3)

    failures: list[str] = []
    if "可直接使用" in slides[3] or "文件可打开，无需接管" not in slides[3]:
        failures.append("S03 must say the 31 files opened without manual takeover, not that they were directly usable")
    if "直接可用" in chart_text or "文件可打开、无需接管" not in chart_text:
        failures.append("S03 chart category must retain the source's opened-without-takeover wording")
    if "第二轮达成一致" in slides[4] or "复核能收敛分歧" in slides[4]:
        failures.append("S04 must not convert three evidence-free assents into resolved disagreement")
    for fact in ("单位错误", "无来源数字"):
        if fact not in slides[4]:
            failures.append(f"S04 is missing the observed {fact}")
    if "覆盖的风险" in slides[5] or "覆盖了人工补充风险的 v2" not in slides[5]:
        failures.append("S05 must describe the observed v2 overwrite as an event, not a hypothetical risk")
    if "三次达成一致" in slides[11] or "第二轮只增加赞同" not in slides[11]:
        failures.append("S11 must retain that three tasks only added assent, not evidence")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pptx")
    args = parser.parse_args()
    failures = check(args.pptx)
    for failure in failures:
        print(f"FAIL: {failure}")
    if not failures:
        print("PASS: D01 source-sensitive slide claims")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
