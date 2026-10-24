"""Bounded native notes replacement without serializing visible slide graphs."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

from lxml import etree as XML

from .models import DeckIR
from .slide_package import _native_xml, _rels_path, _resolve, _slides

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _relationships(package: ZipFile, part: str):
    name = _rels_path(part)
    if name not in package.namelist():
        raise ValueError("native notes relationship part is missing")
    root = _native_xml(package.read(name))
    if root.tag != f"{{{_REL}}}Relationships":
        raise ValueError("native notes relationship root is unsupported")
    return root


def _notes_parts(package: ZipFile) -> list[str | None]:
    """Resolve active slide order and reject shared/ambiguous notes graphs."""
    result = []
    for slide in _slides(package):
        references = [rel for rel in _relationships(package, slide)
                      if rel.get("Type") == f"{_DOC_REL}/notesSlide"]
        if len(references) > 1:
            raise ValueError("native notes relationship is ambiguous")
        if not references:
            result.append(None)
            continue
        reference = references[0]
        if reference.get("TargetMode") == "External":
            raise ValueError("external native notes relationships are unsupported")
        notes = _resolve(slide, reference.get("Target", ""))
        if notes not in package.namelist():
            raise ValueError("native notes part is missing")
        backlinks = [rel for rel in _relationships(package, notes)
                     if rel.get("Type") == f"{_DOC_REL}/slide"]
        if (len(backlinks) != 1 or backlinks[0].get("TargetMode") == "External"
                or _resolve(notes, backlinks[0].get("Target", "")) != slide):
            raise ValueError("native notes slide backlink is ambiguous or incorrect")
        result.append(notes)
    existing = [part for part in result if part is not None]
    if len(existing) != len(set(existing)):
        raise ValueError("native notes parts cannot be shared by active slides")
    return result


def _notes_body(payload: bytes):
    root = _native_xml(payload)
    if root.tag != f"{{{_P}}}notes":
        raise ValueError("native notes root is unsupported")
    bodies = []
    for shape in root.iter(f"{{{_P}}}sp"):
        placeholder = shape.find(f"{{{_P}}}nvSpPr/{{{_P}}}nvPr/{{{_P}}}ph")
        if placeholder is not None and placeholder.get("type") == "body":
            bodies.append(shape.find(f"{{{_P}}}txBody"))
    if len(bodies) != 1 or bodies[0] is None:
        raise ValueError("native notes body carrier is missing or ambiguous")
    return root, bodies[0]


def _body_text(body) -> str:
    return "\n".join("".join(
        "\v" if node.tag == f"{{{_A}}}br" else node.text or ""
        for node in paragraph.iter()
        if node.tag in {f"{{{_A}}}t", f"{{{_A}}}br"}
    ) for paragraph in body.findall(f"{{{_A}}}p"))


def _style_key(node) -> bytes:
    return XML.tostring(node, method="c14n") if node is not None else b""


def _replace_body(payload: bytes, expected: str, replacement: str) -> bytes:
    root, body = _notes_body(payload)
    if _body_text(body) != expected:
        raise ValueError("native notes carrier is stale relative to DeckIR")
    paragraphs = body.findall(f"{{{_A}}}p")
    if not paragraphs:
        raise ValueError("native notes body has no paragraph carrier")
    # This version supports uniform plain note text only. Never flatten mixed
    # formatting, fields, hyperlinks or unknown paragraph-level XML silently.
    allowed_paragraph = {f"{{{_A}}}{name}" for name in ("pPr", "r", "endParaRPr")}
    allowed_run = {f"{{{_A}}}rPr", f"{{{_A}}}t"}
    run_styles = []
    for paragraph in paragraphs:
        if any(child.tag not in allowed_paragraph for child in paragraph):
            raise ValueError("native notes contain unsupported paragraph content")
        for run in paragraph.findall(f"{{{_A}}}r"):
            style = run.find(f"{{{_A}}}rPr")
            if (any(child.tag not in allowed_run for child in run)
                    or len(run.findall(f"{{{_A}}}t")) != 1
                    or (style is not None and any(
                        child.tag in {f"{{{_A}}}hlinkClick", f"{{{_A}}}hlinkMouseOver"}
                        for child in style
                    ))):
                raise ValueError("native notes contain unsupported run content")
            run_styles.append(style)
    if (len({_style_key(style) for style in run_styles}) > 1
            or len({_style_key(p.find(f"{{{_A}}}pPr")) for p in paragraphs}) > 1
            or len({_style_key(p.find(f"{{{_A}}}endParaRPr")) for p in paragraphs}) > 1):
        raise ValueError("native notes mixed formatting cannot be preserved")
    paragraph_style = paragraphs[0].find(f"{{{_A}}}pPr")
    end_style = paragraphs[0].find(f"{{{_A}}}endParaRPr")
    run_style = run_styles[0] if run_styles else None
    for paragraph in paragraphs:
        body.remove(paragraph)
    for text in replacement.split("\n"):
        paragraph = XML.SubElement(body, f"{{{_A}}}p")
        if paragraph_style is not None:
            paragraph.append(deepcopy(paragraph_style))
        run = XML.SubElement(paragraph, f"{{{_A}}}r")
        if run_style is not None:
            run.append(deepcopy(run_style))
        XML.SubElement(run, f"{{{_A}}}t").text = text
        if end_style is not None:
            paragraph.append(deepcopy(end_style))
    return XML.tostring(root, encoding="utf-8", xml_declaration=True)


def replace_native_speaker_notes(
    source: Path, candidate: Path, before: DeckIR, after: DeckIR, slide_ids: list[str],
) -> None:
    """Copy the exact package, replacing only existing approved note bodies.

    Missing or unsupported notes carriers fail closed rather than introducing
    a new master graph or falling back to a full presentation save.
    """
    targets = set(slide_ids)
    with ZipFile(source) as package:
        names = package.namelist()
        if len(names) != len(set(names)):
            raise ValueError("native notes package contains duplicate part names")
        parts = _notes_parts(package)
        if len(parts) != len(before.slides):
            raise ValueError("native notes slide count differs from DeckIR")
        payloads = {}
        for index, (original, changed) in enumerate(zip(before.slides, after.slides, strict=True)):
            if original.id not in targets or original.speaker_notes == changed.speaker_notes:
                continue
            part = parts[index]
            if part is None:
                raise ValueError("native notes body is absent; creating note graphs is unsupported")
            payloads[part] = _replace_body(
                package.read(part), original.speaker_notes, changed.speaker_notes,
            )
        if not payloads:
            raise ValueError("native notes transaction produced no material change")
        # Build only after validating every target, so a later invalid page
        # cannot even leave a partially written candidate.
        with ZipFile(candidate, "w") as edited:
            edited.comment = package.comment
            for entry in package.infolist():
                edited.writestr(entry, payloads.get(entry.filename, package.read(entry.filename)))
