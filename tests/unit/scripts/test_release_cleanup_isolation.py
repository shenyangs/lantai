# -*- coding: utf-8 -*-
"""Original independent exchange assertions at the actual isolation syscall.

Finite cleanup-name exchange uses synthetic files only.
"""
import json
import os
from pathlib import Path

import pytest

from tests.unit.scripts.test_release_cleanup_recovery import SimulatedPowerLoss
from tests.unit.scripts.test_release_deployment_transaction import (
    deployment_layout_fixture,
    prepared,
    transaction_module_fixture,
)

__all__ = ["deployment_layout_fixture", "transaction_module_fixture"]


# One fault scenario includes admission and settlement assertions.
# pylint: disable-next=too-many-statements
def test_cleanup_leaf_exchange_is_not_deleted_or_accepted(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
):
    root, _names, sources = layout
    stage = prepared(txmod, layout)
    txmod.promote(root, sources=sources, owner_pid=os.getpid())
    receipt = tmp_path / "accepted.json"
    old_app = stage / "old-app"
    parent_identity = (old_app.stat().st_dev, old_app.stat().st_ino)
    parked = tmp_path / "parked-original-marker"
    leaf = old_app / "marker"
    sentinel = b"new foreign file must remain"
    real_unlink = os.unlink
    real_rename = os.rename
    injected = False

    def exchange_before_actual_unlink(name, *args, **kwargs):
        nonlocal injected
        parent = kwargs.get("dir_fd")
        if not injected and str(name) == "marker" and parent is not None:
            current = os.fstat(parent)
            if (current.st_dev, current.st_ino) == parent_identity:
                injected = True
                real_rename("marker", str(parked), src_dir_fd=parent)
                fd = os.open(
                    "marker",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent,
                )
                try:
                    os.write(fd, sentinel)
                    os.fsync(fd)
                finally:
                    os.close(fd)
        return real_unlink(name, *args, **kwargs)

    if hasattr(txmod, "_rename_no_replace_at"):
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        move = txmod._rename_no_replace_at

        def exchange_before_capture(src_fd, src_name, dst_fd, dst_name):
            nonlocal injected
            current = os.fstat(src_fd)
            if (
                not injected
                and str(src_name) == "marker"
                and (current.st_dev, current.st_ino) == parent_identity
            ):
                injected = True
                real_rename("marker", str(parked), src_dir_fd=src_fd)
                fd = os.open(
                    "marker",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=src_fd,
                )
                try:
                    os.write(fd, sentinel)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return move(src_fd, src_name, dst_fd, dst_name)

        monkeypatch.setattr(
            txmod,
            "_rename_no_replace_at",
            exchange_before_capture,
        )
    else:
        monkeypatch.setattr(txmod.os, "unlink", exchange_before_actual_unlink)
    blocked = False
    try:
        txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    except (ValueError, OSError):
        blocked = True
    assert injected, "finite exchange must occur at the actual cleanup unlink"
    assert parked.read_bytes() == b"old-app"
    assert (
        leaf.exists() and leaf.read_bytes() == sentinel
    ), "new inode was deleted after the preceding identity check"
    assert blocked and (root / txmod.CLEANUP_NAME).exists()
    assert (
        not receipt.exists()
    ), "changed cleanup inventory cannot publish acceptance"
    assert (root / "unrelated.txt").read_bytes() == b"preserve this"


def _installed(tx, layout):
    root, names, sources = layout
    stage = prepared(tx, layout)
    tx.promote(root, sources=sources, owner_pid=os.getpid())
    return root, names, sources, stage


def _area(tx, root):
    record = json.loads((root / tx.CLEANUP_NAME).read_text())
    return root / tx.CLEANUP_DIR / record["quarantine"]["name"]


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_exchange_at_capture_preserves_directory_or_literal_link(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
    kind,
):
    root, _, _, stage = _installed(txmod, layout)
    leaf = stage / (
        "old-app/Contents/MacOS" if kind == "directory" else "old-app/marker"
    )
    parent_identity = (leaf.parent.stat().st_dev, leaf.parent.stat().st_ino)
    parked = tmp_path / "parked-original"
    outside = tmp_path / "external-sentinel"
    outside.write_bytes(b"never follow the foreign link")
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    move = txmod._rename_no_replace_at
    injected = False

    def exchange(src_fd, src_name, dst_fd, dst_name):
        nonlocal injected
        info = os.fstat(src_fd)
        if (
            not injected
            and src_name == leaf.name
            and (info.st_dev, info.st_ino) == parent_identity
        ):
            injected = True
            os.rename(src_name, str(parked), src_dir_fd=src_fd)
            if kind == "directory":
                os.mkdir(src_name, 0o700, dir_fd=src_fd)
                (leaf / "foreign-sentinel").write_bytes(
                    b"preserve foreign directory",
                )
            else:
                os.symlink(str(outside), src_name, dir_fd=src_fd)
        return move(src_fd, src_name, dst_fd, dst_name)

    monkeypatch.setattr(txmod, "_rename_no_replace_at", exchange)
    receipt = tmp_path / "accepted.json"
    with pytest.raises((ValueError, OSError)):
        txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert injected and parked.exists()
    assert (
        (
            (leaf / "foreign-sentinel").read_bytes()
            == b"preserve foreign directory"
        )
        if kind == "directory"
        else leaf.is_symlink() and os.readlink(leaf) == str(outside)
    )
    assert outside.read_bytes() == b"never follow the foreign link"
    assert (root / txmod.CLEANUP_NAME).exists() and not receipt.exists()


def test_recreated_source_after_capture_retains_both_objects(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
):
    root, _, _, stage = _installed(txmod, layout)
    leaf = stage / "old-app/marker"
    identity = (leaf.parent.stat().st_dev, leaf.parent.stat().st_ino)
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    move = txmod._rename_no_replace_at
    injected = False

    def recreate(src_fd, src_name, dst_fd, dst_name):
        nonlocal injected
        result = move(src_fd, src_name, dst_fd, dst_name)
        info = os.fstat(src_fd)
        if (
            not injected
            and src_name == "marker"
            and (info.st_dev, info.st_ino) == identity
        ):
            injected = True
            leaf.write_bytes(b"foreign recreated source")
        return result

    monkeypatch.setattr(txmod, "_rename_no_replace_at", recreate)
    receipt = tmp_path / "accepted.json"
    with pytest.raises(ValueError):
        txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert injected and leaf.read_bytes() == b"foreign recreated source"
    assert (_area(txmod, root) / "item").read_bytes() == b"old-app"
    assert (root / txmod.CLEANUP_NAME).exists() and not receipt.exists()


def test_kernel_capture_cannot_overwrite_an_occupied_private_slot(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
):
    root, _, _, stage = _installed(txmod, layout)
    leaf = stage / "old-app/marker"
    identity = (leaf.parent.stat().st_dev, leaf.parent.stat().st_ino)
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    move = txmod._rename_no_replace_at
    injected = False

    def occupy(src_fd, src_name, dst_fd, dst_name):
        nonlocal injected
        info = os.fstat(src_fd)
        if (
            not injected
            and src_name == "marker"
            and (info.st_dev, info.st_ino) == identity
        ):
            injected = True
            fd = os.open(
                "item",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_fd,
            )
            try:
                os.write(fd, b"foreign capture slot")
                os.fsync(fd)
            finally:
                os.close(fd)
        return move(src_fd, src_name, dst_fd, dst_name)

    monkeypatch.setattr(txmod, "_rename_no_replace_at", occupy)
    receipt = tmp_path / "accepted.json"
    with pytest.raises(FileExistsError):
        txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert injected and leaf.read_bytes() == b"old-app"
    assert (
        _area(txmod, root) / "item"
    ).read_bytes() == b"foreign capture slot"
    assert (root / txmod.CLEANUP_NAME).exists() and not receipt.exists()


@pytest.mark.parametrize(
    "boundary",
    ["marker", "state.json", ".lantai-release-transaction"],
)
@pytest.mark.parametrize("recovery", ["same-owner-finish", "dead-owner-begin"])
def test_crash_after_atomic_capture_recovers_before_any_delete(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
    boundary,
    recovery,
):
    root, names, sources, stage = _installed(txmod, layout)
    receipt = tmp_path / "accepted.json"
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    move = txmod._rename_no_replace_at
    injected = False

    def crash(src_fd, src_name, dst_fd, dst_name):
        nonlocal injected
        result = move(src_fd, src_name, dst_fd, dst_name)
        if not injected and src_name == boundary and dst_name == "item":
            injected = True
            raise SimulatedPowerLoss(
                "finite crash after actual no-overwrite capture",
            )
        return result

    with monkeypatch.context() as changed:
        changed.setattr(txmod, "_rename_no_replace_at", crash)
        with pytest.raises(SimulatedPowerLoss):
            txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    area = _area(txmod, root)
    assert injected and (area / "item").exists()
    assert (
        json.loads((area / "pending.json").read_text())["relative"] is not None
    )
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
    assert not area.exists() and not (root / txmod.CLEANUP_NAME).exists()
    assert json.loads(receipt.read_text())["source_sha"] == "a" * 40
    assert all(
        txmod.fingerprint(root / names[r]) == txmod.fingerprint(sources[r])
        for r in names
    )


@pytest.mark.parametrize(
    "boundary",
    ["pending-intent", "completed-authority", "legacy-authority"],
)
def test_crash_after_durable_intent_or_completion_resumes(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
    boundary,
):
    root, _, _, stage = _installed(txmod, layout)
    receipt = tmp_path / "accepted.json"
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    atomic = txmod._atomic_json
    injected = False

    def crash(path, payload, **kwargs):
        nonlocal injected
        result = atomic(path, payload, **kwargs)
        hit = (
            boundary == "pending-intent"
            and Path(path).name == "pending.json"
            and payload.get("relative") == "old-app/marker"
            or boundary == "completed-authority"
            and Path(path) == root / txmod.CLEANUP_NAME
            and payload.get("quarantine", {}).get("complete")
            or boundary == "legacy-authority"
            and Path(path) == root / txmod.CLEANUP_NAME
            and "quarantine" not in payload
        )
        if not injected and hit:
            injected = True
            raise SimulatedPowerLoss(
                "finite crash after durable private intent",
            )
        return result

    with monkeypatch.context() as changed:
        changed.setattr(txmod, "_atomic_json", crash)
        with pytest.raises(SimulatedPowerLoss):
            txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert (
        injected
        and (root / txmod.CLEANUP_NAME).exists()
        and not receipt.exists()
    )
    txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert not stage.exists() and not (root / txmod.CLEANUP_NAME).exists()
    assert json.loads(receipt.read_text())["state"] == "accepted"


@pytest.mark.parametrize(
    "change",
    ["same-bytes-new-inode", "directory-link", "public-permissions"],
)
def test_changed_private_capture_area_blocks_without_losing_held_object(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
    change,
):
    root, _, _, stage = _installed(txmod, layout)
    receipt = tmp_path / "accepted.json"
    # White-box lock exercises the actual private boundary.
    # pylint: disable-next=protected-access
    move = txmod._rename_no_replace_at

    def crash(src_fd, src_name, dst_fd, dst_name):
        result = move(src_fd, src_name, dst_fd, dst_name)
        if src_name == "marker" and dst_name == "item":
            raise SimulatedPowerLoss("finite capture crash")
        return result

    with monkeypatch.context() as changed:
        changed.setattr(txmod, "_rename_no_replace_at", crash)
        with pytest.raises(SimulatedPowerLoss):
            txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    area = _area(txmod, root)
    parked = tmp_path / "parked-private-area"
    if change == "public-permissions":
        area.chmod(0o755)
        held = area
    else:
        area.rename(parked)
        held = parked
        if change == "directory-link":
            area.symlink_to(parked, target_is_directory=True)
        else:
            area.mkdir(mode=0o700)
            (area / "foreign-sentinel").write_bytes(b"new area must remain")
    before = txmod.fingerprint(stage)
    authority = (root / txmod.CLEANUP_NAME).read_bytes()
    with pytest.raises((ValueError, OSError)):
        txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert txmod.fingerprint(stage) == before
    assert (root / txmod.CLEANUP_NAME).read_bytes() == authority
    assert (held / "item").read_bytes() == b"old-app"
    assert not receipt.exists()
    if change == "same-bytes-new-inode":
        assert (
            area / "foreign-sentinel"
        ).read_bytes() == b"new area must remain"
