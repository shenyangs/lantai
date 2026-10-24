# -*- coding: utf-8 -*-
from __future__ import annotations

import operator
from contextlib import ExitStack

import os
import json
from pathlib import Path
import subprocess
import sys

import pytest
import psutil

from lantai.security.desktop_process_probe import (
    OWNER_ENV,
    signal_owned,
    snapshot,
)


@pytest.fixture(name="processes")
def owned_processes(monkeypatch):
    with ExitStack() as process_stack:
        children = []
        # Exercise native identity and environment reads only for fixture-owned
        # PIDs. The separate partial-scan lock retains fail-closed production
        # scans.
        monkeypatch.setattr(
            psutil,
            "pids",
            lambda: [child.pid for child in children],
        )

        def spawn(nonce=None, arguments=()):
            env = {**os.environ}
            env.pop(OWNER_ENV, None)
            if nonce:
                env[OWNER_ENV] = nonce
            child = process_stack.enter_context(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                        *arguments,
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
            )
            children.append(child)
            return child

        yield spawn
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_native_environment_proof_does_not_accept_a_command_line_marker(
    processes,
):
    nonce = "0123456789abcdef" * 4
    owned = processes(nonce)
    foreign = processes(arguments=(f"{OWNER_ENV}={nonce}",))
    records = {item["pid"]: item for item in snapshot(nonce)}
    assert records[owned.pid]["owned"] is True
    assert records[foreign.pid]["owned"] is False
    assert set(records[owned.pid]) == {
        "pid",
        "ppid",
        "birth",
        "alive",
        "owned",
    }


def test_foreign_native_process_survives_an_unauthorized_signal(processes):
    nonce = "0123456789abcdef" * 4
    foreign = processes("fedcba9876543210" * 4)
    birth = str(psutil.Process(foreign.pid).create_time())
    with pytest.raises(ValueError, match="ownership"):
        signal_owned(
            nonce,
            [{"pid": foreign.pid, "birth": birth, "signal": "SIGKILL"}],
        )
    assert foreign.poll() is None


def test_recycled_process_identity_cannot_receive_a_signal(processes):
    nonce = "0123456789abcdef" * 4
    child = processes(nonce)
    signal_owned(
        nonce,
        [{"pid": child.pid, "birth": "an-earlier-birth", "signal": "SIGKILL"}],
    )
    assert child.poll() is None


def test_native_owned_process_is_confirmed_dead_after_kill(processes):
    nonce = "0123456789abcdef" * 4
    child = processes(nonce)
    records = {item["pid"]: item for item in snapshot(nonce)}
    signal_owned(
        nonce,
        [
            {
                "pid": child.pid,
                "birth": records[child.pid]["birth"],
                "signal": "SIGKILL",
            },
        ],
    )
    child.wait(timeout=5)
    assert not any(
        item["pid"] == child.pid and item["alive"] for item in snapshot(nonce)
    )


def test_a_partial_environment_scan_cannot_be_reported_as_complete(
    monkeypatch,
):
    monkeypatch.setattr(psutil, "pids", lambda: [os.getpid()])

    def denied(_self):
        raise psutil.AccessDenied(os.getpid())

    monkeypatch.setattr(psutil.Process, "environ", denied)
    with pytest.raises(psutil.AccessDenied):
        snapshot("0123456789abcdef" * 4)


def test_windows_snapshot_excludes_another_user_before_reading_environment(
    monkeypatch,
):
    monkeypatch.delattr(os, "geteuid", raising=False)
    current_pid = os.getpid()
    monkeypatch.setattr(psutil, "pids", lambda: [current_pid, current_pid + 1])

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def username(self):
            return (
                "DOMAIN\\Owner"
                if self.pid == current_pid
                else "NT AUTHORITY\\SYSTEM"
            )

        def create_time(self):
            return 1.0

        def status(self):
            return psutil.STATUS_RUNNING

        def environ(self):
            if self.pid != current_pid:
                raise psutil.AccessDenied(self.pid)
            return {}

        def ppid(self):
            return 1

        def is_running(self):
            return True

    monkeypatch.setattr(psutil, "Process", Process)
    assert [item["pid"] for item in snapshot("test-owner")] == [current_pid]


def test_snapshot_never_publishes_the_windows_idle_pid(monkeypatch):
    monkeypatch.setattr(psutil, "pids", lambda: [0])

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def username(self):
            return "Owner"

        def uids(self):
            return type("uids", (), {"effective": os.geteuid()})()

        def create_time(self):
            return 0.0

        def status(self):
            return psutil.STATUS_RUNNING

        def environ(self):
            return {}

        def ppid(self):
            return 0

        def is_running(self):
            return True

    monkeypatch.setattr(psutil, "Process", Process)
    assert operator.eq(snapshot("test-owner"), [])


@pytest.mark.parametrize("entry", ["module", "packaged"])
def test_probe_entry_avoids_cli_and_credential_bootstrap(entry, tmp_path):
    root = Path(__file__).resolve().parents[3]
    launcher = (
        "runpy.run_module('lantai',run_name='__main__')"
        if entry == "module"
        else (
            "runpy.run_path("
            f"{str(root / 'electron' / 'entry.py')!r}"
            ",run_name='__main__')"
        )
    )
    script = (
        "\nimport runpy,sys,importlib.abc\nimport "
        "os,psutil\npsutil.pids=lambda: "
        "[os.getpid()]\nattempts=[]\nclass "
        "Watch(importlib.abc.MetaPathFinder):\n    "
        "def find_spec(self,fullname,path=None,targe"
        "t=None):\n        if fullname == "
        "'lantai.envs' or fullname.startswith('lanta"
        "i.cli'):\n            attempts.append(fullna"
        "me)\n            raise ImportError('credenti"
        "al and CLI imports are forbidden by this "
        "probe')\nsys.meta_path.insert(0,Watch())\nsys"
        ".argv=['probe','--desktop-process-probe']\nt"
        "ry:\n    "
        f"{launcher}"
        "\nfinally:\n    if attempts: raise "
        "SystemExit(45)\n"
    )
    env = {
        **os.environ,
        "LANTAI_METADATA_ONLY": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANTAI_WORKING_DIR": str(tmp_path / "profile"),
        "LANTAI_SECRET_DIR": str(tmp_path / "profile.secret"),
    }
    env.pop(OWNER_ENV, None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=root,
        input=json.dumps({"nonce": "0123456789abcdef" * 4}),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)["records"]
    assert records
    assert all(
        set(item) == {"pid", "ppid", "birth", "alive", "owned"}
        for item in records
    )
    assert not (tmp_path / "profile.secret").exists()


@pytest.mark.parametrize("entry", ["module", "packaged"])
def test_probe_entry_does_not_publish_a_denied_scan(entry, tmp_path):
    root = Path(__file__).resolve().parents[3]
    launcher = (
        "runpy.run_module('lantai',run_name='__main__')"
        if entry == "module"
        else (
            "runpy.run_path("
            f"{str(root / 'electron' / 'entry.py')!r}"
            ",run_name='__main__')"
        )
    )
    script = f"""
import os,psutil,runpy,sys
psutil.pids=lambda: [os.getpid()]
def denied(_self):
    raise psutil.AccessDenied(os.getpid())
psutil.Process.environ=denied
sys.argv=['probe','--desktop-process-probe']
{launcher}
"""
    nonce = "0123456789abcdef" * 4
    env = {
        **os.environ,
        "LANTAI_METADATA_ONLY": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANTAI_WORKING_DIR": str(tmp_path / "profile"),
        "LANTAI_SECRET_DIR": str(tmp_path / "profile.secret"),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=root,
        input=json.dumps({"nonce": nonce}),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert (
        result.stderr.strip() == "process ownership probe failed: AccessDenied"
    )
    assert nonce not in result.stderr
    assert not (tmp_path / "profile.secret").exists()
