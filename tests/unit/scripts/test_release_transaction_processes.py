# -*- coding: utf-8 -*-
"""Finite real App processes at the actual transaction mutation boundaries."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import psutil
import pytest

from tests.unit.scripts.test_release_process_stop import process_scope_fixture

__all__ = ["process_scope_fixture"]


@pytest.fixture(name="txscope")
def transaction_scope_fixture(scope, tmp_path, monkeypatch):
    stop, processes = scope
    root, sources = tmp_path / "downloads", tmp_path / "sources"
    root.mkdir()
    sources.mkdir()
    names = {
        "app": "Synthetic.app",
        "dmg": "Synthetic.dmg",
        "zip": "Synthetic.zip",
    }
    for parent, prefix in ((root, b"old-"), (sources, b"new-")):
        app, _, _ = processes.app(
            str(parent.relative_to(tmp_path) / names["app"]),
        )
        (app / "marker").write_bytes(prefix + b"app")
        for role in ("dmg", "zip"):
            (parent / names[role]).write_bytes(prefix + role.encode())
    script = (
        Path(__file__).resolve().parents[3]
        / "scripts/release_deployment_transaction.py"
    )
    monkeypatch.syspath_prepend(str(script.parent))
    spec = importlib.util.spec_from_file_location(
        "finite_transaction_processes",
        script,
    )
    tx = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tx
    spec.loader.exec_module(tx)
    paths = {role: sources / name for role, name in names.items()}
    stage = tx.begin(
        root,
        destinations=names,
        source_sha="a" * 40,
        version="1.2.3",
        owner_pid=os.getpid(),
    )
    for role, source in paths.items():
        if source.is_dir():
            shutil.copytree(source, stage / role)
        else:
            shutil.copy2(source, stage / role)

    class Scope:
        def backend(self, app):
            return (
                app
                / "Contents/Resources/lantai-server/Lantai Backend.app"
                / "Contents/MacOS/lantai-server"
            )

        def promote(self):
            tx.promote(root, sources=paths, owner_pid=os.getpid())

        def snapshot(self):
            return (
                (stage / "state.json").read_bytes(),
                {
                    role: tx.fingerprint(root / name)
                    if (root / name).exists()
                    else None
                    for role, name in names.items()
                },
                {
                    role: tx.fingerprint(stage / ("old-" + role))
                    if (stage / ("old-" + role)).exists()
                    else None
                    for role in names
                },
            )

    result = Scope()
    result.tx, result.own, result.stop = tx, processes, stop
    result.root, result.names, result.paths, result.stage = (
        root,
        names,
        paths,
        stage,
    )
    return result


def test_promote_settles_owned_detached_helper_before_first_move(
    txscope,
    monkeypatch,
):
    s = txscope
    parent, helper = s.own.spawn(
        s.backend(s.root / s.names["app"]),
        "helper",
        nonce=s.own.nonce,
    )
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    move = s.tx._move

    def assert_stopped(*args):
        assert not s.own.live(parent.pid) and not s.own.live(helper)
        return move(*args)

    monkeypatch.setattr(s.tx, "_move", assert_stopped)
    s.promote()
    assert s.tx.previous_running(s.root, owner_pid=os.getpid())


@pytest.mark.parametrize("entry", ["promote", "rollback", "dead-owner-begin"])
def test_denied_stop_retains_journal_and_all_artifacts(
    txscope,
    monkeypatch,
    entry,
):
    s = txscope
    if entry != "promote":
        s.promote()
    child, _ = s.own.spawn(
        s.backend(s.root / s.names["app"]),
        nonce=s.own.nonce,
    )
    before = s.snapshot()

    def denied(process, *args, **kwargs):
        del args, kwargs
        assert process.pid == child.pid
        raise psutil.AccessDenied(child.pid)

    monkeypatch.setattr(psutil.Process, "send_signal", denied)
    monkeypatch.setattr(psutil.Process, "kill", denied)
    monkeypatch.setattr(
        s.tx,
        "_owner_alive",
        lambda _: False,
    )  # finite release-owner boundary
    with pytest.raises((ValueError, RuntimeError, psutil.Error)):
        if entry == "promote":
            s.promote()
        elif entry == "rollback":
            s.tx.rollback(s.root, owner_pid=os.getpid())
        else:
            s.tx.begin(
                s.root,
                destinations=s.names,
                source_sha="b" * 40,
                version="1.2.4",
                owner_pid=os.getpid(),
            )
    assert s.own.live(child.pid)
    assert s.snapshot() == before


def test_registered_launch_helper_survives_anchor_exit_but_not_rollback(
    txscope,
):
    s = txscope
    s.promote()
    s.tx.launch_intent(s.root, owner_pid=os.getpid())
    parent, helper = s.own.spawn(
        s.backend(s.root / s.names["app"]),
        "helper",
        nonce=s.own.nonce,
    )
    s.tx.register_launch(s.root, owner_pid=os.getpid(), timeout=1)
    parent.kill()
    parent.wait(timeout=3)
    assert s.own.live(helper)
    s.tx.rollback(s.root, owner_pid=os.getpid())
    assert not s.own.live(helper)
    assert (s.root / s.names["app"] / "marker").read_bytes() == b"old-app"


@pytest.mark.parametrize("entry", ["rollback", "finish", "dead-owner-begin"])
def test_unregistered_launch_cannot_be_treated_as_an_empty_instance(
    txscope,
    monkeypatch,
    entry,
):
    s = txscope
    s.promote()
    s.tx.launch_intent(s.root, owner_pid=os.getpid())
    before = s.snapshot()
    monkeypatch.setattr(s.tx, "_owner_alive", lambda _: False)
    with pytest.raises((ValueError, RuntimeError)):
        if entry == "rollback":
            s.tx.rollback(s.root, owner_pid=os.getpid())
        elif entry == "finish":
            s.tx.finish(
                s.root,
                owner_pid=os.getpid(),
                receipt=s.root / "receipt.json",
            )
        else:
            s.tx.begin(
                s.root,
                destinations=s.names,
                source_sha="b" * 40,
                version="1.2.4",
                owner_pid=os.getpid(),
            )
    assert s.snapshot() == before
    assert not (s.root / "receipt.json").exists()


def test_acceptance_stops_only_backup_instance_and_keeps_new_app(txscope):
    s = txscope
    s.promote()
    old, _ = s.own.spawn(s.backend(s.stage / "old-app"), nonce=s.own.nonce)
    s.tx.launch_intent(s.root, owner_pid=os.getpid())
    current, _ = s.own.spawn(
        s.backend(s.root / s.names["app"]),
        nonce=uuid.uuid4().hex,
    )
    s.tx.register_launch(s.root, owner_pid=os.getpid(), timeout=1)
    s.tx.finish(s.root, owner_pid=os.getpid(), receipt=s.root / "receipt.json")
    assert not s.own.live(old.pid)
    assert s.own.live(current.pid)
    receipt = json.loads((s.root / "receipt.json").read_text())
    assert receipt["state"] == "accepted"
    assert "nonce" not in json.dumps(receipt).lower()


def test_artifact_exchange_during_process_wait_is_rechecked_before_moves(
    txscope,
    monkeypatch,
):
    s = txscope
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    settle = s.tx._settle_app_processes

    def exchange(*args, **kwargs):
        result = settle(*args, **kwargs)
        (s.root / s.names["dmg"]).write_bytes(b"foreign during stop")
        return result

    monkeypatch.setattr(s.tx, "_settle_app_processes", exchange)
    with pytest.raises((ValueError, RuntimeError)):
        s.promote()
    assert (s.root / s.names["app"] / "marker").read_bytes() == b"old-app"
    assert (s.root / s.names["zip"]).read_bytes() == b"old-zip"
    assert (s.root / s.names["dmg"]).read_bytes() == b"foreign during stop"
    assert s.stage.exists()
