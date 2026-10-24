from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from lxml import etree as XML
import pytest

from lantai.presentation.models import NativeGroupStyleContext
from lantai.presentation.native_group import native_context_sha256, parse_native_xml

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
THM15 = "http://schemas.microsoft.com/office/thememl/2012/main"
URI = "{05A4C25C-085E-4340-85A3-A5531E510DB2}"


def _theme():
    root = XML.Element(f"{{{A}}}theme", nsmap={"a": A, "thm15": THM15})
    extensions = XML.SubElement(root, f"{{{A}}}extLst")
    extension = XML.SubElement(extensions, f"{{{A}}}ext", {"uri": URI})
    family = XML.SubElement(extension, f"{{{THM15}}}themeFamily", {
        "name": "Synthetic Theme",
        "id": "{62F939B6-93AF-4DB8-9C6B-D6C7DFDC589F}",
        "vid": "{4A3C46E8-61CC-4603-A589-7422A47A8E4A}",
    })
    return root, extension, family


def _context(root):
    return SimpleNamespace(
        theme_xml=XML.tostring(root, encoding="unicode"),
        master_color_map_xml=f'<p:clrMap xmlns:p="{P}"/>',
        master_other_style_xml="",
        presentation_default_text_style_xml="",
    )


def test_bounded_theme_family_is_losslessly_pinned_and_not_available_to_carriers():
    root, _, family = _theme()
    context = _context(root)
    original = XML.tostring(root, method="c14n", exclusive=True)
    pinned = NativeGroupStyleContext(**vars(context), sha256=native_context_sha256(context))
    assert XML.tostring(XML.fromstring(pinned.theme_xml.encode()), method="c14n", exclusive=True) == original
    before = pinned.sha256
    family.set("name", "Another Synthetic Theme")
    assert native_context_sha256(_context(root)) != before
    with pytest.raises(ValueError, match="unsupported native group"):
        parse_native_xml(context.theme_xml)
    with pytest.raises(ValueError, match="unsupported native group"):
        NativeGroupStyleContext(**vars(context), sha256="0" * 64)


@pytest.mark.parametrize("malformation", [
    "relationship_attribute", "external_link_attribute", "unknown_attribute",
    "missing_attribute", "invalid_guid", "child_extension", "foreign_child",
    "non_whitespace_text", "wrong_extension_uri", "wrong_parent", "duplicate_family",
    "unknown_theme_element", "theme_family_in_other_style",
])
def test_theme_identity_support_keeps_unsafe_or_unbounded_contexts_rejected(malformation):
    root, extension, family = _theme()
    if malformation == "relationship_attribute":
        family.set(f"{{{R}}}id", "rId99")
    elif malformation == "external_link_attribute":
        family.set("href", "https://invalid.example/external")
    elif malformation == "unknown_attribute":
        family.set("custom", "unsupported")
    elif malformation == "missing_attribute":
        del family.attrib["vid"]
    elif malformation == "invalid_guid":
        family.set("id", "not-a-guid")
    elif malformation == "child_extension":
        XML.SubElement(family, f"{{{THM15}}}extLst")
    elif malformation == "foreign_child":
        XML.SubElement(family, "{https://invalid.example/native}payload")
    elif malformation == "non_whitespace_text":
        family.text = "not metadata"
    elif malformation == "wrong_extension_uri":
        extension.set("uri", "{00000000-0000-0000-0000-000000000000}")
    elif malformation == "wrong_parent":
        extension.remove(family)
        root.append(family)
    elif malformation == "duplicate_family":
        root.find(f"{{{A}}}extLst").append(deepcopy(extension))
    elif malformation == "unknown_theme_element":
        family.tag = f"{{{THM15}}}themeVariant"
    context = _context(root)
    if malformation == "theme_family_in_other_style":
        context.theme_xml = f'<a:theme xmlns:a="{A}"/>'
        other = XML.Element(f"{{{P}}}otherStyle", nsmap={"p": P, "a": A})
        other.append(extension)
        context.master_other_style_xml = XML.tostring(other, encoding="unicode")
    with pytest.raises(ValueError, match="unsupported native group"):
        NativeGroupStyleContext(**vars(context), sha256=native_context_sha256(context))


@pytest.mark.parametrize("malformation", ["doctype", "entity", "oversized", "too_many_nodes"])
def test_theme_context_retains_xml_resource_and_entity_guards(malformation):
    root, _, _ = _theme()
    context = _context(root)
    if malformation == "doctype":
        context.theme_xml = '<!DOCTYPE theme SYSTEM "file:///must-not-read">' + context.theme_xml
    elif malformation == "entity":
        context.theme_xml = '<!DOCTYPE theme [<!ENTITY unsafe "not expanded">]>' + context.theme_xml
    elif malformation == "oversized":
        context.theme_xml += " " * 524288
    elif malformation == "too_many_nodes":
        for _ in range(4097):
            XML.SubElement(root, f"{{{A}}}extra")
        context = _context(root)
    with pytest.raises(ValueError, match="unsupported native group"):
        native_context_sha256(context)
