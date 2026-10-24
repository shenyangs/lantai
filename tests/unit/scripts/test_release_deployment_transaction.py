# -*- coding: utf-8 -*-
"""Finite real filesystem tests; no Downloads, signatures, OS App or upload."""
import importlib.util
import json
import os
import plistlib
import shutil
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(name="txmod")
def transaction_module_fixture(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "release_deployment",
        ROOT / "scripts/release_deployment_transaction.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # These existing locks exercise real filesystem races only. Actual process
    # inventory and signals are covered by test_release_transaction_processes.
    monkeypatch.setattr(module.process_stop.probe.psutil, "pids", lambda: [])
    return module


@pytest.fixture(name="layout")
def deployment_layout_fixture(tmp_path):
    root = tmp_path / "synthetic-downloads"
    root.mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()
    destinations = {
        "app": "Lantai.app",
        "dmg": "Lantai.dmg",
        "zip": "Lantai.zip",
    }
    paths = {}
    for role, name in destinations.items():
        target, source = root / name, sources / name
        if role == "app":
            target.mkdir()
            source.mkdir()
            (target / "marker").write_bytes(b"old-app")
            (source / "marker").write_bytes(b"new-app")
            (source / "framework-alias").symlink_to("marker")
            for app in (target, source):
                contents = app / "Contents"
                (contents / "MacOS").mkdir(parents=True)
                (contents / "MacOS/Synthetic").write_bytes(
                    b"finite non-executed App",
                )
                (contents / "Info.plist").write_bytes(
                    plistlib.dumps(
                        {
                            "CFBundleExecutable": "Synthetic",
                            "CFBundleIdentifier": "com.lantai.app",
                        },
                    ),
                )
        else:
            target.write_bytes(("old-" + role).encode())
            source.write_bytes(("new-" + role).encode())
        paths[role] = source
    (root / "unrelated.txt").write_bytes(b"preserve this")
    return root, destinations, paths


def prepared(module, layout, owner=None):
    root, destinations, paths = layout
    owner = owner or os.getpid()
    stage = module.begin(
        root,
        destinations=destinations,
        source_sha="a" * 40,
        version="1.2.3",
        owner_pid=owner,
    )
    for role, source in paths.items():
        if source.is_dir():
            shutil.copytree(source, stage / role, symlinks=True)
        else:
            shutil.copy2(source, stage / role)
    return stage


def assert_old(layout):
    root, destinations, _ = layout
    assert (root / destinations["app"] / "marker").read_bytes() == b"old-app"
    for role in ("dmg", "zip"):
        assert (root / destinations[role]).read_bytes() == (
            "old-" + role
        ).encode()
    assert (root / "unrelated.txt").read_bytes() == b"preserve this"


def test_complete_set_keeps_backups_until_verified_acceptance(
    txmod,
    layout,
    tmp_path,
):
    root, destinations, paths = layout
    stage = prepared(txmod, layout)
    txmod.promote(root, sources=paths, owner_pid=os.getpid())
    for role in destinations:
        assert (stage / ("old-" + role)).exists()
        assert txmod.fingerprint(
            root / destinations[role],
        ) == txmod.fingerprint(paths[role])
    receipt = tmp_path / "release-evidence/deployment.json"
    txmod.finish(root, owner_pid=os.getpid(), receipt=receipt)
    assert not stage.exists()
    report = json.loads(receipt.read_text())
    assert report["state"] == "accepted"
    assert report["source_sha"] == "a" * 40
    assert report["version"] == "1.2.3"
    assert (root / "unrelated.txt").read_bytes() == b"preserve this"


def test_data_flush_failure_cannot_replace_any_previous_artifact(
    txmod,
    layout,
    monkeypatch,
):
    root, _, paths = layout
    stage = prepared(txmod, layout)
    real_sync = txmod.os.fsync

    def disk_failure(fd):
        metadata = os.fstat(fd)
        if stat.S_ISREG(metadata.st_mode) and metadata.st_size == len(
            b"new-app",
        ):
            raise OSError("synthetic replacement data flush failure")
        return real_sync(fd)

    monkeypatch.setattr(txmod.os, "fsync", disk_failure)
    with pytest.raises(OSError, match="data flush"):
        txmod.promote(root, sources=paths, owner_pid=os.getpid())
    assert_old(layout)
    assert (
        json.loads((stage / "state.json").read_text())["state"] == "prepared"
    )


@pytest.mark.parametrize("bad_role", ["app", "dmg", "zip"])
def test_partial_stage_cannot_replace_any_old_asset(txmod, layout, bad_role):
    root, _, paths = layout
    stage = prepared(txmod, layout)
    path = stage / bad_role
    if path.is_dir():
        (path / "marker").write_bytes(b"partial")
    else:
        path.write_bytes(b"partial")
    with pytest.raises(ValueError):
        txmod.promote(root, sources=paths, owner_pid=os.getpid())
    assert_old(layout)
    txmod.rollback(root, owner_pid=os.getpid())
    assert_old(layout)


@pytest.mark.parametrize("boundary", range(1, 7))
def test_process_crash_at_each_rename_recovers_entire_previous_set(
    txmod,
    layout,
    monkeypatch,
    boundary,
):
    root, destinations, paths = layout
    monkeypatch.setattr(
        txmod,
        "_owner_record",
        lambda pid: {"pid": pid, "birth": float(pid)},
    )
    monkeypatch.setattr(txmod, "_owner_alive", lambda record: True)
    stage = prepared(txmod, layout, owner=800001)
    # Inject power loss at the six actual artifact moves; journal replacement
    # is a separate, internal operation.
    move_owner, move_name = _move_seam(txmod)
    real_replace = getattr(move_owner, move_name)
    moves = 0

    class SimulatedPowerLoss(BaseException):
        pass

    def replace_then_crash(source, target, *args, **kwargs):
        nonlocal moves
        result = real_replace(source, target, *args, **kwargs)
        if (
            Path(source).name != "state.json.tmp"
            and Path(target).name != "state.json"
        ):
            moves += 1
            if moves == boundary:
                raise SimulatedPowerLoss()
        return result

    monkeypatch.setattr(move_owner, move_name, replace_then_crash)
    with pytest.raises(SimulatedPowerLoss):
        txmod.promote(root, sources=paths, owner_pid=800001)
    monkeypatch.setattr(move_owner, move_name, real_replace)
    assert stage.exists(), "crash must leave its durable owner and plan"
    monkeypatch.setattr(txmod, "_owner_alive", lambda record: False)
    txmod.begin(
        root,
        destinations=destinations,
        source_sha="b" * 40,
        version="1.2.4",
        owner_pid=800002,
    )
    assert_old(layout)


def test_failed_post_copy_acceptance_rolls_back_all_three(txmod, layout):
    root, _, paths = layout
    prepared(txmod, layout)
    txmod.promote(root, sources=paths, owner_pid=os.getpid())
    txmod.rollback(root, owner_pid=os.getpid())
    assert_old(layout)


def test_live_owner_cannot_be_replaced_or_rolled_back(txmod, layout):
    root, destinations, paths = layout
    stage = prepared(txmod, layout)
    with pytest.raises(ValueError):
        txmod.begin(
            root,
            destinations=destinations,
            source_sha="b" * 40,
            version="1.2.4",
            owner_pid=os.getpid(),
        )
    with pytest.raises(ValueError):
        txmod.promote(root, sources=paths, owner_pid=os.getpid() + 999999)
    with pytest.raises(ValueError):
        txmod.rollback(root, owner_pid=os.getpid() + 999999)
    assert stage.exists()
    assert_old(layout)


def test_unknown_owner_probe_preserves_everything(txmod, layout, monkeypatch):
    root, destinations, _ = layout
    stage = prepared(txmod, layout)

    def denied(_):
        raise PermissionError("synthetic process identity unavailable")

    monkeypatch.setattr(txmod, "_owner_alive", denied)
    with pytest.raises(PermissionError):
        txmod.begin(
            root,
            destinations=destinations,
            source_sha="b" * 40,
            version="1.2.4",
            owner_pid=os.getpid(),
        )
    assert stage.exists()
    assert_old(layout)


def test_tampered_destination_keeps_backup_and_refuses_acceptance(
    txmod,
    layout,
    tmp_path,
):
    root, destinations, paths = layout
    stage = prepared(txmod, layout)
    txmod.promote(root, sources=paths, owner_pid=os.getpid())
    (root / destinations["dmg"]).write_bytes(b"foreign modification")
    with pytest.raises(ValueError):
        txmod.finish(
            root,
            owner_pid=os.getpid(),
            receipt=tmp_path / "receipt.json",
        )
    with pytest.raises(ValueError):
        txmod.rollback(root, owner_pid=os.getpid())
    assert (root / destinations["dmg"]).read_bytes() == b"foreign modification"
    assert (stage / "old-dmg").read_bytes() == b"old-dmg"


def test_no_previous_assets_are_removed_on_rollback(txmod, layout):
    root, destinations, paths = layout
    for name in destinations.values():
        path = root / name
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    prepared(txmod, layout)
    txmod.promote(root, sources=paths, owner_pid=os.getpid())
    txmod.rollback(root, owner_pid=os.getpid())
    assert all(not (root / name).exists() for name in destinations.values())
    assert (root / "unrelated.txt").read_bytes() == b"preserve this"


@pytest.mark.parametrize(
    "bad",
    ["../outside.app", "/outside.app", "same", ".lantai-release-transaction"],
)
def test_destination_paths_are_confined_before_mutation(txmod, layout, bad):
    root, destinations, _ = layout
    destinations["app"] = bad
    if bad == "same":
        destinations["dmg"] = bad
    with pytest.raises(ValueError):
        txmod.begin(
            root,
            destinations=destinations,
            source_sha="a" * 40,
            version="1.2.3",
            owner_pid=os.getpid(),
        )
    assert not (root / txmod.TX_NAME).exists()


def test_foreign_journal_and_symlink_are_preserved(txmod, layout, tmp_path):
    root, destinations, _ = layout
    stage = root / txmod.TX_NAME
    stage.mkdir()
    (stage / "state.json").write_text("{broken")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        txmod.begin(
            root,
            destinations=destinations,
            source_sha="a" * 40,
            version="1.2.3",
            owner_pid=os.getpid(),
        )
    assert (stage / "state.json").read_text() == "{broken"
    shutil.rmtree(stage)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "preserve").write_bytes(b"keep")
    stage.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        txmod.begin(
            root,
            destinations=destinations,
            source_sha="a" * 40,
            version="1.2.3",
            owner_pid=os.getpid(),
        )
    assert (outside / "preserve").read_bytes() == b"keep"


def _move_seam(module):
    # Injection follows the actual native move entry point. The old check +
    # replace implementation is retained as the RED baseline.
    return (
        (module, "_rename_no_replace")
        if hasattr(module, "_rename_no_replace")
        else (module.os, "replace")
    )


def test_atomic_move_preserves_a_foreign_destination_created_after_validation(
    txmod,
    layout,
    monkeypatch,
):
    root, names, sources = layout
    stage = prepared(txmod, layout)
    owner, name = _move_seam(txmod)
    move = getattr(owner, name)
    injected = False

    def foreign_destination(source, target, *args, **kwargs):
        nonlocal injected
        if (
            Path(source) == stage / "dmg"
            and Path(target) == root / names["dmg"]
        ):
            assert not Path(target).exists()
            Path(target).write_bytes(b"foreign newly-created destination")
            injected = True
        return move(source, target, *args, **kwargs)

    monkeypatch.setattr(owner, name, foreign_destination)
    with pytest.raises((OSError, ValueError)):
        txmod.promote(root, sources=sources, owner_pid=os.getpid())
    assert injected
    assert (
        root / names["dmg"]
    ).read_bytes() == b"foreign newly-created destination"
    assert (stage / "old-dmg").read_bytes() == b"old-dmg"


def test_directory_exchange_cannot_redirect_deployment_into_an_external_tree(
    txmod,
    layout,
    tmp_path,
    monkeypatch,
):
    root, names, sources = layout
    stage = prepared(txmod, layout)
    outside, parked = tmp_path / "external-tree", tmp_path / "original-tree"
    shutil.copytree(root, outside, symlinks=True)
    before = txmod.fingerprint(outside)
    owner, name = _move_seam(txmod)
    move = getattr(owner, name)
    injected = False

    def exchange(source, target, *args, **kwargs):
        nonlocal injected
        if (
            not injected
            and Path(source) == root / names["app"]
            and Path(target) == stage / "old-app"
        ):
            root.rename(parked)
            root.symlink_to(outside, target_is_directory=True)
            injected = True
        return move(source, target, *args, **kwargs)

    monkeypatch.setattr(owner, name, exchange)
    with pytest.raises((OSError, ValueError)):
        txmod.promote(root, sources=sources, owner_pid=os.getpid())
    assert injected
    assert txmod.fingerprint(outside) == before
    assert (parked / names["app"] / "marker").read_bytes() == b"old-app"
