# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
import errno
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Optional

from .utils import (
    truncate_text_output,
    DEFAULT_MAX_BYTES,
    MAX_FILE_READ_BYTES,
)
from ...config.context import (
    get_current_workspace_context,
    get_current_workspace_dir,
    get_current_recent_max_bytes,
)
from ...constant import WORKING_DIR, TRUNCATION_NOTICE_MARKER
from ...security.file_access_grants import (
    ExternalFileGrantError,
    authorize_external_file_path,
)
from ...runtime.tool_response import TextBlock, ToolResponse
from ...utils.async_settlement import run_sync_settled


_AGENT_HOME_READ_ONLY_REFERENCES = frozenset({"PRODUCT_GUIDE.md"})
_OFFICE_TEXT_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx"})
_EXTRACTED_TEXT_EXTENSIONS = _OFFICE_TEXT_EXTENSIONS | frozenset({".pdf"})


def _error_response(message: str) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(type="text", text=f"Error: {message}")],
        is_error=True,
    )


def _agent_home_reference_fallback(
    path: Path,
    *,
    workspace_dir: Path,
    operation: str,
) -> Path | None:
    """Resolve an absent topic-local reference from the agent home."""
    if (
        operation != "read"
        or path.is_absolute()
        or len(path.parts) != 1
        or path.name not in _AGENT_HOME_READ_ONLY_REFERENCES
    ):
        return None
    workspace_context = get_current_workspace_context()
    if workspace_context is None:
        return None
    try:
        (workspace_dir / path.name).lstat()
    except FileNotFoundError:
        pass
    else:
        return None
    reference_path = (
        workspace_context.agent_home.expanduser().resolve() / path.name
    )
    try:
        reference_stat = reference_path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(reference_stat.st_mode):
        return None
    return reference_path


def _is_agent_home_read_only_reference(path: Path, *, operation: str) -> bool:
    if (
        operation != "read"
        or path.name not in _AGENT_HOME_READ_ONLY_REFERENCES
    ):
        return False
    workspace_context = get_current_workspace_context()
    if workspace_context is None:
        return False
    agent_home = workspace_context.agent_home.expanduser().resolve()
    return path.parent == agent_home


def _skill_read_redirect(file_path: str) -> str | None:
    """Explain the typed Skill boundary for mistaken topic-local file reads."""

    path = Path(str(file_path or ""))
    if path.is_absolute() or len(path.parts) < 3 or path.parts[0] != "skills":
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    workspace_context = get_current_workspace_context()
    if workspace_context is None:
        return None
    workspace_dir = _workspace_dir()
    project_root = workspace_context.project_root.expanduser().resolve()
    agent_home = workspace_context.agent_home.expanduser().resolve()
    if project_root != workspace_dir or project_root == agent_home:
        return None
    skill_name = path.parts[1]
    skill_file = "/".join(path.parts[2:])
    return (
        "Skill resources are not topic-workspace files. Use "
        f'read_skill(skill_name="{skill_name}", file_path="{skill_file}") instead.'
    )


def _resolve_file_path(
    file_path: str,
    *,
    operation: str = "read",
    allow_directory: bool = False,
) -> str:
    """Resolve a file path and require it to stay inside the workspace.

    Args:
        file_path: The input file path (absolute or relative).

    Returns:
        The resolved absolute file path as string.
    """
    if not file_path:
        raise ValueError("No `file_path` provided.")
    workspace_dir = (
        (get_current_workspace_dir() or WORKING_DIR).expanduser().resolve()
    )
    path = Path(file_path).expanduser()
    reference_path = _agent_home_reference_fallback(
        path,
        workspace_dir=workspace_dir,
        operation=operation,
    )
    if reference_path is not None:
        return str(reference_path)
    candidate = path if path.is_absolute() else workspace_dir / file_path
    try:
        candidate_stat = candidate.lstat()
    except (FileNotFoundError, OSError):
        candidate_stat = None
    if (
        candidate_stat is not None
        and stat.S_ISDIR(candidate_stat.st_mode)
        and not allow_directory
    ):
        raise ValueError(
            "file path is a directory, but this operation needs a specific "
            f"file: {candidate}. Choose a file inside that directory or use "
            "glob_search to locate it first.",
        )
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace_dir)
    except ValueError as exc:
        if path.is_absolute():
            try:
                return str(authorize_external_file_path(path, operation=operation))
            except ExternalFileGrantError as grant_exc:
                raise ValueError(
                    f"file path must stay inside workspace: {workspace_dir}; "
                    f"external grant rejected: {grant_exc}",
                ) from grant_exc
        raise ValueError(
            f"file path must stay inside workspace: {workspace_dir}",
        ) from exc
    return str(resolved)


def _workspace_directory_overview(file_path: str) -> ToolResponse | None:
    """Return a safe file inventory when ``read_file`` receives a directory.

    Only directories inside the active topic workspace are recoverable. Hidden
    runtime state is omitted, symlinks are not followed, and external paths
    continue through the normal authorization/error path.
    """

    if not file_path:
        return None
    workspace_dir = (
        (get_current_workspace_dir() or WORKING_DIR).expanduser().resolve()
    )
    raw_path = Path(file_path).expanduser()
    candidate = (
        raw_path if raw_path.is_absolute() else workspace_dir / raw_path
    )
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace_dir)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not resolved.is_dir():
        return None

    visible_files: list[str] = []
    for current_root, directory_names, file_names in os.walk(
        resolved,
        topdown=True,
        followlinks=False,
    ):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not name.startswith(".")
            and not (Path(current_root) / name).is_symlink()
        )
        for name in sorted(file_names):
            if name.startswith("."):
                continue
            path = Path(current_root) / name
            if path.is_symlink() or not path.is_file():
                continue
            visible_files.append(path.relative_to(resolved).as_posix())
            if len(visible_files) >= 50:
                break
        if len(visible_files) >= 50:
            break

    if not visible_files:
        text = (
            f"The path {resolved} is a directory, not a file. "
            "No user files are currently available inside it. To create a "
            "file, call `write_file` with a concrete file path such as "
            "`output.cjs`; do not call `read_file` on the directory."
        )
    else:
        inventory = "\n".join(f"- {path}" for path in visible_files)
        suffix = (
            "\n- ... (showing the first 50 files)"
            if len(visible_files) == 50
            else ""
        )
        text = (
            f"The path {resolved} is a directory, not a file. "
            "Files available inside it:\n"
            f"{inventory}{suffix}\n"
            "Call `read_file` again with one concrete file path, or use "
            "`glob_search` to narrow the list."
        )
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def _get_encoding_for_file(file_path: str) -> str:
    """Determine the appropriate encoding for a file based on its type.

    For cross-platform compatibility, especially with Windows Excel/Notepad:
    - CSV/TSV/TXT files: Use UTF-8-BOM (Windows Excel needs BOM to detect UTF-8)
    - All other files: Use UTF-8 (safer default, no BOM)

    Args:
        file_path: Path to the file

    Returns:
        Encoding string: "utf-8-sig" or "utf-8"
    """
    suffix = Path(file_path).suffix.lower()

    # Files that need BOM for Windows compatibility
    if suffix in {".csv", ".tsv", ".tab", ".txt", ".log"}:
        return "utf-8-sig"

    # Default: UTF-8 without BOM (safe for all other files)
    # This includes: .sh, .yaml, .json, .py, .js, .md, etc.
    return "utf-8"


def _workspace_dir() -> Path:
    return (get_current_workspace_dir() or WORKING_DIR).expanduser().resolve()


def _stat_parent_in_workspace(parent: Path) -> os.stat_result:
    workspace_dir = _workspace_dir()
    try:
        parent_resolved = parent.resolve(strict=True)
        parent_resolved.relative_to(workspace_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(
            f"parent directory must stay inside workspace: {workspace_dir}",
        ) from exc
    return os.stat(str(parent))


def _stat_parent_for_path(path: Path, *, operation: str) -> os.stat_result:
    try:
        return _stat_parent_in_workspace(path.parent)
    except ValueError as workspace_exc:
        if _is_agent_home_read_only_reference(path, operation=operation):
            return os.stat(str(path.parent))
        try:
            authorize_external_file_path(path, operation=operation)
        except ExternalFileGrantError as grant_exc:
            raise ValueError(
                f"parent directory must stay inside workspace or match an "
                f"external file grant: {_workspace_dir()}; external grant "
                f"rejected: {grant_exc}",
            ) from grant_exc
        return os.stat(str(path.parent))


def _ensure_workspace_parent_directories(path: Path) -> None:
    """Create missing in-workspace parents without following symlinks."""

    workspace_dir = _workspace_dir()
    resolved_path = path.resolve(strict=False)
    try:
        relative_parent = resolved_path.parent.relative_to(workspace_dir)
    except ValueError:
        # External writes are allowed only to an already granted concrete file;
        # never create directory structures beyond the topic workspace.
        return
    if not relative_parent.parts:
        return

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError(
            "safe parent directory creation is unavailable on this platform",
        )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow
    )
    current_fd = os.open(str(workspace_dir), directory_flags)
    try:
        for component in relative_parent.parts:
            try:
                os.mkdir(component, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
    finally:
        os.close(current_fd)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_regular_file_no_follow(
    path: Path,
    flags: int,
    mode: int = 0o666,
    *,
    operation: str = "read",
) -> int:
    parent_before = _stat_parent_for_path(path, operation=operation)
    open_flags = flags | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        open_flags |= nofollow

    try:
        fd = os.open(str(path), open_flags, mode)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ValueError(f"refusing to follow symlink for file path: {path}") from exc
        raise

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"file path must be a regular file: {path}")
        parent_after = os.stat(str(path.parent))
        if not _same_file_identity(parent_before, parent_after):
            raise ValueError(
                f"parent directory changed while opening file: {path.parent}",
            )
        return fd
    except Exception:
        os.close(fd)
        raise


def _decode_file_bytes(data: bytes, *, encoding: str) -> str:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        return data.decode(encoding, errors="ignore")


def _read_text_file_no_follow(
    path: Path,
    *,
    max_bytes: int,
    start_byte: int = 0,
    operation: str = "read",
) -> tuple[str, int]:
    fd = _open_regular_file_no_follow(path, os.O_RDONLY, operation=operation)
    with os.fdopen(fd, "rb") as file:
        if start_byte:
            file.seek(start_byte)
        data = file.read(max_bytes)
    encoding = "utf-8-sig" if start_byte == 0 else "utf-8"
    return _decode_file_bytes(data, encoding=encoding), len(data)


def _detected_extracted_suffix(path: Path) -> str:
    """Detect formats whose binary payload must never enter text fallback."""

    suffix = path.suffix.lower()
    if suffix in _EXTRACTED_TEXT_EXTENSIONS:
        return suffix
    fd = _open_regular_file_no_follow(path, os.O_RDONLY, operation="read")
    with os.fdopen(fd, "rb") as file:
        signature = file.read(8)
    if signature.startswith(b"%PDF-"):
        return ".pdf"
    return suffix


def _sha256_file(path: Path, *, operation: str = "read") -> str:
    digest = hashlib.sha256()
    fd = _open_regular_file_no_follow(path, os.O_RDONLY, operation=operation)
    with os.fdopen(fd, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_precondition_error(
    path: Path,
    expected_sha256: str | None,
    *,
    operation: str = "read",
) -> str | None:
    if not expected_sha256:
        return None
    if not path.exists():
        return f"file hash mismatch for {path}: file does not exist."
    current_sha256 = _sha256_file(path, operation=operation)
    if current_sha256 != expected_sha256:
        return (
            f"file hash mismatch for {path}: expected sha256 "
            f"{expected_sha256}, current sha256 {current_sha256}."
        )
    return None


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, content: str, encoding: str) -> int:
    parent = path.parent
    parent_before = _stat_parent_for_path(path, operation="write")
    try:
        existing_stat = path.lstat()
    except FileNotFoundError:
        existing_stat = None
    if existing_stat is not None:
        if stat.S_ISLNK(existing_stat.st_mode):
            raise ValueError(f"refusing to replace symlink file path: {path}")
        if not stat.S_ISREG(existing_stat.st_mode):
            raise ValueError(f"file path must be a regular file: {path}")

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(parent),
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if path.exists():
            os.chmod(temp_path, path.lstat().st_mode & 0o777)
        parent_after = os.stat(str(parent))
        if not _same_file_identity(parent_before, parent_after):
            raise ValueError(f"parent directory changed while writing file: {parent}")
        os.replace(str(temp_path), str(path))
        _fsync_directory(parent)
        return len(content.encode(encoding))
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _byte_offset_for_line(text: str, line_number: int) -> int:
    if line_number <= 1:
        return 0
    lines = text.splitlines(keepends=True)
    return sum(len(line.encode("utf-8")) for line in lines[: line_number - 1])


def _append_text_file_no_follow(path: Path, content: str, encoding: str) -> int:
    fd = _open_regular_file_no_follow(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        operation="write",
    )
    with os.fdopen(fd, "a", encoding=encoding) as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    return len(content.encode(encoding))


async def read_file(  # pylint: disable=too-many-return-statements
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    start_byte: Optional[int] = None,
) -> ToolResponse:
    """Read one concrete file. Relative paths resolve from WORKING_DIR.

    PDF, DOCX, XLSX, and PPTX files are transparently converted to readable
    text with Lantai's bundled document readers; do not install Python
    packages just to inspect these formats.

    ``file_path`` must identify a file, not the workspace root. If a directory
    is supplied accidentally, this tool returns a safe overview of user files
    inside it. Select one of those files for a later read, or call ``write_file``
    with a concrete filename when the task is to create a new file.

    Use start_line/end_line to read a specific line range (output includes
    line numbers). Omit both to read the full file.

    Args:
        file_path (`str`):
            Path to one specific file. Never use a bare workspace directory.
        start_line (`int`, optional):
            First line to read (1-based, inclusive).
        end_line (`int`, optional):
            Last line to read (1-based, inclusive).
        start_byte (`int`, optional):
            Zero-based byte offset for continuing a truncated long-line read.
    """

    # Convert start_line/end_line to int if they are strings
    if start_line is not None:
        try:
            start_line = int(start_line)
        except (ValueError, TypeError):
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: start_line must be an integer, got {start_line!r}.",
                    ),
                ],
            )

    if end_line is not None:
        try:
            end_line = int(end_line)
        except (ValueError, TypeError):
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: end_line must be an integer, got {end_line!r}.",
                    ),
                ],
            )

    if start_byte is not None:
        try:
            start_byte = int(start_byte)
        except (ValueError, TypeError):
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: start_byte must be an integer, got {start_byte!r}.",
                    ),
                ],
            )
        if start_byte < 0:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text="Error: start_byte must be greater than or equal to 0.",
                    ),
                ],
            )

    requested_file_path = file_path
    directory_overview = _workspace_directory_overview(file_path)
    if directory_overview is not None:
        return directory_overview
    try:
        file_path = _resolve_file_path(file_path, operation="read")
    except ValueError as exc:
        return _error_response(str(exc))

    if not os.path.exists(file_path):
        redirect = _skill_read_redirect(requested_file_path)
        if redirect:
            return _error_response(redirect)
        return _error_response(f"The file {file_path} does not exist.")

    if not os.path.isfile(file_path):
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: The path {file_path} is not a file.",
                ),
            ],
        )

    try:
        max_bytes = get_current_recent_max_bytes() or DEFAULT_MAX_BYTES
        office_metadata: dict[str, Any] = {}
        office_content: str | None = None
        office_suffix = _detected_extracted_suffix(Path(file_path))
        if office_suffix in _EXTRACTED_TEXT_EXTENSIONS:
            if start_byte is not None:
                return _error_response(
                    "start_byte is not supported for extracted documents; "
                    "use start_line/end_line to continue reading extracted text.",
                )
            if office_suffix == ".pdf":
                from ...app.wiki.core import extract_source_text_detail

                extracted = await run_sync_settled(
                    extract_source_text_detail, Path(file_path), detected_suffix=".pdf"
                )
                office_content = str(extracted.get("text") or "")
                office_metadata = {
                    "source_format": "pdf",
                    "extraction_method": str(
                        extracted.get("method") or "unknown"
                    ),
                    "extraction_status": str(
                        extracted.get("status") or "unknown"
                    ),
                    "page_count": int(extracted.get("page_count") or 0),
                    "scanned_document": bool(
                        extracted.get("scanned_document")
                        or extracted.get("status") == "no_text"
                    ),
                }
            else:
                from ...app.office_documents import preview_office_document

                preview = await run_sync_settled(
                    preview_office_document, file_path, mode="text"
                )
                office_content = preview.content
                office_metadata = {
                    "source_format": office_suffix.lstrip("."),
                    "extraction_method": preview.method,
                }
            if not office_content:
                status = str(office_metadata.get("extraction_status") or "no_text")
                detail = str(
                    extracted.get("message")
                    if office_suffix == ".pdf"
                    else ""
                ).strip()
                if not detail:
                    detail = f"No readable text could be extracted from {file_path}."
                return ToolResponse(
                    content=[TextBlock(type="text", text=detail)],
                    metadata=office_metadata,
                    is_error=status in {"encrypted", "failed", "corrupt"},
                )
        if start_byte is not None:
            file_size = os.path.getsize(file_path)
            if start_byte > file_size:
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=f"Error: start_byte {start_byte} exceeds file size ({file_size} bytes).",
                        ),
                    ],
                )
            text, bytes_read = _read_text_file_no_follow(
                Path(file_path),
                start_byte=start_byte,
                max_bytes=max_bytes,
                operation="read",
            )
            next_byte = start_byte + bytes_read
            if next_byte < file_size:
                notice = (
                    TRUNCATION_NOTICE_MARKER + f"\nThe output above was truncated."
                    f"\nThe full content is saved to the file and contains {file_size} bytes in total."
                    f"\nThis excerpt starts at byte offset {start_byte} and covers the next {bytes_read} bytes."
                    "\nIf the current content is not enough, "
                    f"call `read_file` with file_path={file_path} "
                    f"start_byte={next_byte} to read more."
                )
                text = text + notice
            return ToolResponse(
                content=[TextBlock(type="text", text=text)],
            )

        if office_content is not None:
            content = office_content
        else:
            content, _ = _read_text_file_no_follow(
                Path(file_path),
                max_bytes=MAX_FILE_READ_BYTES,
                operation="read",
            )
        all_lines = content.split("\n")
        total = len(all_lines)

        # Determine read range
        s = max(1, start_line if start_line is not None else 1)
        e = min(total, end_line if end_line is not None else total)

        if s > total:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: start_line {s} exceeds file length ({total} lines).",
                    ),
                ],
            )

        if s > e:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: start_line ({s}) > end_line ({e}).",
                    ),
                ],
            )

        # Extract selected lines
        selected_content = "\n".join(all_lines[s - 1 : e])

        # Apply smart truncation (consistent with shell output format)
        selected_start_byte = _byte_offset_for_line(content, s)
        text = truncate_text_output(
            selected_content,
            start_line=s,
            total_lines=total,
            file_path=file_path,
            max_bytes=max_bytes,
            start_byte=selected_start_byte,
        )

        # Add continuation hint if partial read without truncation.
        # Use TRUNCATION_NOTICE_MARKER format so ToolResultCompactor can
        # re-truncate with the correct start_line when compacting old messages.
        if text == selected_content and e < total:
            content_bytes = len(text.encode("utf-8"))
            notice = (
                TRUNCATION_NOTICE_MARKER + f"\nThe output above was truncated."
                f"\nThe full content is saved to the file "
                f"and contains {total} lines in total."
                f"\nThis excerpt starts at line {s} and "
                f"covers the next {content_bytes} bytes."
                "\nIf the current content is not enough, "
                f"call `read_file` with file_path={file_path} "
                f"start_line={e + 1} to read more."
            )
            text = text + notice

        return ToolResponse(
            content=[TextBlock(type="text", text=text)],
            metadata=office_metadata or None,
        )

    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Read file failed due to \n{e}",
                ),
            ],
        )


async def write_file(
    file_path: str,
    content: str,
    expected_sha256: Optional[str] = None,
) -> ToolResponse:
    """Create or overwrite a file. Relative paths resolve from WORKING_DIR.

    Missing parent directories inside the current topic workspace are created
    automatically. Always provide a concrete filename, not a directory path.

    Args:
        file_path (`str`):
            Path to the file.
        content (`str`):
            Content to write.
        expected_sha256 (`str`, optional):
            Current file SHA-256 required before replacing an existing file.
    """

    if not file_path:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text="Error: No `file_path` provided.",
                ),
            ],
        )

    try:
        file_path = _resolve_file_path(file_path, operation="write")
    except ValueError as exc:
        return _error_response(str(exc))
    path = Path(file_path)
    precondition_error = _hash_precondition_error(
        path,
        expected_sha256,
        operation="write",
    )
    if precondition_error:
        return _error_response(precondition_error)
    encoding = _get_encoding_for_file(file_path)

    try:
        _ensure_workspace_parent_directories(path)
        bytes_written = _atomic_write_text(path, content, encoding)
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Wrote {bytes_written} bytes to {file_path}.",
                ),
            ],
        )
    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Write file failed due to \n{e}",
                ),
            ],
        )


# pylint: disable=too-many-return-statements
async def edit_file(
    file_path: str,
    old_text: str,
    new_text: str,
    expected_sha256: Optional[str] = None,
) -> ToolResponse:
    """Find-and-replace text in a file. All occurrences of old_text are
    replaced with new_text. Relative paths resolve from WORKING_DIR.

    Args:
        file_path (`str`):
            Path to the file.
        old_text (`str`):
            Exact text to find.
        new_text (`str`):
            Replacement text.
        expected_sha256 (`str`, optional):
            Current file SHA-256 required before applying the edit.
    """

    if not file_path:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text="Error: No `file_path` provided.",
                ),
            ],
        )

    try:
        resolved_path = _resolve_file_path(file_path, operation="write")
    except ValueError as exc:
        return _error_response(str(exc))

    if not os.path.exists(resolved_path):
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: The file {resolved_path} does not exist.",
                ),
            ],
        )

    if not os.path.isfile(resolved_path):
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: The path {resolved_path} is not a file.",
                ),
            ],
        )

    path = Path(resolved_path)
    precondition_error = _hash_precondition_error(
        path,
        expected_sha256,
        operation="write",
    )
    if precondition_error:
        return _error_response(precondition_error)

    try:
        content, _ = _read_text_file_no_follow(
            Path(resolved_path),
            max_bytes=MAX_FILE_READ_BYTES,
            operation="write",
        )
    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Read file failed due to \n{e}",
                ),
            ],
        )

    if old_text not in content:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: The text to replace was not found in {file_path}.",
                ),
            ],
        )

    new_content = content.replace(old_text, new_text)
    write_response = await write_file(
        file_path=resolved_path,
        content=new_content,
        expected_sha256=expected_sha256,
    )

    if write_response.content and len(write_response.content) > 0:
        write_text = write_response.content[0].get("text", "")
        if write_text.startswith("Error:"):
            return write_response

    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=f"Successfully replaced text in {file_path}.",
            ),
        ],
    )


async def append_file(
    file_path: str,
    content: str,
) -> ToolResponse:
    """Append content to the end of a file. Relative paths resolve from
    WORKING_DIR.

    Args:
        file_path (`str`):
            Path to the file.
        content (`str`):
            Content to append.
    """

    if not file_path:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text="Error: No `file_path` provided.",
                ),
            ],
        )

    try:
        file_path = _resolve_file_path(file_path, operation="write")
    except ValueError as exc:
        return _error_response(str(exc))
    encoding = _get_encoding_for_file(file_path)

    try:
        bytes_written = _append_text_file_no_follow(
            Path(file_path),
            content,
            encoding,
        )
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Appended {bytes_written} bytes to {file_path}.",
                ),
            ],
        )
    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Append file failed due to \n{e}",
                ),
            ],
        )
