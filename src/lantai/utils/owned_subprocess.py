# -*- coding: utf-8 -*-
"""subprocess.run with optional operation deadline and process-tree
ownership."""
from __future__ import annotations

import json
import os
import signal
import subprocess as _subprocess
import sys
import time
from contextlib import suppress

import psutil

from .operation_control import current_operation, remaining_timeout


def __getattr__(name):
    return getattr(_subprocess, name)


class _PosixScope:
    def __init__(self, process):
        self.pid = process.pid
        try:
            self.birth = psutil.Process(self.pid).create_time()
            if os.getpgid(self.pid) != self.pid:
                raise RuntimeError(
                    "Owned subprocess has no isolated process group",
                )
        except psutil.NoSuchProcess:
            self.birth = (
                None  # A very short-lived leader can already have exited.
            )

    def _check_identity(self):
        try:
            birth = psutil.Process(self.pid).create_time()
        except psutil.NoSuchProcess:
            return
        if self.birth is None or birth != self.birth:
            raise RuntimeError("Owned subprocess group identity changed")

    def signal(self, value):
        self._check_identity()
        with suppress(ProcessLookupError):
            os.killpg(self.pid, value)

    def empty(self):
        self._check_identity()
        for process in psutil.process_iter(attrs=["pid", "status"]):
            try:
                if (
                    os.getpgid(process.pid) == self.pid
                    and process.info["status"] != psutil.STATUS_ZOMBIE
                ):
                    return False
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
        return True

    def wait_empty(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not self.empty():
            if time.monotonic() >= deadline:
                raise RuntimeError("Owned subprocess tree did not settle")
            time.sleep(0.02)


def _settle(process, scope):
    if isinstance(scope, _SupervisedScope):
        scope.stop()
        process.communicate(timeout=3.0)
        scope.finish()
        return
    if os.name == "nt":
        scope.terminate()
    else:
        scope.signal(signal.SIGTERM)
    grace = time.monotonic() + 1.0
    while True:
        try:
            process.communicate(timeout=0.05)
            if os.name == "nt" or scope.empty():
                break
        except _subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= grace:
            if os.name == "nt":
                scope.terminate()
            else:
                scope.signal(signal.SIGKILL)
            process.communicate(timeout=2.0)
            break
    scope.wait_empty()


class _SupervisedScope:
    def __init__(self, process, life_fd, receipt_fd):
        self.process = process
        self.life_fd = life_fd
        self.receipt_fd = receipt_fd
        self.receipt = None
        try:
            self.birth = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            self.birth = None

    def stop(self):
        if self.process.poll() is None:
            # This exact guardian performs target group cleanup. Killing the
            # guardian itself would discard the retained writer fence.
            if psutil.Process(self.process.pid).create_time() != self.birth:
                raise RuntimeError("process supervisor identity changed")
            self.process.terminate()

    def finish(self):
        if self.receipt is None:
            self.receipt = json.loads(os.read(self.receipt_fd, 65536) or b"{}")
        if not self.receipt.get("settled"):
            raise RuntimeError(
                "owned process supervisor did not confirm settlement",
            )
        if "error" in self.receipt:
            if self.receipt.get("errno") is not None:
                raise OSError(
                    self.receipt["errno"],
                    "owned command could not start",
                )
            raise RuntimeError(
                "owned process supervisor failed: " + self.receipt["error"],
            )
        return self.receipt["returncode"]

    def close(self):
        os.close(self.life_fd)
        os.close(self.receipt_fd)


def _supervised_process(popenargs, kwargs, writer_fd):
    if len(popenargs) != 1 or "args" in kwargs:
        raise TypeError("owned command requires one args value")
    if kwargs.get("preexec_fn") is not None or kwargs.get("pass_fds"):
        raise ValueError(
            "owned command cannot pass custom child admission hooks",
        )
    args = popenargs[0]
    command = {
        "args": os.fsdecode(args)
        if isinstance(args, (str, bytes, os.PathLike))
        else [os.fsdecode(value) for value in args],
        "shell": kwargs.pop("shell", False),
        "executable": os.fsdecode(kwargs.pop("executable"))
        if kwargs.get("executable") is not None
        else None,
    }
    kwargs.pop("executable", None)
    config_read, config_write = os.pipe()
    life_read, life_write = os.pipe()
    receipt_read, receipt_write = os.pipe()
    descriptors = (config_read, life_read, receipt_write)
    if writer_fd is not None:
        descriptors += (writer_fd,)
    argv = [
        str(value)
        for value in (config_read, life_read, receipt_write, writer_fd or 0)
    ]
    launcher = (
        [sys.executable, "--owned-process-supervisor"]
        if getattr(sys, "frozen", False)
        else [
            sys.executable,
            "-B",
            "-m",
            "lantai.utils.posix_process_supervisor",
        ]
    )
    process = None
    try:
        # The caller retains this guardian until receipt/cleanup settles.
        # pylint: disable-next=consider-using-with
        process = _subprocess.Popen(
            launcher + argv,
            pass_fds=descriptors,
            **kwargs,
        )
        for descriptor in (config_read, life_read, receipt_write):
            os.close(descriptor)
        payload = json.dumps(command).encode("utf-8")
        while payload:
            payload = payload[os.write(config_write, payload) :]
        os.close(config_write)
        scope = _SupervisedScope(process, life_write, receipt_read)
        return process, scope
    except BaseException:
        # EOF retires an admitted guardian even if configuration handoff fails.
        for descriptor in (
            config_read,
            config_write,
            life_read,
            life_write,
            receipt_read,
            receipt_write,
        ):
            with suppress(OSError):
                os.close(descriptor)
        if process is not None:
            process.wait(timeout=3.0)
        raise


def _prepare_owned_process_options(stdin_payload, capture_output, kwargs):
    if stdin_payload is not None:
        if "stdin" in kwargs:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = _subprocess.PIPE
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError(
                (
                    "stdout and stderr arguments may not be used with "
                    "capture_output"
                ),
            )
        kwargs.update(stdout=_subprocess.PIPE, stderr=_subprocess.PIPE)
    if os.name == "nt":
        kwargs["creationflags"] = (
            kwargs.get("creationflags", 0) | 0x00000004
        )  # CREATE_SUSPENDED
    else:
        kwargs["start_new_session"] = True


def _communicate_owned(process, control, popenargs, timeout, stdin_payload):
    local_deadline = time.monotonic() + timeout
    pending_input = stdin_payload
    while True:
        control.ensure_active()
        remaining = local_deadline - time.monotonic()
        if remaining <= 0:
            raise _subprocess.TimeoutExpired(popenargs[0], timeout)
        try:
            stdout, stderr = process.communicate(
                input=pending_input,
                timeout=min(0.05, remaining),
            )
            return stdout, stderr
        except _subprocess.TimeoutExpired:
            pending_input = None


def _settle_failed_process(process, scope, settled):
    if (
        isinstance(scope, _SupervisedScope)
        and scope.receipt is not None
        and scope.receipt.get("settled")
    ):
        settled = True
    if process is not None and not settled:
        if scope is not None:
            _settle(process, scope)
        else:
            # Before Windows admission the suspended process cannot spawn.
            process.kill()
            process.communicate(timeout=2.0)
        settled = True
    elif process is None:
        settled = True
    return settled


# Keep subprocess.run's public input keyword for API compatibility.
def run(
    *popenargs,
    input=None,  # pylint: disable=redefined-builtin
    capture_output=False,
    timeout=None,
    check=False,
    **kwargs,
):
    control = current_operation()
    if control is None:
        return _subprocess.run(
            *popenargs,
            input=input,
            capture_output=capture_output,
            timeout=timeout,
            check=check,
            **kwargs,
        )
    control.ensure_active()
    timeout = remaining_timeout(timeout)
    _prepare_owned_process_options(input, capture_output, kwargs)
    process = None
    scope = None
    settled = False
    control.resource_started()
    try:
        if os.name == "nt":
            # Admission may fail; the owner must still reap this process.
            # pylint: disable-next=consider-using-with
            process = _subprocess.Popen(*popenargs, **kwargs)
            from .windows_process_job import WindowsProcessJob

            scope = WindowsProcessJob(process)
        else:
            process, scope = _supervised_process(
                popenargs,
                kwargs,
                control.writer_lock_fd,
            )
        stdout, stderr = _communicate_owned(
            process,
            control,
            popenargs,
            timeout,
            input,
        )
        # A leader exiting does not prove its descendants have exited.
        if os.name == "nt":
            scope.terminate()
            scope.wait_empty()
        else:
            process.returncode = scope.finish()
        settled = True
        control.ensure_active()
        if check and process.returncode:
            raise _subprocess.CalledProcessError(
                process.returncode,
                popenargs[0],
                output=stdout,
                stderr=stderr,
            )
        return _subprocess.CompletedProcess(
            popenargs[0],
            process.returncode,
            stdout,
            stderr,
        )
    except BaseException:
        settled = _settle_failed_process(process, scope, settled)
        raise
    finally:
        if os.name == "nt" and scope is not None:
            scope.close()
        elif isinstance(scope, _SupervisedScope):
            scope.close()
        control.resource_settled(settled)
