"""Repair only identifiable native table collisions at a private boundary."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from zipfile import ZipFile

from lxml import etree as XML

from .models import DeckIR, TableObject

_PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DML = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS = {"p": _PML, "a": _DML}


def repair_table_shape_ids(root, table_names: set[str]) -> list[str]:
    """Keep every other identity intact; never guess an ambiguous reference."""
    nodes = list(root.iter(f"{{{_PML}}}cNvPr"))
    by_id: dict[int, list] = {}
    by_name: dict[str, list] = {}
    for node in nodes:
        try:
            value = int(node.get("id", "0"))
        except ValueError:
            raise ValueError("native slide has ambiguous shape ids") from None
        if value < 1:
            raise ValueError("native slide has ambiguous shape ids")
        by_id.setdefault(value, []).append(node)
        by_name.setdefault(node.get("name", ""), []).append(node)
    next_id = max(by_id, default=0) + 1
    repairs = []
    for old_id, collision in by_id.items():
        if len(collision) == 1:
            continue
        selected = [node for node in collision if node.get("name") in table_names]
        if len(collision) != 2 or len(selected) != 1:
            raise ValueError("native slide has ambiguous shape ids")
        target = selected[0]
        name = target.get("name")
        carrier = target.getparent().getparent()
        other_carrier = next(node for node in collision if node is not target).getparent().getparent()
        if (len(by_name[name]) != 1
                or carrier.tag != f"{{{_PML}}}graphicFrame"
                or carrier.getparent().tag != f"{{{_PML}}}spTree"
                or len(carrier.findall(".//a:tbl", _NS)) != 1
                or other_carrier.getparent() is not carrier.getparent()):
            raise ValueError("native table identity is missing or ambiguous")
        for node in root.iter():
            references = [value for key, value in node.attrib.items()
                          if XML.QName(key).localname == "spid"]
            if node.tag in {f"{{{_DML}}}stCxn", f"{{{_DML}}}endCxn"}:
                references.append(node.get("id", ""))
            if any(value.strip().lstrip("+").isdigit() and int(value) == old_id
                   for value in references):
                raise ValueError("native duplicate table id has an ambiguous reference")
        target.set("id", str(next_id))
        repairs.append(f"{name}: native duplicate table id {old_id} -> {next_id}")
        next_id += 1
    return repairs


def restore_generated_table_ids(deck: DeckIR, output: Path) -> list[str]:
    """Bound the known PptxGenJS table-ID defect to authored native tables."""
    replacements = {}
    repairs = []
    with ZipFile(output) as package:
        for index, slide in enumerate(deck.slides, start=1):
            part = f"ppt/slides/slide{index}.xml"
            payload = package.read(part)
            if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
                raise ValueError("unsupported native slide XML")
            root = XML.fromstring(payload, parser=XML.XMLParser(
                resolve_entities=False, load_dtd=False, no_network=True,
            ))
            changes = repair_table_shape_ids(root, {
                obj.id for obj in slide.objects if isinstance(obj, TableObject)
            })
            if changes:
                replacements[part] = XML.tostring(root, encoding="utf-8", xml_declaration=True)
                repairs.extend(f"p{index}: {change}" for change in changes)
    if not replacements:
        return []
    fd, name = tempfile.mkstemp(prefix=f".{output.stem}-tables-", suffix=".pptx", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with ZipFile(output) as source, ZipFile(temporary, "w") as target:
            for entry in source.infolist():
                target.writestr(entry, replacements.get(entry.filename, source.read(entry.filename)))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return repairs
