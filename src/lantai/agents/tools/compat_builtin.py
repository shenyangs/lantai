# -*- coding: utf-8 -*-
"""Compatibility wrappers for older built-in tool names."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ...config.context import get_current_workspace_dir
from ...constant import WORKING_DIR
from ...runtime.tool_response import TextBlock, ToolResponse
from ...security.tool_execution_context import build_sandboxed_tool_env
from ...security.tool_policy import _detect_distribution
from .file_io import (
    _atomic_write_text,
    _read_text_file_no_follow,
    _resolve_file_path,
    read_file,
    write_file,
)
from .utils import MAX_FILE_READ_BYTES
from .shell import (
    _kill_process_tree_win32,
    _output_limit_notice,
    _read_limited_stream,
    _terminate_process_group_unix,
)

PYTHON_OUTPUT_MAX_BYTES = 256 * 1024


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def _normalise_range(ranges: list[int] | None) -> tuple[int | None, int | None]:
    if not ranges:
        return None, None
    if len(ranges) != 2:
        raise ValueError("ranges must be [start_line, end_line]")
    return int(ranges[0]), int(ranges[1])


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


def _preferred_line_ending(
    lines: list[str],
    start_index: int,
    end_index: int,
) -> str:
    for line in lines[start_index:end_index]:
        ending = _line_ending(line)
        if ending:
            return ending
    for line in lines:
        ending = _line_ending(line)
        if ending:
            return ending
    return "\n"


def _replacement_lines(
    content: str,
    newline: str,
    *,
    force_final_newline: bool,
) -> list[str]:
    if content == "":
        return []

    bodies = content.splitlines()
    replacement: list[str] = []
    for index, body in enumerate(bodies):
        is_last = index == len(bodies) - 1
        should_end = not is_last or force_final_newline
        replacement.append(body + (newline if should_end else ""))
    return replacement


def _replace_range_preserving_newlines(
    original: str,
    content: str,
    start_line: int,
    end_line: int,
) -> str:
    lines = original.splitlines(keepends=True)
    start_index = start_line - 1
    end_index = min(end_line, len(lines))
    newline = _preferred_line_ending(lines, start_index, end_index)
    suffix_exists = end_index < len(lines)
    original_has_final_newline = original.endswith(("\n", "\r"))
    force_final_newline = suffix_exists or original_has_final_newline
    replacement = _replacement_lines(
        content,
        newline,
        force_final_newline=force_final_newline,
    )
    return "".join(lines[:start_index] + replacement + lines[end_index:])


def _is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_python_code_execution_supported(
    *,
    distribution: str | None = None,
) -> bool:
    """Return whether arbitrary Python execution can be registered."""

    ambient_distribution = _detect_distribution()
    requested_distribution = (
        _detect_distribution(distribution)
        if distribution is not None
        else ambient_distribution
    )
    if "app_store" in {ambient_distribution, requested_distribution}:
        return False
    return not _is_frozen_runtime()


async def _terminate_python_process_tree(
    proc: asyncio.subprocess.Process,
) -> bool:
    if sys.platform == "win32":
        await asyncio.to_thread(_kill_process_tree_win32, proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
            return True
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                return False
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                return False
        return True

    return await _terminate_process_group_unix(proc, owned_pgid=proc.pid)


async def _collect_limited_python_output(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    max_bytes: int,
) -> tuple[int, str, str]:
    limit_event = asyncio.Event()
    stdout_task = asyncio.create_task(
        _read_limited_stream(
            proc.stdout,
            stream_name="stdout",
            max_bytes=max_bytes,
            limit_event=limit_event,
        )
    )
    stderr_task = asyncio.create_task(
        _read_limited_stream(
            proc.stderr,
            stream_name="stderr",
            max_bytes=max_bytes,
            limit_event=limit_event,
        )
    )
    wait_task = asyncio.create_task(proc.wait())
    limit_task = asyncio.create_task(limit_event.wait())
    timed_out = False
    output_limited = False
    cleanup_confirmed = True

    try:
        done, pending = await asyncio.wait(
            {wait_task, limit_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task in done:
            returncode = wait_task.result()
        elif limit_task in done and limit_event.is_set():
            output_limited = True
            returncode = -1
            cleanup_confirmed = await _terminate_python_process_tree(proc)
        else:
            timed_out = True
            returncode = -1
            cleanup_confirmed = await _terminate_python_process_tree(proc)
        for task in pending:
            task.cancel()
    except asyncio.CancelledError:
        await _terminate_python_process_tree(proc)
        raise
    finally:
        limit_task.cancel()

    try:
        stdout_data, stdout_total, stdout_truncated, _ = await asyncio.wait_for(
            stdout_task,
            timeout=1,
        )
    except asyncio.TimeoutError:
        stdout_task.cancel()
        stdout_data, stdout_total, stdout_truncated = b"", 0, False
    try:
        stderr_data, stderr_total, stderr_truncated, _ = await asyncio.wait_for(
            stderr_task,
            timeout=1,
        )
    except asyncio.TimeoutError:
        stderr_task.cancel()
        stderr_data, stderr_total, stderr_truncated = b"", 0, False

    stdout_str = stdout_data.decode("utf-8", errors="replace")
    stderr_str = stderr_data.decode("utf-8", errors="replace")
    if not cleanup_confirmed:
        stderr_str += "\nProcess group cleanup could not be confirmed."
    if stdout_truncated:
        stdout_str += _output_limit_notice("stdout", stdout_total, max_bytes)
    if stderr_truncated:
        stderr_str += _output_limit_notice("stderr", stderr_total, max_bytes)
    if timed_out:
        timeout_msg = (
            f"TimeoutError: Python code execution exceeded {timeout} seconds."
        )
        stderr_str = f"{stderr_str}\n{timeout_msg}" if stderr_str else timeout_msg
    if output_limited:
        limit_msg = (
            f"Python output exceeded the limit of {max_bytes} bytes "
            "and was terminated."
        )
        stderr_str = f"{stderr_str}\n{limit_msg}" if stderr_str else limit_msg
    return returncode, stdout_str, stderr_str


async def execute_python_code(
    code: str,
    timeout: float = 300,
    **_kwargs: Any,
) -> ToolResponse:
    """Execute Python code in a temporary file and return stdout/stderr."""

    if not is_python_code_execution_supported():
        if _detect_distribution() == "app_store":
            reason = "App Store distribution disables arbitrary Python execution."
        else:
            reason = "packaged desktop runtime has no controlled Python worker."
        return _text_response(f"Error: execute_python_code is unavailable: {reason}")

    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 300

    fd, temp_path = tempfile.mkstemp(prefix="lantai-python-", suffix=".py")
    os.close(fd)
    path = Path(temp_path)
    try:
        path.write_text(code or "", encoding="utf-8")
        cwd = get_current_workspace_dir() or WORKING_DIR
        subprocess_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            subprocess_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            subprocess_kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=build_sandboxed_tool_env(),
            **subprocess_kwargs,
        )
        returncode, stdout, stderr = await _collect_limited_python_output(
            proc,
            timeout=timeout,
            max_bytes=PYTHON_OUTPUT_MAX_BYTES,
        )

        text = (
            f"<returncode>{returncode}</returncode>"
            f"<stdout>{stdout}</stdout>"
            f"<stderr>{stderr}</stderr>"
        )
        return _text_response(text)
    except Exception as exc:  # pylint: disable=broad-except
        return _text_response(f"Error: Python code execution failed due to \n{exc}")
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


async def view_text_file(
    file_path: str,
    ranges: list[int] | None = None,
) -> ToolResponse:
    """View text file content, optionally constrained to a line range."""

    try:
        start_line, end_line = _normalise_range(ranges)
    except ValueError as exc:
        return _text_response(f"Error: {exc}")
    return await read_file(file_path, start_line=start_line, end_line=end_line)


async def write_text_file(
    file_path: str,
    content: str,
    ranges: list[int] | None = None,
) -> ToolResponse:
    """Write text to a file, optionally replacing an inclusive line range."""

    if not ranges:
        return await write_file(file_path=file_path, content=content)

    try:
        start_line, end_line = _normalise_range(ranges)
    except ValueError as exc:
        return _text_response(f"Error: {exc}")
    if start_line is None or end_line is None:
        return await write_file(file_path=file_path, content=content)
    if start_line < 1 or end_line < start_line:
        return _text_response("Error: ranges must be a valid inclusive line range.")

    try:
        path = Path(_resolve_file_path(file_path, operation="write"))
    except ValueError as exc:
        return _text_response(f"Error: {exc}")
    if not path.exists():
        return _text_response(f"Error: The file {path} does not exist.")
    if not path.is_file():
        return _text_response(f"Error: The path {path} is not a file.")

    try:
        original, _ = _read_text_file_no_follow(
            path,
            max_bytes=MAX_FILE_READ_BYTES,
            operation="write",
        )
        lines = original.splitlines(keepends=True)
        if start_line > len(lines):
            return _text_response(
                f"Error: start_line {start_line} exceeds file length ({len(lines)} lines).",
            )
        new_content = _replace_range_preserving_newlines(
            original,
            content,
            start_line,
            end_line,
        )
        _atomic_write_text(path, new_content, "utf-8")
        return _text_response(
            f"Wrote {len(new_content.encode('utf-8'))} bytes to {path}.",
        )
    except Exception as exc:  # pylint: disable=broad-except
        return _text_response(f"Error: Write file failed due to \n{exc}")
