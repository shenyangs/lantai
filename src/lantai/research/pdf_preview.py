"""Hash-verified, bounded local raster previews of immutable PDF snapshots."""
from __future__ import annotations

import base64
import hashlib
import io
import math
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .evidence import _within
from .pdf_layout import PDF_RENDER_LOCK


@contextmanager
def verified_pdf_page(workspace: Path, source: dict, number: int):
    """Yield a private copy of exactly the source bytes after hash validation."""
    mapping = next((p for p in source['pages'] if p['page'] == number), None)
    if isinstance(number, bool) or number < 1 or mapping is None:
        raise ValueError('Invalid PDF page number')
    relative = Path(source['snapshot_path'])
    if relative.suffix.lower() != '.pdf':
        raise ValueError('Page preview requires a PDF source')
    path = _within(workspace, str(relative))
    directory = _within(workspace, '.lantai/research/blobs')
    if path.parent != directory or path.name != source['raw_hash'] + '.pdf':
        raise ValueError('PDF snapshot integrity path mismatch')
    # Copy and hash in one pass: rendering uses the exact verified bytes, with
    # bounded Python memory even for a large document or a concurrently changed file.
    with tempfile.TemporaryDirectory(prefix='lantai-research-preview-') as folder:
        fixed = Path(folder) / 'source.pdf'
        digest = hashlib.sha256()
        with path.open('rb') as original, fixed.open('xb') as snapshot:
            for piece in iter(lambda: original.read(1024 * 1024), b''):
                digest.update(piece)
                snapshot.write(piece)
        if digest.hexdigest() != source['raw_hash']:
            raise ValueError('PDF snapshot integrity mismatch')
        yield fixed, mapping


def preview_page(workspace: Path, source: dict, number: int) -> dict:
    with verified_pdf_page(workspace, source, number) as (fixed, mapping):
        import pypdfium2
        with PDF_RENDER_LOCK:
            document = pypdfium2.PdfDocument(str(fixed))
            try:
                page = document[number - 1]
                try:
                    width, height = page.get_size()
                    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                        raise ValueError('Invalid PDF page geometry')
                    native_frame = page.get_rotation() == 0 and all(
                        abs(a - b) < .01 for box in (page.get_mediabox(), page.get_cropbox())
                        for a, b in zip(box, (0, 0, width, height)))
                    allowed = mapping.get('coordinate_system') == 'normalized_top_left' and (
                        mapping.get('text_origin') == 'local_vision_ocr' or native_frame)
                    bitmap = page.render(scale=min(2, 1800 / max(width, height)))
                    try:
                        image = bitmap.to_pil()
                        try:
                            buffer = io.BytesIO()
                            image.save(buffer, format='PNG')
                            result = dict(width=image.width, height=image.height,
                                          png_base64=base64.b64encode(buffer.getvalue()).decode('ascii'))
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                document.close()
    return dict(source_version_id=source['id'], raw_hash=source['raw_hash'], page=number,
                overlay_allowed=allowed,
                overlay_reason='' if allowed else 'native_coordinate_frame_unverified', **result)
