"""Graft one compiled slide into the authoritative package.

Untargeted slide XML, relationships and assets remain byte-for-byte original.
New target dependencies get isolated names; layouts continue to use the
source presentation's master context instead of rewriting other slides.
"""
from __future__ import annotations

import posixpath
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from lxml import etree as XML

from .models import TableObject
from .native_table_identity import repair_table_shape_ids
from .table_editing import replace_native_table_carrier

_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_DML = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _rels_path(part: str) -> str:
    parent, name = posixpath.split(part)
    return posixpath.join(parent, "_rels", name + ".rels")


def _resolve(part: str, target: str) -> str:
    result = posixpath.normpath(posixpath.join(posixpath.dirname(part), target)) if not target.startswith("/") else target.lstrip("/")
    if result.startswith("../"):
        raise ValueError("slide relationship escapes the package")
    return result


def _slides(package: ZipFile) -> list[str]:
    rels = ET.fromstring(package.read("ppt/_rels/presentation.xml.rels"))
    targets = {r.get("Id"): _resolve("ppt/presentation.xml", r.get("Target", "")) for r in rels}
    root = ET.fromstring(package.read("ppt/presentation.xml"))
    return [targets[s.get(f"{{{_DOC_REL}}}id")] for s in root.findall(f"{{{_PML}}}sldIdLst/{{{_PML}}}sldId")]


def _native_xml(payload: bytes):
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("unsupported native slide XML")
    return XML.fromstring(payload, parser=XML.XMLParser(
        resolve_entities=False, load_dtd=False, no_network=True,
    ))


def _native_identity(root):
    identities = list(root.iter(f"{{{_PML}}}cNvPr"))
    ids = [int(node.get("id", "0")) for node in identities]
    if any(value < 1 for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("native slide has ambiguous shape ids")
    by_name: dict[str, list] = {}
    for node in identities:
        by_name.setdefault(node.get("name", ""), []).append(node)
    return identities, by_name, set(ids)


def _native_relationship_ids(root) -> set[str]:
    return {value for node in root.iter() for key, value in node.attrib.items()
            if XML.QName(key).namespace == _DOC_REL}


def _preserve_native_objects(
    original_payload: bytes, compiled_payload: bytes,
    source_rels, compiled_rels, preserved_names: set[str], prefix: str,
    table_text_edits: dict[str, tuple[TableObject, TableObject]] | None = None,
) -> tuple[bytes, list[str]]:
    """Keep stable native IDs and transplant only unchanged object carriers.

    Preserved carriers retain their original relationship IDs and dependencies.
    Conflicting *compiled* references move instead; modifying a preserved XML
    carrier would itself violate the local-edit boundary.
    """
    original, compiled = _native_xml(original_payload), _native_xml(compiled_payload)
    table_text_edits = table_text_edits or {}
    # A historical duplicate is eligible only for an explicitly approved,
    # exact text-only table edit. Prove the source carrier before repairing
    # its identity; unchanged or unrelated duplicates remain hard errors.
    for name, edit in table_text_edits.items():
        matches = [node for node in original.iter(f"{{{_PML}}}cNvPr")
                   if node.get("name") == name]
        if len(matches) != 1:
            raise ValueError("native table identity is missing or ambiguous")
        carrier = matches[0].getparent().getparent()
        if (carrier.tag != f"{{{_PML}}}graphicFrame"
                or carrier.getparent().tag != f"{{{_PML}}}spTree"):
            raise ValueError("native table identity is missing or ambiguous")
        replace_native_table_carrier(carrier, *edit)
    identity_repairs = repair_table_shape_ids(original, set(table_text_edits))
    _, old_names, old_ids = _native_identity(original)
    compiled_ids, new_names, _ = _native_identity(compiled)
    preserved = {}
    for name in preserved_names | table_text_edits.keys():
        if len(old_names.get(name, [])) != 1 or len(new_names.get(name, [])) != 1:
            raise ValueError("unchanged native object is missing or ambiguous")
        old_node = old_names[name][0].getparent().getparent()
        new_node = new_names[name][0].getparent().getparent()
        if old_node.getparent().tag != f"{{{_PML}}}spTree" or new_node.getparent().tag != f"{{{_PML}}}spTree":
            raise ValueError("unchanged native object is not a top-level carrier")
        if name in table_text_edits:
            old_node = replace_native_table_carrier(old_node, *table_text_edits[name])
        preserved[name] = (old_node, new_node)

    next_id = max(old_ids) + 1
    id_map = {}
    for node in compiled_ids:
        old = old_names.get(node.get("name", ""), [])
        if len(old) == 1 and len(new_names[node.get("name", "")]) == 1:
            new_id = old[0].get("id")
        else:
            new_id = str(next_id)
            next_id += 1
        id_map[node.get("id")] = new_id
        node.set("id", new_id)
    for node in compiled.iter():
        if node.tag in {f"{{{_DML}}}stCxn", f"{{{_DML}}}endCxn"}:
            if node.get("id") in id_map:
                node.set("id", id_map[node.get("id")])
        if node.get("spid") in id_map:
            node.set("spid", id_map[node.get("spid")])

    referenced = set().union(*(_native_relationship_ids(old_node) for old_node, _ in preserved.values()))
    displaced = set().union(*(_native_relationship_ids(new_node) for _, new_node in preserved.values()))
    original_rels = {rel.get("Id"): rel for rel in source_rels}
    if referenced - original_rels.keys():
        raise ValueError("unchanged native object has a missing relationship")
    used_rel_ids = referenced | {rel.get("Id") for rel in compiled_rels}
    rel_map = {}
    for rel in compiled_rels:
        rel_id = rel.get("Id")
        if rel_id in referenced:
            replacement = prefix + rel_id
            while replacement in used_rel_ids:
                replacement += "-new"
            used_rel_ids.add(replacement)
            rel_map[rel_id] = replacement
            rel.set("Id", replacement)
    for node in compiled.iter():
        for key, value in list(node.attrib.items()):
            if XML.QName(key).namespace == _DOC_REL and value in rel_map:
                node.set(key, rel_map[value])
    for rel_id in sorted(referenced):
        compiled_rels.append(deepcopy(original_rels[rel_id]))
    preserved_payloads = []
    for index, (old_node, new_node) in enumerate(preserved.values()):
        # lxml reconciles namespaces when a subtree is inserted, which can
        # rewrite even a redundant native namespace declaration. Serialize
        # the original carrier in its own context and insert it only after
        # serializing the compiled page; untouched XML stays untouched.
        marker = XML.Comment(f"{prefix}preserved-{index}")
        new_node.getparent().replace(new_node, marker)
        preserved_payloads.append((
            XML.tostring(marker),
            XML.tostring(old_node, encoding="utf-8", with_tail=False),
        ))
    payload = XML.tostring(compiled, encoding="utf-8", xml_declaration=True)
    for marker, carrier in preserved_payloads:
        if payload.count(marker) != 1:
            raise ValueError("unchanged native carrier insertion is ambiguous")
        payload = payload.replace(marker, carrier, 1)
    final = _native_xml(payload)
    _, _, final_ids = _native_identity(final)
    for node in final.iter():
        if node.tag in {f"{{{_DML}}}stCxn", f"{{{_DML}}}endCxn"} and int(node.get("id", "0")) not in final_ids:
            raise ValueError("native connector references a removed object")
    # Only discard compiled references formerly owned by transplanted carriers.
    # Shared references remain live; implicit layout/notes relationships are
    # not XML attributes and must not be pruned by a generic unused-ID filter.
    unused = {rel_map.get(rel_id, rel_id) for rel_id in displaced} - _native_relationship_ids(final)
    for rel in list(compiled_rels):
        if rel.get("Id") in unused:
            compiled_rels.remove(rel)
    return payload, identity_repairs


def graft_compiled_slide(
    source: Path, candidate: Path, slide_index: int, *,
    preserved_object_ids: set[str] | None = None,
    table_text_edits: dict[str, tuple[TableObject, TableObject]] | None = None,
) -> list[str]:
    """Replace the candidate with source + its one redesigned slide graph."""
    temporary = candidate.with_name(f".{candidate.name}.{uuid4().hex}.tmp")
    try:
        with ZipFile(source) as original, ZipFile(candidate) as compiled:
            before, after = _slides(original), _slides(compiled)
            if len(before) != len(after):
                raise ValueError("compiled redesign changed the slide count")
            destination, incoming = before[slide_index], after[slide_index]
            source_rels = ET.fromstring(original.read(_rels_path(destination)))
            layout = next((_resolve(destination, r.get("Target", "")) for r in source_rels if r.get("Type", "").endswith("/slideLayout")), None)
            if layout is None:
                raise ValueError("source target slide has no native layout")
            mapping = dict(zip(after, before))
            prefix = f"edit-{uuid4().hex}-"
            payloads: dict[str, bytes] = {}
            copied: dict[str, str] = {}

            def copy_part(part: str, target: str | None = None) -> str:
                if part in copied:
                    return copied[part]
                new = target or posixpath.join(posixpath.dirname(part), prefix + posixpath.basename(part))
                copied[part] = new
                payloads[new] = compiled.read(part)
                rel_name = _rels_path(part)
                if rel_name in compiled.namelist():
                    rels = ET.fromstring(compiled.read(rel_name))
                    for rel in rels:
                        if rel.get("TargetMode") == "External":
                            continue
                        child = _resolve(part, rel.get("Target", ""))
                        if rel.get("Type", "").endswith("/slideLayout"):
                            resolved = layout
                        elif child in mapping:
                            resolved = mapping[child]
                        else:
                            resolved = copy_part(child)
                        rel.set("Target", posixpath.relpath(resolved, posixpath.dirname(new)))
                    payloads[_rels_path(new)] = ET.tostring(rels, encoding="utf-8", xml_declaration=True)
                return new

            copy_part(incoming, destination)
            rewritten_rels = ET.fromstring(payloads[_rels_path(destination)])
            payloads[destination], identity_repairs = _preserve_native_objects(
                original.read(destination), payloads[destination], source_rels,
                rewritten_rels, preserved_object_ids or set(), prefix,
                table_text_edits=table_text_edits,
            )
            payloads[_rels_path(destination)] = ET.tostring(
                rewritten_rels, encoding="utf-8", xml_declaration=True,
            )
            # Do not import the graph of a discarded compiled carrier (for
            # example its duplicate chart and embedded workbook). This filters
            # only new payloads: every original package part stays untouched,
            # including the user's already-unreferenced dependencies.
            reachable: set[str] = set()

            def retain_part(part: str) -> None:
                if part not in payloads or part in reachable:
                    return
                reachable.add(part)
                rel_name = _rels_path(part)
                if rel_name in payloads:
                    reachable.add(rel_name)
                    for rel in ET.fromstring(payloads[rel_name]):
                        if rel.get("TargetMode") != "External":
                            retain_part(_resolve(part, rel.get("Target", "")))

            retain_part(destination)
            payloads = {name: payload for name, payload in payloads.items() if name in reachable}
            types = ET.fromstring(original.read("[Content_Types].xml"))
            compiled_types = ET.fromstring(compiled.read("[Content_Types].xml"))
            overrides = {r.get("PartName"): r.get("ContentType") for r in compiled_types if r.tag == f"{{{_CT}}}Override"}
            defaults = {r.get("Extension"): r.get("ContentType") for r in compiled_types if r.tag == f"{{{_CT}}}Default"}
            existing = {r.get("PartName") for r in types if r.tag == f"{{{_CT}}}Override"}
            for old, new in copied.items():
                name = "/" + new
                if new not in payloads or name in existing:
                    continue
                content_type = overrides.get("/" + old) or defaults.get(old.rsplit(".", 1)[-1])
                if not content_type:
                    raise ValueError(f"compiled dependency has no content type: {old}")
                ET.SubElement(types, f"{{{_CT}}}Override", PartName=name, ContentType=content_type)
            payloads["[Content_Types].xml"] = ET.tostring(types, encoding="utf-8", xml_declaration=True)
            with ZipFile(temporary, "w") as result:
                for entry in original.infolist():
                    result.writestr(entry, payloads.pop(entry.filename, original.read(entry.filename)))
                for name, payload in payloads.items():
                    result.writestr(name, payload)
        temporary.replace(candidate)
    finally:
        temporary.unlink(missing_ok=True)
    return identity_repairs
