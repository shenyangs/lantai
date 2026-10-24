"""A stable restore fence and an attested plan for whole-group recovery."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .authoritative_store import (
    atomic_write_bytes, exclusive_store_lock, fsync_directory, load_validated,
    write_authoritative, recover_last_good,
)
from .path_transaction import recover_path_transaction, _validated

_WRITERS = threading.local()


def restore_lock_path(root: Path) -> Path:
    return root.parent / f".{root.name}.backup-restore.lock"


def restore_journal_path(root: Path) -> Path:
    return root.parent / f".{root.name}.restore-transaction.json"


def _intent_path(root: Path) -> Path:
    return root.parent / f".{root.name}.restore-intent.json"


def _decode_intent(value: str) -> dict:
    intent = json.loads(value)
    if (not isinstance(intent, dict) or type(intent.get("version")) is not int
            or intent["version"] != 1 or intent.get("phase") not in {"staging", "prepared", "settled"}
            or not isinstance(intent.get("paths"), list)):
        raise ValueError("Invalid restore recovery plan")
    seen: list[Path] = []
    for item in intent["paths"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid restore recovery target")
        for key in ("destination", "staged"):
            if not isinstance(item.get(key), str):
                raise ValueError("Invalid restore recovery path")
            path = Path(item[key])
            if not path.is_absolute() or path.parent.resolve() != path.parent or path.is_symlink():
                raise ValueError("Unsafe restore recovery path")
        dst, staged = Path(item["destination"]), Path(item["staged"])
        if staged == dst or any(dst == prior or dst.is_relative_to(prior) or prior.is_relative_to(dst) for prior in seen):
            raise ValueError("Overlapping restore recovery targets")
        identity = item.get("staged_identity")
        if identity is None and intent["phase"] == "staging":
            seen.append(dst)
            continue
        if (not isinstance(identity, list) or len(identity) != 2
                or any(type(part) is not int or part < 0 for part in identity)):
            raise ValueError("Unidentified restore staging path")
        seen.append(dst)
    transaction = intent.get("transaction")
    if transaction is not None:
        if intent["phase"] != "prepared":
            raise ValueError("Restore transaction has an invalid recovery phase")
        try:
            _validated(Path("/unused-restore-journal"), targets=set(seen), state=transaction, check_paths=False)
        except RuntimeError as error:
            raise ValueError(str(error)) from error
        approved = {item["destination"]: item["staged"] for item in intent["paths"]}
        if {record["destination"]: record["staged"] for record in transaction["paths"]} != approved:
            raise ValueError("Restore transaction does not cover its complete recovery plan")
        previous = intent.get("previous_journal_sha256")
        if previous is not None and (not isinstance(previous, str) or len(previous) != 64
                                     or any(c not in "0123456789abcdef" for c in previous)):
            raise ValueError("Invalid previous restore journal receipt")
    return intent


def _load_intent_for_recovery(root: Path) -> dict | None:
    path = _intent_path(root)
    # This transaction owns its exact publication intent. Complete only a
    # matching candidate, with the generic store's identity checks intact.
    if path.with_name(path.name + ".pending.json").exists():
        if path.with_name(path.name + ".corruption.json").exists():
            return load_validated(path, _decode_intent, missing=lambda: None)
        try:
            primary = path.read_bytes()
        except FileNotFoundError:
            recover_last_good(path, _decode_intent, expected_missing=True)
        else:
            recover_last_good(path, _decode_intent, expected_sha256=hashlib.sha256(primary).hexdigest())
    return load_validated(path, _decode_intent, missing=lambda: None)


def _save_intent(root: Path, intent: dict) -> None:
    value = json.dumps(intent, sort_keys=True).encode("utf-8")
    _decode_intent(value.decode("utf-8"))
    write_authoritative(_intent_path(root), value)


def record_restore_intent(root: Path, replacements: list[tuple[Path, Path]]) -> None:
    """Attest approved destinations and the exact staged inodes before swap."""
    paths = []
    for destination, staged in replacements:
        if destination.is_symlink() or staged.is_symlink():
            raise ValueError("Unsafe restore staging path")
        destination, staged = destination.resolve(), staged.resolve()
        stat = staged.stat()
        paths.append({"destination": str(destination), "staged": str(staged),
                      "staged_identity": [stat.st_dev, stat.st_ino]})
    _save_intent(root, {"version": 1, "phase": "prepared", "paths": paths})


def reserve_restore_staging(root: Path, replacements: list[tuple[Path, Path]]) -> None:
    """Reserve empty staging names before creating any extraction directory."""
    paths = []
    for destination, staged in replacements:
        if destination.is_symlink() or staged.exists() or staged.is_symlink():
            raise FileExistsError("An uncommitted restore staging path already exists")
        paths.append({"destination": str(destination.resolve()), "staged": str(staged.resolve()),
                      "staged_identity": None})
    _save_intent(root, {"version": 1, "phase": "staging", "paths": paths})


def claim_restore_stage(root: Path, destination: Path, staged: Path) -> None:
    """Attest the inode before writing archive bytes into a reserved stage."""
    intent = load_validated(_intent_path(root), _decode_intent, missing=lambda: None)
    if intent is None or intent["phase"] != "staging":
        raise RuntimeError("Restore staging has no recovery owner")
    for item in intent["paths"]:
        if item["destination"] == str(destination.resolve()) and item["staged"] == str(staged.resolve()):
            if item["staged_identity"] is not None or staged.is_symlink():
                raise RuntimeError("Restore staging ownership changed")
            stat = staged.stat()
            item["staged_identity"] = [stat.st_dev, stat.st_ino]
            _save_intent(root, intent)
            return
    raise RuntimeError("Restore staging is outside the reserved plan")


def write_restore_transaction(root: Path, journal: Path, state: dict) -> None:
    """Publish the complete transaction in the attested plan before a rename.

    The plain journal is a mirror. Its previous bytes are allowed if a crash
    falls between the authoritative publication and updating that mirror.
    """
    intent = _load_intent_for_recovery(root)
    if intent is None or intent["phase"] != "prepared":
        raise RuntimeError("Restore transaction has no complete recovery owner")
    previous = journal.read_bytes() if journal.exists() else None
    intent["transaction"] = json.loads(json.dumps(state))
    intent["previous_journal_sha256"] = hashlib.sha256(previous).hexdigest() if previous is not None else None
    _save_intent(root, intent)
    atomic_write_bytes(journal, json.dumps(state, sort_keys=True).encode("utf-8"))


def read_restore_transaction(root: Path, journal: Path) -> dict:
    intent = _load_intent_for_recovery(root)
    if intent is None or intent.get("transaction") is None:
        raise RuntimeError("Unidentified restore transaction; recovery is blocked")
    state = intent["transaction"]
    expected = hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()
    if journal.exists():
        actual = hashlib.sha256(journal.read_bytes()).hexdigest()
        if actual not in {expected, intent.get("previous_journal_sha256")}:
            raise RuntimeError("Restore journal differs from its complete recovery receipt")
    return state


def recover_restore_group(root: Path) -> None:
    """Under the restore fence, settle all targets before any memory read."""
    journal = restore_journal_path(root)
    intent = _load_intent_for_recovery(root)
    if intent is None or intent["phase"] == "settled":
        if journal.exists():
            raise RuntimeError("Unidentified restore transaction; recovery is blocked")
        return
    targets = {Path(item["destination"]) for item in intent["paths"]}
    if intent["phase"] == "staging" and journal.exists():
        raise RuntimeError("Restore promotion began without a complete staging receipt")
    with ExitStack() as locks:
        for dst in sorted(targets, key=str):
            # All public memory operations acquire the restore fence first.
            # These stable sibling locks also protect older direct callers.
            locks.enter_context(exclusive_store_lock(dst.parent / f".{dst.name}.memory-state.lock"))
        # Validate every surviving staged inode before touching any live or
        # rollback path. A different process's stage must remain intact.
        for item in intent["paths"]:
            staged = Path(item["staged"])
            if staged.exists() or staged.is_symlink():
                stat = staged.lstat()
                identity = item["staged_identity"]
                if identity is not None and (staged.is_symlink() or [stat.st_dev, stat.st_ino] != identity):
                    raise RuntimeError("Restore staging ownership changed; recovery is blocked")
        if intent.get("transaction") is not None:
            state = read_restore_transaction(root, journal)
            recover_path_transaction(journal, targets=targets, state=state,
                                     write_journal=lambda path, value: write_restore_transaction(root, path, value))
        elif journal.exists():
            raise RuntimeError("Restore promotion has no attested transaction receipt")
        for item in intent["paths"]:
            staged = Path(item["staged"])
            if staged.exists() or staged.is_symlink():
                stat = staged.lstat()
                identity = item["staged_identity"]
                if identity is None:
                    # Only an empty reservation can exist before its inode
                    # receipt. Archive writes start after claim_restore_stage.
                    if staged.is_symlink() or (staged.is_dir() and any(staged.iterdir())) or (staged.is_file() and stat.st_size):
                        raise RuntimeError("Unidentified restore staging content; recovery is blocked")
                elif staged.is_symlink() or [stat.st_dev, stat.st_ino] != identity:
                    raise RuntimeError("Restore staging ownership changed; recovery is blocked")
                if staged.is_dir():
                    shutil.rmtree(staged)
                else:
                    staged.unlink()
                fsync_directory(staged.parent)
        # Settled plans no longer need to retain names of unused reservations.
        _save_intent(root, {"version": 1, "phase": "settled", "paths": []})


@contextmanager
def restore_writer_fence(root: Path):
    root = root.resolve()
    key = (os.getpid(), str(root))
    with exclusive_store_lock(restore_lock_path(root)):
        owners = getattr(_WRITERS, "owners", None)
        if owners is None:
            owners = _WRITERS.owners = {}
        if key not in owners:
            recover_restore_group(root)
        owners[key] = owners.get(key, 0) + 1
        try:
            yield
        finally:
            owners[key] -= 1
            if not owners[key]:
                del owners[key]


@contextmanager
def restore_read_fence():
    # Import at entry so test profiles and application runtime share the
    # current data root, rather than a second hard-coded profile location.
    from ..constant import WORKING_DIR

    with exclusive_store_lock(restore_lock_path(WORKING_DIR)):
        owners = getattr(_WRITERS, "owners", {})
        if (os.getpid(), str(WORKING_DIR.resolve())) not in owners:
            recover_restore_group(WORKING_DIR)
        yield
