from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from lantai.office_skill_runtime import preflight_office_artifact
from lantai.ooxml_content_types import (
    CONTENT_TYPES_NAMESPACE,
    WORKBOOK_MIME,
)


@pytest.mark.parametrize(
    ("kind", "members"),
    [
        ("pptx", ("[Content_Types].xml", "ppt/presentation.xml")),
        ("docx", ("[Content_Types].xml", "word/document.xml")),
        ("xlsx", ("[Content_Types].xml", "xl/workbook.xml")),
    ],
)
def test_preflight_accepts_minimal_ooxml_container(
    tmp_path: Path,
    kind: str,
    members: tuple[str, ...],
) -> None:
    artifact = tmp_path / f"artifact.{kind}"
    with zipfile.ZipFile(artifact, "w") as package:
        for member in members:
            package.writestr(member, "<xml />")

    result = preflight_office_artifact(artifact, kind)

    assert result["passed"] is True
    assert result["issues"] == []
    assert result["sha256"]


def test_preflight_reports_missing_required_member(tmp_path: Path) -> None:
    artifact = tmp_path / "broken.pptx"
    with zipfile.ZipFile(artifact, "w") as package:
        package.writestr("[Content_Types].xml", "<xml />")

    result = preflight_office_artifact(artifact, "pptx")

    assert result["passed"] is False
    assert result["issues"] == ["missing package member: ppt/presentation.xml"]


def test_preflight_rejects_pptx_default_after_override(tmp_path: Path) -> None:
    artifact = tmp_path / "malformed.pptx"
    manifest = (
        f'<Types xmlns="{CONTENT_TYPES_NAMESPACE}">'
        '<Override PartName="/ppt/presentation.xml" '
        'ContentType="application/presentation+xml"/>'
        f'<Default Extension="xlsx" ContentType="{WORKBOOK_MIME}"/>'
        "</Types>"
    )
    with zipfile.ZipFile(artifact, "w") as package:
        package.writestr("[Content_Types].xml", manifest)
        package.writestr("ppt/presentation.xml", "<xml />")

    result = preflight_office_artifact(artifact, "pptx")

    assert result["passed"] is False
    assert result["issues"] == [
        "OPC content-types order is invalid: Default(xlsx) appears after Override",
    ]


def test_preflight_accepts_pdf_with_header_and_trailer(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.pdf"
    artifact.write_bytes(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n")

    result = preflight_office_artifact(artifact, "pdf")

    assert result["passed"] is True


def test_preflight_rejects_wrong_extension(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"%PDF-1.7\n%%EOF")

    with pytest.raises(ValueError, match="expected a .pdf file"):
        preflight_office_artifact(artifact, "pdf")
