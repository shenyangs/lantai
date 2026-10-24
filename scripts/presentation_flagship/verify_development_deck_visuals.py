"""Check two observed visual regressions in the synthetic development decks.

This is a targeted render lock, not a general aesthetic or product acceptance test.
"""

import argparse
from pathlib import Path

from PIL import Image


def check_render_inventory(render_root):
    for deck, slide_count in (("d01", 19), ("d02", 12), ("d03", 10)):
        render_dir = render_root / f"render-{deck}"
        actual = {path.name for path in render_dir.glob("slide-*.png")}
        expected = {f"slide-{number}.png" for number in range(1, slide_count + 1)}
        if actual != expected:
            raise AssertionError(
                f"{deck}: render inventory mismatch; missing={sorted(expected - actual)}, "
                f"extra={sorted(actual - expected)}"
            )
        for path in render_dir.glob("slide-*.png"):
            with Image.open(path) as image:
                if image.size != (1280, 720):
                    raise AssertionError(f"{path}: expected 1280×720 render, got {image.size}")


def pixels_in_box(image, box, predicate):
    left, top, right, bottom = box
    return [
        (x, y)
        for y in range(top, bottom)
        for x in range(left, right)
        if predicate(image.getpixel((x, y)))
    ]


def check_first_step_marker(render_root):
    image = Image.open(render_root / "render-d01" / "slide-7.png").convert("RGB")
    pixels = pixels_in_box(image, (85, 262, 109, 294), lambda rgb: min(rgb) > 220)
    if not pixels:
        raise AssertionError("D01 slide 7: first step marker is missing")
    width = max(x for x, _ in pixels) - min(x for x, _ in pixels) + 1
    height = max(y for _, y in pixels) - min(y for _, y in pixels) + 1
    if width < height:
        raise AssertionError(f"D01 slide 7: '01' is vertically wrapped ({width=} {height=})")


def check_orphan_punctuation(render_root):
    image = Image.open(render_root / "render-d03" / "slide-2.png").convert("RGB")
    pixels = pixels_in_box(image, (75, 541, 120, 573), lambda rgb: max(rgb) < 180)
    if pixels:
        raise AssertionError("D03 slide 2: orphan punctuation below the management conclusion")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("render_root", type=Path)
    args = parser.parse_args()
    check_render_inventory(args.render_root)
    check_first_step_marker(args.render_root)
    check_orphan_punctuation(args.render_root)
    print("targeted development-deck render regressions: pass")


if __name__ == "__main__":
    main()
