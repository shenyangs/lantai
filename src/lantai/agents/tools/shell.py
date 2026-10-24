# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
"""The shell command tool."""

import asyncio
import logging
import locale
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from ...constant import WORKING_DIR
from ...config.context import (
    get_current_shell_command_timeout,
    get_current_workspace_dir,
)
from ...runtime.tool_response import TextBlock, ToolResponse
from ...utils.async_settlement import await_settled
from ...security.tool_execution_context import build_sandboxed_tool_env
from ...security.tool_policy import evaluate_shell_command

SHELL_OUTPUT_MAX_BYTES = 256 * 1024
_SHELL_READ_CHUNK_BYTES = 8192
_PROCESS_GROUP_GRACE_SECONDS = 2.0
_PROCESS_GROUP_POLL_SECONDS = 0.05
_logger = logging.getLogger(__name__)


def _python_environment_recovery(
    stdout: str,
    stderr: str,
) -> tuple[dict[str, object], str] | None:
    """Return a safe, agent-actionable recovery plan for Python env failures."""

    combined = f"{stdout}\n{stderr}".lower()
    if "externally-managed-environment" in combined:
        reason = "pep_668"
    elif "pip: command not found" in combined or "pip: not found" in combined:
        reason = "pip_command_missing"
    elif "no module named" in combined or "modulenotfounderror" in combined:
        reason = "python_module_missing"
    else:
        return None

    venv_path = ".lantai/venvs/python"
    if sys.platform == "win32":
        create_command = f"py -3 -m venv {venv_path}"
        python_command = f"{venv_path}/Scripts/python.exe"
    else:
        create_command = f"python3 -m venv {venv_path}"
        python_command = f"{venv_path}/bin/python"

    recovery = {
        "kind": "python_workspace_venv",
        "reason": reason,
        "retryable": True,
        "venv_path": venv_path,
        "create_command": create_command,
        "python_command": python_command,
        "forbidden_workarounds": [
            "--break-system-packages",
            "sudo pip install",
            "global pip configuration changes",
        ],
    }
    guidance = (
        "\n[lantai:recovery]\n"
        "A safe workspace-local Python recovery is available. "
        "Do not use --break-system-packages, sudo pip, or change global pip "
        "configuration.\n"
        "Agent next action:\n"
        "1. If the task is only reading an Office file, retry with read_file "
        "or bundled OfficeCLI; no package installation is needed.\n"
        f"2. Otherwise run `{create_command}`.\n"
        f"3. Install only the required libraries with "
        f"`{python_command} -m pip install <packages>`, then rerun the original "
        f"work with `{python_command}`.\n"
        "4. Use that explicit interpreter in later tool calls; shell activation "
        "with `source` is not persistent.\n"
        "Carry out this recovery and continue the original task when policy "
        "allows. Do not ask a non-technical user to paste these commands."
    )
    return recovery, guidance


def _kill_process_tree_win32(pid: int) -> None:
    """Kill a process and all its descendants on Windows via taskkill.

    Uses ``taskkill /F /T`` which forcefully terminates the entire process
    tree, including grandchild processes that ``Popen.kill()`` would miss.
    """
    try:
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass


def _collapse_newlines_outside_quotes(cmd: str) -> str:
    r"""Collapse newlines outside quoted strings; preserve those inside.

    Used only on Unix where sh/bash correctly handles newlines in quotes.
    Handles backslash-newline (line continuation) by removing both chars,
    and treats single-quoted content as fully literal per POSIX.
    """
    result: list[str] = []
    in_single_quote = False
    in_double_quote = False
    i = 0
    length = len(cmd)

    while i < length:
        char = cmd[i]

        # Toggle quote state
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            result.append(char)
            i += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            result.append(char)
            i += 1
            continue

        # Inside single quotes: everything is literal (POSIX)
        if in_single_quote:
            result.append(char)
            i += 1
            continue

        # Backslash-newline (line continuation): remove both chars
        if char == "\\" and i + 1 < length and cmd[i + 1] in ("\r", "\n"):
            i += 2
            # \r\n sequence: skip the \n as well
            if i < length and cmd[i - 1] == "\r" and cmd[i] == "\n":
                i += 1
            continue

        # Backslash escape (non-newline): keep both chars
        if char == "\\" and i + 1 < length:
            result.append(char)
            result.append(cmd[i + 1])
            i += 2
            continue

        # Newlines
        if char in ("\r", "\n"):
            if in_double_quote:
                # Preserve newlines inside double quotes
                result.append(char)
            else:
                # Collapse \r\n as a single space
                if char == "\r" and i + 1 < length and cmd[i + 1] == "\n":
                    i += 1
                result.append(" ")
            i += 1
            continue

        result.append(char)
        i += 1

    return "".join(result)


def _collapse_embedded_newlines(cmd: str) -> str:
    r"""Keep valid Unix shell scripts intact and normalize Windows input.

    A JSON ``\n`` is an intentional newline after decoding. On Unix it may
    separate commands or appear inside a quoted multiline argument, and the
    shell already handles both cases. Replacing it with a space silently
    merges commands (``run\nverify`` becomes ``run verify``).

    Windows still collapses newlines because ``cmd.exe`` truncates at a raw
    line break.
    """
    if "\n" not in cmd:
        return cmd
    if sys.platform == "win32":
        # cmd.exe truncates at newlines regardless of quoting — must
        # collapse all to ensure the command executes at all.
        return cmd.replace("\r\n", " ").replace("\n", " ")
    return cmd.replace("\r\n", "\n").replace("\r", "\n")


def _sanitize_win_cmd(cmd: str) -> str:
    """Fix common LLM escaping artefacts for Windows ``cmd.exe``.

    LLMs sometimes produce commands with backslash-escaped double quotes
    (``\\"``) — valid in bash/JSON but meaningless to ``cmd.exe``.  When
    *every* double-quote in the command is preceded by a backslash, it is
    almost certainly a double-escape artefact, so we strip them.
    """
    if '\\"' in cmd and '"' not in cmd.replace('\\"', ""):
        return cmd.replace('\\"', '"')
    return cmd


def _output_limit_notice(stream_name: str, total_bytes: int, max_bytes: int) -> str:
    return (
        f"\n[lantai:{stream_name}_truncated]\n"
        f"Output exceeded {max_bytes} bytes; retained the first {max_bytes} "
        f"bytes out of at least {total_bytes} bytes."
    )


def _read_temp_file(path: str, max_bytes: int = SHELL_OUTPUT_MAX_BYTES) -> str:
    """Read a temporary output file and return its decoded content."""
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]
        text = smart_decode(data)
        if truncated:
            text += _output_limit_notice("output", max_bytes + 1, max_bytes)
        return text
    except OSError:
        return ""


def _temp_file_exceeds(path: str | None, max_bytes: int) -> bool:
    if path is None:
        return False
    try:
        return os.path.getsize(path) > max_bytes
    except OSError:
        return False


def _owned_process_group_state_unix(pgid: int) -> bool | None:
    """Return whether the launched group exists; None means permission denied."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return True


async def _wait_for_owned_group_exit_unix(pgid: int, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        state = _owned_process_group_state_unix(pgid)
        if state is False:
            return True
        if state is None:
            return False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_PROCESS_GROUP_POLL_SECONDS, remaining))


async def _terminate_process_group_unix(
    proc: asyncio.subprocess.Process,
    *,
    owned_pgid: int,
) -> bool:
    """Settle the session launched by this tool, including its descendants.

    Both callers launch with ``start_new_session=True``. The captured leader
    PID remains the group's identity after ``proc.wait()`` has reaped it.
    """
    if owned_pgid <= 1 or owned_pgid != proc.pid:
        _logger.warning("Owned process group cleanup unconfirmed: invalid group %s", owned_pgid)
        return False

    try:
        os.killpg(owned_pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        _logger.warning("Owned process group %s cleanup unconfirmed: %s", owned_pgid, exc)
        return False
    else:
        if not await _wait_for_owned_group_exit_unix(
            owned_pgid, _PROCESS_GROUP_GRACE_SECONDS
        ):
            try:
                os.killpg(owned_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                _logger.warning(
                    "Owned process group %s cleanup unconfirmed: %s", owned_pgid, exc
                )
                return False

    if not await _wait_for_owned_group_exit_unix(
        owned_pgid, _PROCESS_GROUP_GRACE_SECONDS
    ):
        _logger.warning("Owned process group %s cleanup unconfirmed after KILL", owned_pgid)
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=_PROCESS_GROUP_GRACE_SECONDS)
    except asyncio.TimeoutError:
        _logger.warning("Owned process group %s leader reap unconfirmed", owned_pgid)
        return False
    return True


async def _read_limited_stream(
    stream: asyncio.StreamReader | None,
    *,
    stream_name: str,
    max_bytes: int,
    limit_event: asyncio.Event,
) -> tuple[bytes, int, bool, str]:
    if stream is None:
        return b"", 0, False, stream_name
    chunks: list[bytes] = []
    total_bytes = 0
    retained_bytes = 0
    truncated = False
    while True:
        chunk = await stream.read(_SHELL_READ_CHUNK_BYTES)
        if not chunk:
            break
        total_bytes += len(chunk)
        remaining = max_bytes - retained_bytes
        if remaining > 0:
            keep = chunk[:remaining]
            chunks.append(keep)
            retained_bytes += len(keep)
        if total_bytes > max_bytes:
            truncated = True
            limit_event.set()
    return b"".join(chunks), total_bytes, truncated, stream_name


async def _collect_limited_output_unix(
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

    owned_tasks = (stdout_task, stderr_task, wait_task, limit_task)

    async def settle_output_tasks() -> None:
        for task in owned_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*owned_tasks, return_exceptions=True)

    try:
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
                cleanup_confirmed = await _terminate_process_group_unix(
                    proc, owned_pgid=proc.pid
                )
            else:
                timed_out = True
                returncode = -1
                cleanup_confirmed = await _terminate_process_group_unix(
                    proc, owned_pgid=proc.pid
                )
            for task in pending:
                task.cancel()

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

            stdout_str = smart_decode(stdout_data)
            stderr_str = smart_decode(stderr_data)
            if not cleanup_confirmed:
                stderr_str += "\nProcess group cleanup could not be confirmed."
            if stdout_truncated:
                stdout_str += _output_limit_notice("stdout", stdout_total, max_bytes)
            if stderr_truncated:
                stderr_str += _output_limit_notice("stderr", stderr_total, max_bytes)
            if timed_out:
                timeout_msg = (
                    f"TimeoutError: The command execution exceeded the timeout of {timeout} seconds."
                )
                stderr_str = f"{stderr_str}\n{timeout_msg}" if stderr_str else timeout_msg
            if output_limited:
                limit_msg = (
                    f"Command output exceeded the limit of {max_bytes} bytes and was terminated."
                )
                stderr_str = f"{stderr_str}\n{limit_msg}" if stderr_str else limit_msg
            return returncode, stdout_str, stderr_str
        finally:
            await await_settled(asyncio.create_task(settle_output_tasks()))
    except asyncio.CancelledError:
        # The leader may already have exited while descendants still hold
        # stdout/stderr. Keep the launched group until its stop is settled,
        # including repeated cancellation during cleanup itself.
        await await_settled(asyncio.create_task(
            _terminate_process_group_unix(proc, owned_pgid=proc.pid)
        ))
        raise


# pylint: disable=too-many-branches, too-many-statements
def _execute_subprocess_sync(
    cmd: str,
    cwd: str,
    timeout: float,
    env: dict | None = None,
    *,
    cancel_event: threading.Event | None = None,
    max_bytes: int = SHELL_OUTPUT_MAX_BYTES,
) -> tuple[int, str, str]:
    """Execute subprocess synchronously in a thread.

    This function runs in a separate thread to avoid Windows asyncio
    subprocess limitations.

    stdout/stderr are redirected to temporary files instead of pipes.
    On Windows, child processes inherit pipe handles and keep them open
    even after the parent exits, which causes ``communicate()`` to block
    until *all* holders close (e.g. a Chrome process launched via
    ``Start-Process``).  With temp-file redirection, ``proc.wait()``
    only waits for the direct child (``cmd.exe``) to exit, so commands
    that spawn background processes return immediately.

    .. note::

       Callers must pre-process *cmd* through
       :func:`_collapse_embedded_newlines` before passing it here.
       ``execute_shell_command`` already does this.

    Args:
        cmd (`str`):
            The shell command to execute (must not contain embedded
            newlines — see note above).
        cwd (`str`):
            The working directory for the command execution.
        timeout (`float`):
            The maximum time (in seconds) allowed for the command to run.
        env (`dict | None`):
            Environment variables for the subprocess.

    Returns:
        `tuple[int, str, str]`:
            A tuple containing the return code, standard output, and
            standard error of the executed command. If timeout occurs, the
            return code will be -1 and stderr will contain timeout information.
    """
    stdout_path: str | None = None
    stderr_path: str | None = None
    stdout_file = None
    stderr_file = None

    try:
        cmd = _sanitize_win_cmd(cmd)
        wrapped = f'cmd /D /S /C "{cmd}"'

        stdout_fd, stdout_path = tempfile.mkstemp(prefix="lantai_out_")
        stderr_fd, stderr_path = tempfile.mkstemp(prefix="lantai_err_")
        stdout_file = os.fdopen(stdout_fd, "wb")
        stderr_file = os.fdopen(stderr_fd, "wb")

        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            wrapped,
            shell=False,
            stdout=stdout_file,
            stderr=stderr_file,
            text=False,
            cwd=cwd,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        # Parent copies are no longer needed — the child inherited its own
        # handles via CreateProcess.  Closing here avoids holding the files
        # open longer than necessary.
        stdout_file.close()
        stdout_file = None
        stderr_file.close()
        stderr_file = None

        timed_out = False
        output_limited = False
        cancelled = False
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if _temp_file_exceeds(stdout_path, max_bytes) or _temp_file_exceeds(
                stderr_path,
                max_bytes,
            ):
                output_limited = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)

        if timed_out or output_limited or cancelled:
            _kill_process_tree_win32(proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

        stdout_str = _read_temp_file(stdout_path, max_bytes=max_bytes)
        stderr_str = _read_temp_file(stderr_path, max_bytes=max_bytes)

        if timed_out:
            timeout_msg = (
                f"Command execution exceeded the timeout of {timeout} seconds."
            )
            if stderr_str:
                stderr_str = f"{stderr_str}\n{timeout_msg}"
            else:
                stderr_str = timeout_msg
            return -1, stdout_str, stderr_str

        if output_limited:
            limit_msg = (
                f"Command output exceeded the limit of {max_bytes} bytes "
                "and was terminated."
            )
            if stderr_str:
                stderr_str = f"{stderr_str}\n{limit_msg}"
            else:
                stderr_str = limit_msg
            return -1, stdout_str, stderr_str

        if cancelled:
            cancel_msg = "Command execution was cancelled."
            if stderr_str:
                stderr_str = f"{stderr_str}\n{cancel_msg}"
            else:
                stderr_str = cancel_msg
            return -1, stdout_str, stderr_str

        returncode = proc.returncode if proc.returncode is not None else -1
        return returncode, stdout_str, stderr_str

    except Exception as e:
        return -1, "", str(e)
    finally:
        for f in (stdout_file, stderr_file):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        for path in (stdout_path, stderr_path):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _build_subprocess_env() -> dict:
    """Build a Finder-safe shell environment for packaged macOS apps."""
    return build_sandboxed_tool_env()


# pylint: disable=too-many-branches, too-many-statements
async def execute_shell_command(
    command: str,
    timeout: float = 60.0,
    cwd: Optional[Path] = None,
) -> ToolResponse:
    """Execute a shell command and return its output.

    Platform shells: Windows uses cmd.exe; Linux/macOS use /bin/sh or /bin/bash.

    IMPORTANT: Always consider the operating system before choosing commands.

    Args:
        command (`str`):
            The shell command to execute.
        timeout (`float`, defaults to `60.0`):
            The maximum time (in seconds) allowed for the command to run.
            Default is 60.0 seconds.
        cwd (`Optional[Path]`, defaults to `None`):
            The working directory for the command execution.
            If None, defaults to WORKING_DIR.

    Returns:
        `ToolResponse`:
            The tool response containing the return code, standard output, and
            standard error of the executed command. If timeout occurs, the
            return code will be -1 and stderr will contain timeout information.
    """

    cmd = _collapse_embedded_newlines((command or "").strip())

    if isinstance(timeout, str):
        try:
            timeout = float(timeout)
        except (ValueError, TypeError):
            timeout = 60.0

    # Apply agent-configured default when the caller used the hardcoded
    # default (60.0).  An explicit LLM-provided value != 60.0 is kept.
    if timeout == 60.0:
        configured = get_current_shell_command_timeout()
        if configured is not None:
            timeout = configured

    # Use current workspace_dir from context, fallback to WORKING_DIR
    if cwd is not None:
        working_dir = cwd
    else:
        working_dir = get_current_workspace_dir() or WORKING_DIR

    policy_decision = evaluate_shell_command(cmd, cwd=working_dir)
    if policy_decision.decision == "forbidden":
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=(
                        "Tool policy blocked this shell command.\n"
                        f"Decision: {policy_decision.decision}\n"
                        f"Rule: {policy_decision.matched_rule_id or 'unknown'}\n"
                        f"Reason: {policy_decision.reason}"
                    ),
                ),
            ],
            is_error=True,
        )

    # Finder-launched macOS apps inherit a very small PATH, so include common
    # user tool locations such as ~/.local/bin where uv is usually installed.
    env = _build_subprocess_env()

    try:
        if sys.platform == "win32":
            # Windows: use thread pool to avoid asyncio subprocess limitations
            cancel_event = threading.Event()
            try:
                returncode, stdout_str, stderr_str = await asyncio.to_thread(
                    _execute_subprocess_sync,
                    cmd,
                    str(working_dir),
                    timeout,
                    env,
                    cancel_event=cancel_event,
                    max_bytes=SHELL_OUTPUT_MAX_BYTES,
                )
            except asyncio.CancelledError:
                cancel_event.set()
                raise
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                bufsize=0,
                cwd=str(working_dir),
                env=env,
                start_new_session=True,
            )
            returncode, stdout_str, stderr_str = await _collect_limited_output_unix(
                proc,
                timeout=timeout,
                max_bytes=SHELL_OUTPUT_MAX_BYTES,
            )

        if returncode == 0:
            if stdout_str:
                response_text = stdout_str
            else:
                response_text = "Command executed successfully (no output)."
            if stderr_str:
                response_text += f"\n[stderr]\n{stderr_str}"
        else:
            response_parts = [f"Command failed with exit code {returncode}."]
            if stdout_str:
                response_parts.append(f"\n[stdout]\n{stdout_str}")
            if stderr_str:
                response_parts.append(f"\n[stderr]\n{stderr_str}")
            response_text = "".join(response_parts)

        metadata: dict[str, object] = {"exit_code": returncode}
        if returncode != 0:
            python_recovery = _python_environment_recovery(
                stdout_str,
                stderr_str,
            )
            if python_recovery is not None:
                recovery, guidance = python_recovery
                metadata["recovery"] = recovery
                response_text += guidance

        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=response_text,
                ),
            ],
            metadata=metadata,
            is_error=returncode != 0,
        )

    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Shell command execution failed due to \n{e}",
                ),
            ],
            is_error=True,
        )


def smart_decode(data: bytes) -> str:
    try:
        decoded_str = data.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        decoded_str = data.decode(encoding, errors="replace")

    return decoded_str.strip("\n")
