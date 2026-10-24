#!/usr/bin/env python3
"""Guard the local D04 prototype's source-sensitive claims against SRC-R01.

Slide-specific expectations make this a local regression lock, not a generic
external-product scorer, visual check, model pass, or product-path pass.
The fixed expectations come from benchmarks/materials/D04_research_notes.md in
lantai-ppt-flagship-rebuild-20260922.zip, not from the authoring code.
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
}


def xml_text(root: ET.Element) -> str:
    return "".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))


def check(path: str) -> list[str]:
    failures: list[str] = []
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        slide_names = {
            name for name in names
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            and name.removeprefix("ppt/slides/slide").removesuffix(".xml").isdigit()
        }
        if len(slide_names) != 12:
            failures.append(f"expected 12 slides, found {len(slide_names)}")
        roots = {
            number: ET.fromstring(archive.read(f"ppt/slides/slide{number}.xml"))
            for number in range(1, 13)
            if f"ppt/slides/slide{number}.xml" in names
        }
        slides = {number: xml_text(root) for number, root in roots.items()}
        whole = "".join(slides.values())

        def require(number: int, phrases: tuple[str, ...]) -> None:
            for phrase in phrases:
                if phrase not in slides.get(number, ""):
                    failures.append(f"slide {number} missing source claim: {phrase}")

        require(1, ("SRC-R01", "合成测试", "48", "+15", "+21"))
        require(3, ("定义", "示例", "反例", "练习", "不是学生能力"))
        require(4, ("48", "24", "非随机", "教师", "因果"))
        require(5, ("个体数据", "方差", "显著性", "置信区间"))
        require(8, (
            "先看哪里不对，比较容易理解为什么要这样做",
            "例子太多时，反而忘记定义",
            "匿名合成引语",
        ))
        require(9, ("变化对象", "颜色", "位置", "标签", "形状", "前后", "练习时间"))
        require(10, ("61", "76", "+15", "60", "81", "+21", "后续研究检验"))
        require(11, ("样本小", "非随机", "教师因素", "只测短期"))
        require(12, ("随机或交叉", "延迟测验", "过程表现", "预算与时间", "未给出"))

        table = roots.get(6).find(".//a:tbl", NS) if 6 in roots else None
        if table is None:
            failures.append("slide 6 has no native table")
        else:
            rows = [
                [xml_text(cell) for cell in row.findall("a:tc", NS)]
                for row in table.findall("a:tr", NS)
            ]
            expected = [
                ["材料顺序", "人数", "课前平均", "课后平均", "平均变化"],
                ["A · 定义后示例", "24", "61", "76", "+15"],
                ["B · 反例后定义 + 练习", "24", "60", "81", "+21"],
            ]
            if rows != expected:
                failures.append(f"slide 6 native table differs from SRC-R01: {rows!r}")

        if 7 not in roots or roots[7].find(".//c:chart", NS) is None:
            failures.append("slide 7 has no native chart")
        chart_names = [name for name in names if name.startswith("ppt/slides/charts/chart") and name.endswith(".xml")]
        if len(chart_names) != 1:
            failures.append(f"expected one native chart data part, found {len(chart_names)}")
        else:
            chart = ET.fromstring(archive.read(chart_names[0]))
            series_values = [
                [v.text or "" for v in series.findall(".//c:numCache/c:pt/c:v", NS)]
                for series in chart.findall(".//c:ser", NS)
            ]
            if series_values != [["61", "76"], ["60", "81"]]:
                failures.append(f"chart series do not match SRC-R01: {series_values!r}")

        for invented in ("来自A班", "来自B班", "来自 A 班", "来自 B 班", "2 周", "4 周", "两周", "四周"):
            if invented in whole:
                failures.append(f"unsupported D04 detail: {invented}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pptx")
    args = parser.parse_args()
    failures = check(args.pptx)
    for failure in failures:
        print(f"FAIL: {failure}")
    if not failures:
        print("PASS: D04 source-sensitive claims and native data objects")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
