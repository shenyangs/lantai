# -*- coding: utf-8 -*-
"""Fail-closed text extraction for presentation source materials.

This is not a general Word renderer. Unsupported semantic surfaces must be
reported before the planner can treat a source as covered.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from zipfile import BadZipFile, ZipFile

import olefile

from .models import InputMaterial, PresentationSourceCoverage


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_MAX_MATERIAL_CHARS = 1_000_000
_MAX_DOCX_XML_BYTES = 16_000_000
_TEXT_SUFFIXES = frozenset(
    {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml"}
)
_UNSUPPORTED_ELEMENTS = {
    f"{_W}drawing": "drawing/chart/image",
    f"{_W}pict": "drawing/chart/image",
    f"{_W}object": "embedded object",
    f"{_W}txbxContent": "text box",
    f"{_W}altChunk": "alternate content",
    f"{_W}footnoteReference": "footnotes",
    f"{_W}endnoteReference": "endnotes",
    f"{_W}commentReference": "comments",
    f"{_W}commentRangeStart": "comments",
    f"{_W}ins": "tracked changes",
    f"{_W}del": "tracked changes",
    f"{_W}moveFrom": "tracked changes",
    f"{_W}moveTo": "tracked changes",
    f"{_W}sdt": "content control",
    f"{_W}fldSimple": "field",
    f"{_W}fldChar": "field",
    f"{_W}instrText": "field",
    f"{_W}hyperlink": "hyperlink target",
    f"{_W}numPr": "numbering",
    f"{_W}gridSpan": "merged table cells",
    f"{_W}vMerge": "merged table cells",
    f"{_W}customXml": "custom XML content",
    f"{_M}oMath": "equation",
    f"{_M}oMathPara": "equation",
    f"{_MC}AlternateContent": "alternate content",
}


class PresentationMaterialError(ValueError):
    """A declared source cannot be completely grounded by this reader."""


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _material_snapshot(path: Path, expected_digest: str) -> Iterator[Path]:
    """Bind every parser to the same private bytes as the source checksum."""

    with tempfile.TemporaryDirectory(prefix="lantai-ppt-source-") as temporary:
        snapshot = Path(temporary) / path.name
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source, snapshot.open("xb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
                    digest.update(chunk)
        except OSError as exc:
            raise PresentationMaterialError(
                f"unable to snapshot material {path.name}"
            ) from exc
        if digest.hexdigest() != expected_digest:
            raise PresentationMaterialError(
                f"material {path.name} changed while preparing its source snapshot"
            )
        yield snapshot


def _bounded_text(text: str, name: str) -> str:
    if len(text) > _MAX_MATERIAL_CHARS:
        raise PresentationMaterialError(
            f"material {name} exceeds the {_MAX_MATERIAL_CHARS}-character extraction limit"
        )
    return text


def _paragraph_text(paragraph: ET.Element) -> str:
    pieces: list[str] = []
    for element in paragraph.iter():
        if element.tag == f"{_W}t":
            pieces.append(element.text or "")
        elif element.tag == f"{_W}noBreakHyphen":
            pieces.append("\u2011")
        elif element.tag == f"{_W}softHyphen":
            pieces.append("\u00ad")
        elif element.tag == f"{_W}tab":
            pieces.append("\t")
        elif element.tag in {f"{_W}br", f"{_W}cr"}:
            pieces.append("\n")
    return "".join(pieces).strip()


def _table_lines(table: ET.Element) -> list[str]:
    lines: list[str] = []
    for row in table.findall(f"{_W}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{_W}tc"):
            blocks: list[str] = []
            for block in cell:
                if block.tag == f"{_W}p":
                    value = _paragraph_text(block)
                    if value:
                        blocks.append(value)
                elif block.tag == f"{_W}tbl":
                    blocks.extend(_table_lines(block))
            cells.append(" / ".join(blocks))
        if any(cells):
            lines.append(" | ".join(cells))
    return lines


def _numbered_style_ids(styles_root: ET.Element | None) -> set[str]:
    if styles_root is None:
        return set()
    parents: dict[str, str] = {}
    numbered: set[str] = set()
    for style in styles_root.findall(f"{_W}style"):
        style_id = style.get(f"{_W}styleId")
        if not style_id:
            continue
        if style.find(f"{_W}pPr/{_W}numPr") is not None:
            numbered.add(style_id)
        parent = style.find(f"{_W}basedOn")
        if parent is not None and parent.get(f"{_W}val"):
            parents[style_id] = parent.get(f"{_W}val", "")
    changed = True
    while changed:
        inherited = {
            style_id
            for style_id, parent_id in parents.items()
            if parent_id in numbered
        }
        changed = not inherited <= numbered
        numbered.update(inherited)
    return numbered


def _docx_text(path: Path) -> str:
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise PresentationMaterialError(
                    f"DOCX {path.name} has duplicate package entries"
                )
            if "word/document.xml" not in names:
                raise PresentationMaterialError(
                    f"DOCX {path.name} has no word/document.xml"
                )
            unsupported_parts = [
                name
                for name in names
                if name.startswith(
                    (
                        "word/comments",
                        "word/footnotes",
                        "word/endnotes",
                        "word/charts/",
                        "word/media/",
                        "word/embeddings/",
                    )
                )
            ]
            if unsupported_parts:
                raise PresentationMaterialError(
                    f"DOCX {path.name} contains unsupported "
                    f"{', '.join(unsupported_parts[:4])}; source coverage is incomplete"
                )
            if any(re.fullmatch(r"word/(?:header|footer)\d+\.xml", name) for name in names):
                raise PresentationMaterialError(
                    f"DOCX {path.name} contains unsupported header/footer; "
                    "source coverage is incomplete"
                )
            info = archive.getinfo("word/document.xml")
            if info.file_size > _MAX_DOCX_XML_BYTES:
                raise PresentationMaterialError(
                    f"DOCX {path.name} document.xml exceeds safe extraction limit"
                )
            root = ET.fromstring(archive.read(info))
            styles_root = None
            if "word/styles.xml" in names:
                style_info = archive.getinfo("word/styles.xml")
                if style_info.file_size > _MAX_DOCX_XML_BYTES:
                    raise PresentationMaterialError(
                        f"DOCX {path.name} styles.xml exceeds safe extraction limit"
                    )
                styles_root = ET.fromstring(archive.read(style_info))
    except (BadZipFile, ET.ParseError, OSError, RuntimeError) as exc:
        raise PresentationMaterialError(
            f"unable to read DOCX {path.name}: {exc}"
        ) from exc

    unsupported = sorted(
        {_UNSUPPORTED_ELEMENTS[el.tag] for el in root.iter() if el.tag in _UNSUPPORTED_ELEMENTS}
    )
    if unsupported:
        raise PresentationMaterialError(
            f"DOCX {path.name} contains unsupported {', '.join(unsupported)}; "
            "source coverage is incomplete"
        )
    numbered_styles = _numbered_style_ids(styles_root)
    for paragraph in root.iter(f"{_W}p"):
        style = paragraph.find(f"{_W}pPr/{_W}pStyle")
        if style is not None and style.get(f"{_W}val") in numbered_styles:
            raise PresentationMaterialError(
                f"DOCX {path.name} uses style-inherited numbering; "
                "source coverage is incomplete"
            )
    body = root.find(f"{_W}body")
    if body is None:
        raise PresentationMaterialError(f"DOCX {path.name} has no body")
    lines: list[str] = []
    for block in body:
        if block.tag == f"{_W}p":
            value = _paragraph_text(block)
            if not value:
                continue
            style = block.find(f"{_W}pPr/{_W}pStyle")
            style_id = style.get(f"{_W}val", "") if style is not None else ""
            heading = re.fullmatch(r"Heading([1-6])", style_id, re.IGNORECASE)
            lines.append(f"{'#' * int(heading.group(1))} {value}" if heading else value)
        elif block.tag == f"{_W}tbl":
            lines.extend(_table_lines(block))
        elif block.tag != f"{_W}sectPr":
            raise PresentationMaterialError(
                f"DOCX {path.name} contains unsupported body element {block.tag}"
            )
    return _bounded_text("\n".join(lines), path.name)


def _validate_legacy_doc(path: Path) -> None:
    """Reject non-Word payloads before LibreOffice can import them as text."""

    try:
        if not olefile.isOleFile(str(path)):
            raise OSError("OLE compound-file signature is invalid")
        with olefile.OleFileIO(str(path)) as compound:
            if not compound.exists("WordDocument"):
                raise OSError("WordDocument stream is missing")
            if compound.get_size("WordDocument") <= 0:
                raise OSError("WordDocument stream is empty")
    except (OSError, ValueError, IndexError) as exc:
        raise PresentationMaterialError(
            f"DOC {path.name} is not a valid OLE compound document: {exc}"
        ) from exc


def read_material(material: InputMaterial) -> tuple[str, PresentationSourceCoverage]:
    converted_sha256: str | None = None
    if material.content is not None:
        content = _bounded_text(material.content, material.name)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if material.sha256 and material.sha256.removeprefix("sha256:") != digest:
            raise PresentationMaterialError(
                f"material {material.name} changed since its declared sha256"
            )
        method = "inline_text"
        status = "supported_content_extracted"
        limitations: list[str] = []
    else:
        if not material.path:
            raise PresentationMaterialError(
                f"material {material.name!r} has no readable path"
            )
        path = Path(material.path).expanduser()
        if not path.is_file():
            raise PresentationMaterialError(f"material file does not exist: {path}")
        digest = _digest(path)
        if material.sha256 and material.sha256.removeprefix("sha256:") != digest:
            raise PresentationMaterialError(
                f"material {path.name} changed since its declared sha256"
            )
        with _material_snapshot(path, digest) as path:
            suffix = path.suffix.lower()
            media_type = str(material.media_type or "").lower()
            limitations = []
            if suffix == ".docx":
                content = _docx_text(path)
                method = "docx_ooxml"
                status = "supported_content_extracted"
            elif suffix == ".doc":
                from lantai.app.office_conversion import convert_office_document

                _validate_legacy_doc(path)
                try:
                    with tempfile.TemporaryDirectory(prefix="lantai-ppt-doc-") as temp:
                        converted = convert_office_document(path, "docx", temp)
                        content = _docx_text(converted.output_path)
                        converted_sha256 = _digest(converted.output_path)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise PresentationMaterialError(
                        f"unable to convert DOC {path.name} to DOCX: {exc}"
                    ) from exc
                method = "libreoffice_doc_to_docx_ooxml"
                status = "converted_doc_unverified"
                limitations = [
                    "Legacy DOC conversion may lose content; compare extracted text "
                    "and converted file against the original before factual approval."
                ]
            elif suffix == ".pdf" or media_type == "application/pdf":
                from lantai.app.wiki.core import extract_source_text_detail

                try:
                    detail = extract_source_text_detail(
                        path,
                        detected_suffix=".pdf",
                        require_complete_pdf=True,
                        pdf_max_chars=_MAX_MATERIAL_CHARS,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    raise PresentationMaterialError(
                        f"PDF {path.name} did not yield verified text (status: failed)"
                    ) from exc
                raw_status = detail.get("status") if isinstance(detail, dict) else None
                safe_status = (
                    raw_status
                    if raw_status
                    in {
                        "text_extracted",
                        "ocr_extracted",
                        "no_text",
                        "encrypted",
                        "corrupt",
                        "failed",
                        "limit_exceeded",
                    }
                    else "failed"
                )
                extracted = detail.get("text") if isinstance(detail, dict) else None
                if safe_status == "limit_exceeded":
                    raise PresentationMaterialError(
                        f"PDF {path.name} exceeds the {_MAX_MATERIAL_CHARS}-character extraction limit"
                    )
                page_count = detail.get("page_count") if isinstance(detail, dict) else None
                complete = (
                    isinstance(detail, dict)
                    and detail.get("text_complete") is True
                    and isinstance(page_count, int)
                    and not isinstance(page_count, bool)
                    and page_count > 0
                    and detail.get("extracted_pages") == page_count
                )
                if (
                    safe_status != "text_extracted"
                    or not complete
                    or not isinstance(extracted, str)
                    or not extracted.strip()
                ):
                    raise PresentationMaterialError(
                        f"PDF {path.name} did not yield verified text (status: {safe_status})"
                    )
                content = _bounded_text(extracted, path.name)
                raw_method = str(detail.get("method") or "unknown")
                extractor_method = re.sub(r"[^a-z0-9_]+", "_", raw_method.lower()).strip("_")
                method = f"pdf_{extractor_method or 'unknown'}"
                status = "supported_content_extracted"
                page_count = detail.get("page_count")
                page_scope = (
                    f"covered {page_count} pages"
                    if isinstance(page_count, int)
                    and not isinstance(page_count, bool)
                    and page_count > 0
                    else "reported page coverage was unavailable"
                )
                limitations = [
                    f"PDF text extraction {page_scope}; charts, images, and visual layout "
                    "were not treated as grounded facts. Only extracted text may be used."
                ]
            elif suffix in _TEXT_SUFFIXES or media_type.startswith("text/"):
                try:
                    content = _bounded_text(path.read_text(encoding="utf-8"), path.name)
                except (OSError, UnicodeError) as exc:
                    raise PresentationMaterialError(
                        f"unable to read material {path.name}: {exc}"
                    ) from exc
                method = "utf8_text"
                status = "supported_content_extracted"
            else:
                raise PresentationMaterialError(
                    f"stable generation requires a supported source; unsupported material: {path.name}"
                )
    return content, PresentationSourceCoverage(
        material_id=material.id,
        source_name=material.name,
        source_sha256=digest,
        converted_sha256=converted_sha256,
        method=method,
        status=status,
        extracted_chars=len(content),
        limitations=limitations,
    )
