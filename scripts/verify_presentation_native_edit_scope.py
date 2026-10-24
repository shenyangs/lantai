#!/usr/bin/env python3
"""Read-only, hash-bound native edit scope proof; not full-case acceptance."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import posixpath
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops

from lantai.presentation.design_system import prepare_deck_for_composition
from lantai.presentation.models import DeckIR
from lantai.presentation.reconcile import reconcile_pptx


REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _xml(payload: bytes):
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("package XML declarations are not supported")
    return ET.fromstring(payload)


def _rels_name(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def _target(part: str, target: str) -> str:
    name = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                              else posixpath.join(posixpath.dirname(part), target))
    if not target or "\\" in target or name in {".", ".."} or name.startswith("../"):
        raise ValueError("invalid package relationship target")
    return name


def _names(package: zipfile.ZipFile) -> set[str]:
    entries = package.infolist()
    names = [item.filename for item in entries]
    if len(names) != len(set(names)):
        raise ValueError("duplicate package members")
    if any(n.startswith("/") or "\\" in n or posixpath.normpath(n) != n
           or n.startswith("../")
           for n in (item.filename.rstrip("/") if item.is_dir() else item.filename for item in entries)):
        raise ValueError("invalid package member name")
    return {item.filename for item in entries if not item.is_dir()}


def _slide_parts(package: zipfile.ZipFile) -> list[str]:
    presentation = "ppt/presentation.xml"
    relationships = {}
    for item in _xml(package.read(_rels_name(presentation))):
        key = item.get("Id")
        if not key or key in relationships:
            raise ValueError("invalid presentation relationship identity")
        relationships[key] = item
    slides = []
    for item in _xml(package.read(presentation)).findall(f"{{{P_NS}}}sldIdLst/{{{P_NS}}}sldId"):
        relation = relationships.get(item.get(f"{{{R_NS}}}id"))
        if (relation is None or relation.get("TargetMode") == "External"
                or relation.get("Type") != f"{R_NS}/slide"):
            raise ValueError("invalid native slide relationship")
        slides.append(_target(presentation, relation.get("Target", "")))
    if not slides or len(slides) != len(set(slides)):
        raise ValueError("missing or duplicate native slides")
    return slides


def _closure(package: zipfile.ZipFile, names: set[str], root: str) -> set[str]:
    seen, pending = set(), [root]
    while pending:
        part = pending.pop()
        if part in seen:
            continue
        if part not in names:
            raise ValueError("missing package dependency")
        seen.add(part)
        relations = _rels_name(part)
        if relations not in names:
            continue
        seen.add(relations)
        identities = set()
        for item in _xml(package.read(relations)):
            key = item.get("Id")
            if not key or key in identities:
                raise ValueError("invalid dependency relationship identity")
            identities.add(key)
            if item.get("TargetMode") != "External":
                pending.append(_target(part, item.get("Target", "")))
    return seen


def _content_types(package: zipfile.ZipFile, names: set[str]) -> tuple[dict, dict]:
    root = _xml(package.read("[Content_Types].xml"))
    if root.tag != f"{{{CT_NS}}}Types":
        raise ValueError("invalid content type document")
    defaults, overrides = {}, {}
    for item in root:
        content_type = item.get("ContentType", "")
        if not content_type or "/" not in content_type:
            raise ValueError("invalid content type declaration")
        if item.tag == f"{{{CT_NS}}}Default":
            extension = item.get("Extension", "").lower()
            if not extension or "/" in extension or "." in extension or extension in defaults:
                raise ValueError("invalid default content type identity")
            defaults[extension] = content_type
        elif item.tag == f"{{{CT_NS}}}Override":
            declared = item.get("PartName", "")
            part = _target("", declared)
            if not declared.startswith("/") or declared != "/" + part or part in overrides:
                raise ValueError("invalid override content type identity")
            if part not in names:
                raise ValueError("content type refers to a missing package member")
            overrides[part] = content_type
        else:
            raise ValueError("unsupported content type declaration")
    if any(part not in overrides and _extension(part) not in defaults
           for part in names - {"[Content_Types].xml"}):
        raise ValueError("package member has no content type")
    return defaults, overrides


def _extension(part: str) -> str:
    name = posixpath.basename(part)
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def _content_types_scoped(old: zipfile.ZipFile, new: zipfile.ZipFile,
                          old_names: set[str], new_names: set[str]) -> bool:
    old_defaults, old_overrides = _content_types(old, old_names)
    new_defaults, new_overrides = _content_types(new, new_names)
    if any(new_defaults.get(ext) != value for ext, value in old_defaults.items()):
        return False
    if any(new_overrides.get(part) != value for part, value in old_overrides.items()
           if part in new_names):
        return False
    added = new_names - old_names
    if not (new_overrides.keys() - old_overrides.keys()).issubset(added):
        return False
    for extension in new_defaults.keys() - old_defaults.keys():
        uncovered = {part for part in new_names if part not in new_overrides
                     and _extension(part) == extension}
        if not uncovered or not uncovered.issubset(added):
            return False
    return True


def _slide_payload(deck: DeckIR, index: int) -> dict:
    """Only verified asset locations are normalized; semantic fields remain."""
    payload = deck.slides[index].model_dump(mode="json")
    payload["scene_hash"] = None
    catalog = {asset.asset_id: asset for asset in deck.assets}
    used = []
    for obj in payload["objects"]:
        if obj["kind"] != "image":
            continue
        asset = catalog.get(obj["asset_id"])
        if asset is None or not obj.get("path"):
            raise ValueError("image asset identity is missing")
        if (_sha256(Path(asset.local_path)) != asset.sha256
                or _sha256(Path(obj["path"])) != asset.sha256):
            raise ValueError("image asset bytes do not match identity")
        obj["path"] = f"asset-sha256:{asset.sha256}"
        entry = asset.model_dump(mode="json")
        entry["local_path"] = obj["path"]
        used.append(entry)
    payload["verified_image_assets"] = sorted(used, key=lambda a: a["asset_id"])
    return payload


def _pixels(before: Path, after: Path) -> tuple[bool, bool, str, str]:
    old_bytes, new_bytes = before.read_bytes(), after.read_bytes()
    with Image.open(io.BytesIO(old_bytes)) as a, Image.open(io.BytesIO(new_bytes)) as b:
        x, y = a.convert("RGBA"), b.convert("RGBA")
        dimensions_equal = x.size == y.size
        pixels_equal = dimensions_equal and not any(
            channel.getbbox() for channel in ImageChops.difference(x, y).split()
        )
    return (dimensions_equal, pixels_equal, hashlib.sha256(old_bytes).hexdigest(),
            hashlib.sha256(new_bytes).hexdigest())


def verify_native_edit_scope(*, before_pptx: Path, after_pptx: Path,
                             before_deck_ir: Path, after_deck_ir: Path,
                             before_render_dir: Path, after_render_dir: Path,
                             allowed_slide_ids: list[str],
                             required_slide_ids: list[str] | None = None) -> dict:
    before_pptx, after_pptx = Path(before_pptx), Path(after_pptx)
    before = DeckIR.model_validate_json(Path(before_deck_ir).read_text(encoding="utf-8"))
    after = DeckIR.model_validate_json(Path(after_deck_ir).read_text(encoding="utf-8"))
    slide_ids = [s.id for s in before.slides]
    allowed, required = set(allowed_slide_ids), set(required_slide_ids or [])
    if (before.id != after.id or slide_ids != [s.id for s in after.slides]
            or len(slide_ids) != len(set(slide_ids))):
        raise ValueError("deck identity or slide order changed")
    if (not allowed or len(allowed) != len(allowed_slide_ids)
            or len(required) != len(required_slide_ids or [])
            or not allowed.issubset(slide_ids) or not required.issubset(allowed)):
        raise ValueError("invalid allowed or required slide scope")
    rows, target_before, target_after = [], set(), set()
    with tempfile.TemporaryDirectory(prefix="lantai-scope-import-") as temp:
        canonical = prepare_deck_for_composition(reconcile_pptx(
            before, before_pptx, assets_dir=Path(temp) / "assets",
        ))
        if [s.id for s in canonical.slides] != slide_ids:
            raise ValueError("authoritative import changed slide identity")
        with zipfile.ZipFile(before_pptx) as old, zipfile.ZipFile(after_pptx) as new:
            old_names, new_names = _names(old), _names(new)
            content_types_scoped = _content_types_scoped(old, new, old_names, new_names)
            old_parts, new_parts = _slide_parts(old), _slide_parts(new)
            if old_parts != new_parts or len(old_parts) != len(slide_ids):
                raise ValueError("native slide order or count changed")
            for index, slide_id in enumerate(slide_ids):
                a, b = _closure(old, old_names, old_parts[index]), _closure(new, new_names, new_parts[index])
                dependencies_equal = a == b and all(old.read(p) == new.read(p) for p in a & b)
                dimensions_equal, pixels_equal, old_render_sha, new_render_sha = _pixels(
                    Path(before_render_dir) / f"slide-{index + 1}.png",
                    Path(after_render_dir) / f"slide-{index + 1}.png",
                )
                expected, actual = _slide_payload(canonical, index), _slide_payload(after, index)
                ir_equal = expected == actual
                semantic_expected, semantic_actual = dict(expected), dict(actual)
                semantic_expected.pop("revision")
                semantic_actual.pop("revision")
                semantic_changed = semantic_expected != semantic_actual
                target = slide_id in allowed
                if target:
                    target_before.update(a)
                    target_after.update(b)
                rows.append({"slide_id": slide_id, "allowed": target,
                             "required": slide_id in required,
                             "package_dependencies_equal": dependencies_equal,
                             "dimensions_equal": dimensions_equal,
                             "render_pixels_equal": pixels_equal,
                             "before_render_sha256": old_render_sha,
                             "after_render_sha256": new_render_sha,
                             "canonical_ir_equal": ir_equal,
                             "material_ir_changed": semantic_changed})
            changed = {p for p in old_names & new_names if old.read(p) != new.read(p)}
            added, removed = new_names - old_names, old_names - new_names
            changes_scoped = (changed.issubset(target_before | target_after | {"[Content_Types].xml"})
                              and added.issubset(target_after) and removed.issubset(target_before))
    non_target_ok = all(r["package_dependencies_equal"] and r["render_pixels_equal"]
                        and r["canonical_ir_equal"] for r in rows if not r["allowed"])
    required_changed = all(r["material_ir_changed"] for r in rows if r["required"])
    result = {"schema": "com.lantai.presentation-native-edit-scope.v1",
              "before_pptx_sha256": _sha256(before_pptx), "after_pptx_sha256": _sha256(after_pptx),
              "before_ir_sha256": _sha256(Path(before_deck_ir)), "after_ir_sha256": _sha256(Path(after_deck_ir)),
              "slide_count": len(rows), "non_target_count": len(slide_ids) - len(allowed),
              "allowed_slide_ids": sorted(allowed), "required_slide_ids": sorted(required),
              "non_target_scope_passed": non_target_ok, "package_changes_scoped": changes_scoped,
              "content_types_scoped": content_types_scoped,
              "required_slides_materially_changed": required_changed,
              "changed_existing_parts": sorted(changed), "added_parts": sorted(added),
              "removed_parts": sorted(removed), "slides": rows, "provider_calls": 0,
              "full_case_or_office_app_human_acceptance_claimed": False}
    result["ok"] = (non_target_ok and changes_scoped and content_types_scoped and required_changed
                    and all(r["dimensions_equal"] for r in rows))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("before-pptx", "after-pptx", "before-deck-ir", "after-deck-ir",
                 "before-render-dir", "after-render-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--allowed-slide-id", action="append", required=True)
    parser.add_argument("--required-slide-id", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.output:
            evidence = args.output.resolve()
            input_files = (args.before_pptx, args.after_pptx, args.before_deck_ir, args.after_deck_ir)
            if (any(evidence == path.resolve() for path in input_files)
                    or any(evidence.is_relative_to(path.resolve()) for path in (
                        args.before_render_dir, args.after_render_dir))):
                raise ValueError("scope evidence must stay outside its inputs")
        result = verify_native_edit_scope(
            before_pptx=args.before_pptx, after_pptx=args.after_pptx,
            before_deck_ir=args.before_deck_ir, after_deck_ir=args.after_deck_ir,
            before_render_dir=args.before_render_dir, after_render_dir=args.after_render_dir,
            allowed_slide_ids=args.allowed_slide_id, required_slide_ids=args.required_slide_id,
        )
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    except Exception as error:
        print(json.dumps({"ok": False, "state": "invalid_scope_evidence",
                          "error_type": type(error).__name__,
                          "error_sha256": hashlib.sha256(str(error).encode()).hexdigest()}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
