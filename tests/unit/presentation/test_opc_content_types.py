from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from lantai.ooxml_content_types import (
    CONTENT_TYPES_NAMESPACE,
    find_content_type_sequence_issue,
    OpcContentTypesError,
    WORKBOOK_MIME,
    inspect_content_type_order,
    prepare_literal_chart_workbook_candidate,
)


def _package(path: Path, children: list[tuple[str, dict[str, str]]]) -> None:
    namespace = CONTENT_TYPES_NAMESPACE
    root = ET.Element(f"{{{namespace}}}Types")
    for tag, attributes in children:
        ET.SubElement(root, f"{{{namespace}}}{tag}", **attributes)
    ET.register_namespace("", namespace)
    with ZipFile(path, "w") as package:
        package.writestr(
            "[Content_Types].xml",
            ET.tostring(root, encoding="utf-8", xml_declaration=True),
        )
        package.writestr("ppt/presentation.xml", b"presentation-bytes")


def test_prepare_adds_workbook_default_before_overrides_without_mutating_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "author-draft.pptx"
    prepared = tmp_path / "prepared-candidate.pptx"
    _package(
        source,
        [
            (
                "Default",
                {"Extension": "xml", "ContentType": "application/xml"},
            ),
            (
                "Override",
                {
                    "PartName": "/ppt/presentation.xml",
                    "ContentType": "application/presentation+xml",
                },
            ),
        ],
    )
    original_bytes = source.read_bytes()

    report = prepare_literal_chart_workbook_candidate(source, prepared)

    assert source.read_bytes() == original_bytes
    assert report["xlsxDefault"] == WORKBOOK_MIME
    assert report["defaultsBeforeOverrides"] is True
    with ZipFile(prepared) as package:
        root = ET.fromstring(package.read("[Content_Types].xml"))
        assert [child.tag.rsplit("}", 1)[-1] for child in root] == [
            "Default",
            "Default",
            "Override",
        ]
        assert package.read("ppt/presentation.xml") == b"presentation-bytes"


def test_prepare_repairs_existing_default_after_override(tmp_path: Path) -> None:
    source = tmp_path / "malformed-final.pptx"
    prepared = tmp_path / "repaired-candidate.pptx"
    _package(
        source,
        [
            (
                "Override",
                {
                    "PartName": "/ppt/presentation.xml",
                    "ContentType": "application/presentation+xml",
                },
            ),
            (
                "Default",
                {"Extension": "xlsx", "ContentType": WORKBOOK_MIME},
            ),
        ],
    )
    with pytest.raises(OpcContentTypesError, match="appears after Override"):
        inspect_content_type_order(source, require_workbook_default=True)
    assert "Default(xlsx) appears after Override" in str(
        find_content_type_sequence_issue(source),
    )

    prepare_literal_chart_workbook_candidate(source, prepared)

    report = inspect_content_type_order(
        prepared,
        require_workbook_default=True,
    )
    assert report["defaultsBeforeOverrides"] is True
    assert find_content_type_sequence_issue(prepared) is None


def test_prepare_rejects_incompatible_xlsx_default_without_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "wrong-mime.pptx"
    prepared = tmp_path / "should-not-exist.pptx"
    _package(
        source,
        [
            (
                "Default",
                {"Extension": "xlsx", "ContentType": "application/octet-stream"},
            ),
        ],
    )

    with pytest.raises(OpcContentTypesError, match="incompatible content type"):
        prepare_literal_chart_workbook_candidate(source, prepared)

    assert not prepared.exists()


def test_prepare_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source.pptx"
    prepared = tmp_path / "existing.pptx"
    _package(source, [])
    prepared.write_bytes(b"keep-me")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        prepare_literal_chart_workbook_candidate(source, prepared)

    assert prepared.read_bytes() == b"keep-me"
