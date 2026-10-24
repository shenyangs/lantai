# -*- coding: utf-8 -*-
"""Isolated PPTX rendering used by presentation visual QA."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
import posixpath
import tempfile
import time
from threading import BoundedSemaphore, Event
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter

from ..app.office_conversion import (
    OfficeConversionUnavailable,
    convert_office_document,
    find_soffice,
)

from .pdf_render import render_pdf_pages
from ..utils import owned_subprocess as subprocess
from ..utils.operation_control import (
    bind_operation,
    check_operation,
    controlled_capacity,
    current_operation,
    publication_boundary,
    remaining_timeout,
)


class PresentationRenderUnavailable(RuntimeError):
    """Raised when no supported local renderer is available."""


@dataclass(frozen=True)
class RenderResult:
    pdf_path: Path
    slide_images: tuple[Path, ...]
    renderer_id: str = "libreoffice+pdfium"
    renderer_version: str = "unknown"


_SLIDE_IMAGE_RE = re.compile(r"^slide-(\d+)\.png$")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_RENDER_POLICY_VERSION = "slide-local-pdf/v1"
_SLIDE_EXPORT_CAPACITY = BoundedSemaphore(4)


def _slide_text_counts(source: Path) -> list[Counter]:
    """Read slide order from the package, including hidden slides."""
    check_operation()
    namespaces = {
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "r": (
            "http://schemas.openxmlformats.org/officeDocument/"
            "2006/relationships"
        ),
    }
    with ZipFile(source) as archive:
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        relationships = ET.fromstring(
            archive.read("ppt/_rels/presentation.xml.rels"),
        )
        targets = {
            item.attrib["Id"]: item.attrib["Target"] for item in relationships
        }
        expected = []
        for slide in presentation.findall("p:sldIdLst/p:sldId", namespaces):
            check_operation()
            target = targets[slide.attrib[f"{{{namespaces['r']}}}id"]]
            member = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("ppt", target))
            )
            root = ET.fromstring(archive.read(member))
            expected.append(
                Counter(
                    _CJK_RE.findall(
                        "".join(
                            node.text or ""
                            for node in root.findall(".//a:t", namespaces)
                        ),
                    ),
                ),
            )
    return expected


def verify_rendered_text(source: Path, pdf_path: Path) -> None:
    """Reject substantial CJK text loss; this is not a visual acceptance test.

    Counts ignore reading order because PDF text order may differ from OOXML.
    Extractable text does not prove that a renderer drew the correct glyphs.
    """
    expected = _slide_text_counts(source)
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        if len(document) != len(expected):
            raise RuntimeError(
                "rendered PDF page count differs from PPTX slide count",
            )
        for index, required in enumerate(expected):
            check_operation()
            if not required:
                continue
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    actual = Counter(
                        _CJK_RE.findall(text_page.get_text_range()),
                    )
                finally:
                    text_page.close()
            finally:
                page.close()
            missing = sum((required - actual).values())
            if missing >= 3 and missing / sum(required.values()) > 0.05:
                raise RuntimeError(
                    f"rendered slide {index + 1} lost {missing} of "
                    f"{sum(required.values())} CJK characters; "
                    "check renderer fonts",
                )
    finally:
        document.close()


def slide_image_paths(directory: str | Path) -> tuple[Path, ...]:
    """Return renderer outputs in numeric page order, including page 10+."""

    candidates: list[tuple[int, Path]] = []
    for path in Path(directory).glob("slide-*.png"):
        match = _SLIDE_IMAGE_RE.match(path.name)
        if match:
            candidates.append((int(match.group(1)), path))
    return tuple(path for _index, path in sorted(candidates))


@lru_cache(maxsize=1)
def presentation_renderer_version() -> str:
    """Return the concrete local renderer version for cache/evidence
    binding."""

    parts: list[str] = [_RENDER_POLICY_VERSION]
    try:
        completed = subprocess.run(
            [find_soffice(), "--version"],
            capture_output=True,
            text=True,
            timeout=remaining_timeout(10),
            check=False,
        )
        detail = (completed.stdout or completed.stderr).strip()
        if detail:
            parts.append(detail)
    except (OSError, subprocess.SubprocessError, OfficeConversionUnavailable):
        check_operation()
    try:
        parts.append(f"pypdfium2 {version('pypdfium2')}")
    except PackageNotFoundError:
        pass
    try:
        parts.append(f"pypdf {version('pypdf')}")
    except PackageNotFoundError:
        pass
    return "; ".join(parts) or "unknown"


def _export_slide_local_pdf(source: Path, staging: Path, timeout: float) -> Path:
    """Do not let an edited slide change another slide's PDF font subset.

    LibreOffice's whole-deck font subsets can move unedited text by fractions
    of a point when the glyph set on another slide changes. Every slide is
    exported from the actual input with its own font subset, then assembled
    without rewriting content streams. PDFium is only used after workers join.
    """
    count = len(_slide_text_counts(source))
    if not count:
        raise RuntimeError("presentation contains no slides")
    deadline = time.monotonic() + float(remaining_timeout(timeout))
    parent = current_operation()
    aborted = Event()

    def ensure_export_active() -> None:
        if parent is not None:
            parent.ensure_active()
        if aborted.is_set():
            raise RuntimeError(
                "slide-local export cancelled after another slide failed",
            )
        if time.monotonic() >= deadline:
            raise TimeoutError("slide-local export exceeded its absolute deadline")

    def export_seconds() -> float:
        remaining = deadline - time.monotonic()
        return min(remaining, parent.remaining_seconds()) if parent else remaining

    bounded = (
        replace(
            parent,
            ensure_active=ensure_export_active,
            remaining_seconds=export_seconds,
        )
        if parent is not None else None
    )

    def convert(number: int) -> Path:
        with controlled_capacity(_SLIDE_EXPORT_CAPACITY, ensure_export_active):
            ensure_export_active()
            result = convert_office_document(
                source, "pdf", staging / "pages",
                output_name=f"slide-{number}.pdf",
                timeout=export_seconds(),
                pdf_page_range=number,
            )
            ensure_export_active()
            if len(PdfReader(result.output_path).pages) != 1:
                raise RuntimeError(
                    f"slide-local export {number} did not produce one page",
                )
            return result.output_path

    def export(number: int) -> Path:
        try:
            if bounded is not None:
                with bind_operation(bounded):
                    return convert(number)
            return convert(number)
        except BaseException:
            aborted.set()
            raise

    with ThreadPoolExecutor(max_workers=min(4, count)) as pool:
        futures = [
            pool.submit(copy_context().run, export, number)
            for number in range(1, count + 1)
        ]
        pages = [future.result() for future in futures]
    check_operation()
    if time.monotonic() >= deadline:
        raise TimeoutError("slide-local export exceeded its absolute deadline")
    merged = staging / "presentation.pdf"
    with PdfWriter() as writer:
        for page in pages:
            check_operation()
            writer.append(page, import_outline=False)
        with merged.open("wb") as stream:
            writer.write(stream)
    return merged


def render_pptx(
    path: str | Path,
    output_dir: str | Path,
    *,
    timeout: float = 120,
) -> RenderResult:
    """Render in a disposable LibreOffice profile so jobs cannot
    contaminate each other."""
    check_operation()
    source = Path(path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        raise FileNotFoundError(source)

    with tempfile.TemporaryDirectory(
        prefix=".slide-local-render-", dir=destination,
    ) as scratch:
        staging = Path(scratch)
        try:
            staged_pdf = _export_slide_local_pdf(source, staging, timeout)
        except OfficeConversionUnavailable as exc:
            raise PresentationRenderUnavailable(str(exc)) from exc
        verify_rendered_text(source, staged_pdf)
        try:
            rendered_pages = render_pdf_pages(
                staged_pdf, staging, dpi=144, prefix="slide-", image_format="PNG",
            )
        except Exception as exc:
            check_operation()
            raise RuntimeError(f"bundled PDFium rendering failed: {exc}") from exc
        images = tuple(destination / page.name for page in rendered_pages)
        pdf_path = destination / "presentation.pdf"
        with publication_boundary():
            staged_pdf.replace(pdf_path)
            for staged, final in zip(rendered_pages, images):
                staged.replace(final)
    return RenderResult(
        pdf_path=pdf_path,
        slide_images=images,
        renderer_id="libreoffice-slide-local+pdfium",
        renderer_version=presentation_renderer_version(),
    )
