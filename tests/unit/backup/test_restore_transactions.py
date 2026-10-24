"""Disposable restore fault fixtures; never read or mutate user backup roots."""
from __future__ import annotations

import asyncio
import importlib
import io
import threading
import zipfile
from pathlib import Path

import pytest

from lantai.backup.models import RestoreBackupRequest

restore_ops = importlib.import_module("lantai.backup._ops.restore")
swap = importlib.import_module("lantai.backup._utils.safe_swap")


def _dir(path: Path, value: str) -> Path:
    path.mkdir()
    (path / "value.txt").write_text(value, encoding="utf-8")
    return path


def _archive(entries: dict[str, str]) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, value in entries.items():
            zf.writestr(name, value)
    buffer.seek(0)
    return buffer


def test_second_root_commit_failure_rolls_back_every_root_and_config(tmp_path, monkeypatch):
    first = _dir(tmp_path / "first", "old-first")
    second = _dir(tmp_path / "second", "old-second")
    _dir(tmp_path / "first.restore_tmp", "new-first")
    _dir(tmp_path / "second.restore_tmp", "new-second")
    config = tmp_path / restore_ops.CONFIG_FILE
    config.write_text("old-config", encoding="utf-8")
    staged_config = tmp_path / "config.staged"
    staged_config.write_text("new-config", encoding="utf-8")
    monkeypatch.setattr(restore_ops, "WORKING_DIR", tmp_path)
    original_commit = restore_ops.commit_tmp

    def fail_second(dst):
        if dst == second:
            raise OSError("fixture second-root commit failure")
        original_commit(dst)

    monkeypatch.setattr(restore_ops, "commit_tmp", fail_second)
    with pytest.raises(OSError, match="second-root"):
        restore_ops._commit_and_finalize([first, second], staged_config, {}, [], "fixture")
    actual = {
        "first": (first / "value.txt").read_text(),
        "second": (second / "value.txt").read_text(),
        "config": config.read_text(),
    }
    assert actual == {"first": "old-first", "second": "old-second", "config": "old-config"}


@pytest.mark.asyncio
async def test_cancelled_restore_holds_writer_fence_until_underlying_thread_settles(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    second_entered = threading.Event()
    monkeypatch.setattr(restore_ops, "_RESTORE_LOCK", asyncio.Lock())

    def controlled_restore(backup_id, request):
        if backup_id == "first":
            entered.set()
            try:
                assert release.wait(2), "fixture failed to release first restore"
            finally:
                finished.set()
        else:
            second_entered.set()

    monkeypatch.setattr(restore_ops, "_restore_sync", controlled_restore)
    first = asyncio.create_task(restore_ops.restore("first", RestoreBackupRequest()))
    second = None
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.002)
        assert entered.is_set()
        first.cancel()
        await asyncio.sleep(0.005)
        first.cancel()
        second = asyncio.create_task(restore_ops.restore("second", RestoreBackupRequest()))
        await asyncio.sleep(0.05)
        assert not second_entered.is_set(), "cancelled HTTP awaiter released a still-active restore thread's writer fence"
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
        for _ in range(100):
            if finished.is_set():
                break
            await asyncio.sleep(0.002)
        assert finished.is_set(), "fixture restore thread failed to settle"


def test_unattested_current_root_does_not_authorize_deleting_last_old_root(tmp_path):
    current = _dir(tmp_path / "workspace", "partial-new")
    old = _dir(tmp_path / "workspace.restore_old", "complete-old")
    swap.cleanup_stale_restore_artifacts(current)
    assert old.is_dir() and (old / "value.txt").read_text() == "complete-old", "cleanup destroyed the only rollback copy without a committed transaction receipt"


def test_unsafe_archive_entry_rejects_whole_staging_instead_of_silent_partial_restore(tmp_path):
    dst = _dir(tmp_path / "workspace", "old")
    with zipfile.ZipFile(_archive({"agent/good.txt": "good", "agent/../outside.txt": "bad"})) as zf:
        with pytest.raises(ValueError, match="(?i)(path|unsafe|travers|archive)"):
            swap.extract_to_tmp(zf, "agent/", dst)
    assert (dst / "value.txt").read_text() == "old"
    assert not (tmp_path / "outside.txt").exists()


def test_second_stager_cannot_replace_an_uncommitted_first_stager(tmp_path):
    dst = _dir(tmp_path / "workspace", "old")
    with zipfile.ZipFile(_archive({"agent/value.txt": "first"})) as first:
        staged = swap.extract_to_tmp(first, "agent/", dst)
    with zipfile.ZipFile(_archive({"agent/value.txt": "second"})) as second:
        with pytest.raises((RuntimeError, FileExistsError), match="(?i)(owner|staging|restore|active|exist)"):
            swap.extract_to_tmp(second, "agent/", dst)
    assert (staged / "value.txt").read_text() == "first"


def test_single_root_failed_second_rename_restores_original(tmp_path, monkeypatch):
    dst = _dir(tmp_path / "workspace", "old")
    staged = _dir(tmp_path / "workspace.restore_tmp", "new")
    original_rename = Path.rename

    def fail_promotion(path, target):
        if path == staged:
            raise OSError("fixture promotion failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_promotion)
    with pytest.raises(OSError, match="promotion failure"):
        swap.commit_tmp(dst)
    assert (dst / "value.txt").read_text() == "old"
