"""Bundled PDFium rendering shared by PDF and presentation Skills."""

from __future__ import annotations

from pathlib import Path
import os
import tempfile

import pypdfium2 as pdfium
from PIL import Image

from ..utils.operation_control import check_operation, publication_boundary


def render_pdf_pages(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    dpi: int = 144,
    prefix: str = "page_",
    zero_pad: int = 0,
    image_format: str = "PNG",
    max_dim: int | None = None,
) -> tuple[Path, ...]:
    """Render every page without relying on a system Poppler executable."""

    check_operation()
    source = Path(pdf_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    normalized_format = image_format.strip().upper()
    if normalized_format not in {"JPEG", "PNG"}:
        raise ValueError(f"Unsupported PDF render format: {image_format}")
    suffix = ".jpg" if normalized_format == "JPEG" else ".png"

    document = pdfium.PdfDocument(str(source))
    outputs: list[Path] = []
    try:
        for index, page in enumerate(document, start=1):
            check_operation()
            bitmap = None
            try:
                bitmap = page.render(scale=dpi / 72)
                image = bitmap.to_pil().convert("RGB")
            finally:
                if bitmap is not None:
                    bitmap.close()
                page.close()
            if max_dim and (image.width > max_dim or image.height > max_dim):
                scale_factor = min(max_dim / image.width, max_dim / image.height)
                resized = image.resize(
                    (
                        max(1, int(image.width * scale_factor)),
                        max(1, int(image.height * scale_factor)),
                    ),
                    Image.Resampling.LANCZOS,
                )
                image.close()
                image = resized
            number = f"{index:0{zero_pad}d}" if zero_pad > 0 else str(index)
            output = destination / f"{prefix}{number}{suffix}"
            fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=destination)
            os.close(fd)
            staged = Path(temporary)
            try:
                check_operation()
                image.save(staged, normalized_format)
                with publication_boundary():
                    staged.replace(output)
                outputs.append(output)
            finally:
                image.close()
                staged.unlink(missing_ok=True)
    finally:
        document.close()
    if not outputs:
        raise RuntimeError("PDFium produced no page images")
    return tuple(outputs)
