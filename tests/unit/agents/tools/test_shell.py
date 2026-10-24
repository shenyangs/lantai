# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import os
import shlex
import sys
import threading
import time
from pathlib import Path

import pytest

import lantai.agents.tools.shell as shell
from lantai.security import tool_execution_context


def test_build_subprocess_env_includes_user_local_bin(
    tmp_path,
    monkeypatch,
) -> None:
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")

    env = shell._build_subprocess_env()  # pylint: disable=protected-access
    path_entries = env["PATH"].split(os.pathsep)

    assert str(local_bin) in path_entries
    assert "/usr/bin" in path_entries
    assert str(Path(shell.sys.executable).parent) == path_entries[0]


def test_build_subprocess_env_strips_internal_tokens(monkeypatch) -> None:
    monkeypatch.setenv("LANTAI_BROWSER_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("LANTAI_DESKTOP_AUTH_TOKEN", "desktop-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("CUSTOM_AUTH_HEADER", "bearer secret")
    monkeypatch.setenv("LC_ALL", "C")

    env = shell._build_subprocess_env()  # pylint: disable=protected-access

    assert "LANTAI_BROWSER_BRIDGE_TOKEN" not in env
    assert "LANTAI_DESKTOP_AUTH_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "CUSTOM_AUTH_HEADER" not in env
    assert env["LC_ALL"] == "C"


@pytest.mark.asyncio
async def test_execute_shell_command_resolves_bundled_officecli(
    tmp_path,
    monkeypatch,
) -> None:
    backend_root = tmp_path / "lantai-server"
    backend = backend_root / "lantai-server"
    bundled_bin = backend_root / "bin"
    bundled_bin.mkdir(parents=True)
    backend.touch()
    officecli = bundled_bin / "officecli"
    officecli.write_text(
        "#!/bin/sh\nprintf 'officecli 9.9.9\\n'\n",
        encoding="utf-8",
    )
    officecli.chmod(0o755)
    monkeypatch.setattr(tool_execution_context.sys, "executable", str(backend))
    monkeypatch.setenv("OFFICECLI_PATH", str(officecli))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    response = await shell.execute_shell_command(
        "officecli --version",
        timeout=5,
        cwd=tmp_path,
    )

    assert response.is_error is False
    assert "officecli 9.9.9" in response.content[0]["text"]


@pytest.mark.asyncio
async def test_execute_shell_command_preserves_unix_command_separating_newlines(
    tmp_path,
) -> None:
    response = await shell.execute_shell_command(
        "printf first > first.txt\nprintf second > second.txt",
        timeout=5,
        cwd=tmp_path,
    )

    assert response.is_error is False
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second"


@pytest.mark.asyncio
async def test_execute_shell_command_surfaces_pep668_recovery_plan(tmp_path) -> None:
    response = await shell.execute_shell_command(
        "printf 'error: externally-managed-environment\\n' >&2; exit 1",
        timeout=5,
        cwd=tmp_path,
    )

    text = response.content[0]["text"]
    recovery = response.metadata["recovery"]
    assert response.is_error is True
    assert recovery["kind"] == "python_workspace_venv"
    assert recovery["reason"] == "pep_668"
    assert recovery["venv_path"] == ".lantai/venvs/python"
    assert "Do not use --break-system-packages" in text
    assert "python3 -m venv .lantai/venvs/python" in text
    assert ".lantai/venvs/python/bin/python -m pip" in text
    assert "Do not ask a non-technical user to paste these commands" in text


@pytest.mark.asyncio
async def test_execute_shell_command_does_not_misclassify_unrelated_failure(
    tmp_path,
) -> None:
    response = await shell.execute_shell_command(
        "printf 'ordinary failure\\n' >&2; exit 2",
        timeout=5,
        cwd=tmp_path,
    )

    assert response.is_error is True
    assert response.metadata == {"exit_code": 2}
    assert "[lantai:recovery]" not in response.content[0]["text"]


@pytest.mark.asyncio
async def test_execute_shell_command_terminates_when_output_exceeds_limit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shell, "SHELL_OUTPUT_MAX_BYTES", 4096)
    code = "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    response = await shell.execute_shell_command(command, timeout=5, cwd=tmp_path)
    text = response.content[0]["text"]

    assert "Command failed with exit code -1." in text
    assert "Command output exceeded the limit of 4096 bytes" in text
    assert "[lantai:stdout_truncated]" in text
    assert len(text.encode("utf-8")) < 12000


@pytest.mark.asyncio
async def test_execute_shell_command_windows_cancel_signals_sync_runner(
    tmp_path,
    monkeypatch,
) -> None:
    started = threading.Event()
    released = threading.Event()
    cancel_events: list[threading.Event | None] = []

    def fake_sync_runner(
        _cmd,
        _cwd,
        _timeout,
        _env,
        cancel_event=None,
        **_kwargs,
    ):
        cancel_events.append(cancel_event)
        started.set()
        deadline = time.monotonic() + 2.0
        while cancel_event is not None and not cancel_event.is_set():
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        released.set()
        return -1, "", "cancelled"

    monkeypatch.setattr(shell.sys, "platform", "win32")
    monkeypatch.setattr(shell, "_execute_subprocess_sync", fake_sync_runner)

    task = asyncio.create_task(
        shell.execute_shell_command("long-running", timeout=5, cwd=tmp_path)
    )
    assert await asyncio.to_thread(started.wait, 1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(released.wait, 1.0)

    assert cancel_events
    assert cancel_events[0] is not None
    assert cancel_events[0].is_set()


def test_execute_subprocess_sync_terminates_when_temp_output_exceeds_limit(
    tmp_path,
    monkeypatch,
) -> None:
    killed: list[int] = []

    class FakeProcess:
        pid = 12345
        returncode = None

        def __init__(self, *args, stdout, stderr, **kwargs):
            self.stdout = os.fdopen(os.dup(stdout.fileno()), "wb")
            self.stderr = os.fdopen(os.dup(stderr.fileno()), "wb")
            self.poll_count = 0
            self.killed = False

        def poll(self):
            if self.poll_count == 0:
                self.stdout.write(b"x" * 9000)
                self.stdout.flush()
            self.poll_count += 1
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -9 if self.killed else 0
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    monkeypatch.setattr(shell.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, raising=False)
    monkeypatch.setattr(shell.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(shell, "_kill_process_tree_win32", lambda pid: killed.append(pid))

    returncode, stdout, stderr = shell._execute_subprocess_sync(
        "spam",
        str(tmp_path),
        timeout=10,
        env={},
        max_bytes=4096,
    )

    assert returncode == -1
    assert killed == [12345]
    assert "[lantai:output_truncated]" in stdout
    assert "Command output exceeded the limit of 4096 bytes" in stderr
