# -*- coding: utf-8 -*-
"""Finite synthetic process tree; never opens an Office document or user store.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# The launcher prepends STATE and DELAYED as synthetic constants.
# Annotations leave those injected values intact.
STATE: Path
DELAYED: bool
stopped = False


def on_term(signum, frame):
    del frame, signum
    global stopped
    stopped = True


def write(name, value):
    # STATE is supplied by the launcher before this source executes.
    # pylint: disable-next=undefined-variable
    target = STATE / name
    temporary = STATE / (name + ".new")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(target)


def touch(name):
    (STATE / name).write_text("synthetic", encoding="utf-8")


def wait_until(predicate, seconds=75):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


signal.signal(signal.SIGTERM, on_term)
STATE.mkdir(parents=True, exist_ok=True)

if "--synthetic-child" in sys.argv:
    write("child.json", {"pid": os.getpid(), "pgid": os.getpgrp()})
    touch("child.ready")
    wait_until(lambda: stopped or (STATE / "release").exists())
    if stopped:
        touch("child.term")
    if DELAYED and stopped:
        touch("child.cleanup-entered")
        wait_until((STATE / "release").exists)
    touch("child.cleanup-complete")
    sys.exit(0)

# The explicit owner settles this process in cleanup.
# pylint: disable-next=consider-using-with
child = subprocess.Popen(
    [sys.executable, __file__, "--synthetic-child"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    if not wait_until((STATE / "child.ready").exists, seconds=5):
        raise RuntimeError("synthetic child did not become ready")
    write(
        "parent.json",
        {
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "child_pid": child.pid,
            "script": str(Path(__file__).resolve()),
        },
    )
    touch("parent.ready")
    wait_until(lambda: stopped or (STATE / "release").exists())
    if stopped:
        touch("parent.term")
        # Deliberately do NOT relay SIGTERM. This proves production group
        # signalling reaches the descendant rather than a cooperative relay.
    else:
        child.send_signal(signal.SIGTERM)
    try:
        returncode = child.wait(timeout=65)
    except subprocess.TimeoutExpired as exc:
        child.kill()
        returncode = child.wait(timeout=5)
        raise RuntimeError("synthetic safety cleanup was required") from exc
    write("child.reaped", {"pid": child.pid, "returncode": returncode})
    touch("parent.cleanup-complete")
finally:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=5)
# Natural completion is a controlled executable failure, not a fake native
# artifact. The real wrappers must settle it and never publish a success.
sys.exit(97)
