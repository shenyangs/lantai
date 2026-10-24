# -*- coding: utf-8 -*-
"""Durable acceptance and rollback locks at real filesystem crash boundaries.
"""
import json
import os
from pathlib import Path

import pytest

from tests.unit.scripts.test_release_deployment_transaction import (
    deployment_layout_fixture,
    prepared,
    transaction_module_fixture,
)
from tests.unit.scripts.test_release_transaction_processes import (
    process_scope_fixture,
    transaction_scope_fixture,
)

__all__ = [
    "deployment_layout_fixture",
    "process_scope_fixture",
    "transaction_module_fixture",
    "transaction_scope_fixture",
]


class SimulatedPowerLoss(BaseException):
    pass


def _deletion_boundary(tx, name, parent):
    """Map the actual isolated deletion back to its original inventory name."""
    if str(name) != "item" or parent is None:
        return str(name)
    fd = os.open("pending.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(fd, "r") as handle:
        relative = json.load(handle)["relative"]
    return tx.TX_NAME if relative == "." else Path(relative).name


def _installed(tx, layout):
    root, names, paths = layout
    stage = prepared(tx, layout)
    tx.promote(root, sources=paths, owner_pid=os.getpid())
    return root, names, paths, stage


def _crash_after_delete(tx, monkeypatch, boundary, operation="unlink"):
    real = getattr(tx.os, operation)

    def crash(name, *args, **kwargs):
        original = _deletion_boundary(tx, name, kwargs.get("dir_fd"))
        result = real(name, *args, **kwargs)
        if original == boundary:
            raise SimulatedPowerLoss(
                "finite crash after actual cleanup mutation",
            )
        return result

    monkeypatch.setattr(tx.os, operation, crash)


@pytest.mark.parametrize(
    "boundary,operation",
    [
        ("marker", "unlink"),
        ("Synthetic", "unlink"),
        ("MacOS", "rmdir"),
        ("state.json", "unlink"),
        (".lantai-release-transaction", "rmdir"),
    ],
)
@pytest.mark.parametrize("recovery", ["same-owner-finish", "dead-owner-begin"])
def test_partial_accepted_cleanup_recovers_each_actual_delete(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
    boundary,
    operation,
    recovery,
):
    root, names, paths, stage = _installed(txmod, layout)
    receipt = tmp_path / "accepted.json"
    with monkeypatch.context() as changed:
        _crash_after_delete(txmod, changed, boundary, operation)
        with pytest.raises(SimulatedPowerLoss):
            txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    authority = root / txmod.CLEANUP_NAME
    assert authority.is_file() and authority.stat().st_mode & 0o777 == 0o600
    assert not receipt.exists()
    if recovery == "same-owner-finish":
        txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
        assert not stage.exists()
    else:
        monkeypatch.setattr(txmod, "_owner_alive", lambda _: False)
        next_stage = txmod.begin(
            root,
            destinations=names,
            source_sha="b" * 40,
            version="1.2.4",
            owner_pid=os.getpid(),
        )
        assert (
            json.loads((next_stage / "state.json").read_text())["source_sha"]
            == "b" * 40
        )
    assert not authority.exists()
    assert json.loads(receipt.read_text())["source_sha"] == "a" * 40
    assert all(
        txmod.fingerprint(root / names[r]) == txmod.fingerprint(paths[r])
        for r in names
    )
    assert (root / "unrelated.txt").read_bytes() == b"preserve this"


@pytest.mark.parametrize(
    "boundary,operation",
    [
        ("marker", "unlink"),
        ("state.json", "unlink"),
        (".lantai-release-transaction", "rmdir"),
    ],
)
def test_partial_rollback_cleanup_recovers_without_acceptance(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
    boundary,
    operation,
):
    root, names, _, stage = _installed(txmod, layout)
    with monkeypatch.context() as changed:
        _crash_after_delete(txmod, changed, boundary, operation)
        with pytest.raises(SimulatedPowerLoss):
            txmod.rollback(root, owner_pid=os.getpid())
    assert (root / names["app"] / "marker").read_bytes() == b"old-app"
    assert (
        json.loads((root / txmod.CLEANUP_NAME).read_text())["state"]["state"]
        == "rolled-back"
    )
    txmod.rollback(root, owner_pid=os.getpid())
    assert not stage.exists() and not (root / txmod.CLEANUP_NAME).exists()
    assert (root / names["dmg"]).read_bytes() == b"old-dmg"
    assert (root / names["zip"]).read_bytes() == b"old-zip"
    assert not (tmp_path / "accepted.json").exists()


@pytest.mark.parametrize(
    "change",
    ["new-file", "same-bytes-new-inode", "directory-link"],
)
def test_partial_cleanup_refuses_unknown_remaining_objects_before_more_deletes(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
    change,
):
    root, _, _, stage = _installed(txmod, layout)
    receipt = tmp_path / "accepted.json"
    with monkeypatch.context() as changed:
        _crash_after_delete(txmod, changed, "marker")
        with pytest.raises(SimulatedPowerLoss):
            txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "preserve").write_bytes(b"external bytes")
    if change == "new-file":
        (stage / "unknown.txt").write_bytes(b"do not delete")
    elif change == "same-bytes-new-inode":
        old = stage / "old-dmg"
        # Keep the original inode allocated so the fixture cannot reuse it.
        old.rename(tmp_path / "parked-original-dmg")
        old.write_bytes(b"old-dmg")
    else:
        (stage / "old-app").rename(tmp_path / "parked-old-app")
        (stage / "old-app").symlink_to(outside, target_is_directory=True)
    before = txmod.fingerprint(stage)
    authority = (root / txmod.CLEANUP_NAME).read_bytes()
    with pytest.raises((ValueError, OSError)):
        txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert txmod.fingerprint(stage) == before
    assert (root / txmod.CLEANUP_NAME).read_bytes() == authority
    assert (outside / "preserve").read_bytes() == b"external bytes"
    assert not receipt.exists()


@pytest.mark.parametrize(
    "when",
    ["before-finish", "during-process-wait", "after-partial-cleanup"],
)
def test_foreign_public_receipt_blocks_before_any_further_backup_deletion(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
    when,
):
    root, _, _, stage = _installed(txmod, layout)
    receipt = tmp_path / "accepted.json"
    if when == "after-partial-cleanup":
        with monkeypatch.context() as changed:
            _crash_after_delete(txmod, changed, "marker")
            with pytest.raises(SimulatedPowerLoss):
                txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    foreign = b'{"external":"preserve receipt"}\n'
    if when == "during-process-wait":
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        settle = txmod._settle_app_processes

        def inject(*args, **kwargs):
            result = settle(*args, **kwargs)
            receipt.write_bytes(foreign)
            return result

        monkeypatch.setattr(txmod, "_settle_app_processes", inject)
    else:
        receipt.write_bytes(foreign)
    before = txmod.fingerprint(stage)
    with pytest.raises(ValueError):
        txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert (
        stage.exists()
    ), "conflicting receipt must retain remaining backup authority"
    assert txmod.fingerprint(stage) == before
    assert receipt.read_bytes() == foreign


# Keep the existing test selector unchanged.
def test_public_receipt_published_before_private_retirement_is_idempotently_recovered(  # noqa: E501
    txmod,
    layout,
    tmp_path,
    monkeypatch,
):
    root, names, _, stage = _installed(txmod, layout)
    receipt = tmp_path / "accepted.json"
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    atomic = txmod._atomic_json

    def crash_after_receipt(path, payload, **kwargs):
        result = atomic(path, payload, **kwargs)
        if Path(path) == receipt:
            raise SimulatedPowerLoss(
                "finite crash after durable public receipt",
            )
        return result

    with monkeypatch.context() as changed:
        changed.setattr(txmod, "_atomic_json", crash_after_receipt)
        with pytest.raises(SimulatedPowerLoss):
            txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert not stage.exists() and (root / txmod.CLEANUP_NAME).exists()
    before, inode = receipt.read_bytes(), receipt.stat().st_ino
    txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert receipt.read_bytes() == before and receipt.stat().st_ino == inode
    assert not (root / txmod.CLEANUP_NAME).exists()
    assert (root / names["app"] / "marker").read_bytes() == b"new-app"


def test_cleanup_recovery_keeps_registered_new_app_alive(
    txscope,
    tmp_path,
    monkeypatch,
):
    import uuid

    s = txscope
    s.promote()
    old, _ = s.own.spawn(s.backend(s.stage / "old-app"), nonce=s.own.nonce)
    s.tx.launch_intent(s.root, owner_pid=os.getpid())
    current, _ = s.own.spawn(
        s.backend(s.root / s.names["app"]),
        nonce=uuid.uuid4().hex,
    )
    s.tx.register_launch(s.root, owner_pid=os.getpid(), timeout=1)
    receipt = tmp_path / "accepted.json"
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    atomic = s.tx._atomic_json

    def crash_before_receipt(path, payload, **kwargs):
        if Path(path) == receipt:
            raise SimulatedPowerLoss("finite crash after backup cleanup")
        return atomic(path, payload, **kwargs)

    with monkeypatch.context() as changed:
        changed.setattr(s.tx, "_atomic_json", crash_before_receipt)
        with pytest.raises(SimulatedPowerLoss):
            s.tx.finish(s.root, owner_pid=os.getpid(), receipt=receipt)
    assert not s.own.live(old.pid) and s.own.live(current.pid)
    s.tx.finish(s.root, owner_pid=os.getpid(), receipt=receipt)
    assert s.own.live(current.pid)
    assert json.loads(receipt.read_text())["state"] == "accepted"


@pytest.mark.parametrize("unknown", ["root", "helper", "backend-nonce"])
def test_retained_orphan_helper_authority_does_not_adopt_a_new_instance(
    txscope,
    monkeypatch,
    unknown,
):
    import shutil
    import uuid

    s = txscope
    monkeypatch.setattr(
        s.tx.process_stop.probe.psutil,
        "_LOWEST_PID",
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        min(s.tx.process_stop.probe.psutil._psplatform.pids()),
    )
    backend = s.backend(s.root / s.names["app"])
    helper_exe = (
        s.root
        / s.names["app"]
        / "Contents/Frameworks/Finite Helper.app/Contents/MacOS/Finite Helper"
    )
    s.tx.rollback(s.root, owner_pid=os.getpid())
    helper_exe.parent.mkdir(parents=True)
    helper_exe.write_bytes(backend.read_bytes())
    helper_exe.chmod(0o700)
    stage = s.tx.begin(
        s.root,
        destinations=s.names,
        source_sha="a" * 40,
        version="1.2.3",
        owner_pid=os.getpid(),
    )
    for role, source in s.paths.items():
        if source.is_dir():
            shutil.copytree(source, stage / role)
        else:
            shutil.copy2(source, stage / role)
    parent, helper = s.own.spawn(
        backend,
        "desktop-helper",
        helper_exe,
        nonce=s.own.nonce,
    )
    owner = s.tx.process_stop.bootstrap(s.root / s.names["app"])
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    with s.tx._exclusive(s.root):
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        s.tx._save_process_owners(stage, [("previous", owner)])
    parent.kill()
    parent.wait(timeout=3)
    assert s.own.live(helper)
    exe = {
        "root": s.root / s.names["app"] / "Contents/MacOS/Synthetic",
        "helper": helper_exe,
        "backend-nonce": backend,
    }[unknown]
    fresh, _ = s.own.spawn(
        exe,
        nonce=uuid.uuid4().hex if unknown == "backend-nonce" else None,
    )
    before = s.snapshot()
    with pytest.raises(s.tx.process_stop.ProcessStopError):
        s.promote()
    assert s.snapshot() == before
    assert s.own.live(helper) and s.own.live(fresh.pid)


def test_private_cleanup_authority_mode_is_checked_on_the_opened_file(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
):
    root, _, _, stage = _installed(txmod, layout)
    receipt = tmp_path / "accepted.json"
    with monkeypatch.context() as changed:
        _crash_after_delete(txmod, changed, "marker")
        with pytest.raises(SimulatedPowerLoss):
            txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    authority = root / txmod.CLEANUP_NAME
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    read = txmod._read_json

    def expose_between_stat_and_open(path, **kwargs):
        if Path(path) == authority:
            authority.chmod(0o644)
        return read(path, **kwargs)

    monkeypatch.setattr(txmod, "_read_json", expose_between_stat_and_open)
    before = txmod.fingerprint(stage)
    with pytest.raises(ValueError):
        txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert txmod.fingerprint(stage) == before
    assert authority.exists() and not receipt.exists()


def test_saved_nonceless_helper_settles_after_anchor_exit(
    txscope,
    monkeypatch,
):
    tx, own, root, names, paths, stage = (
        txscope.tx,
        txscope.own,
        txscope.root,
        txscope.names,
        txscope.paths,
        txscope.stage,
    )
    # Restricted fixture pids must retain psutil's actual ancestry terminator.
    monkeypatch.setattr(
        tx.process_stop.probe.psutil,
        "_LOWEST_PID",
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        min(tx.process_stop.probe.psutil._psplatform.pids()),
    )
    backend = (
        root
        / names["app"]
        / "Contents/Resources/lantai-server/Lantai Backend.app"
        / "Contents/MacOS/lantai-server"
    )
    helper_exe = (
        root
        / names["app"]
        / "Contents/Frameworks/Finite Helper.app/Contents/MacOS/Finite Helper"
    )
    tx.rollback(root, owner_pid=os.getpid())
    helper_exe.parent.mkdir(parents=True)
    helper_exe.write_bytes(backend.read_bytes())
    helper_exe.chmod(0o700)
    # The deliberately added executable is part of old authoritative bytes.
    # Recreate the prepared record before any process or artifact promotion.
    stage = tx.begin(
        root,
        destinations=names,
        source_sha="a" * 40,
        version="1.2.3",
        owner_pid=os.getpid(),
    )
    import shutil

    for role, source in paths.items():
        if source.is_dir():
            shutil.copytree(source, stage / role, symlinks=True)
        else:
            shutil.copy2(source, stage / role)
    parent, helper = own.spawn(
        backend,
        "desktop-helper",
        helper_exe,
        nonce=own.nonce,
    )
    owner = tx.process_stop.bootstrap(root / names["app"])
    assert any(r["pid"] == helper for r in owner.roots)
    assert (
        tx.process_stop.probe.psutil.Process(helper)
        .environ()
        .get(tx.process_stop.probe.OWNER_ENV)
        is None
    )
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    with tx._exclusive(root):
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        tx._save_process_owners(stage, [("previous", owner)])
    parent.kill()
    parent.wait(timeout=3)
    assert not own.live(parent.pid) and own.live(helper)
    tx.promote(root, sources=paths, owner_pid=os.getpid())
    assert not own.live(
        helper,
    ), "a recorded helper must settle after its actual anchor exits"
    assert (root / names["app"] / "marker").read_bytes() == b"new-app"


def test_acceptance_recovers_crash_after_cleanup_before_public_receipt(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
):
    root, names, paths = layout
    prepared(txmod, layout)
    txmod.promote(root, sources=paths, owner_pid=os.getpid())
    receipt = tmp_path / "public-receipt.json"
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    real = txmod._atomic_json

    def crash_before_public(path, payload, **kwargs):
        if Path(path) == receipt:
            raise SimulatedPowerLoss("finite crash before public receipt")
        return real(path, payload, **kwargs)

    with monkeypatch.context() as changed:
        changed.setattr(txmod, "_atomic_json", crash_before_public)
        with pytest.raises(SimulatedPowerLoss):
            txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert not receipt.exists()
    monkeypatch.setattr(txmod, "_owner_alive", lambda record: False)
    txmod.begin(
        root,
        destinations=names,
        source_sha="b" * 40,
        version="1.2.4",
        owner_pid=os.getpid(),
    )
    assert receipt.exists(), (
        "accepted cleanup must retain enough durable authority to finish its"
        " public receipt after crash"
    )
    assert json.loads(receipt.read_text())["state"] == "accepted"
    assert (root / names["app"] / "marker").read_bytes() == b"new-app"


def test_cleanup_crash_cannot_delete_the_only_recovery_journal(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
):
    root, names, paths = layout
    prepared(txmod, layout)
    txmod.promote(root, sources=paths, owner_pid=os.getpid())
    receipt = tmp_path / "public-receipt.json"
    real = txmod.os.unlink

    def crash_after_journal(name, *args, **kwargs):
        original = _deletion_boundary(txmod, name, kwargs.get("dir_fd"))
        result = real(name, *args, **kwargs)
        if original == "state.json":
            raise SimulatedPowerLoss("finite crash after state removal")
        return result

    with monkeypatch.context() as changed:
        changed.setattr(txmod.os, "unlink", crash_after_journal)
        with pytest.raises(SimulatedPowerLoss):
            txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    monkeypatch.setattr(txmod, "_owner_alive", lambda record: False)
    txmod.begin(
        root,
        destinations=names,
        source_sha="b" * 40,
        version="1.2.4",
        owner_pid=os.getpid(),
    )
    assert (
        receipt.exists()
    ), "partial cleanup must preserve a recoverable acceptance record"
    assert json.loads(receipt.read_text())["state"] == "accepted"
    assert (root / names["app"] / "marker").read_bytes() == b"new-app"
