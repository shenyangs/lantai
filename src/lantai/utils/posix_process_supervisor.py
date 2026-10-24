# -*- coding: utf-8 -*-
"""A private child that retains operation ownership across caller crashes.

Commands arrive over inherited pipes, never a public endpoint. The target gets
its own process group and no ownership descriptors. The supervisor releases
the inherited writer lock only after that entire group has settled.
"""
from __future__ import annotations

import json
import os
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Any


def _validated_descriptors(arguments) -> tuple[int, int, int, int]:
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 4 or not all(
        value.isdecimal() for value in arguments
    ):
        raise ValueError("invalid private process supervisor descriptors")
    config_fd, life_fd, receipt_fd, writer_fd = map(int, arguments)
    if len({config_fd, life_fd, receipt_fd}) != 3:
        raise ValueError("private process supervisor descriptors overlap")
    for descriptor in (config_fd, life_fd, receipt_fd):
        if descriptor <= 2 or not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            raise ValueError(
                "private process supervisor requires inherited pipes",
            )
    if writer_fd and (
        writer_fd <= 2 or not stat.S_ISREG(os.fstat(writer_fd).st_mode)
    ):
        raise ValueError("private process supervisor requires a writer lock")
    return config_fd, life_fd, receipt_fd, writer_fd


def _read_configuration(config_fd, life_fd, owner_alive, is_stopped):
    configuration = bytearray()
    while not is_stopped() and owner_alive():
        ready, _, _ = select.select([config_fd, life_fd], [], [], 0.02)
        if config_fd in ready:
            block = os.read(config_fd, 65536)
            if not block:
                break
            configuration.extend(block)
    os.close(config_fd)
    return configuration


def _settle_target(process, scope):
    if process.poll() is None or not scope.empty():
        scope.signal(signal.SIGTERM)
        deadline = time.monotonic() + 0.75
        while process.poll() is None or not scope.empty():
            if time.monotonic() >= deadline:
                scope.signal(signal.SIGKILL)
                break
            time.sleep(0.02)
        process.wait(timeout=2)
        scope.wait_empty()


def _settle_failed_target(process, scope, settled):
    if process is None:
        settled = True
    elif scope is not None:
        try:
            scope.signal(signal.SIGKILL)
            process.wait(timeout=2)
            scope.wait_empty()
            settled = True
        except BaseException:
            pass
    return settled


def _retain_fence_until_settled(process, scope):
    # Never make an unproved tree reclaimable by dropping its fence.
    # The caller records pending settlement; a crash also retains the
    # inherited kernel lock until this exact tree can be proved empty.
    while True:
        try:
            if scope is not None:
                scope.signal(signal.SIGKILL)
                process.wait(timeout=1)
                if scope.empty():
                    break
        except BaseException:
            pass
        time.sleep(0.1)


def main(arguments=None) -> int:
    config_fd, life_fd, receipt_fd, writer_fd = _validated_descriptors(
        arguments,
    )
    from .owned_subprocess import _PosixScope

    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        del signum, frame
        stopped = True

    signal.signal(signal.SIGTERM, stop)

    def owner_alive():
        ready, _, _ = select.select([life_fd], [], [], 0)
        return not ready or bool(os.read(life_fd, 1))

    process = scope = None
    settled = False
    receipt: dict[str, Any] = {}
    try:
        configuration = _read_configuration(
            config_fd,
            life_fd,
            owner_alive,
            lambda: stopped,
        )
        if stopped or not owner_alive():
            settled = True
            receipt = {"returncode": 0, "settled": True}
            return 0
        command = json.loads(configuration)
        # close_fds keeps every ownership pipe and the writer fence out of
        # native executables. Only this supervisor retains the fence.
        # This owner retains the process until its entire group settles.
        # pylint: disable-next=consider-using-with
        process = subprocess.Popen(
            command["args"],
            shell=command["shell"],
            executable=command.get("executable"),
            start_new_session=True,
            close_fds=True,
        )
        scope = _PosixScope(process)
        while not stopped and owner_alive() and process.poll() is None:
            select.select([life_fd], [], [], 0.02)
        _settle_target(process, scope)
        settled = True
        receipt = {"returncode": process.returncode, "settled": True}
    except BaseException as error:
        receipt = {
            "error": type(error).__name__,
            "errno": getattr(error, "errno", None),
        }
        settled = _settle_failed_target(process, scope, settled)
        receipt["settled"] = settled
    finally:
        try:
            os.write(receipt_fd, json.dumps(receipt).encode("utf-8"))
        except (BrokenPipeError, OSError):
            pass
        os.close(receipt_fd)
        if not settled:
            _retain_fence_until_settled(process, scope)
        os.close(life_fd)
        if writer_fd:
            os.close(writer_fd)
    return 0 if settled else 1


if __name__ == "__main__":
    raise SystemExit(main())
