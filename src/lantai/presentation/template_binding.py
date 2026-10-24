"""Execute pinned native template context inside the sole PPTX writer.

Only selected layout/master graphs are imported. Reference learning remains a
clean-room measurement boundary: raw template XML, media and page content do
not enter its profiles. No existing or failed candidate is patched in place.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import posixpath
import tempfile
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

from lxml import etree as XML

from .models import (
    DeckIR, TemplateAnchorRequirements, TemplateMode, TemplatePackageBinding,
    TemplatePageAnchorRequirements, TemplateSlideAnchors,
)


_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS = {"p": _P, "a": _A, "r": _R}
_MAX_BYTES = 128 * 1024 * 1024
_MAX_PART_BYTES = 64 * 1024 * 1024
_XML_TYPES = {
    "slideMaster": "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml",
    "slideLayout": "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml",
    "theme": "application/vnd.openxmlformats-officedocument.theme+xml",
    "themeOverride": "application/vnd.openxmlformats-officedocument.themeOverride+xml",
}
_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/tiff", "image/bmp",
    "image/x-emf", "image/x-wmf",
})


class TemplateBindingError(ValueError):
    """An owned failure code containing no template text or private paths."""


class TemplateAnchorCoverageError(TemplateBindingError):
    """Missing approved IDs/roles, safe for existing bounded page feedback."""

    def __init__(
        self, source_slide_number: int, missing_anchors: tuple[tuple[int, str], ...],
    ) -> None:
        super().__init__("template_anchor_required_coverage_missing")
        self.source_slide_number = source_slide_number
        self.missing_anchors = missing_anchors


def _xml(payload: bytes):
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise TemplateBindingError("template_xml_entities_forbidden")
    try:
        return XML.fromstring(payload, parser=XML.XMLParser(
            resolve_entities=False, load_dtd=False, no_network=True,
        ))
    except XML.XMLSyntaxError:
        raise TemplateBindingError("template_xml_invalid") from None


def _bytes(root) -> bytes:
    # lxml retains source namespace prefixes, including mc:Ignorable values.
    return XML.tostring(root, encoding="utf-8", xml_declaration=True)


def _rels_path(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def _validate_xml_relationships(root, relationships) -> None:
    available = {relation.get("Id") for relation in relationships}
    for node in root.iter():
        for attribute, ident in node.attrib.items():
            if attribute.startswith("{" + _R + "}") and ident not in available:
                raise TemplateBindingError("template_xml_relationship_missing")


def _target(part: str, raw: str) -> str:
    if not raw or "\\" in raw or "\x00" in raw or "?" in raw or "#" in raw:
        raise TemplateBindingError("template_relationship_target_invalid")
    resolved = posixpath.normpath(
        raw.lstrip("/") if raw.startswith("/") else posixpath.join(posixpath.dirname(part), raw),
    )
    if not resolved.startswith("ppt/") or resolved.startswith("ppt/../"):
        raise TemplateBindingError("template_relationship_target_invalid")
    return resolved


class _Package:
    def __init__(self, data: bytes) -> None:
        if len(data) > _MAX_BYTES:
            raise TemplateBindingError("template_package_limit_exceeded")
        try:
            with ZipFile(BytesIO(data)) as package:
                entries = package.infolist()
                names = [entry.filename for entry in entries]
                if (
                    len(entries) > 8192 or len(set(names)) != len(names)
                    or sum(entry.file_size for entry in entries) > _MAX_BYTES
                    or any(
                        entry.file_size > _MAX_PART_BYTES or entry.flag_bits & 1
                        or entry.filename.startswith("/") or "\\" in entry.filename
                        or ".." in entry.filename.split("/")
                        for entry in entries
                    )
                ):
                    raise TemplateBindingError("template_package_limit_or_name_invalid")
                self.parts = {entry.filename: package.read(entry) for entry in entries if not entry.is_dir()}
        except (BadZipFile, OSError, RuntimeError):
            raise TemplateBindingError("template_package_invalid") from None
        self.types = _xml(self.read("[Content_Types].xml"))
        self.overrides = {node.get("PartName", "").lstrip("/"): node.get("ContentType", "") for node in self.types.findall(f"{{{_CT}}}Override")}
        self.defaults = {node.get("Extension", ""): node.get("ContentType", "") for node in self.types.findall(f"{{{_CT}}}Default")}

    def read(self, part: str) -> bytes:
        try:
            return self.parts[part]
        except KeyError:
            raise TemplateBindingError("template_package_part_missing") from None

    def relationships(self, part: str):
        payload = self.parts.get(_rels_path(part))
        root = _xml(payload) if payload is not None else XML.Element(f"{{{_PKG}}}Relationships", nsmap={None: _PKG})
        ids = [rel.get("Id") for rel in root]
        if root.tag != f"{{{_PKG}}}Relationships" or any(rel.tag != f"{{{_PKG}}}Relationship" for rel in root) or any(not ident for ident in ids) or len(ids) != len(set(ids)):
            raise TemplateBindingError("template_relationship_id_invalid")
        return root

    def link(self, part: str, kind: str) -> str:
        matches = [rel for rel in self.relationships(part) if rel.get("Type") == f"{_R}/{kind}"]
        if len(matches) != 1 or matches[0].get("TargetMode", "Internal") != "Internal":
            raise TemplateBindingError("template_native_context_missing_or_ambiguous")
        target = _target(part, matches[0].get("Target", ""))
        self.read(target)
        return target

    def slides(self) -> list[str]:
        root = _xml(self.read("ppt/presentation.xml"))
        relations = {rel.get("Id"): rel for rel in self.relationships("ppt/presentation.xml")}
        slides = []
        for node in root.findall("p:sldIdLst/p:sldId", _NS):
            relation = relations.get(node.get(f"{{{_R}}}id"))
            if relation is None or relation.get("Type") != f"{_R}/slide" or relation.get("TargetMode", "Internal") != "Internal":
                raise TemplateBindingError("template_slide_order_invalid")
            slides.append(_target("ppt/presentation.xml", relation.get("Target", "")))
        if not slides or len(slides) != len(set(slides)):
            raise TemplateBindingError("template_slide_order_invalid")
        for slide in slides:
            self.read(slide)
        return slides


def _load_source(path: str | Path, expected_sha: str) -> _Package:
    try:
        with Path(path).open("rb") as source:
            data = source.read(_MAX_BYTES + 1)
    except OSError:
        raise TemplateBindingError("template_source_unavailable") from None
    if len(data) > _MAX_BYTES:
        raise TemplateBindingError("template_package_limit_exceeded")
    if sha256(data).hexdigest() != expected_sha:
        raise TemplateBindingError("template_source_pin_mismatch")
    source = _Package(data)
    size = _xml(source.read("ppt/presentation.xml")).find("p:sldSz", _NS)
    if size is None or (size.get("cx"), size.get("cy")) != ("12192000", "6858000"):
        raise TemplateBindingError("template_canvas_not_supported")
    return source


def _context_signature(source: _Package, slide: str) -> tuple:
    root = _xml(source.read(slide))
    common = root.find("p:cSld", _NS)
    background = common.find("p:bg", _NS) if common is not None else None
    colors = root.find("p:clrMapOvr", _NS)
    # Include native background relationships instead of comparing rIds alone.
    referenced = set(background.xpath(".//@r:embed | .//@r:link", namespaces=_NS)) if background is not None else set()
    relations = source.relationships(slide)
    targets = tuple(sorted(
        (rel.get("Id"), rel.get("Type"), rel.get("TargetMode", "Internal"), _target(slide, rel.get("Target", "")))
        for rel in relations if rel.get("Id") in referenced
    ))
    return (
        source.link(slide, "slideLayout"), root.get("showMasterSp", "1"),
        _bytes(background) if background is not None else None,
        _bytes(colors) if colors is not None else None, targets,
    )


def authorized_template_anchor_catalog(
    *, template_path: str | Path, source_sha256: str, template_mode: TemplateMode,
) -> dict | None:
    """Expose pinned native shape measurements to the author, not style learning.

    Source text is untrusted reference data for semantic anchor selection. No
    original body is selected by this function, and raw XML, media and private
    paths never enter the model catalog or abstract reference profiles.
    """
    if template_mode == TemplateMode.STYLE_REFERENCE:
        return None
    if template_mode not in {TemplateMode.STRICT, TemplateMode.BRAND_PRESERVE_OPTIMIZE}:
        raise TemplateBindingError("template_policy_not_supported")
    source = _load_source(template_path, source_sha256)
    pages = []
    shape_count = text_size = 0
    for number, part in enumerate(source.slides(), 1):
        root = _xml(source.read(part))
        native_shapes = []
        for shape in root.findall("p:cSld/p:spTree/p:sp", _NS):
            identity = shape.find("p:nvSpPr/p:cNvPr", _NS)
            frame = shape.find("p:spPr/a:xfrm", _NS)
            if identity is None or frame is None or identity.get("hidden", "0") in {"1", "true"}:
                continue
            try:
                shape_id = int(identity.get("id"))
                offset, extent = frame.find("a:off", _NS), frame.find("a:ext", _NS)
                emu = tuple(int(node.get(attr)) for node, attr in (
                    (offset, "x"), (offset, "y"), (extent, "cx"), (extent, "cy"),
                ))
                if shape_id < 1 or int(frame.get("rot", "0")) or any(frame.get(key, "0") not in {"0", "false"} for key in ("flipH", "flipV")):
                    continue
            except (TypeError, ValueError, AttributeError):
                raise TemplateBindingError("template_anchor_geometry_invalid") from None
            native_text = "\n".join("".join(paragraph.itertext()) for paragraph in shape.findall("p:txBody/a:p", _NS))
            shape_count += 1
            text_size += len(native_text)
            if shape_count > 1024 or text_size > 32768 or number > 60:
                raise TemplateBindingError("template_anchor_catalog_limit_exceeded")
            has_text = shape.find("p:txBody", _NS) is not None
            no_dependencies = (
                shape.find("p:nvSpPr/p:nvPr/p:ph", _NS) is None
                and not any(attribute.startswith("{" + _R + "}")
                            for node in shape.iter() for attribute in node.attrib)
            )
            purposes = ["title_frame"] if has_text else []
            if no_dependencies:
                purposes.append("static_brand")
                if len(shape.findall("p:txBody/a:p/a:r/a:t", _NS)) == 1 and shape.find("p:txBody/a:p/a:fld", _NS) is None:
                    purposes.append("page_number")
            native_shapes.append({
                "source_shape_id": shape_id,
                "source_name": identity.get("name", ""),
                "native_text": native_text,
                "frame_inches": dict(zip(("x", "y", "w", "h"), (value / 914400 for value in emu), strict=True)),
                "font_sizes_pt": sorted({int(node.get("sz")) / 100 for node in shape.findall(".//a:rPr", _NS) if node.get("sz", "").isdigit()}),
                "font_families": sorted({node.get("typeface") for node in shape.findall(".//a:latin", _NS) if node.get("typeface")}),
                "native_shape_type": (geometry.get("prst") if (geometry := shape.find("p:spPr/a:prstGeom", _NS)) is not None else "custom"),
                "supported_purposes": purposes,
            })
        pages.append({"source_slide_number": number, "native_shapes": native_shapes})
    return {"source_sha256": source_sha256, "template_mode": template_mode.value, "pages": pages}


def validate_required_template_anchors(
    requirements: TemplateAnchorRequirements | dict | None,
    *, catalog: dict | None, target_slide_count: int,
) -> TemplateAnchorRequirements | None:
    """Validate caller-selected requirements, without selecting any uses."""
    if requirements is None:
        return None
    try:
        required = TemplateAnchorRequirements.model_validate(
            requirements.model_dump(mode="python")
            if isinstance(requirements, TemplateAnchorRequirements) else requirements,
        )
    except ValueError:
        raise TemplateBindingError("template_anchor_requirements_invalid") from None
    if catalog is None:
        raise TemplateBindingError("template_anchor_requirements_need_native_template")
    if required.source_sha256 != catalog["source_sha256"]:
        raise TemplateBindingError("template_anchor_requirements_pin_mismatch")
    if {page.target_slide_number for page in required.pages} != set(range(1, target_slide_count + 1)):
        raise TemplateBindingError("template_anchor_requirements_target_pages_mismatch")
    for page in required.pages:
        source_pages = [item for item in catalog["pages"] if item["source_slide_number"] == page.source_slide_number]
        if len(source_pages) != 1:
            raise TemplateBindingError("template_anchor_requirements_source_page_invalid")
        for anchor in page.anchors:
            matches = [item for item in source_pages[0]["native_shapes"] if item["source_shape_id"] == anchor.source_shape_id]
            if len(matches) != 1 or anchor.purpose not in matches[0]["supported_purposes"]:
                raise TemplateBindingError("template_anchor_requirements_source_shape_invalid")
    return required


def require_template_anchor_coverage(
    anchors: TemplateSlideAnchors | None, requirements: TemplatePageAnchorRequirements | None,
) -> None:
    """Fail on omission or role substitution; never patch the author's scene."""
    if requirements is None:
        return
    selected = (
        {(use.source_shape_id, use.purpose) for use in anchors.uses}
        if anchors is not None and anchors.source_slide_number == requirements.source_slide_number
        else set()
    )
    missing = tuple(
        (anchor.source_shape_id, anchor.purpose) for anchor in requirements.anchors
        if (anchor.source_shape_id, anchor.purpose) not in selected
    )
    if missing:
        raise TemplateAnchorCoverageError(requirements.source_slide_number, missing)


def bind_authorized_template(
    deck: DeckIR, *, template_path: str | Path, source_sha256: str,
    template_mode: TemplateMode, source_slide_numbers: list[int] | None = None,
    required_anchors: TemplateAnchorRequirements | dict | None = None,
) -> DeckIR:
    """Bind authorized native context without selecting style in the compiler.

    Repeating a single unambiguous context is safe. Multiple active contexts
    require an explicit author-selected page mapping, never a local heuristic.
    STYLE_REFERENCE imports no original XML or media and performs no file IO.
    """
    if template_mode == TemplateMode.STYLE_REFERENCE:
        if required_anchors is not None:
            raise TemplateBindingError("template_anchor_requirements_need_native_template")
        return deck.model_copy(update={
            "template_binding": None,
            "slides": [slide.model_copy(update={"template_anchors": None}) for slide in deck.slides],
        })
    if template_mode not in {TemplateMode.STRICT, TemplateMode.BRAND_PRESERVE_OPTIMIZE}:
        raise TemplateBindingError("template_policy_not_supported")
    source = _load_source(template_path, source_sha256)
    slides = source.slides()
    if required_anchors is not None:
        required = validate_required_template_anchors(
            required_anchors, target_slide_count=len(deck.slides),
            catalog=authorized_template_anchor_catalog(
                template_path=template_path, source_sha256=source_sha256, template_mode=template_mode,
            ),
        )
        for page in required.pages:
            require_template_anchor_coverage(deck.slides[page.target_slide_number - 1].template_anchors, page)
    if source_slide_numbers is None:
        if (
            any(slide.template_anchors is None for slide in deck.slides)
            and len({_context_signature(source, slide) for slide in slides}) != 1
        ):
            raise TemplateBindingError("template_context_mapping_required")
        source_slide_numbers = [
            slide.template_anchors.source_slide_number
            if slide.template_anchors is not None else 1
            for slide in deck.slides
        ]
    binding = TemplatePackageBinding(
        source_path=str(Path(template_path).resolve()), source_sha256=source_sha256,
        source_slide_numbers=source_slide_numbers,
    )
    bound = DeckIR.model_validate({**deck.model_dump(mode="python"), "template_binding": binding})
    validate_template_binding(bound)
    return bound


@dataclass(frozen=True)
class _BindingSnapshot:
    source: _Package
    slides: tuple[str, ...]
    layouts: tuple[str, ...]
    masters: tuple[str, ...]
    copied: dict[str, str]
    payloads: dict[str, bytes]
    types: dict[str, str]
    title_frames: tuple[tuple[tuple[str, bytes], ...], ...]
    static_shapes: tuple[tuple[tuple[str, bytes], ...], ...]


def validate_template_binding(deck: DeckIR) -> _BindingSnapshot | None:
    """Snapshot and validate pinned dependencies before starting the backend."""
    binding = deck.template_binding
    if binding is None:
        if any(slide.template_anchors is not None for slide in deck.slides):
            raise TemplateBindingError("template_anchor_requires_authorized_binding")
        return None
    if len(binding.source_slide_numbers) != len(deck.slides):
        raise TemplateBindingError("template_target_slide_count_mismatch")
    source = _load_source(binding.source_path, binding.source_sha256)
    source_slides = source.slides()
    if any(number < 1 or number > len(source_slides) for number in binding.source_slide_numbers):
        raise TemplateBindingError("template_source_slide_number_invalid")
    slides = tuple(source_slides[number - 1] for number in binding.source_slide_numbers)
    title_frames = []
    static_shapes = []
    for output_number, (page, source_part, source_number) in enumerate(zip(deck.slides, slides, binding.source_slide_numbers, strict=True), 1):
        frames = []
        static = []
        if page.template_anchors is not None:
            if page.template_anchors.source_slide_number != source_number:
                raise TemplateBindingError("template_anchor_source_mapping_mismatch")
            root = _xml(source.read(source_part))
            for use in page.template_anchors.uses:
                matches = [
                    shape for shape in root.findall("p:cSld/p:spTree/p:sp", _NS)
                    if (identity := shape.find("p:nvSpPr/p:cNvPr", _NS)) is not None
                    and identity.get("id") == str(use.source_shape_id)
                ]
                if len(matches) != 1:
                    raise TemplateBindingError("template_anchor_source_shape_missing_or_ambiguous")
                shape = matches[0]
                identity = shape.find("p:nvSpPr/p:cNvPr", _NS)
                frame = shape.find("p:spPr/a:xfrm", _NS)
                if (
                    frame is None
                    or use.purpose != "static_brand" and shape.find("p:txBody", _NS) is None
                    or identity.get("hidden", "0") in {"1", "true"}
                    or int(frame.get("rot", "0"))
                    or frame.get("flipH", "0") not in {"0", "false"}
                    or frame.get("flipV", "0") not in {"0", "false"}
                ):
                    raise TemplateBindingError("template_title_anchor_not_supported")
                offset, extent = frame.find("a:off", _NS), frame.find("a:ext", _NS)
                if offset is None or extent is None:
                    raise TemplateBindingError("template_title_anchor_geometry_missing")
                if use.purpose in {"static_brand", "page_number"}:
                    if (
                        shape.find("p:nvSpPr/p:nvPr/p:ph", _NS) is not None
                        or any(attribute.startswith("{" + _R + "}")
                               for node in shape.iter() for attribute in node.attrib)
                    ):
                        raise TemplateBindingError("template_static_anchor_dependency_not_supported")
                    if use.purpose == "page_number":
                        text_nodes = shape.findall("p:txBody/a:p/a:r/a:t", _NS)
                        if len(text_nodes) != 1 or shape.find("p:txBody/a:p/a:fld", _NS) is not None:
                            raise TemplateBindingError("template_page_number_format_not_supported")
                        text_nodes[0].text = str(output_number)
                    static.append((use.target_object_id, _bytes(shape)))
                    continue
                target = next(obj for obj in page.objects if obj.id == use.target_object_id)
                if target.frame is None:
                    raise TemplateBindingError("template_title_anchor_geometry_missing")
                expected = tuple(int(node.get(attr)) for node, attr in (
                    (offset, "x"), (offset, "y"), (extent, "cx"), (extent, "cy"),
                ))
                authored = tuple(round(value * 914400) for value in (
                    target.frame.x, target.frame.y, target.frame.w, target.frame.h,
                ))
                if authored != expected:
                    raise TemplateBindingError("template_title_anchor_authored_geometry_mismatch")
                frames.append((use.target_object_id, _bytes(frame)))
        title_frames.append(tuple(frames))
        static_shapes.append(tuple(static))
    layouts = tuple(source.link(slide, "slideLayout") for slide in slides)
    masters = tuple(dict.fromkeys(source.link(layout, "slideMaster") for layout in layouts))
    selected_layouts = set(layouts)
    copied: dict[str, str] = {}
    payloads: dict[str, bytes] = {}
    types: dict[str, str] = {}
    prefix = "template-" + binding.source_sha256[:16] + "-"

    def copy_part(part: str, kind: str) -> str:
        content_type = source.overrides.get(part) or source.defaults.get(part.rsplit(".", 1)[-1], "")
        if kind == "image":
            if content_type not in _IMAGE_TYPES:
                raise TemplateBindingError("template_image_type_not_supported")
        elif _XML_TYPES.get(kind) != content_type:
            raise TemplateBindingError("template_dependency_type_not_supported")
        if part in copied:
            return copied[part]
        name = posixpath.join(posixpath.dirname(part), prefix + posixpath.basename(part))
        copied[part] = name  # Reserve before traversing layout/master cycles.
        types[name] = content_type
        payload = source.read(part)
        relationships = source.relationships(part)
        removed_ids = set()
        for relation in list(relationships):
            rel_type = relation.get("Type", "")
            rel_kind = rel_type.removeprefix(_R + "/")
            if relation.get("TargetMode", "Internal") == "External":
                if rel_type != _R + "/hyperlink":
                    raise TemplateBindingError("template_external_dependency_forbidden")
                continue  # Never fetch external hyperlinks.
            if relation.get("TargetMode", "Internal") != "Internal":
                raise TemplateBindingError("template_relationship_mode_invalid")
            if rel_type != _R + "/" + rel_kind or rel_kind not in _XML_TYPES and rel_kind != "image":
                raise TemplateBindingError("template_dependency_not_supported")
            target = _target(part, relation.get("Target", ""))
            if kind == "slideMaster" and rel_kind == "slideLayout" and target not in selected_layouts:
                removed_ids.add(relation.get("Id"))
                relationships.remove(relation)
                continue
            new_target = copy_part(target, rel_kind)
            relation.set("Target", posixpath.relpath(new_target, posixpath.dirname(name)))
        if kind in _XML_TYPES:
            root = _xml(payload)
            if kind == "slideMaster":
                layout_list = root.find("p:sldLayoutIdLst", _NS)
                if layout_list is not None:
                    for node in list(layout_list):
                        if node.get(f"{{{_R}}}id") in removed_ids:
                            layout_list.remove(node)
                payload = _bytes(root)
            _validate_xml_relationships(root, relationships)
        payloads[name] = payload
        if len(relationships):
            payloads[_rels_path(name)] = _bytes(relationships)
        return name

    for layout in dict.fromkeys(layouts):
        copy_part(layout, "slideLayout")
    # Slide-local background assets are part of context, never page content.
    for slide in set(slides):
        root = _xml(source.read(slide))
        background = root.find("p:cSld/p:bg", _NS)
        referenced = set(background.xpath(".//@r:embed | .//@r:link", namespaces=_NS)) if background is not None else set()
        relations = {rel.get("Id"): rel for rel in source.relationships(slide)}
        for ident in referenced:
            relation = relations.get(ident)
            if relation is None or relation.get("Type") != _R + "/image" or relation.get("TargetMode", "Internal") != "Internal":
                raise TemplateBindingError("template_background_dependency_not_supported")
            copy_part(_target(slide, relation.get("Target", "")), "image")
    return _BindingSnapshot(source, slides, layouts, masters, copied, payloads, types, tuple(title_frames), tuple(static_shapes))


def apply_template_binding(snapshot: _BindingSnapshot | None, staged_output: Path) -> int:
    """Finish the compiler's private staged package before its atomic commit."""
    if snapshot is None:
        return 0
    compiled = _Package(staged_output.read_bytes())
    targets = compiled.slides()
    if len(targets) != len(snapshot.slides):
        raise TemplateBindingError("template_compiled_slide_count_mismatch")
    if set(snapshot.payloads) & set(compiled.parts):
        raise TemplateBindingError("template_dependency_name_collision")
    payloads = {**compiled.parts, **snapshot.payloads}
    for target, source_slide, source_layout, title_frames, static_shapes in zip(targets, snapshot.slides, snapshot.layouts, snapshot.title_frames, snapshot.static_shapes, strict=True):
        root = _xml(compiled.read(target))
        original = _xml(snapshot.source.read(source_slide))
        relationships = compiled.relationships(target)
        layouts = [rel for rel in relationships if rel.get("Type") == _R + "/slideLayout"]
        if len(layouts) != 1:
            raise TemplateBindingError("template_compiled_layout_missing_or_ambiguous")
        layouts[0].set("Target", posixpath.relpath(snapshot.copied[source_layout], posixpath.dirname(target)))
        common = root.find("p:cSld", _NS)
        if common is None:
            raise TemplateBindingError("template_compiled_slide_structure_invalid")
        for object_id, frame_payload in title_frames:
            matches = [
                shape for shape in common.findall("p:spTree/p:sp", _NS)
                if (identity := shape.find("p:nvSpPr/p:cNvPr", _NS)) is not None
                and identity.get("name") == object_id
            ]
            if len(matches) != 1:
                raise TemplateBindingError("template_compiled_title_missing_or_ambiguous")
            properties = matches[0].find("p:spPr", _NS)
            old_frame = properties.find("a:xfrm", _NS) if properties is not None else None
            if old_frame is None:
                raise TemplateBindingError("template_compiled_title_geometry_missing")
            properties.replace(old_frame, _xml(frame_payload))
        shape_tree = common.find("p:spTree", _NS)
        if shape_tree is None:
            raise TemplateBindingError("template_compiled_slide_structure_invalid")
        identities = shape_tree.findall(".//p:cNvPr", _NS)
        names = {identity.get("name") for identity in identities}
        next_shape_id = max((int(identity.get("id", "0")) for identity in identities), default=0) + 1
        for object_id, shape_payload in static_shapes:
            if object_id in names or object_id.startswith("meta:"):
                raise TemplateBindingError("template_static_anchor_identity_collision")
            shape = _xml(shape_payload)
            identity = shape.find("p:nvSpPr/p:cNvPr", _NS)
            identity.set("id", str(next_shape_id))
            identity.set("name", object_id)
            next_shape_id += 1
            names.add(object_id)
            shape_tree.append(shape)
        old_background = common.find("p:bg", _NS)
        if old_background is not None:
            common.remove(old_background)
        background = original.find("p:cSld/p:bg", _NS)
        if background is not None:
            background = deepcopy(background)
            source_relations = {rel.get("Id"): rel for rel in snapshot.source.relationships(source_slide)}
            used_ids = {rel.get("Id") for rel in relationships}
            for node in background.iter():
                for attr in (f"{{{_R}}}embed", f"{{{_R}}}link"):
                    ident = node.get(attr)
                    if ident is None:
                        continue
                    source_rel = source_relations[ident]
                    new_id = "rIdTemplateBackground1"
                    while new_id in used_ids:
                        new_id += "x"
                    used_ids.add(new_id)
                    copied_rel = deepcopy(source_rel)
                    copied_rel.set("Id", new_id)
                    copied_rel.set("Target", posixpath.relpath(snapshot.copied[_target(source_slide, source_rel.get("Target", ""))], posixpath.dirname(target)))
                    relationships.append(copied_rel)
                    node.set(attr, new_id)
            common.insert(0, background)
        colors = root.find("p:clrMapOvr", _NS)
        if colors is not None:
            root.remove(colors)
        colors = original.find("p:clrMapOvr", _NS)
        if colors is not None:
            root.append(deepcopy(colors))
        if original.get("showMasterSp") is None:
            root.attrib.pop("showMasterSp", None)
        else:
            root.set("showMasterSp", original.get("showMasterSp"))
        payloads[target] = _bytes(root)
        payloads[_rels_path(target)] = _bytes(relationships)
    presentation = _xml(compiled.read("ppt/presentation.xml"))
    relationships = compiled.relationships("ppt/presentation.xml")
    for relation in list(relationships):
        if relation.get("Type") == _R + "/slideMaster":
            relationships.remove(relation)
    old_masters = presentation.find("p:sldMasterIdLst", _NS)
    if old_masters is not None:
        presentation.remove(old_masters)
    master_list = XML.Element(f"{{{_P}}}sldMasterIdLst")
    presentation.insert(0, master_list)
    used_ids = {rel.get("Id") for rel in relationships}
    for index, master in enumerate(snapshot.masters, 1):
        ident = f"rIdTemplateMaster{index}"
        while ident in used_ids:
            ident += "x"
        used_ids.add(ident)
        XML.SubElement(relationships, f"{{{_PKG}}}Relationship", Id=ident, Type=_R + "/slideMaster", Target=posixpath.relpath(snapshot.copied[master], "ppt"))
        XML.SubElement(master_list, f"{{{_P}}}sldMasterId", id=str(2147483647 + index), attrib={f"{{{_R}}}id": ident})
    payloads["ppt/presentation.xml"] = _bytes(presentation)
    payloads[_rels_path("ppt/presentation.xml")] = _bytes(relationships)
    types = deepcopy(compiled.types)
    for part, content_type in snapshot.types.items():
        XML.SubElement(types, f"{{{_CT}}}Override", PartName="/" + part, ContentType=content_type)
    payloads["[Content_Types].xml"] = _bytes(types)
    if sum(len(data) for data in payloads.values()) > _MAX_BYTES:
        raise TemplateBindingError("template_bound_package_limit_exceeded")
    fd, name = tempfile.mkstemp(prefix=".template-binding-", suffix=".pptx", dir=staged_output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as package:
            for part, data in payloads.items():
                package.writestr(part, data)
        # Only a complete private package may replace the compiler's stage.
        os.replace(temporary, staged_output)
    finally:
        temporary.unlink(missing_ok=True)
    return sum(len(shapes) for shapes in snapshot.static_shapes)
