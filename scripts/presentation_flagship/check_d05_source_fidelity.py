#!/usr/bin/env python3
"""Check source-sensitive claims in the local D05 teaching prototype.

These slide-specific expectations derive from SRC-T01 / D05_teaching_notes.md.
This is neither a generic external-product scorer nor an aesthetic or product-path pass.
"""

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile


NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main"}


def slide_text(root: ET.Element) -> str:
    return "".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))


def check(path: str) -> list[str]:
    failures: list[str] = []
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        slide_names = {
            name for name in names
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        }
        if len(slide_names) != 8:
            failures.append(f"expected 8 slides, found {len(slide_names)}")
        roots = {
            number: ET.fromstring(archive.read(f"ppt/slides/slide{number}.xml"))
            for number in range(1, 9)
            if f"ppt/slides/slide{number}.xml" in names
        }
        slides = {number: slide_text(root) for number, root in roots.items()}

        def require(number: int, phrases: tuple[str, ...]) -> None:
            for phrase in phrases:
                if phrase not in slides.get(number, ""):
                    failures.append(f"slide {number} missing source point: {phrase}")

        require(1, ("SRC-T01", "合成教学", "建立主次", "区分类别", "表示状态"))
        require(2, ("建立主次", "区分类别", "表示状态", "不存在", "永远对应"))
        require(3, ("24", "12", "6", "同样亮", "同样大", "仅供练习"))
        require(4, ("24", "12", "6", "数字与含义不变", "不是篡改数据"))
        require(5, ("同类对象共享语义", "标签", "位置", "形状", "不应每页任意换色"))
        require(6, ("正常", "注意", "错误", "继续当前流程", "检查信息再继续", "停下并修正"))
        require(7, ("主要结论", "两个支撑", "一个备注", "不需要真实公司资料", "甲", "乙", "丙", "丁"))
        require(8, ("去掉颜色", "看得懂吗", "主要结论", "支撑 01", "支撑 02", "备注", "文化、屏幕与视觉差异"))

        for number in (3, 4):
            for value in ("24", "12", "6"):
                if slides.get(number, "").count(value) != 1:
                    failures.append(f"slide {number} should contain exercise value {value} exactly once")
        for number in range(1, 9):
            root = roots.get(number)
            if root is not None and len(root.findall(".//p:sp", NS)) < 5:
                failures.append(f"slide {number} lacks native text/shape objects")
        media = [name for name in names if name.startswith("ppt/media/")]
        if media:
            failures.append(f"expected native-only diagrams, found embedded media: {media}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pptx")
    args = parser.parse_args()
    failures = check(args.pptx)
    for failure in failures:
        print(f"FAIL: {failure}")
    if not failures:
        print("PASS: D05 source-sensitive teaching points and native diagrams")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
