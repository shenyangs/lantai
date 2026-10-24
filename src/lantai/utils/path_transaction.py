"""Publish several staged paths while retaining a durable rollback record."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .authoritative_store import (
    atomic_write_bytes, fsync_directory, load_validated, write_authoritative,
    recover_last_good,
)


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _owner_path(journal: Path) -> Path:
    return journal.with_name(journal.name + ".owner.json")


def _decode_owner(value: str) -> dict:
    owner = json.loads(value)
    if (not isinstance(owner, dict) or type(owner.get("version")) is not int
            or owner["version"] != 1 or owner.get("phase") not in {"active", "settled"}):
        raise ValueError("Invalid path transaction recovery owner")
    if owner["phase"] == "active":
        if not isinstance(owner.get("transaction"), dict):
            raise ValueError("Invalid path transaction recovery state")
        _validated(Path("/unused-path-journal"), state=owner["transaction"], check_paths=False)
        previous = owner.get("previous_journal_sha256")
        if previous is not None and (not isinstance(previous, str) or len(previous) != 64
                                     or any(c not in "0123456789abcdef" for c in previous)):
            raise ValueError("Invalid path transaction previous receipt")
    return owner


def _load_owner(journal: Path) -> dict | None:
    path = _owner_path(journal)
    if path.with_name(path.name + ".pending.json").exists():
        if not path.with_name(path.name + ".corruption.json").exists():
            try:
                value = path.read_bytes()
            except FileNotFoundError:
                recover_last_good(path, _decode_owner, expected_missing=True)
            else:
                recover_last_good(path, _decode_owner, expected_sha256=hashlib.sha256(value).hexdigest())
    return load_validated(path, _decode_owner, missing=lambda: None)


def _read(journal: Path) -> dict | None:
    owner = _load_owner(journal)
    if owner is None:
        if journal.exists():
            raise RuntimeError("Path transaction has no attested recovery owner")
        return None
    if owner["phase"] == "settled":
        if journal.exists():
            raise RuntimeError("Settled path transaction has an unidentified journal")
        return None
    state = owner["transaction"]
    expected = hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()
    if journal.exists() and hashlib.sha256(journal.read_bytes()).hexdigest() not in {
            expected, owner.get("previous_journal_sha256")}:
        raise RuntimeError("Path transaction journal differs from its durable recovery receipt")
    return state


def _write(journal: Path, state: dict) -> None:
    previous = journal.read_bytes() if journal.exists() else None
    value = {"version": 1, "phase": "active", "transaction": state,
             "previous_journal_sha256": hashlib.sha256(previous).hexdigest() if previous is not None else None}
    encoded = json.dumps(value, sort_keys=True).encode("utf-8")
    _decode_owner(encoded.decode("utf-8"))
    write_authoritative(_owner_path(journal), encoded)
    atomic_write_bytes(journal, json.dumps(state, sort_keys=True).encode("utf-8"))


def _settle_owner(journal: Path) -> None:
    if _owner_path(journal).exists():
        write_authoritative(_owner_path(journal), b'{"version":1,"phase":"settled"}')


def _identity(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def _sync_staged(path: Path) -> None:
    members = [path] if path.is_file() else list(path.rglob("*"))
    # A dangling symlink matches neither is_file nor is_dir. Validate every
    # directory entry before filtering, or it would silently reach live data.
    if path.is_symlink() or any(item.is_symlink() for item in members):
        raise ValueError("Unsafe symlink in path transaction staging")
    files = [item for item in members if item.is_file()]
    for file in files:
        if file.is_symlink():
            raise ValueError("Unsafe symlink in path transaction staging")
        with file.open("rb") as handle:
            os.fsync(handle.fileno())
    if path.is_dir():
        for directory in sorted((item for item in path.rglob("*") if item.is_dir()),
                                key=lambda item: len(item.parts), reverse=True):
            if directory.is_symlink():
                raise ValueError("Unsafe directory symlink in path transaction staging")
            fsync_directory(directory)
        fsync_directory(path)
    fsync_directory(path.parent)


def _validated(journal: Path, *, root: Path | None = None, targets: set[Path] | None = None,
               state: dict | None = None, check_paths: bool = True) -> dict:
    state = _read(journal) if state is None else state
    if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1
            or state.get("phase") not in {"prepared", "committed"}
            or not isinstance(state.get("id"), str)
            or len(state["id"]) != 32 or any(c not in "0123456789abcdef" for c in state["id"])
            or not isinstance(state.get("paths"), list)):
        raise RuntimeError("Invalid path transaction; recovery is blocked")
    seen: list[Path] = []
    for record in state["paths"]:
        if not isinstance(record, dict) or any(type(record.get(k)) is not bool for k in ("existed", "started")):
            raise RuntimeError("Invalid path transaction record")
        identity = record.get("identity")
        if record["existed"] and (not isinstance(identity, list) or len(identity) != 2
                                  or any(type(value) is not int or value < 0 for value in identity)):
            raise RuntimeError("Invalid path transaction original identity")
        if any(not isinstance(record.get(k), str) for k in ("destination", "old")):
            raise RuntimeError("Invalid path transaction destination")
        dst, old = Path(record["destination"]), Path(record["old"])
        if not dst.is_absolute() or old != dst.with_name(f".{dst.name}.transaction-old-{state['id']}"):
            raise RuntimeError("Invalid path transaction rollback destination")
        if dst.is_symlink() or old.is_symlink() or dst.parent.resolve() != dst.parent:
            raise RuntimeError("Unsafe path transaction destination")
        if root is not None and (dst == root or not dst.is_relative_to(root)):
            raise RuntimeError("Path transaction destination is outside its working directory")
        if targets is not None and dst not in targets:
            raise RuntimeError("Path transaction destination is outside the approved restore targets")
        staged = record.get("staged")
        if staged is not None and (not isinstance(staged, str) or not Path(staged).is_absolute()
                                  or Path(staged) in {dst, old} or Path(staged).is_symlink()):
            raise RuntimeError("Invalid path transaction staging destination")
        staged_identity = record.get("staged_identity")
        if staged is not None:
            if (not isinstance(staged_identity, list) or len(staged_identity) != 2
                    or any(type(value) is not int or value < 0 for value in staged_identity)):
                raise RuntimeError("Invalid path transaction staged identity")
            if check_paths and _exists(Path(staged)) and _identity(Path(staged)) != staged_identity:
                raise RuntimeError("Path transaction staging ownership changed")
        if check_paths and _exists(old) and (not record["existed"] or _identity(old) != identity):
            raise RuntimeError("Path transaction rollback ownership changed")
        if state["phase"] == "committed":
            published = record.get("published_identity")
            if staged is not None and (not isinstance(published, list) or len(published) != 2
                                       or any(type(value) is not int or value < 0 for value in published)):
                raise RuntimeError("Invalid path transaction publication identity")
            if not record["started"] or (check_paths and staged is None and _exists(dst)):
                raise RuntimeError("Path transaction commit is incomplete")
            if check_paths and staged is not None and (not _exists(dst) or _identity(dst) != published):
                raise RuntimeError("Path transaction published ownership changed")
        if any(dst == previous or dst.is_relative_to(previous) or previous.is_relative_to(dst) for previous in seen):
            raise RuntimeError("Overlapping path transaction destinations")
        seen.append(dst)
    return state


def _finish(journal: Path, state: dict) -> None:
    _validated(journal, state=state)
    for record in state["paths"]:
        old = Path(record["old"])
        _remove(old)
        fsync_directory(old.parent)
    journal.unlink(missing_ok=True)
    fsync_directory(journal.parent)
    _settle_owner(journal)


def _rollback(journal: Path, state: dict, *, write_journal=None) -> None:
    writer = write_journal or _write
    for record in reversed(state["paths"]):
        if not record["started"]:
            continue
        dst, old = Path(record["destination"]), Path(record["old"])
        staged = Path(record["staged"]) if record["staged"] is not None else None
        if _exists(old):
            _remove(dst)
            old.rename(dst)
        elif record["existed"]:
            # The old path has not been moved, or the individual publisher
            # already rolled back its failed promotion.
            if not _exists(dst) or _identity(dst) != record["identity"]:
                raise RuntimeError("Rollback copy is missing; transaction remains blocked")
        elif staged is not None and not _exists(staged):
            _remove(dst)
        fsync_directory(dst.parent)
        record["started"] = False
        writer(journal, state)
    journal.unlink(missing_ok=True)
    fsync_directory(journal.parent)
    if write_journal is None:
        _settle_owner(journal)


def recover_path_transaction(journal: Path, *, root: Path | None = None, targets: set[Path] | None = None,
                             state: dict | None = None, write_journal=None) -> None:
    """Under the caller's stable lock, finish a commit or restore every old path."""
    if state is None:
        state = _read(journal)
        if state is None:
            return
    state = _validated(journal, root=root, targets=targets, state=state)
    if state["phase"] == "committed":
        _finish(journal, state)
    else:
        _rollback(journal, state, write_journal=write_journal)


def promote_path(dst: Path, staged: Path | None, old: Path) -> None:
    if _exists(dst):
        dst.rename(old)
        fsync_directory(dst.parent)
    if staged is not None:
        staged_parent = staged.parent
        staged.rename(dst)
        fsync_directory(dst.parent)
        if staged_parent != dst.parent:
            fsync_directory(staged_parent)


@contextmanager
def path_transaction(
    journal: Path,
    replacements: list[tuple[Path, Path | None]],
    *,
    promote: Callable[[Path, Path | None, Path], None] = promote_path,
    write_journal=None,
    read_journal=None,
) -> Iterator[None]:
    """Retain old paths through the caller's final validation/configuration.

    The caller holds the same stable lock used for recovery and readers.
    Staging is complete before this context; no staging path is silently
    replaced or deleted by another transaction.
    """
    if journal.exists() or (write_journal is None and _read(journal) is not None):
        raise RuntimeError("An unsettled path transaction already exists")
    writer = write_journal or _write
    transaction_id = uuid.uuid4().hex
    records = []
    for dst, staged in replacements:
        dst, staged = Path(dst), Path(staged) if staged is not None else None
        if dst.is_symlink() or (staged is not None and (staged.is_symlink() or not staged.exists())):
            raise ValueError("Unsafe or missing path transaction staging path")
        dst = dst.resolve(strict=False)
        staged = staged.resolve(strict=False) if staged is not None else None
        if staged is not None:
            _sync_staged(staged)
        dst.parent.mkdir(parents=True, exist_ok=True)
        records.append({"destination": str(dst), "staged": str(staged) if staged else None,
                        "old": str(dst.with_name(f".{dst.name}.transaction-old-{transaction_id}")),
                        "existed": _exists(dst), "started": False,
                        "identity": _identity(dst) if _exists(dst) else None,
                        "staged_identity": _identity(staged) if staged is not None else None})
    state = {"version": 1, "id": transaction_id, "phase": "prepared", "paths": records}
    writer(journal, state)
    try:
        _validated(journal, state=read_journal(journal) if read_journal else None)
        for record in records:
            record["started"] = True
            writer(journal, state)
            promote(Path(record["destination"]), Path(record["staged"]) if record["staged"] else None,
                    Path(record["old"]))
        yield
        for record in records:
            record["published_identity"] = _identity(Path(record["destination"])) if record["staged"] is not None else None
        state["phase"] = "committed"
        writer(journal, state)
    except BaseException:
        # Disk errors during rollback leave the journal and surviving copies
        # intact, so the next owner can recover before exposing mixed state.
        if state["phase"] == "committed":
            persisted = _validated(journal, state=read_journal(journal) if read_journal else None)
            if persisted["phase"] == "prepared":
                _rollback(journal, persisted, write_journal=write_journal)
        else:
            _rollback(journal, state, write_journal=write_journal)
        raise
    else:
        _finish(journal, state)
