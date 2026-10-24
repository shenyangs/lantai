# -*- coding: utf-8 -*-
"""Cross-process store locks keep Windows and POSIX authority equivalent."""

from __future__ import annotations

import os
import subprocess
from contextlib import ExitStack
import sys
import time
from pathlib import Path

import pytest


_WORKER = """
import os, sys
from pathlib import Path
from lantai.utils.file_lock import locked_file_descriptor
descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    try:
        with locked_file_descriptor(descriptor,
                exclusive=sys.argv[2] == 'exclusive',
                blocking=sys.argv[3] == 'blocking'):
            if sys.argv[4] == 'probe':
                print('acquired', flush=True)
            else:
                Path(sys.argv[4]).write_text('acquired')
                sys.stdin.readline()
    except BlockingIOError:
        print('busy', flush=True)
        sys.exit(11)
finally:
    os.close(descriptor)
"""


def _probe(path: Path, *, exclusive: bool = True):
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _WORKER,
            str(path),
            "exclusive" if exclusive else "shared",
            "nonblocking",
            "probe",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _wait_ready(process: subprocess.Popen, ready: Path) -> None:
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            _, stderr = process.communicate()
            pytest.fail(f"lock holder exited {process.returncode}: {stderr}")
        time.sleep(0.01)
    assert ready.exists(), "cross-process lock did not become available"


def test_runtime_and_presentation_import_without_posix_fcntl() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "\nimport importlib, sys\n# Let dependencies select their"
                " real host backend before faulting ours.\nimport mcp\nsy"
                "s.modules['fcntl'] = None\nfor name in ('lantai.runtime."
                "cost_accounting', 'lantai.runtime.remote_tasks',\n      "
                "       'lantai.runtime.turn_control', 'lantai.presentati"
                "on.jobs',\n             'lantai.presentation.pipeline_re"
                "gistry', 'lantai.app.reading.managed'):\n    importlib.i"
                "mport_module(name)\n"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("exclusive", [True, False])
def test_writer_is_excluded_and_shared_readers_can_coexist(
    tmp_path: Path,
    exclusive: bool,
) -> None:
    from lantai.utils.file_lock import locked_file_descriptor

    path = tmp_path / "authority.lock"
    with path.open("a+b") as handle:
        with locked_file_descriptor(handle.fileno(), exclusive=exclusive):
            writer = _probe(path)
            assert writer.returncode == 11, writer.stderr
            assert writer.stdout.strip() == "busy"
            reader = _probe(path, exclusive=False)
            assert reader.returncode == (11 if exclusive else 0), reader.stderr
        released = _probe(path)
        assert released.returncode == 0, released.stderr
        assert released.stdout.strip() == "acquired"


def test_blocking_writer_waits_then_acquires_after_release(
    tmp_path: Path,
) -> None:
    from lantai.utils.file_lock import locked_file_descriptor

    with ExitStack() as process_stack:
        path = tmp_path / "authority.lock"
        ready = tmp_path / "writer.ready"
        process = None
        try:
            with path.open("a+b") as handle:
                with locked_file_descriptor(handle.fileno()):
                    process = process_stack.enter_context(
                        subprocess.Popen(
                            [
                                sys.executable,
                                "-c",
                                _WORKER,
                                str(path),
                                "exclusive",
                                "blocking",
                                str(ready),
                            ],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        ),
                    )
                    time.sleep(0.1)
                    assert process.poll() is None
                    assert not ready.exists()
                _wait_ready(process, ready)
                process.communicate(input="release\n", timeout=10)
                assert process.returncode == 0
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


def test_exception_releases_authority_without_changing_lock_bytes(
    tmp_path: Path,
) -> None:
    from lantai.utils.file_lock import locked_file_descriptor

    path = tmp_path / "authority.lock"
    path.write_bytes(b"existing-lock-metadata")
    with path.open("r+b") as handle:
        with pytest.raises(ValueError, match="aborted publication"):
            with locked_file_descriptor(handle.fileno()):
                raise ValueError("aborted publication")
    result = _probe(path)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == b"existing-lock-metadata"


def test_killed_writer_cannot_keep_authority(tmp_path: Path) -> None:
    path = tmp_path / "authority.lock"
    ready = tmp_path / "holder.ready"
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WORKER,
            str(path),
            "exclusive",
            "blocking",
            str(ready),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            _wait_ready(process, ready)
            assert _probe(path).returncode == 11
            process.kill()
            process.communicate(timeout=10)
            result = _probe(path)
            assert result.returncode == 0, result.stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


def test_missing_platform_lock_fails_closed(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("this fault injects the POSIX backend import failure")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sys
from lantai.utils.file_lock import locked_file_descriptor
sys.modules['fcntl'] = None
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    try:
        with locked_file_descriptor(fd):
            print('unsafe-body-entered')
    except ModuleNotFoundError:
        sys.exit(12)
finally:
    os.close(fd)
""",
            str(tmp_path / "authority.lock"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 12, result.stderr
    assert "unsafe-body-entered" not in result.stdout
