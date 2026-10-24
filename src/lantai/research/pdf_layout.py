"""Local PDF geometry, with exact snapshot alignment and opt-in scan recognition."""
from __future__ import annotations

import math
import sys
import tempfile
import threading
from functools import wraps

PDF_RENDER_LOCK = threading.RLock()


def _serialized_render(function):
    @wraps(function)
    def call(*args, **kwargs):
        with PDF_RENDER_LOCK:
            return function(*args, **kwargs)
    return call

from pathlib import Path


def map_words(text, words, width, height):
    """Accept only identical non-whitespace content in identical reading order."""
    positions = [i for i, char in enumerate(text) if not char.isspace()]
    normalized = ''.join(text[i] for i in positions)
    parts = [''.join(c for c in w['text'] if not c.isspace()) for w in words]
    if not normalized or ''.join(parts) != normalized or width <= 0 or height <= 0:
        return []
    regions, cursor = [], 0
    for word, part in zip(words, parts):
        if not part:
            continue
        x, y = word['x0'] / width, word['top'] / height
        w, h = (word['x1'] - word['x0']) / width, (word['bottom'] - word['top']) / height
        if not all(math.isfinite(v) for v in (x, y, w, h)) or not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x + 1e-6 and 0 < h <= 1 - y + 1e-6):
            return []
        regions.append(dict(start=positions[cursor], end=positions[cursor + len(part) - 1] + 1,
                            x=x, y=y, width=w, height=h, confidence=None))
        cursor += len(part)
    return regions


class PdfLayout:
    def __init__(self, path):
        self.path, self.document = path, None

    def __enter__(self):
        try:
            import pdfplumber
            self.document = pdfplumber.open(self.path)
        except Exception:
            pass
        return self

    def __exit__(self, *args):
        if self.document is not None:
            self.document.close()

    def regions(self, number, text):
        if self.document is None:
            return []
        page = None
        try:
            page = self.document.pages[number - 1]
            return map_words(text, page.extract_words(), float(page.width), float(page.height))
        except Exception:
            return []
        finally:
            if page is not None:
                page.close()


@_serialized_render
def recognize_page(path: Path, number: int):
    """Render one page locally; reuse Wiki's Vision adapter without its UI cap."""
    if sys.platform != 'darwin':
        return []
    try:
        import pypdfium2
        from ..app.wiki.core import _extract_layout_lines_from_image_with_vision
        with tempfile.TemporaryDirectory(prefix='lantai-research-ocr-') as folder:
            image_path = Path(folder) / 'page.png'
            document = pypdfium2.PdfDocument(str(path))
            try:
                page = document[number - 1]
                try:
                    width, height = page.get_size()
                    # Cap raster dimensions, never the number of pages or text lines.
                    scale = min(2.0, 3000 / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        try:
                            image.save(image_path)
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                document.close()
            return _extract_layout_lines_from_image_with_vision(
                image_path, page_number=number, line_limit=None, accurate=True)
    except Exception:
        return []


def ocr_text(lines):
    text, regions = '', []
    for line in lines:
        value = str(line.get('text') or '').strip()
        if not value:
            continue
        if text:
            text += '\n'
        start = len(text)
        text += value
        regions.append({k: line[k] for k in ('x', 'y', 'width', 'height', 'confidence')} |
                       {'start': start, 'end': len(text)})
    return text, regions
