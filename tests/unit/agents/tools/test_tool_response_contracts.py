# -*- coding: utf-8 -*-
"""Behavior locks for representative runtime-owned ToolResponse shapes."""

from __future__ import annotations

from types import SimpleNamespace
import zipfile

import pytest
from pptx import Presentation

from lantai.config.context import set_current_workspace_dir
from lantai.agents.tools.get_current_time import set_user_timezone
from lantai.agents.tools.send_file import send_file_to_user
from lantai.runtime.tool_response import ToolResultBlock, ToolUseBlock


def test_tool_use_block_has_legacy_compatible_shape() -> None:
    block = ToolUseBlock(
        id="call-1",
        name="read_file",
        input={"path": "MEMORY.md"},
        raw_input='{"path":"MEMORY.md"}',
    )

    assert block == {
        "type": "tool_use",
        "id": "call-1",
        "name": "read_file",
        "input": {"path": "MEMORY.md"},
        "raw_input": '{"path":"MEMORY.md"}',
    }


def test_tool_result_block_has_legacy_compatible_shape() -> None:
    block = ToolResultBlock(
        id="call-1",
        name="read_file",
        output=[{"type": "text", "text": "ok"}],
    )

    assert block == {
        "type": "tool_result",
        "id": "call-1",
        "name": "read_file",
        "output": [{"type": "text", "text": "ok"}],
    }


@pytest.mark.asyncio
async def test_set_user_timezone_empty_value_returns_text_tool_response() -> None:
    response = await set_user_timezone("")

    assert response.content == [
        {"type": "text", "text": "Error: timezone is empty."},
    ]
    assert response.is_last is True
    assert response.stream is False


@pytest.mark.asyncio
async def test_send_file_missing_path_returns_text_tool_response(
    tmp_path,
) -> None:
    missing = tmp_path / "missing.txt"
    set_current_workspace_dir(tmp_path)
    try:
        response = await send_file_to_user(str(missing))
    finally:
        set_current_workspace_dir(None)

    assert response.content == [
        {
            "type": "text",
            "text": f"Error: The file {missing} does not exist.",
        },
    ]
    assert response.is_last is True
    assert response.stream is False


@pytest.mark.asyncio
async def test_send_file_existing_document_returns_file_and_text_blocks(
    tmp_path,
) -> None:
    document = tmp_path / "report.md"
    document.write_text("hello", encoding="utf-8")
    set_current_workspace_dir(tmp_path)
    try:
        response = await send_file_to_user(str(document))
    finally:
        set_current_workspace_dir(None)

    assert response.content[0]["type"] == "file"
    assert response.content[0]["filename"] == "report.md"
    assert response.content[0]["source"]["type"] == "url"
    assert response.content[0]["source"]["url"].startswith("file://")
    assert response.content[0]["source"]["url"].endswith("/report.md")
    assert response.content[1] == {
        "type": "text",
        "text": "File sent successfully.",
    }


@pytest.mark.asyncio
async def test_send_file_recalculates_xlsx_before_delivery(
    tmp_path, monkeypatch
) -> None:
    workbook = tmp_path / "forecast.xlsx"
    workbook.write_bytes(b"xlsx-fixture")
    calls: list[str] = []

    def fake_recalculate(path: str):
        calls.append(path)
        return SimpleNamespace(
            sha256="sha256:verified",
            converter="libreoffice",
        )

    monkeypatch.setattr(
        "lantai.app.office_conversion.recalculate_xlsx",
        fake_recalculate,
    )
    set_current_workspace_dir(tmp_path)
    try:
        response = await send_file_to_user(str(workbook))
    finally:
        set_current_workspace_dir(None)

    assert calls == [str(workbook)]
    assert response.is_error is False
    assert response.metadata == {
        "xlsx_formula_cache": "recalculated",
        "xlsx_sha256": "sha256:verified",
        "xlsx_recalculator": "libreoffice",
    }
    assert "formulas were recalculated" in response.content[1]["text"]


@pytest.mark.asyncio
async def test_send_file_marks_xlsx_cache_degraded_when_recalculation_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    workbook = tmp_path / "forecast.xlsx"
    workbook.write_bytes(b"xlsx-fixture")

    def unavailable(_path: str):
        raise RuntimeError("converter missing at /private/path")

    monkeypatch.setattr(
        "lantai.app.office_conversion.recalculate_xlsx",
        unavailable,
    )
    set_current_workspace_dir(tmp_path)
    try:
        response = await send_file_to_user(str(workbook))
    finally:
        set_current_workspace_dir(None)

    assert response.is_error is False
    assert response.metadata == {
        "xlsx_formula_cache": "unavailable",
        "xlsx_recalculation_error": "RuntimeError",
    }
    assert "/private/path" not in response.content[1]["text"]
    assert "existing cached values" in response.content[1]["text"]


@pytest.mark.asyncio
async def test_send_file_rejects_workspace_escape(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    set_current_workspace_dir(workspace)
    try:
        response = await send_file_to_user(str(outside))
    finally:
        set_current_workspace_dir(None)

    assert (
        "file path must stay inside workspace" in response.content[0]["text"]
    )


@pytest.mark.asyncio
async def test_send_file_rejects_invalid_pptx_container(tmp_path) -> None:
    invalid_pptx = tmp_path / "董事会汇报.pptx"
    invalid_pptx.write_text("this is a script, not a presentation", encoding="utf-8")
    set_current_workspace_dir(tmp_path)
    try:
        response = await send_file_to_user(str(invalid_pptx))
    finally:
        set_current_workspace_dir(None)

    assert response.is_error is True
    assert response.content == [
        {
            "type": "text",
            "text": (
                "Error: The file 董事会汇报.pptx is not a valid PPTX "
                "presentation."
            ),
        },
    ]


@pytest.mark.asyncio
async def test_send_file_accepts_valid_pptx_container(tmp_path) -> None:
    valid_pptx = tmp_path / "董事会汇报.pptx"
    Presentation().save(valid_pptx)
    set_current_workspace_dir(tmp_path)
    try:
        response = await send_file_to_user(str(valid_pptx))
    finally:
        set_current_workspace_dir(None)

    assert response.is_error is False
    assert response.content[0]["type"] == "file"
    assert response.content[0]["filename"] == "董事会汇报.pptx"


@pytest.mark.asyncio
async def test_send_file_rejects_pptx_default_after_override(tmp_path) -> None:
    malformed = tmp_path / "malformed.pptx"
    manifest = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/ppt/presentation.xml" '
        'ContentType="application/presentation+xml"/>'
        '<Default Extension="xlsx" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet"/>'
        "</Types>"
    )
    with zipfile.ZipFile(malformed, "w") as package:
        package.writestr("[Content_Types].xml", manifest)
        package.writestr("ppt/presentation.xml", "<xml />")
    set_current_workspace_dir(tmp_path)
    try:
        response = await send_file_to_user(str(malformed))
    finally:
        set_current_workspace_dir(None)

    assert response.is_error is True
    assert "Default(xlsx) appears after Override" in response.content[0]["text"]


@pytest.mark.asyncio
async def test_send_file_accepts_optional_model_authored_title(tmp_path) -> None:
    valid_pptx = tmp_path / "presentation.pptx"
    Presentation().save(valid_pptx)
    set_current_workspace_dir(tmp_path)
    try:
        response = await send_file_to_user(
            str(valid_pptx),
            title="董事会汇报",
        )
    finally:
        set_current_workspace_dir(None)

    assert response.is_error is False
    assert response.content[0]["filename"] == "presentation.pptx"
