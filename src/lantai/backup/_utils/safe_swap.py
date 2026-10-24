# -*- coding: utf-8 -*-
"""Crash-safe directory swap utilities for backup restore operations.

Three-phase protocol:
  Phase 1 – Extract into a sibling ``.restore_tmp`` directory.
  Phase 2 – Atomically rename old dst → ``.restore_old``, then tmp → dst.
  Phase 3 – Remove ``.restore_old`` (old data is now disposable).

A mid-restore crash never leaves *dst* in a broken state: either the old
content is intact or the new content is complete.

Important: do NOT place files that must be preserved inside *dst* before
calling ``extract_to_tmp`` + ``commit_tmp``.  Phase 2/3 replaces the entire
directory tree, so anything written inside *dst* beforehand will be lost.

Two-phase transactional usage
------------------------------
For restoring multiple directories atomically (so a failure in one target
does not leave others already committed), use the split API::

    # Stage all targets first:
    tmp_paths = []
    for (zf, prefix, dst, zip_slip_base) in targets:
        tmp_paths.append(extract_to_tmp(zf, prefix, dst, zip_slip_base))

    # Commit only when all extractions succeeded:
    for dst in dsts:
        commit_tmp(dst)

On any extraction failure, call ``discard_tmp(dst)`` for each staged dst
to clean up.

Concurrency
-------------
Each *dst* path is protected by a stable sibling process lock so that
concurrent callers cannot interleave their extract / commit / discard
operations for the same destination.  The lock is held for the duration of
the entire phase-1, phase-2+3, or cleanup operation.
"""
from __future__ import annotations

import logging
import shutil
import stat
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path

from ...utils.authoritative_store import exclusive_store_lock, fsync_directory

logger = logging.getLogger(__name__)

_RESTORE_TMP_SUFFIX = ".restore_tmp"
_RESTORE_OLD_SUFFIX = ".restore_old"

# Per-destination threading locks.  The dict itself is guarded by _LOCKS_GUARD.
_GROUP_SWAP = threading.local()


def _lock_for(dst: Path):
    destination = dst.resolve(strict=False)
    return exclusive_store_lock(destination.parent / f".{destination.name}.restore.lock")


@contextmanager
def retain_old_for_transaction(old: Path):
    previous = getattr(_GROUP_SWAP, "old", None)
    _GROUP_SWAP.old = old
    try:
        yield
    finally:
        _GROUP_SWAP.old = previous


@contextmanager
def owned_restore_staging(on_create):
    """Claim each empty staging inode before extraction writes its bytes."""
    previous = getattr(_GROUP_SWAP, "on_create", None)
    _GROUP_SWAP.on_create = on_create
    try:
        yield
    finally:
        _GROUP_SWAP.on_create = previous


def claim_created_stage(destination: Path, staged: Path) -> None:
    callback = getattr(_GROUP_SWAP, "on_create", None)
    if callback is not None:
        callback(destination, staged)


def cleanup_stale_restore_artifacts(base_dir: Path) -> None:
    """Restore a retained old directory when its live destination is absent.

    Staging and rollback copies with a live destination require their owning
    transaction's receipt; this helper preserves those unconfirmed bytes.
    """
    with _lock_for(base_dir):
        _cleanup_stale_restore_artifacts_locked(base_dir)


def _cleanup_stale_restore_artifacts_locked(base_dir: Path) -> None:
    """Implementation of cleanup_stale_restore_artifacts (caller holds
    lock)."""
    old = base_dir.with_name(base_dir.name + _RESTORE_OLD_SUFFIX)

    # Scenario 1: original data saved in .restore_old; recover it first.
    if old.exists() and not base_dir.exists():
        try:
            old.rename(base_dir)
            logger.warning(
                "Recovered %s from stale %s artifact",
                base_dir,
                _RESTORE_OLD_SUFFIX,
            )
        except OSError:
            logger.exception(
                "Failed to recover %s from %s",
                base_dir,
                _RESTORE_OLD_SUFFIX,
            )
            # Keep .restore_old intact to avoid data loss; abort cleanup.
            return

    # Existing live bytes alone do not prove a complete transaction. Keep
    # both rollback and staging copies until their owner explicitly settles.


def _extract_zip_to(
    zf: zipfile.ZipFile,
    prefix: str,
    tmp_dst: Path,
    base_resolved: Path,
) -> None:
    """Phase 1: extract ZIP entries with *prefix* into *tmp_dst*.

    Applies Zip Slip guard: skips any entry whose resolved logical path
    falls outside *base_resolved*.
    """
    selected: list[tuple[zipfile.ZipInfo, str]] = []
    seen: set[str] = set()
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith(prefix):
            continue
        rel = info.filename[len(prefix) :]

        # Zip Slip guard: validate the *logical* destination path.
        parts = Path(rel).parts
        mode = info.external_attr >> 16
        if (not rel or "\\" in rel or Path(rel).is_absolute() or ".." in parts
                or not (base_resolved / rel).resolve().is_relative_to(base_resolved)
                or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not stat.S_ISREG(mode))
                or rel in seen):
            raise ValueError("Unsafe archive path or duplicate member")
        seen.add(rel)
        selected.append((info, rel))

    for info, rel in selected:
        target = tmp_dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _swap_directories(dst: Path, tmp_dst: Path, old_dst: Path) -> None:
    """Phase 2: atomically swap *tmp_dst* into *dst* via two renames.

    If the first rename succeeds but the second fails (e.g. permissions,
    Windows open-handle), the original directory is restored from *old_dst*
    before re-raising, so *dst* is never left absent.
    """
    if not tmp_dst.exists():
        raise RuntimeError(
            f"commit_tmp called without a valid staging directory: {tmp_dst}",
        )
    if old_dst.exists() or old_dst.is_symlink() or dst.is_symlink() or tmp_dst.is_symlink():
        raise RuntimeError("Unsafe or unsettled restore rollback path already exists")

    renamed_to_old = False
    if dst.exists():
        dst.rename(old_dst)
        fsync_directory(dst.parent)
        renamed_to_old = True

    try:
        tmp_dst.rename(dst)
        fsync_directory(dst.parent)
    except OSError:
        # Roll back: restore original data if we moved it away.
        if renamed_to_old and old_dst.exists() and not dst.exists():
            try:
                old_dst.rename(dst)
                fsync_directory(dst.parent)
                logger.warning(
                    "Rolled back %s rename after failed commit; "
                    "original data restored from %s",
                    dst,
                    old_dst,
                )
            except OSError:
                logger.exception(
                    "Rollback of %s failed — original data is in %s",
                    dst,
                    old_dst,
                )
        raise


def _discard_old(old_dst: Path) -> None:
    """Phase 3: remove the backup of the original directory."""
    if old_dst.exists():
        shutil.rmtree(old_dst)


# ---------------------------------------------------------------------------
# Two-phase transactional API
# ---------------------------------------------------------------------------


def extract_to_tmp(
    zf: zipfile.ZipFile,
    prefix: str,
    dst: Path,
    *,
    zip_slip_base: Path | None = None,
) -> Path:
    """Phase 1 only: extract ZIP entries with *prefix* into a sibling
    ``.restore_tmp`` directory and return its path.

    Call :func:`commit_tmp` to promote the extraction into *dst*, or
    :func:`discard_tmp` to roll it back.

    *zip_slip_base* defaults to *dst* and is used for the Zip Slip guard.
    """
    if zip_slip_base is None:
        zip_slip_base = dst
    base_resolved = zip_slip_base.resolve()

    with _lock_for(dst):
        tmp_dst = dst.with_name(dst.name + _RESTORE_TMP_SUFFIX)
        if tmp_dst.exists() or tmp_dst.is_symlink():
            raise FileExistsError("An uncommitted restore staging directory already exists")
        tmp_dst.mkdir(parents=True, exist_ok=False)
        try:
            claim_created_stage(dst, tmp_dst)
            _extract_zip_to(zf, prefix, tmp_dst, base_resolved)
        except BaseException:
            shutil.rmtree(tmp_dst)
            raise
        return tmp_dst


def commit_tmp(dst: Path) -> None:
    """Phases 2 + 3: atomically swap the staged ``.restore_tmp`` into *dst*
    and remove the old directory.

    Must be called after a successful :func:`extract_to_tmp` for the same
    *dst*.

    Raises :class:`RuntimeError` if the staging directory does not exist
    (e.g. called twice for the same *dst*).
    """
    with _lock_for(dst):
        tmp_dst = dst.with_name(dst.name + _RESTORE_TMP_SUFFIX)
        retained_old = getattr(_GROUP_SWAP, "old", None)
        old_dst = retained_old or dst.with_name(dst.name + _RESTORE_OLD_SUFFIX)
        _swap_directories(dst, tmp_dst, old_dst)
        if retained_old is None:
            _discard_old(old_dst)


def discard_tmp(dst: Path) -> None:
    """Remove the ``.restore_tmp`` sibling of *dst* if it exists.

    Used during rollback when a multi-target extraction fails part-way.
    """
    with _lock_for(dst):
        tmp_dst = dst.with_name(dst.name + _RESTORE_TMP_SUFFIX)
        if tmp_dst.exists():
            try:
                shutil.rmtree(tmp_dst)
            except OSError:
                logger.exception(
                    "Failed to discard staging directory %s",
                    tmp_dst,
                )
