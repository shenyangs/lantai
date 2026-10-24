"""A Shell tool stop owns its launched process group after the leader exits."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys

import pytest

from lantai.agents.tools import shell
from lantai.agents.tools.shell import _terminate_process_group_unix


@pytest.mark.skipif(sys.platform == "win32", reason="Unix process groups only")
@pytest.mark.asyncio
async def test_terminate_reaps_owned_child_after_parent_exits(tmp_path) -> None:
    heartbeat = tmp_path / "heartbeat"
    grandchild_heartbeat = tmp_path / "grandchild-heartbeat"
    unrelated_heartbeat = tmp_path / "unrelated-heartbeat"
    child_pid_file = tmp_path / "child.pid"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    heartbeat_code = "\n".join(
        [
            "import signal, sys, time",
            "from pathlib import Path",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "heartbeat, pid_file = map(Path, sys.argv[1:3])",
            "pid_file.write_text(str(__import__('os').getpid()))",
            "while True:",
            "    with heartbeat.open('a') as stream: stream.write('x')",
            "    time.sleep(0.05)",
        ]
    )
    child_code = "\n".join(
        [
            "import os, signal, subprocess, sys, time",
            "from pathlib import Path",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "heartbeat, pid_file, grandchild_heartbeat, grandchild_pid_file = "
            "map(Path, sys.argv[2:6])",
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], "
            "str(grandchild_heartbeat), str(grandchild_pid_file)], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL, close_fds=True)",
            "pid_file.write_text(str(os.getpid()))",
            "while True:",
            "    with heartbeat.open('a') as stream: stream.write('x')",
            "    time.sleep(0.05)",
        ]
    )
    parent_code = "\n".join(
        [
            "import subprocess, sys",
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL, close_fds=True)",
        ]
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        child_code,
        heartbeat_code,
        str(heartbeat),
        str(child_pid_file),
        str(grandchild_heartbeat),
        str(grandchild_pid_file),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    unrelated = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        heartbeat_code,
        str(unrelated_heartbeat),
        str(tmp_path / "unrelated.pid"),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert await asyncio.wait_for(proc.wait(), timeout=3) == 0
        for _ in range(60):
            if all(
                path.exists() and path.stat().st_size > 0
                for path in (heartbeat, grandchild_heartbeat, unrelated_heartbeat)
            ):
                break
            await asyncio.sleep(0.05)
        assert child_pid_file.exists() and grandchild_pid_file.exists()
        assert os.getpgid(int(child_pid_file.read_text())) == proc.pid
        assert os.getpgid(int(grandchild_pid_file.read_text())) == proc.pid
        assert os.getpgid(unrelated.pid) == unrelated.pid

        settled = await _terminate_process_group_unix(proc, owned_pgid=proc.pid)
        child_stopped_at = heartbeat.stat().st_size
        grandchild_stopped_at = grandchild_heartbeat.stat().st_size
        unrelated_at = unrelated_heartbeat.stat().st_size
        await asyncio.sleep(0.25)

        assert heartbeat.stat().st_size == child_stopped_at
        assert grandchild_heartbeat.stat().st_size == grandchild_stopped_at
        assert unrelated_heartbeat.stat().st_size > unrelated_at
        assert settled is True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.killpg(unrelated.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await unrelated.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix process groups only")
@pytest.mark.asyncio
async def test_permission_failure_reports_unsettled_owned_group(
    monkeypatch, caplog
) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(10)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    real_killpg = os.killpg

    def deny_term(pgid: int, sig: int) -> None:
        if pgid == proc.pid and sig == signal.SIGTERM:
            raise PermissionError("isolated denial")
        real_killpg(pgid, sig)

    try:
        monkeypatch.setattr(shell.os, "killpg", deny_term)
        settled = await _terminate_process_group_unix(proc, owned_pgid=proc.pid)
        assert settled is False
        assert proc.returncode is None
        assert "unconfirmed" in caplog.text.lower()
    finally:
        monkeypatch.setattr(shell.os, "killpg", real_killpg)
        try:
            real_killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix process groups only")
@pytest.mark.asyncio
async def test_cancelled_shell_tool_settles_term_ignoring_child(tmp_path) -> None:
    heartbeat = tmp_path / "cancel-heartbeat"
    child_pid_file = tmp_path / "cancel-child.pid"
    child_code = "\n".join(
        [
            "import os, signal, sys, time",
            "from pathlib import Path",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "heartbeat, pid_file = map(Path, sys.argv[1:3])",
            "pid_file.write_text(str(os.getpid()))",
            "while True:",
            "    with heartbeat.open('a') as stream: stream.write('x')",
            "    time.sleep(0.05)",
        ]
    )
    parent_code = "\n".join(
        [
            "import subprocess, sys, time",
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], "
            "*sys.argv[2:]], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)",
            "time.sleep(10)",
        ]
    )
    command = " ".join(
        shlex.quote(part)
        for part in (
            sys.executable,
            "-c",
            parent_code,
            child_code,
            str(heartbeat),
            str(child_pid_file),
        )
    )
    task = asyncio.create_task(
        shell.execute_shell_command(command, timeout=15, cwd=tmp_path)
    )
    owned_pgid: int | None = None
    try:
        for _ in range(60):
            if child_pid_file.exists() and heartbeat.exists():
                break
            await asyncio.sleep(0.05)
        assert child_pid_file.exists() and heartbeat.exists()
        child_pid = int(child_pid_file.read_text())
        owned_pgid = os.getpgid(child_pid)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stopped_at = heartbeat.stat().st_size
        await asyncio.sleep(0.25)

        assert heartbeat.stat().st_size == stopped_at
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if owned_pgid is not None:
            try:
                os.killpg(owned_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
