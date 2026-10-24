"""Validate and normalize OPC content-type manifests for OOXML packages.

The Open Packaging Conventions schema requires every ``Default`` entry in
``[Content_Types].xml`` to precede every ``Override`` entry.  Some downstream
chart-workbook materializers append a new ``Default`` after the overrides.
That package can remain readable to permissive ZIP/XML checks while being
rejected by independent Office importers.

This module is deliberately narrow: it does not rewrite slide content, chart
parts, relationships, or an existing author draft.  It prepares a separate
candidate for literal chart-workbook materialization and provides a hard
post-finalization check.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile


CONTENT_TYPES_PART = "[Content_Types].xml"
CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
WORKBOOK_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_DEFAULT = f"{{{CONTENT_TYPES_NAMESPACE}}}Default"
_OVERRIDE = f"{{{CONTENT_TYPES_NAMESPACE}}}Override"


class OpcContentTypesError(ValueError):
    """Raised when an OPC content-types manifest is missing or inconsistent."""


def _read_manifest(package_path: Path) -> tuple[bytes, ET.Element]:
    try:
        with ZipFile(package_path, "r") as package:
            payload = package.read(CONTENT_TYPES_PART)
    except (BadZipFile, KeyError) as exc:
        raise OpcContentTypesError(
            f"PPTX package has no readable {CONTENT_TYPES_PART}: {package_path}",
        ) from exc
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise OpcContentTypesError(
            f"PPTX content-types manifest is invalid XML: {package_path}",
        ) from exc
    if root.tag != f"{{{CONTENT_TYPES_NAMESPACE}}}Types":
        raise OpcContentTypesError(
            f"unexpected OPC content-types root: {root.tag}",
        )
    return payload, root


def find_content_type_sequence_issue(package_path: str | Path) -> str | None:
    """Return a precise OPC sequence defect without imposing broader policy.

    This deliberately accepts legacy/minimal fixtures that contain no
    ``Default`` or ``Override`` children.  Product delivery boundaries use it
    to reject the independently reproduced defect where a materializer puts a
    ``Default`` after an ``Override``; the strict author/finalizer path uses
    :func:`inspect_content_type_order` instead.
    """

    path = Path(package_path).resolve()
    try:
        with ZipFile(path, "r") as package:
            payload = package.read(CONTENT_TYPES_PART)
        root = ET.fromstring(payload)
    except (BadZipFile, KeyError, ET.ParseError, OSError) as exc:
        return f"unreadable OPC content-types manifest: {exc}"

    seen_override = False
    for child in root:
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name == "Override":
            seen_override = True
        elif local_name == "Default" and seen_override:
            extension = str(child.attrib.get("Extension", "unknown"))
            return (
                "OPC content-types order is invalid: "
                f"Default({extension}) appears after Override"
            )
    return None


def inspect_content_type_order(
    package_path: str | Path,
    *,
    require_workbook_default: bool = False,
) -> dict[str, object]:
    """Return manifest evidence or raise on schema-order/duplicate defects."""

    path = Path(package_path).resolve()
    _, root = _read_manifest(path)
    seen_override = False
    defaults: dict[str, str] = {}
    overrides: set[str] = set()
    first_default_after_override: str | None = None

    for child in root:
        if child.tag == _DEFAULT:
            extension = str(child.attrib.get("Extension", "")).lower()
            content_type = str(child.attrib.get("ContentType", ""))
            if not extension or not content_type:
                raise OpcContentTypesError("OPC Default entry is missing attributes")
            if extension in defaults:
                raise OpcContentTypesError(
                    f"duplicate OPC Default extension: {extension}",
                )
            defaults[extension] = content_type
            if seen_override and first_default_after_override is None:
                first_default_after_override = extension
        elif child.tag == _OVERRIDE:
            part_name = str(child.attrib.get("PartName", ""))
            content_type = str(child.attrib.get("ContentType", ""))
            if not part_name or not content_type:
                raise OpcContentTypesError("OPC Override entry is missing attributes")
            if part_name in overrides:
                raise OpcContentTypesError(
                    f"duplicate OPC Override part: {part_name}",
                )
            overrides.add(part_name)
            seen_override = True
        else:
            raise OpcContentTypesError(
                f"unsupported OPC content-types child: {child.tag}",
            )

    if first_default_after_override is not None:
        raise OpcContentTypesError(
            "OPC content-types order is invalid: "
            f"Default({first_default_after_override}) appears after Override",
        )
    workbook_type = defaults.get("xlsx")
    if workbook_type is not None and workbook_type != WORKBOOK_MIME:
        raise OpcContentTypesError(
            "xlsx Default uses an incompatible content type: " + workbook_type,
        )
    if require_workbook_default and workbook_type != WORKBOOK_MIME:
        raise OpcContentTypesError(
            "literal chart-workbook candidate has no valid xlsx Default",
        )

    return {
        "package": str(path),
        "defaultsBeforeOverrides": True,
        "defaultCount": len(defaults),
        "overrideCount": len(overrides),
        "xlsxDefault": workbook_type,
    }


def prepare_literal_chart_workbook_candidate(
    source_path: str | Path,
    output_path: str | Path,
) -> dict[str, object]:
    """Copy ``source_path`` to a new candidate with a canonical xlsx Default."""

    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if source == output:
        raise OpcContentTypesError("source and output must be different paths")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite OPC candidate: {output}")

    _, root = _read_manifest(source)
    defaults: list[ET.Element] = []
    overrides: list[ET.Element] = []
    workbook_default: ET.Element | None = None
    default_extensions: set[str] = set()
    override_parts: set[str] = set()

    for child in root:
        if child.tag == _DEFAULT:
            extension = str(child.attrib.get("Extension", "")).lower()
            content_type = str(child.attrib.get("ContentType", ""))
            if not extension or not content_type:
                raise OpcContentTypesError("OPC Default entry is missing attributes")
            if extension in default_extensions:
                raise OpcContentTypesError(
                    f"duplicate OPC Default extension: {extension}",
                )
            default_extensions.add(extension)
            if extension == "xlsx":
                if content_type != WORKBOOK_MIME:
                    raise OpcContentTypesError(
                        "xlsx Default uses an incompatible content type: "
                        + content_type,
                    )
                workbook_default = child
            defaults.append(child)
        elif child.tag == _OVERRIDE:
            part_name = str(child.attrib.get("PartName", ""))
            content_type = str(child.attrib.get("ContentType", ""))
            if not part_name or not content_type:
                raise OpcContentTypesError("OPC Override entry is missing attributes")
            if part_name in override_parts:
                raise OpcContentTypesError(
                    f"duplicate OPC Override part: {part_name}",
                )
            override_parts.add(part_name)
            overrides.append(child)
        else:
            raise OpcContentTypesError(
                f"unsupported OPC content-types child: {child.tag}",
            )

    if workbook_default is None:
        workbook_default = ET.Element(
            _DEFAULT,
            Extension="xlsx",
            ContentType=WORKBOOK_MIME,
        )
        defaults.append(workbook_default)

    canonical_root = ET.Element(root.tag, root.attrib)
    canonical_root.extend(defaults)
    canonical_root.extend(overrides)
    ET.register_namespace("", CONTENT_TYPES_NAMESPACE)
    manifest = ET.tostring(
        canonical_root,
        encoding="utf-8",
        xml_declaration=True,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}-",
        suffix=".pptx",
        dir=output.parent,
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with ZipFile(source, "r") as original, ZipFile(
            temporary,
            "w",
            compression=ZIP_DEFLATED,
        ) as prepared:
            for entry in original.infolist():
                payload = (
                    manifest
                    if entry.filename == CONTENT_TYPES_PART
                    else original.read(entry.filename)
                )
                prepared.writestr(entry, payload)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)

    report = inspect_content_type_order(
        output,
        require_workbook_default=True,
    )
    report["source"] = str(source)
    report["prepared"] = True
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or validate OPC content types for PPTX packages",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-literal-chart-candidate")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--input", type=Path, required=True)
    check.add_argument("--require-literal-chart-workbook", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "prepare-literal-chart-candidate":
        report = prepare_literal_chart_workbook_candidate(args.source, args.output)
    else:
        report = inspect_content_type_order(
            args.input,
            require_workbook_default=args.require_literal_chart_workbook,
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
