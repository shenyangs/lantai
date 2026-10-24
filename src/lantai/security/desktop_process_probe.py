"""Fixed-operation process ownership probe for the desktop supervisor.

Only process identities and ownership booleans leave this process. Command
lines, environments and credentials are never serialized or logged.
"""
from __future__ import annotations

import json
import os
import signal
import sys

import psutil

OWNER_ENV = "LANTAI_DESKTOP_INSTANCE_NONCE"


def snapshot(nonce: str) -> list[dict]:
    records = []
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    username = psutil.Process(os.getpid()).username().casefold() if uid is None else None
    for pid in psutil.pids():
        if pid <= 0:
            continue
        try:
            process = psutil.Process(pid)
            if uid is not None and process.uids().effective != uid:
                continue
            if username is not None and process.username().casefold() != username:
                continue
            birth = str(process.create_time())
            status = process.status()
            alive = status not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
            owned = alive and process.environ().get(OWNER_ENV) == nonce
            ppid = process.ppid()
            # Do not publish an environment read from a recycled PID.
            if not process.is_running():
                continue
            records.append({
                "pid": pid, "ppid": ppid, "birth": birth,
                "alive": alive, "owned": bool(owned),
            })
        except psutil.NoSuchProcess:
            continue
        # A partial scan cannot prove that all owned helpers have settled.
        # AccessDenied intentionally propagates, with no process data output.
    return records


def signal_owned(nonce: str, requests: list[dict]) -> None:
    for request in requests:
        pid = request.get("pid")
        birth = request.get("birth")
        name = request.get("signal")
        if type(pid) is not int or pid <= 0 or not isinstance(birth, str):
            raise ValueError("invalid process identity")
        if name not in ("SIGTERM", "SIGKILL"):
            raise ValueError("invalid termination signal")
        try:
            process = psutil.Process(pid)
            if str(process.create_time()) != birth:
                continue  # The registered process has died; this PID is foreign.
            if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                continue
            if process.environ().get(OWNER_ENV) != nonce:
                raise ValueError("process ownership is unconfirmed")
            # psutil's mutating methods recheck creation identity before kill.
            if name == "SIGKILL":
                process.kill()
            else:
                process.send_signal(signal.SIGTERM)
        except psutil.NoSuchProcess:
            continue


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(1024 * 1024 + 1))
        nonce = request.get("nonce")
        if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
            raise ValueError("invalid instance identity")
        signals = request.get("signals", [])
        if not isinstance(signals, list) or len(signals) > 4096:
            raise ValueError("invalid signal request")
        signal_owned(nonce, signals)
        result = {"records": snapshot(nonce)}
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (psutil.Error, ValueError, TypeError, AttributeError, OSError) as error:
        # Do not print exception messages: third-party errors may contain argv.
        print(f"process ownership probe failed: {type(error).__name__}", file=sys.stderr)
        return 1
