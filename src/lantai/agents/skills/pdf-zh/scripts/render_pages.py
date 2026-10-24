"""Render PDF pages through Lantai's bundled PDFium boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lantai.presentation.pdf_render import render_pdf_pages


def render(source: str | Path, output_dir: str | Path) -> tuple[Path, ...]:
    return render_pdf_pages(
        source,
        output_dir,
        dpi=144,
        prefix="page-",
        zero_pad=3,
        image_format="PNG",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Render every PDF page to PNG")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    pages = render(args.path, args.output_dir)
    print(json.dumps({"pages": [str(page) for page in pages]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
