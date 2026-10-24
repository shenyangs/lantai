"""Durable files which distinguish new data from damaged authoritative state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

T = TypeVar("T")
_LOCKS: dict[tuple[int, str], threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_LOCK_DEPTH = threading.local()


class StoreCorruptionError(RuntimeError):
    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Authoritative store {path.name} is blocked: {reason}; explicit recovery required")


def _sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def fsync_directory(path: Path) -> None:
    # Windows does not expose directory fsync through os.open.
    if os.name == "nt":  # pragma: no cover
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_directory(path: Path) -> None:
    missing: list[Path] = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        fsync_directory(directory)
        fsync_directory(directory.parent)


def atomic_write_bytes(path: Path, value: bytes) -> None:
    _durable_directory(path.parent)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def exclusive_store_lock(path: Path) -> Iterator[None]:
    key = (os.getpid(), str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        depths = getattr(_LOCK_DEPTH, "depths", None)
        if depths is None:
            depths = _LOCK_DEPTH.depths = {}
        if key in depths:
            # Opening a second flock descriptor can deadlock even in the
            # same thread. The outer context already owns the process lock.
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        _durable_directory(path.parent)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a+b") as handle:
            if os.name == "nt":  # pragma: no cover
                import msvcrt
                if not handle.seek(0, os.SEEK_END):
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            depths[key] = 1
            try:
                yield
            finally:
                del depths[key]
                if os.name == "nt":  # pragma: no cover
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _blocked(path: Path, reason: str) -> StoreCorruptionError:
    marker = _sidecar(path, ".corruption.json")
    try:
        atomic_write_bytes(marker, json.dumps({"version": 1, "reason": reason}).encode("utf-8"))
    except OSError:
        # A read-only damaged store still fails closed without modifying it.
        pass
    return StoreCorruptionError(path, reason)


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.authority.lock")


def _reject_barriers(path: Path) -> None:
    if _sidecar(path, ".corruption.json").exists():
        raise StoreCorruptionError(path, "previously detected corruption")
    if _sidecar(path, ".pending.json").exists():
        raise StoreCorruptionError(path, "interrupted publication")


def _receipt_bytes(value: bytes) -> bytes:
    return json.dumps({"version": 1, "sha256": hashlib.sha256(value).hexdigest()}).encode("utf-8")


def _validated_backup(path: Path) -> bytes:
    backup = _sidecar(path, ".last-good").read_bytes()
    receipt = json.loads(_sidecar(path, ".last-good.receipt.json").read_bytes().decode("utf-8"))
    if (not isinstance(receipt, dict) or type(receipt.get("version")) is not int
            or receipt["version"] != 1
            or receipt.get("sha256") != hashlib.sha256(backup).hexdigest()):
        raise ValueError("unconfirmed recovery copy")
    return backup


def _verify_receipt(path: Path, value: bytes) -> bool:
    backup = _sidecar(path, ".last-good")
    receipt = _sidecar(path, ".last-good.receipt.json")
    if not backup.exists() and not receipt.exists():
        return False
    try:
        if _validated_backup(path) != value:
            raise ValueError("primary does not match durable receipt")
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, KeyError):
        raise _blocked(path, "data does not match durable receipt") from None
    return True


def _digest(value: bytes | None) -> str | None:
    return hashlib.sha256(value).hexdigest() if value is not None else None


def _begin_publication(path: Path, value: bytes, *, operation: str, original: bytes | None) -> None:
    atomic_write_bytes(_sidecar(path, ".pending.json"), json.dumps({
        "version": 1, "operation": operation, "sha256": _digest(value),
        "previous_sha256": _digest(original),
    }).encode("utf-8"))


def _end_publication(path: Path) -> None:
    _sidecar(path, ".pending.json").unlink()
    fsync_directory(path.parent)


def _check_identity(path: Path, original: bytes | None) -> None:
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError:
        raise StoreCorruptionError(path, "data changed or became unreadable during validation") from None
    if current != original:
        raise StoreCorruptionError(path, "data changed since inspection")


def _read_pending(path: Path) -> dict:
    try:
        pending = json.loads(_sidecar(path, ".pending.json").read_bytes().decode("utf-8"))
        if (not isinstance(pending, dict) or type(pending.get("version")) is not int
                or pending["version"] != 1 or pending.get("operation") not in {"write", "attest", "recover"}
                or not isinstance(pending.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", pending["sha256"]) is None
                or "previous_sha256" not in pending
                or (pending["previous_sha256"] is not None and (
                    not isinstance(pending["previous_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", pending["previous_sha256"]) is None))):
            raise ValueError("invalid publication identity")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise StoreCorruptionError(path, "unidentified interrupted publication") from None
    return pending


def _interrupted_primary(path: Path, *, expected_sha256: str, previous_sha256: str | None) -> bytes | None:
    """Validate the identified write without accepting it as ordinary state."""
    try:
        pending = _read_pending(path)
        if (pending.get("operation") not in {"write", "attest"}
                or pending.get("sha256") != expected_sha256
                or pending["previous_sha256"] != previous_sha256):
            raise ValueError("unidentified publication")
        if _sidecar(path, ".corruption.json").exists():
            raise ValueError("detected corruption")
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            original = None
        if _digest(original) not in {previous_sha256, expected_sha256}:
            raise ValueError("primary changed after publication began")
        if pending["operation"] == "attest" and not (
            previous_sha256 == expected_sha256 == _digest(original)
        ):
            raise ValueError("legacy data changed during attestation")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise StoreCorruptionError(path, "interrupted publication changed since inspection") from None
    return original


def load_interrupted_publication(
    path: Path, decode: Callable[[str], T], *, expected_sha256: str,
    previous_sha256: str | None, missing: Callable[[], T],
) -> T:
    """Inspect an explicitly identified transaction for forward recovery only."""
    with exclusive_store_lock(_lock_path(path)):
        original = _interrupted_primary(
            path, expected_sha256=expected_sha256, previous_sha256=previous_sha256,
        )
        try:
            result = missing() if original is None else decode(original.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise StoreCorruptionError(path, "invalid interrupted publication data") from None
        _check_identity(path, original)
        return result


def resume_interrupted_publication(
    path: Path, value: bytes, *, expected_sha256: str, previous_sha256: str | None,
) -> None:
    """Finish the exact durable write intent, retaining its barrier on failure."""
    with exclusive_store_lock(_lock_path(path)):
        original = _interrupted_primary(
            path, expected_sha256=expected_sha256, previous_sha256=previous_sha256,
        )
        if _digest(value) != expected_sha256:
            raise StoreCorruptionError(path, "candidate does not match interrupted publication")
        _check_identity(path, original)
        atomic_write_bytes(_sidecar(path, ".last-good"), value)
        _check_identity(path, original)
        atomic_write_bytes(path, value)
        atomic_write_bytes(_sidecar(path, ".last-good.receipt.json"), _receipt_bytes(value))
        _end_publication(path)


def load_validated(path: Path, decode: Callable[[str], T], *, missing: Callable[[], T]) -> T:
    with exclusive_store_lock(_lock_path(path)):
        _reject_barriers(path)
        try:
            value = path.read_bytes()
        except FileNotFoundError:
            if _sidecar(path, ".last-good").exists() or _sidecar(path, ".last-good.receipt.json").exists():
                raise _blocked(path, "previously initialized data is missing") from None
            return missing()
        except OSError:
            raise _blocked(path, "unreadable data") from None
        attested = _verify_receipt(path, value)
        try:
            decoded = decode(value.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise _blocked(path, "invalid data or unsupported schema") from None
        _check_identity(path, value)
        if not attested:
            # Attest legacy data only after validation, keeping its exact bytes
            # and the primary file intact. Readers share the publication lock.
            _begin_publication(path, value, operation="attest", original=value)
            atomic_write_bytes(_sidecar(path, ".last-good"), value)
            _check_identity(path, value)
            atomic_write_bytes(_sidecar(path, ".last-good.receipt.json"), _receipt_bytes(value))
            _end_publication(path)
        return decoded


def write_authoritative(path: Path, value: bytes) -> None:
    with exclusive_store_lock(_lock_path(path)):
        _reject_barriers(path)
        original = None
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            if _sidecar(path, ".last-good").exists() or _sidecar(path, ".last-good.receipt.json").exists():
                raise _blocked(path, "previously initialized data is missing") from None
        except OSError:
            raise _blocked(path, "unreadable data") from None
        else:
            _verify_receipt(path, original)
        # The durable barrier precedes all data changes and remains after any
        # failure. No ordinary reader can accept an incomplete publication.
        _begin_publication(path, value, operation="write", original=original)
        atomic_write_bytes(_sidecar(path, ".last-good"), value)
        atomic_write_bytes(path, value)
        atomic_write_bytes(_sidecar(path, ".last-good.receipt.json"), _receipt_bytes(value))
        _end_publication(path)


def recover_last_good(
    path: Path, decode: Callable[[str], T], *,
    expected_sha256: str | None = None, expected_missing: bool = False,
) -> T:
    """Restore confirmed bytes or finish the identified durable write intent."""
    if expected_missing == (expected_sha256 is not None):
        raise ValueError("identify either damaged bytes or a missing primary")
    with exclusive_store_lock(_lock_path(path)):
        try:
            try:
                original = path.read_bytes()
            except FileNotFoundError:
                original = None
            if expected_missing:
                if original is not None:
                    raise StoreCorruptionError(path, "primary is no longer missing")
            elif original is None or hashlib.sha256(original).hexdigest() != expected_sha256:
                raise StoreCorruptionError(path, "damaged data changed since inspection")
            pending_path = _sidecar(path, ".pending.json")
            pending = None
            if pending_path.exists():
                pending = _read_pending(path)
                if pending["operation"] in {"write", "attest"}:
                    backup = original if pending["operation"] == "attest" else _sidecar(path, ".last-good").read_bytes()
                    intended_sha256 = pending.get("sha256")
                    if backup is None or not isinstance(intended_sha256, str) or _digest(backup) != intended_sha256:
                        raise ValueError("no candidate matching the durable intent")
                    restored = decode(backup.decode("utf-8"))
                    _check_identity(path, original)
                    if pending["operation"] == "write" and _sidecar(path, ".last-good").read_bytes() != backup:
                        raise StoreCorruptionError(path, "recovery copy changed during validation")
                    resume_interrupted_publication(
                        path, backup, expected_sha256=intended_sha256,
                        previous_sha256=pending.get("previous_sha256"),
                    )
                    return restored
            backup = _validated_backup(path)
            if pending is not None and (
                _digest(backup) != pending["sha256"]
                or _digest(original) not in {pending["previous_sha256"], pending["sha256"]}
            ):
                raise StoreCorruptionError(path, "recovery does not match interrupted publication")
            restored = decode(backup.decode("utf-8"))
            _check_identity(path, original)
            if _validated_backup(path) != backup:
                raise StoreCorruptionError(path, "recovery copy changed during validation")
        except StoreCorruptionError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
            raise StoreCorruptionError(path, "no validated durable recovery copy") from None
        _begin_publication(path, backup, operation="recover", original=original)
        if original is not None:
            archive = path.with_name(f"{path.name}.corrupt-{expected_sha256}.bin")
            atomic_write_bytes(archive, original)
        _check_identity(path, original)
        atomic_write_bytes(path, backup)
        _sidecar(path, ".corruption.json").unlink(missing_ok=True)
        _end_publication(path)
        return restored
