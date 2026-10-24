# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long,too-many-return-statements
import os
import mimetypes
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import quote

from ...ooxml_content_types import find_content_type_sequence_issue
from ...runtime.tool_response import (
    AudioBlock,
    FileBlock,
    ImageBlock,
    TextBlock,
    ToolResponse,
    VideoBlock,
)
from .file_io import _resolve_file_path


def _error_response(message: str) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(type="text", text=f"Error: {message}")],
        is_error=True,
    )


def _pptx_validation_error(file_path: str) -> str | None:
    """Return an error when a .pptx is not an OOXML presentation container."""

    if Path(file_path).suffix.lower() != ".pptx":
        return None
    filename = os.path.basename(file_path)
    if not zipfile.is_zipfile(file_path):
        return f"The file {filename} is not a valid PPTX presentation."
    try:
        with zipfile.ZipFile(file_path) as archive:
            members = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return f"The file {filename} is not a valid PPTX presentation."
    required = {"[Content_Types].xml", "ppt/presentation.xml"}
    if not required.issubset(members):
        return f"The file {filename} is not a valid PPTX presentation."
    content_type_issue = find_content_type_sequence_issue(file_path)
    if content_type_issue:
        return (
            f"The file {filename} is not a valid PPTX presentation: "
            f"{content_type_issue}."
        )
    return None


def _path_to_file_url(path: str) -> str:
    """Convert a local file path to a proper file:// URL (RFC 8089).

    On Windows, converts:
      C:\\path\\file.txt      →  file:///C:/path/file.txt
      \\\\server\\share\\f.txt  →  file://server/share/f.txt

    Non-ASCII characters and ``%`` are percent-encoded so the URL is
    always valid ASCII and round-trips correctly through url2pathname.
    """
    # Normalize to absolute path
    abs_path = os.path.abspath(path)

    # Convert backslashes to forward slashes (Windows)
    if os.name == "nt":
        abs_path = abs_path.replace("\\", "/")

    # Percent-encode non-ASCII and special characters.
    # ``%`` must NOT be in *safe* — otherwise a literal ``%25`` in a
    # filename would survive un-encoded and be mis-decoded later.
    encoded_path = quote(abs_path, safe="/:@")

    # RFC 8089: file:///  (authority is empty → three slashes)
    if os.name == "nt":
        # UNC path: //server/share/… → file://server/share/…
        if encoded_path.startswith("//"):
            return f"file:{encoded_path}"
        # Local drive: C:/… → file:///C:/…
        return f"file:///{encoded_path}"
    # POSIX: abs_path already starts with "/" → file:///…
    return f"file://{encoded_path}"


def _auto_as_type(mt: str) -> str:
    if mt.startswith("image/"):
        return "image"
    if mt.startswith("audio/"):
        return "audio"
    if mt.startswith("video/"):
        return "video"
    return "file"


async def send_file_to_user(
    file_path: str,
    title: str | None = None,
) -> ToolResponse:
    """Send a file to the user.

    Args:
        file_path (`str`):
            Path to the file to send.
        title (`str | None`):
            Optional display title accepted for tool-call compatibility. The
            persisted filename remains the actual file name so reopening the
            conversation can recover the same attachment deterministically.

    Returns:
        `ToolResponse`:
            The tool response containing the file or an error message.
    """

    del title

    # Normalize the path: expand ~ and fix Unicode normalization differences
    # (e.g. macOS stores filenames as NFD but paths from the LLM arrive as NFC,
    # causing os.path.exists to return False for files that do exist).
    file_path = os.path.expanduser(unicodedata.normalize("NFC", file_path))

    # Resolve relative paths to absolute paths based on workspace directory
    try:
        file_path = _resolve_file_path(file_path)
    except ValueError as exc:
        return _error_response(str(exc))

    if not os.path.exists(file_path):
        return _error_response(f"The file {file_path} does not exist.")

    if not os.path.isfile(file_path):
        return _error_response(f"The path {file_path} is not a file.")

    validation_error = _pptx_validation_error(file_path)
    if validation_error:
        return _error_response(validation_error)

    delivery_metadata: dict[str, object] = {}
    delivery_note = "File sent successfully."
    if Path(file_path).suffix.lower() == ".xlsx":
        try:
            from ...app.office_conversion import recalculate_xlsx

            recalculated = recalculate_xlsx(file_path)
            delivery_metadata.update(
                {
                    "xlsx_formula_cache": "recalculated",
                    "xlsx_sha256": recalculated.sha256,
                    "xlsx_recalculator": recalculated.converter,
                },
            )
            delivery_note = "File sent successfully. XLSX formulas were recalculated before delivery."
        except Exception as exc:  # pylint: disable=broad-except
            delivery_metadata.update(
                {
                    "xlsx_formula_cache": "unavailable",
                    "xlsx_recalculation_error": type(exc).__name__,
                },
            )
            delivery_note = (
                "File sent successfully. Formula recalculation was unavailable; "
                "the workbook is delivered with its existing cached values."
            )

    # Detect MIME type
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        # Default to application/octet-stream for unknown types
        mime_type = "application/octet-stream"
    as_type = _auto_as_type(mime_type)

    try:
        # Use local file URL instead of base64
        file_url = _path_to_file_url(file_path)
        source = {"type": "url", "url": file_url}

        if as_type == "image":
            return ToolResponse(
                content=[
                    ImageBlock(type="image", source=source),
                    TextBlock(type="text", text=delivery_note),
                ],
                metadata=delivery_metadata,
            )
        if as_type == "audio":
            return ToolResponse(
                content=[
                    AudioBlock(type="audio", source=source),
                    TextBlock(type="text", text=delivery_note),
                ],
                metadata=delivery_metadata,
            )
        if as_type == "video":
            return ToolResponse(
                content=[
                    VideoBlock(type="video", source=source),
                    TextBlock(type="text", text=delivery_note),
                ],
                metadata=delivery_metadata,
            )

        return ToolResponse(
            content=[
                FileBlock(
                    type="file",
                    source=source,
                    filename=os.path.basename(file_path),
                ),
                TextBlock(type="text", text=delivery_note),
            ],
            metadata=delivery_metadata,
        )

    except Exception as e:
        return _error_response(f"Send file failed due to \n{e}")
