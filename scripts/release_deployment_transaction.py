#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keep the previous Downloads release until the entire replacement is accepted.

Each rename has a durable plan written first. A dead process's incomplete plan
is recovered by comparing actual bytes; unknown ownership or foreign bytes keep
both the journal and backups for inspection.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from dataclasses import asdict

import psutil


def _load_process_stop():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import stop_release_app_instance

    return stop_release_app_instance


process_stop = _load_process_stop()

TX_NAME = ".lantai-release-transaction"
LOCK_NAME = TX_NAME + ".lock"
ROLES = ("app", "dmg", "zip")
PROCESS_PROOF = "process-owners.json"
CLEANUP_NAME = TX_NAME + ".cleanup.json"
CLEANUP_DIR = TX_NAME + ".cleanup"
_DIRECTORIES = ContextVar("release_deployment_directories", default=None)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _root(path):
    path = Path(os.path.abspath(path))
    _require(
        path.is_dir() and not path.is_symlink(),
        "Deployment root must be an existing real directory",
    )
    _require(path.resolve() == path, "Deployment root contains a symlink")
    return path


def _destinations(destinations):
    _require(
        isinstance(destinations, dict) and set(destinations) == set(ROLES),
        "Deployment requires App, DMG and ZIP",
    )
    values = list(destinations.values())
    _require(
        all(
            isinstance(v, str)
            and v
            and v
            not in (".", "..", TX_NAME, LOCK_NAME, CLEANUP_NAME, CLEANUP_DIR)
            and Path(v).name == v
            and "/" not in v
            and "\\" not in v
            for v in values
        ),
        "Destination must be a confined, non-reserved basename",
    )
    _require(
        len(set(values)) == len(values),
        "Deployment destinations must be distinct",
    )
    return destinations


def _open_directory(path, *, create=False):
    """Walk from / without following a swapped directory or ancestor link."""
    path = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = following
        return fd
    except OSError as exc:
        os.close(fd)
        raise ValueError(
            "Deployment directory is missing, changed or contains a symlink",
        ) from exc


class _Directory:
    def __init__(self, path, fd):
        self.path, self.fd = Path(path), fd
        info = os.fstat(fd)
        self.identity = {"device": info.st_dev, "inode": info.st_ino}

    def assert_visible(self):
        probe = _open_directory(self.path)
        try:
            info = os.fstat(probe)
            _require(
                self.identity == {"device": info.st_dev, "inode": info.st_ino},
                "Deployment directory was exchanged; preserving transaction",
            )
        finally:
            os.close(probe)


def _check_bindings():
    for directory in (_DIRECTORIES.get() or {}).values():
        directory.assert_visible()


@contextmanager
def _directory(path, *, create=False):
    path = Path(os.path.abspath(path))
    pinned = (_DIRECTORIES.get() or {}).get(path)
    directory = pinned or _Directory(
        path,
        _open_directory(path, create=create),
    )
    try:
        _check_bindings()
        directory.assert_visible()
        yield directory
        directory.assert_visible()
    finally:
        if pinned is None:
            os.close(directory.fd)


def _pin_stage(stage):
    bindings = _DIRECTORIES.get()
    _require(
        bindings is not None and stage.parent in bindings,
        "Deployment root is not locked",
    )
    if stage not in bindings:
        root = bindings[stage.parent]
        _check_bindings()
        try:
            fd = os.open(
                stage.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root.fd,
            )
        except OSError as exc:
            raise ValueError(
                "Deployment journal directory is invalid",
            ) from exc
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise ValueError(
                "Deployment journal is not an owned private directory",
            )
        bindings[stage] = _Directory(stage, fd)
    _check_bindings()
    return bindings[stage]


def _metadata(path):
    with _directory(path.parent) as directory:
        try:
            return os.stat(
                path.name,
                dir_fd=directory.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None


def _sync_directory(path):
    with _directory(path) as directory:
        os.fsync(directory.fd)


def _rename_no_replace_at(src_fd, src_name, dst_fd, dst_name):
    """The kernel decides absence atomically; a check + replace is unsafe."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename, flag = (
            library.renameatx_np,
            0x00000004,
        )  # RENAME_EXCL, XNU stdio.h
    elif sys.platform.startswith("linux"):
        rename, flag = library.renameat2, 1  # RENAME_NOREPLACE, Linux fs.h
    else:
        raise ValueError(
            "Atomic no-overwrite deployment is unavailable on this OS",
        )
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(
        src_fd,
        os.fsencode(src_name),
        dst_fd,
        os.fsencode(dst_name),
        flag,
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(dst_name))


def _rename_no_replace(source, target):
    with _directory(source.parent) as src, _directory(target.parent) as dst:
        _check_bindings()
        _rename_no_replace_at(src.fd, source.name, dst.fd, target.name)
        _check_bindings()


def _sync_artifact(path):
    """Flush replacement contents before the durable rename plan is written."""

    def visit(parent, name):
        _check_bindings()
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            return  # Literal App links, never their targets.
        is_directory = stat.S_ISDIR(info.st_mode)
        _require(
            is_directory or stat.S_ISREG(info.st_mode),
            "Special files cannot be deployed",
        )
        fd = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | (os.O_DIRECTORY if is_directory else 0),
            dir_fd=parent,
        )
        try:
            actual = os.fstat(fd)
            _require(
                (info.st_dev, info.st_ino) == (actual.st_dev, actual.st_ino),
                "Replacement changed while opening",
            )
            if is_directory:
                for child in os.listdir(fd):
                    visit(fd, child)
            os.fsync(fd)
            if not is_directory and sys.platform == "darwin":
                fcntl.fcntl(fd, getattr(fcntl, "F_FULLFSYNC", 51))
        finally:
            os.close(fd)

    with _directory(path.parent) as directory:
        visit(directory.fd, path.name)


@contextmanager
def _exclusive(root):
    directory = _Directory(root, _open_directory(root))
    token = _DIRECTORIES.set({root: directory})
    fd = None
    try:
        directory.assert_visible()
        fd = os.open(
            LOCK_NAME,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory.fd,
        )
        metadata = os.fstat(fd)
        _require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_nlink == 1,
            "Deployment lock is not an owned regular file",
        )
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = os.stat(
            LOCK_NAME,
            dir_fd=directory.fd,
            follow_symlinks=False,
        )
        _require(
            (current.st_dev, current.st_ino)
            == (metadata.st_dev, metadata.st_ino),
            "Deployment lock was exchanged",
        )
        _check_bindings()
        yield
    finally:
        if fd is not None:
            os.close(fd)
        for pinned in _DIRECTORIES.get().values():
            os.close(pinned.fd)
        _DIRECTORIES.reset(token)


def _owner_record(pid):
    _require(type(pid) is int and pid > 0, "Invalid deployment owner PID")
    try:
        process = psutil.Process(pid)
        _require(
            process.status() != psutil.STATUS_ZOMBIE,
            "Deployment owner is a zombie",
        )
        return {"pid": pid, "birth": process.create_time()}
    except psutil.NoSuchProcess as exc:
        raise ValueError("Deployment owner no longer exists") from exc


def _owner_alive(record):
    try:
        process = psutil.Process(record["pid"])
        return (
            process.status() != psutil.STATUS_ZOMBIE
            and process.create_time() == record["birth"]
        )
    except psutil.NoSuchProcess:
        return False
    # AccessDenied and other uncertain probes are deliberately not interpreted
    # as a dead owner.


def fingerprint(path):
    path = Path(path)
    digest = hashlib.sha256()

    def visit(parent, name, relative):
        _check_bindings()
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            _require(bool(relative), "An artifact itself cannot be a symlink")
            record = [
                relative,
                "symlink",
                mode,
                os.readlink(name, dir_fd=parent),
            ]
            digest.update(
                json.dumps(
                    record,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode()
                + b"\n",
            )
            return
        is_directory = stat.S_ISDIR(info.st_mode)
        _require(
            is_directory or stat.S_ISREG(info.st_mode),
            "Special files cannot be deployed",
        )
        fd = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | (os.O_DIRECTORY if is_directory else 0),
            dir_fd=parent,
        )
        try:
            actual = os.fstat(fd)
            _require(
                (info.st_dev, info.st_ino) == (actual.st_dev, actual.st_ino),
                "Artifact changed while opening",
            )
            if is_directory:
                record = [relative, "directory", mode]
            else:
                contents = hashlib.sha256()
                for block in iter(lambda: os.read(fd, 1024 * 1024), b""):
                    contents.update(block)
                record = [
                    relative,
                    "file",
                    mode,
                    info.st_size,
                    contents.hexdigest(),
                ]
            digest.update(
                json.dumps(
                    record,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode()
                + b"\n",
            )
            if is_directory:
                for child in sorted(os.listdir(fd)):
                    visit(
                        fd,
                        child,
                        child if not relative else relative + "/" + child,
                    )
            after = os.fstat(fd)
            _require(
                (
                    actual.st_mode,
                    actual.st_size,
                    actual.st_mtime_ns,
                    actual.st_ctime_ns,
                )
                == (
                    after.st_mode,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ),
                "Artifact changed while being fingerprinted",
            )
        finally:
            os.close(fd)

    with _directory(path.parent) as directory:
        visit(directory.fd, path.name, "")
    return digest.hexdigest()


def _read_json(path, *, private=False, max_bytes=None):
    with _directory(path.parent) as directory:
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory.fd,
        )
        try:
            info = os.fstat(fd)
            _require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1,
                "Journal/receipt is not an owned regular file",
            )
            _require(
                not private or stat.S_IMODE(info.st_mode) == 0o600,
                "Private proof permissions changed",
            )
            _require(
                max_bytes is None or info.st_size <= max_bytes,
                "Private proof exceeds its size limit",
            )
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
                if max_bytes is None:
                    return json.load(handle)
                contents = handle.read(max_bytes + 1)
                _require(
                    len(contents.encode("utf-8")) <= max_bytes,
                    "Private proof grew beyond its size limit",
                )
                return json.loads(contents)
        finally:
            os.close(fd)


def _atomic_json(path, payload, *, journal=False):
    with _directory(path.parent, create=True) as directory:
        metadata = _metadata(path)
        if metadata is not None:
            _require(
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.getuid()
                and metadata.st_nlink == 1,
                "Journal/receipt is not an owned regular file",
            )
            if not journal:
                _require(
                    _read_json(path) == payload,
                    "Receipt path already contains different evidence",
                )
                return
        temporary = path.with_name(
            path.name + "." + secrets.token_hex(16) + ".tmp",
        )
        fd = os.open(
            temporary.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory.fd,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                if sys.platform == "darwin":
                    fcntl.fcntl(
                        handle.fileno(),
                        getattr(fcntl, "F_FULLFSYNC", 51),
                    )
            _check_bindings()
            directory.assert_visible()
            if journal:
                os.replace(
                    temporary.name,
                    path.name,
                    src_dir_fd=directory.fd,
                    dst_dir_fd=directory.fd,
                )
            else:
                _rename_no_replace(temporary, path)
            os.fsync(directory.fd)
        finally:
            try:
                os.unlink(temporary.name, dir_fd=directory.fd)
            except FileNotFoundError:
                pass


def _write(stage, state):
    _pin_stage(stage)
    _atomic_json(stage / "state.json", state, journal=True)


def _read(stage):
    directory = _pin_stage(stage)
    state = _read_json(stage / "state.json")
    return _validate_state(state, stage.parent, directory.identity)


def _validate_state(state, root, stage_identity):
    _require(
        isinstance(state, dict)
        and state.get("schema") == "com.lantai.deployment-transaction.v1",
        "Unknown deployment journal",
    )
    required = {
        "schema",
        "state",
        "source_sha",
        "version",
        "owner",
        "entries",
        "directoryIdentity",
    }
    _require(
        required
        <= set(state)
        <= required
        | {
            "previous_running",
            "launch_started",
            "launch_registered",
            "processes_settled",
            "receipt",
        },
        "Unknown journal fields",
    )
    _require(
        isinstance(state["source_sha"], str)
        and bool(re.fullmatch(r"[0-9a-f]{40}", state["source_sha"])),
        "Invalid journal source",
    )
    _require(
        isinstance(state["version"], str)
        and bool(
            re.fullmatch(
                r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",
                state["version"],
            ),
        ),
        "Invalid journal version",
    )
    bindings = _DIRECTORIES.get()
    _require(
        isinstance(stage_identity, dict)
        and set(stage_identity) == {"device", "inode"}
        and all(type(v) is int and v >= 0 for v in stage_identity.values()),
        "Invalid journal directory identity",
    )
    _require(
        state.get("directoryIdentity")
        == {"root": bindings[root].identity, "stage": stage_identity},
        "Deployment journal belongs to different directories",
    )
    owner = state.get("owner", {})
    _require(
        isinstance(owner, dict)
        and set(owner) == {"pid", "birth"}
        and type(owner.get("pid")) is int
        and owner["pid"] > 0
        and type(owner.get("birth")) in (float, int)
        and math.isfinite(owner["birth"])
        and owner["birth"] > 0,
        "Invalid journal owner",
    )
    _require(
        state.get("state")
        in {"prepared", "installing", "installed", "rolled-back", "accepted"},
        "Invalid journal state",
    )
    entries = state.get("entries")
    _require(
        isinstance(entries, dict) and set(entries) == set(ROLES),
        "Incomplete journal plan",
    )
    _require(
        all(
            isinstance(e, dict)
            and set(e) == {"destination", "had_old", "old_hash", "new_hash"}
            for e in entries.values()
        ),
        "Invalid journal entry",
    )
    _destinations({role: entries[role].get("destination") for role in ROLES})
    for role, entry in entries.items():
        _require(
            type(entry.get("had_old")) is bool,
            "Invalid previous release record",
        )
        for key in ("old_hash", "new_hash"):
            value = entry.get(key)
            _require(
                value is None
                or bool(re.fullmatch(r"[0-9a-f]{64}", str(value))),
                "Invalid journal fingerprint",
            )
        _require(
            not entry["had_old"] or entry["old_hash"] is not None,
            "Previous release fingerprint missing",
        )
        if state["state"] in {"installing", "installed", "accepted"}:
            _require(
                entry["new_hash"] is not None,
                "Replacement fingerprint missing",
            )
    for key in (
        "previous_running",
        "launch_started",
        "launch_registered",
        "processes_settled",
    ):
        _require(
            key not in state or type(state[key]) is bool,
            "Invalid process settlement state",
        )
    _require(
        not state.get("launch_registered") or state.get("launch_started"),
        "Launch registration has no intent",
    )
    if state["state"] == "accepted":
        _require(
            isinstance(state.get("receipt"), str),
            "Acceptance receipt is missing",
        )
    if "receipt" in state:
        _receipt_path(root, state, state["receipt"])
    return state


def _receipt_path(root, state, value):
    _require(
        isinstance(value, str) and value and "\0" not in value,
        "Invalid receipt path",
    )
    receipt = Path(value)
    stage = root / TX_NAME
    _require(
        receipt.is_absolute()
        and str(Path(os.path.abspath(receipt))) == value
        and stage not in receipt.parents
        and receipt != stage
        and root / CLEANUP_DIR not in receipt.parents
        and receipt != root / CLEANUP_DIR,
        "Receipt must be an absolute path outside the transaction",
    )
    reserved = {TX_NAME, LOCK_NAME, CLEANUP_NAME, CLEANUP_DIR} | {
        e["destination"] for e in state["entries"].values()
    }
    _require(
        receipt.parent != root or receipt.name not in reserved,
        "Receipt conflicts with deployment authority",
    )
    return receipt


def _receipt_preflight(root, state):
    """A conflicting public receipt must leave all remaining backups intact."""
    if state["state"] != "accepted":
        return
    receipt = _receipt_path(root, state, state["receipt"])
    # Creating the eventual parent now also rejects linked/exchanged ancestors
    # before acceptance. Public acceptance itself is published after cleanup.
    with _directory(receipt.parent, create=True):
        if _present(receipt):
            _require(
                _read_json(receipt) == state,
                "Receipt path already contains different evidence",
            )


def _assert_owner(state, pid):
    _require(
        state["owner"]["pid"] == pid,
        "Deployment belongs to another process",
    )
    _require(
        _owner_record(pid) == state["owner"],
        "Deployment process identity changed",
    )


def _present(path):
    return _metadata(path) is not None


def _same(path, expected):
    info = _metadata(path)
    return (
        expected is not None
        and info is not None
        and not stat.S_ISLNK(info.st_mode)
        and fingerprint(path) == expected
    )


def _validate_layout(root, stage, state):
    """Validate every role before a wait, and again before any move/delete."""
    directory = _pin_stage(stage)
    names = os.listdir(directory.fd)
    allowed = (
        {"state.json", "state.json.tmp", PROCESS_PROOF}
        | set(ROLES)
        | {"old-" + r for r in ROLES}
        | {"rejected-" + r for r in ROLES}
    )
    _require(
        all(
            name in allowed
            or re.fullmatch(
                r"(?:state\.json|process-owners\.json)\.[0-9a-f]{32}\.tmp",
                name,
            )
            for name in names
        ),
        "Unknown files in deployment journal; preserving backups",
    )
    for role, entry in state["entries"].items():
        destination = root / entry["destination"]
        if _present(destination):
            _require(
                _same(destination, entry["old_hash"])
                or _same(destination, entry["new_hash"]),
                "Destination was modified outside this deployment",
            )
        for name, expected in (
            ("old-" + role, entry["old_hash"]),
            ("rejected-" + role, entry["new_hash"]),
        ):
            if _present(stage / name):
                _require(
                    _same(stage / name, expected),
                    "Transaction backup changed; preserving it",
                )
        if _present(stage / role) and entry["new_hash"] is not None:
            _require(
                _same(stage / role, entry["new_hash"]),
                "Staged replacement changed; preserving transaction",
            )
    _check_bindings()


def _process_owners(stage):
    path = stage / PROCESS_PROOF
    if not _present(path):
        return []
    metadata = _metadata(path)
    _require(
        stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_size <= 1024 * 1024,
        "Process proof is not private",
    )
    data = _read_json(path, private=True, max_bytes=1024 * 1024)
    return _decode_process_owners(data)


def _decode_process_owners(data):
    _require(
        isinstance(data, dict)
        and set(data) == {"schema", "owners"}
        and data["schema"] == 1
        and isinstance(data["owners"], list),
        "Unknown process proof",
    )
    _require(len(data["owners"]) <= 4096, "Too many process owners")
    result = []
    for item in data["owners"]:
        _require(
            isinstance(item, dict)
            and set(item) == {"origin", "owner"}
            and item["origin"]
            in {
                "previous",
                "target",
                "staged",
                "backup",
                "rejected",
                "launch",
            },
            "Invalid process origin",
        )
        result.append(
            (item["origin"], process_stop.owner_from_dict(item["owner"])),
        )
    return result


def _save_process_owners(stage, owners):
    _atomic_json(
        stage / PROCESS_PROOF,
        {
            "schema": 1,
            "owners": [
                {"origin": origin, "owner": asdict(owner)}
                for origin, owner in owners
            ],
        },
        journal=True,
    )


def _app_locations(root, stage, state):
    return [
        root / state["entries"]["app"]["destination"],
        stage / "app",
        stage / "old-app",
        stage / "rejected-app",
    ]


def _confirmed_launch(state, owners):
    if state.get("launch_started"):
        _require(
            state.get("launch_registered")
            and any(
                origin == "launch" and owner.had_instance and owner.nonce
                for origin, owner in owners
            ),
            "Launch identity was not registered; preserving journal and backups",
        )


def _settle_app_processes(root, stage, state, *, cleanup=False):
    _validate_layout(root, stage, state)
    retained = _process_owners(stage)
    _confirmed_launch(state, retained)
    locations = _app_locations(root, stage, state)
    candidates = [p for p in locations if _present(p)]
    affected = (
        [p for p in candidates if p.parent == stage] if cleanup else candidates
    )
    plans = []
    for origin, owner in retained:
        if not owner.had_instance:
            continue  # A genuinely empty owner has no relocation credential.
        binding = process_stop.locate_owner(owner, candidates)
        if Path(binding["path"]) in affected:
            plans.append((owner, binding, True))
    # Bootstrap the full scope before signalling any identity. Saved owners
    # survive parent exit; fresh inventories also detect newly opened bundles.
    for app in affected:
        try:
            owners = [
                owner
                for owner, binding, registered in plans
                if registered and binding["path"] == str(app)
            ]
            owner = process_stop.bootstrap(app, retained=owners)
        except process_stop.ProcessStopError:
            if (
                app == stage / "app"
                and state["entries"]["app"]["new_hash"] is None
            ):
                process_stop.ensure_absent(app)
                continue
            raise
        plans.append((owner, owner.app, False))
        if owner.had_instance:
            origin = (
                "previous"
                if app == locations[0]
                and _same(app, state["entries"]["app"]["old_hash"])
                else {
                    locations[0]: "target",
                    stage / "app": "staged",
                    stage / "old-app": "backup",
                    stage / "rejected-app": "rejected",
                }[app]
            )
            item = (origin, owner)
            if item not in retained:
                retained.append(item)
    if retained:
        _save_process_owners(
            stage,
            retained,
        )  # Durable before the first signal.
    for owner, binding, registered in plans:
        process_stop.settle(owner, binding=binding, registered_only=registered)
    _validate_layout(root, stage, state)
    return any(
        origin == "previous" and owner.had_instance
        for origin, owner in retained
    )


def _cleanup_entry(parent, name):
    """Capture object identity and literal bytes; never follow an App link."""
    _check_bindings()
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    identity = [info.st_dev, info.st_ino, info.st_mode, info.st_uid]
    if stat.S_ISLNK(info.st_mode):
        value = {
            "kind": "symlink",
            "identity": identity,
            "link": os.readlink(name, dir_fd=parent),
        }
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    elif stat.S_ISDIR(info.st_mode):
        return {"kind": "directory", "identity": identity}
    else:
        _require(stat.S_ISREG(info.st_mode), "Cleanup contains a special file")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            before = os.fstat(fd)
            _require(
                identity
                == [
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_uid,
                ],
                "Cleanup file was exchanged",
            )
            digest = hashlib.sha256()
            for block in iter(lambda: os.read(fd, 1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(fd)
            value = {
                "kind": "file",
                "identity": identity,
                "size": before.st_size,
                "sha256": digest.hexdigest(),
            }
        finally:
            os.close(fd)
    _require(
        (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        == (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ),
        "Cleanup object changed while being read",
    )
    return value


def _cleanup_inventory(stage):
    directory = _pin_stage(stage)
    result = {}

    def visit(parent, name, relative):
        _require(
            len(result) < 250000 and len(relative) <= 4096,
            "Cleanup inventory is too large",
        )
        entry = result[relative] = _cleanup_entry(parent, name)
        if entry["kind"] != "directory":
            return
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        try:
            actual = os.fstat(fd)
            _require(
                entry["identity"]
                == [
                    actual.st_dev,
                    actual.st_ino,
                    actual.st_mode,
                    actual.st_uid,
                ],
                "Cleanup directory was exchanged",
            )
            names = sorted(os.listdir(fd))
            for child in names:
                visit(fd, child, relative + "/" + child)
            _require(
                names == sorted(os.listdir(fd)),
                "Cleanup inventory changed during capture",
            )
        finally:
            os.close(fd)

    for name in sorted(os.listdir(directory.fd)):
        visit(directory.fd, name, name)
    return result


def _validate_cleanup_manifest(manifest):
    _require(
        isinstance(manifest, dict) and len(manifest) <= 250000,
        "Invalid cleanup inventory",
    )
    allowed = (
        {"state.json", "state.json.tmp", PROCESS_PROOF}
        | set(ROLES)
        | {"old-" + r for r in ROLES}
        | {"rejected-" + r for r in ROLES}
    )
    for relative, entry in manifest.items():
        _require(
            isinstance(relative, str)
            and relative
            and len(relative) <= 4096
            and "\0" not in relative
            and all(p and p not in {".", ".."} for p in relative.split("/"))
            and not relative.startswith("/"),
            "Cleanup path is not confined",
        )
        top = relative.split("/")[0]
        _require(
            top in allowed
            or re.fullmatch(
                r"(?:state\.json|process-owners\.json)\.[0-9a-f]{32}\.tmp",
                top,
            ),
            "Unknown cleanup authority",
        )
        _require(
            isinstance(entry, dict)
            and entry.get("kind") in {"file", "directory", "symlink"},
            "Invalid cleanup object",
        )
        kind = entry["kind"]
        fields = {"kind", "identity"} | (
            {"size", "sha256"}
            if kind == "file"
            else {"link"}
            if kind == "symlink"
            else set()
        )
        _require(set(entry) == fields, "Unknown cleanup object fields")
        identity = entry["identity"]
        _require(
            isinstance(identity, list)
            and len(identity) == 4
            and all(type(v) is int and v >= 0 for v in identity),
            "Invalid cleanup object identity",
        )
        expected_type = {
            "file": stat.S_ISREG,
            "directory": stat.S_ISDIR,
            "symlink": stat.S_ISLNK,
        }[kind]
        _require(
            expected_type(identity[2]),
            "Cleanup object type disagrees with identity",
        )
        if kind == "file":
            _require(
                type(entry["size"]) is int
                and entry["size"] >= 0
                and isinstance(entry["sha256"], str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])),
                "Invalid cleanup file bytes",
            )
        if kind == "symlink":
            _require(
                isinstance(entry["link"], str) and "\0" not in entry["link"],
                "Invalid cleanup link",
            )
        parent = Path(relative).parent
        while str(parent) != ".":
            _require(
                str(parent) in manifest
                and manifest[str(parent)].get("kind") == "directory",
                "Cleanup ancestor is not a recorded directory",
            )
            parent = parent.parent


def _read_cleanup(root):
    path = root / CLEANUP_NAME
    info = _metadata(path)
    if info is None:
        return None
    _require(
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_size <= 64 * 1024 * 1024,
        "Cleanup authority is not private",
    )
    record = _read_json(path, private=True, max_bytes=64 * 1024 * 1024)
    required = {"schema", "state", "manifest", "owners", "cleaning"}
    _require(
        isinstance(record, dict)
        and required <= set(record) <= required | {"quarantine"}
        and record["schema"] == "com.lantai.deployment-cleanup.v1",
        "Unknown cleanup authority",
    )
    state = record["state"]
    _require(
        isinstance(state, dict)
        and isinstance(state.get("directoryIdentity"), dict),
        "Invalid cleanup state",
    )
    _validate_state(state, root, state["directoryIdentity"].get("stage"))
    _require(
        state["state"] in {"accepted", "rolled-back"}
        and state.get("processes_settled") is True,
        "Cleanup has no terminal process proof",
    )
    owners = _decode_process_owners({"schema": 1, "owners": record["owners"]})
    _confirmed_launch(state, owners)
    _require(
        isinstance(record["cleaning"], list)
        and len(set(record["cleaning"])) == len(record["cleaning"])
        and all(
            type(i) is int and 0 <= i < len(owners) for i in record["cleaning"]
        ),
        "Invalid cleanup process scope",
    )
    _validate_cleanup_manifest(record["manifest"])
    if "quarantine" in record:
        area = record["quarantine"]
        _require(
            isinstance(area, dict)
            and set(area)
            == {"name", "baseIdentity", "areaIdentity", "complete"}
            and isinstance(area["name"], str)
            and bool(re.fullmatch(r"[0-9a-f]{32}", area["name"]))
            and type(area["complete"]) is bool,
            "Invalid cleanup isolation authority",
        )
        for key in ("baseIdentity", "areaIdentity"):
            identity = area[key]
            _require(
                isinstance(identity, list)
                and len(identity) == 4
                and all(type(v) is int and v >= 0 for v in identity)
                and stat.S_ISDIR(identity[2])
                and stat.S_IMODE(identity[2]) == 0o700
                and identity[3] == os.getuid(),
                "Cleanup isolation is not an owned private directory",
            )
    return record


def _cleanup_destinations(root, state):
    _receipt_preflight(root, state)
    for entry in state["entries"].values():
        target = root / entry["destination"]
        expected = (
            entry["new_hash"]
            if state["state"] == "accepted"
            else entry["old_hash"]
        )
        _require(
            _same(target, expected)
            if expected is not None
            else not _present(target),
            "Terminal release changed; preserving cleanup authority",
        )


def _remaining_cleanup(_root, stage, record):
    if not _present(stage):
        return False
    directory = _pin_stage(stage)
    _require(
        directory.identity == record["state"]["directoryIdentity"]["stage"],
        "Cleanup stage was exchanged",
    )
    remaining = _cleanup_inventory(stage)
    _require(
        all(
            relative in record["manifest"]
            and record["manifest"][relative] == entry
            for relative, entry in remaining.items()
        ),
        "Cleanup contains an added or exchanged object; preserving remaining backups",
    )
    return True


def _directory_identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid]


def _pin_cleanup_directory(path, *, identity=None, create=False):
    bindings = _DIRECTORIES.get()
    _require(path.parent in bindings, "Cleanup parent is not pinned")
    _check_bindings()
    if path not in bindings:
        if create:
            try:
                os.mkdir(path.name, 0o700, dir_fd=bindings[path.parent].fd)
                os.fsync(bindings[path.parent].fd)
            except FileExistsError:
                pass
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=bindings[path.parent].fd,
        )
        actual = _directory_identity(fd)
        if (
            actual[3] != os.getuid()
            or stat.S_IMODE(actual[2]) != 0o700
            or (identity is not None and actual != identity)
        ):
            os.close(fd)
            raise ValueError("Cleanup isolation directory changed")
        bindings[path] = _Directory(path, fd)
    directory = bindings[path]
    actual = _directory_identity(directory.fd)
    _require(
        actual[3] == os.getuid()
        and stat.S_IMODE(actual[2]) == 0o700
        and (identity is None or actual == identity),
        "Cleanup isolation directory changed",
    )
    _check_bindings()
    return directory


def _update_cleanup(root, record, quarantine):
    _require(
        _read_cleanup(root) == record,
        "Cleanup authority changed before isolation update",
    )
    updated = {**record, "quarantine": quarantine}
    _atomic_json(root / CLEANUP_NAME, updated, journal=True)
    record.update(updated)


def _cleanup_quarantine(root, record):
    proof = record.get("quarantine")
    base = _pin_cleanup_directory(
        root / CLEANUP_DIR,
        identity=proof["baseIdentity"] if proof else None,
        create=proof is None,
    )
    if proof is None:
        path = base.path / secrets.token_hex(16)
        # No backup enters this private area until its identity and intent file
        # are durably bound by the outside cleanup authority.
        os.mkdir(path.name, 0o700, dir_fd=base.fd)
        os.fsync(base.fd)
        area = _pin_cleanup_directory(path)
        _atomic_json(
            path / "pending.json",
            {
                "schema": "com.lantai.deployment-cleanup-move.v1",
                "relative": None,
            },
        )
        proof = {
            "name": path.name,
            "baseIdentity": _directory_identity(base.fd),
            "areaIdentity": _directory_identity(area.fd),
            "complete": False,
        }
        _update_cleanup(root, record, proof)
        return area
    path = base.path / proof["name"]
    if not _present(path):
        _require(
            proof["complete"] and not _present(root / TX_NAME),
            "Incomplete cleanup isolation is missing",
        )
        return None
    return _pin_cleanup_directory(path, identity=proof["areaIdentity"])


def _cleanup_pending(area, record):
    _pin_cleanup_directory(
        area.path,
        identity=record["quarantine"]["areaIdentity"],
    )
    _require(
        set(os.listdir(area.fd)) <= {"pending.json", "item"},
        "Cleanup isolation contains an unknown object",
    )
    pending = _read_json(
        area.path / "pending.json",
        private=True,
        max_bytes=32768,
    )
    _require(
        isinstance(pending, dict)
        and set(pending) == {"schema", "relative"}
        and pending["schema"] == "com.lantai.deployment-cleanup-move.v1",
        "Invalid cleanup move intent",
    )
    relative = pending["relative"]
    _require(
        relative is None
        or isinstance(relative, str)
        and (relative == "." or relative in record["manifest"]),
        "Cleanup move is outside the recorded inventory",
    )
    _require(
        relative is not None or not _present(area.path / "item"),
        "Unrecorded object in cleanup isolation",
    )
    return relative


@contextmanager
def _cleanup_parent(root, stage, record, relative):
    if relative == ".":
        yield _DIRECTORIES.get()[root].fd, stage.name
        return
    directory = _pin_stage(stage)
    fd = os.dup(directory.fd)
    try:
        parts = relative.split("/")
        for index, name in enumerate(parts[:-1]):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            if (
                _directory_identity(child)
                != record["manifest"]["/".join(parts[: index + 1])]["identity"]
            ):
                os.close(child)
                raise ValueError("Cleanup move parent changed")
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _complete_cleanup_move(root, stage, record, area, relative):
    expected = (
        record["manifest"][relative]
        if relative != "."
        else {
            "kind": "directory",
            "identity": [
                record["state"]["directoryIdentity"]["stage"]["device"],
                record["state"]["directoryIdentity"]["stage"]["inode"],
                stat.S_IFDIR | 0o700,
                os.getuid(),
            ],
        }
    )
    with _cleanup_parent(root, stage, record, relative) as (parent, name):
        captured = _present(area.path / "item")
        try:
            source = _cleanup_entry(parent, name)
        except FileNotFoundError:
            source = None
        if not captured and source is not None:
            _require(
                source == expected,
                "Cleanup object changed before capture",
            )
            _check_bindings()
            _rename_no_replace_at(parent, name, area.fd, "item")
            if relative == ".":
                directory = _DIRECTORIES.get().pop(stage, None)
                if directory is not None:
                    os.close(directory.fd)
            os.fsync(parent)
            os.fsync(area.fd)
            captured = True
        if captured:
            _check_bindings()
            try:
                _require(
                    _cleanup_entry(area.fd, "item") == expected,
                    "Captured cleanup object differs from inventory",
                )
            except ValueError:
                # Return a foreign object only if its original name is still
                # absent. Never replace an object that appeared in either slot.
                try:
                    _rename_no_replace_at(area.fd, "item", parent, name)
                    os.fsync(parent)
                    os.fsync(area.fd)
                except OSError:
                    pass
                raise
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError(
                    "Cleanup source name reappeared; preserving both objects",
                )
            if expected["kind"] == "directory":
                fd = os.open(
                    "item",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=area.fd,
                )
                try:
                    _require(
                        _directory_identity(fd) == expected["identity"]
                        and not os.listdir(fd),
                        "Captured cleanup directory changed or is not empty",
                    )
                finally:
                    os.close(fd)
                os.rmdir("item", dir_fd=area.fd)
            else:
                os.unlink("item", dir_fd=area.fd)
            os.fsync(area.fd)
    _atomic_json(
        area.path / "pending.json",
        {"schema": "com.lantai.deployment-cleanup-move.v1", "relative": None},
        journal=True,
    )


def _retire_cleanup_quarantine(root, stage, record, area):
    _require(not _present(stage), "Cleanup stage reappeared before acceptance")
    if not record["quarantine"]["complete"]:
        _require(
            area is not None and _cleanup_pending(area, record) is None,
            "Cleanup move remains incomplete",
        )
        _update_cleanup(
            root,
            record,
            {**record["quarantine"], "complete": True},
        )
    if area is not None:
        _pin_cleanup_directory(
            area.path,
            identity=record["quarantine"]["areaIdentity"],
        )
        _require(
            set(os.listdir(area.fd)) <= {"pending.json"},
            "Completed isolation contains an unknown object",
        )
        if _present(area.path / "pending.json"):
            _require(
                _cleanup_pending(area, record) is None,
                "Completed isolation has an unfinished move",
            )
            os.unlink("pending.json", dir_fd=area.fd)
            os.fsync(area.fd)
        base = _DIRECTORIES.get()[area.path.parent]
        _check_bindings()
        os.rmdir(area.path.name, dir_fd=base.fd)
        _DIRECTORIES.get().pop(area.path)
        os.close(area.fd)
        os.fsync(base.fd)


def _finish_cleanup_authority(root, state, record):
    if state["state"] == "accepted":
        receipt = _receipt_path(root, state, state["receipt"])
        _atomic_json(receipt, state)
        _sync_directory(receipt.parent)
    # The private record outlives all stage deletions and durable publication.
    # An interrupted receipt write can therefore resume without state.json.
    _require(
        _read_cleanup(root) == record,
        "Cleanup authority changed before retirement",
    )
    with _directory(root) as directory:
        os.unlink(CLEANUP_NAME, dir_fd=directory.fd)
        os.fsync(directory.fd)


def _resume_cleanup(root, record):
    stage = root / TX_NAME
    state, manifest = record["state"], record["manifest"]
    _cleanup_destinations(root, state)
    _remaining_cleanup(root, stage, record)
    owners = _decode_process_owners({"schema": 1, "owners": record["owners"]})
    process_stop.ensure_cleanup_absent(
        stage,
        [owners[i][1] for i in record["cleaning"]],
    )
    # The inventory/identity checks precede every deletion and are repeated
    # after the potentially blocking OS probes.
    _cleanup_destinations(root, state)
    present = _remaining_cleanup(root, stage, record)
    area = _cleanup_quarantine(root, record)
    if record["quarantine"]["complete"]:
        _require(not present, "Completed cleanup stage reappeared")
    else:
        pending = _cleanup_pending(area, record)
        if pending is not None:
            _complete_cleanup_move(root, stage, record, area, pending)
        present = _remaining_cleanup(root, stage, record)

    def capture(relative):
        _require(
            _cleanup_pending(area, record) is None,
            "Previous cleanup move is unfinished",
        )
        _atomic_json(
            area.path / "pending.json",
            {
                "schema": "com.lantai.deployment-cleanup-move.v1",
                "relative": relative,
            },
            journal=True,
        )
        _complete_cleanup_move(root, stage, record, area, relative)

    def remove(parent, name, relative):
        _check_bindings()
        current = _cleanup_entry(parent, name)
        _require(
            relative in manifest and current == manifest[relative],
            "Cleanup object changed before deletion",
        )
        if current["kind"] != "directory":
            capture(relative)
            return
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        try:
            actual = os.fstat(fd)
            _require(
                current["identity"]
                == [
                    actual.st_dev,
                    actual.st_ino,
                    actual.st_mode,
                    actual.st_uid,
                ],
                "Cleanup directory changed before opening",
            )
            for child in sorted(os.listdir(fd)):
                remove(fd, child, relative + "/" + child)
            os.fsync(fd)
            _check_bindings()
            _require(
                _cleanup_entry(parent, name) == current,
                "Cleanup directory changed before removal",
            )
            capture(relative)
        finally:
            os.close(fd)

    if present:
        directory = _pin_stage(stage)
        for name in sorted(os.listdir(directory.fd)):
            remove(directory.fd, name, name)
        os.fsync(directory.fd)
        _check_bindings()
        capture(".")
    _cleanup_destinations(root, state)
    _retire_cleanup_quarantine(root, stage, record, area)
    _sync_directory(root)
    _finish_cleanup_authority(root, state, record)


def _clean_stage(stage):
    root = stage.parent
    record = _read_cleanup(root)
    if record is None:
        state = _read(stage)
        _require(
            state["state"] in {"accepted", "rolled-back"},
            "Cleanup requires a terminal transaction",
        )
        _settle_app_processes(root, stage, state, cleanup=True)
        _validate_layout(root, stage, state)
        state["processes_settled"] = True
        _write(stage, state)
        owners = _process_owners(stage)
        candidates = [
            p for p in _app_locations(root, stage, state) if _present(p)
        ]
        cleaning = [
            i
            for i, (_, owner) in enumerate(owners)
            if owner.had_instance
            and Path(
                process_stop.locate_owner(owner, candidates)["path"],
            ).parent
            == stage
        ]
        record = {
            "schema": "com.lantai.deployment-cleanup.v1",
            "state": state,
            "manifest": _cleanup_inventory(stage),
            "owners": [
                {"origin": origin, "owner": asdict(owner)}
                for origin, owner in owners
            ],
            "cleaning": cleaning,
        }
        _validate_cleanup_manifest(record["manifest"])
        _atomic_json(root / CLEANUP_NAME, record)
    _resume_cleanup(root, record)


def _move(source, target):
    _rename_no_replace(source, target)
    _sync_directory(source.parent)
    if target.parent != source.parent:
        _sync_directory(target.parent)


def _restore(root, stage, state):
    # Validate the entire current set before restoring any entry. A foreign
    # change must leave every backup available to the user.
    for role, entry in state["entries"].items():
        destination = root / entry["destination"]
        backup, rejected = stage / ("old-" + role), stage / (
            "rejected-" + role
        )
        if _present(destination):
            _require(
                _same(destination, entry["old_hash"])
                or _same(destination, entry["new_hash"]),
                "Destination was modified outside this deployment",
            )
        if _present(backup):
            _require(
                entry["had_old"] and _same(backup, entry["old_hash"]),
                "Previous release backup was modified",
            )
        if _present(rejected):
            _require(
                _same(rejected, entry["new_hash"]),
                "Rollback staging was modified",
            )
        if entry["had_old"]:
            _require(
                _same(destination, entry["old_hash"])
                or _same(backup, entry["old_hash"]),
                "Previous release is missing",
            )
    previous = _settle_app_processes(root, stage, state)
    _validate_layout(root, stage, state)
    for role in reversed(ROLES):
        entry = state["entries"][role]
        destination = root / entry["destination"]
        backup, rejected = stage / ("old-" + role), stage / (
            "rejected-" + role
        )
        if _same(destination, entry["new_hash"]) and not _same(
            destination,
            entry["old_hash"],
        ):
            _move(destination, rejected)
        if entry["had_old"] and not _present(destination):
            _move(backup, destination)
    state["state"] = "rolled-back"
    state["previous_running"] = (
        state.get("previous_running", False) or previous
    )
    _write(stage, state)
    _clean_stage(stage)


def begin(root, *, destinations, source_sha, version, owner_pid):
    root = _root(root)
    destinations = _destinations(destinations)
    _require(
        bool(re.fullmatch(r"[0-9a-f]{40}", source_sha)),
        "Invalid independent deployment source SHA",
    )
    _require(
        bool(re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version)),
        "Invalid deployment version",
    )
    with _exclusive(root):
        stage = root / TX_NAME
        cleanup = _read_cleanup(root)
        if cleanup is not None:
            _require(
                not _owner_alive(cleanup["state"]["owner"]),
                "Previous cleanup owner is still running",
            )
            _resume_cleanup(root, cleanup)
        if _present(stage):
            state = _read(stage)
            _require(
                not _owner_alive(state["owner"]),
                "Previous deployment owner is still running",
            )
            if state["state"] == "accepted":
                _require(
                    all(
                        _same(root / e["destination"], e["new_hash"])
                        for e in state["entries"].values()
                    ),
                    "Accepted release was modified; preserving its journal",
                )
                _clean_stage(stage)
            else:
                _restore(root, stage, state)
        entries = {}
        for role in ROLES:
            destination = root / destinations[role]
            had_old = _present(destination)
            if had_old:
                _require(
                    not destination.is_symlink()
                    and (
                        destination.is_dir()
                        if role == "app"
                        else destination.is_file()
                    ),
                    "Previous artifact has an unexpected type",
                )
            entries[role] = {
                "destination": destinations[role],
                "had_old": had_old,
                "old_hash": fingerprint(destination) if had_old else None,
                "new_hash": None,
            }
        owner = _owner_record(owner_pid)
        os.mkdir(stage.name, 0o700, dir_fd=_DIRECTORIES.get()[root].fd)
        directory = _pin_stage(stage)
        _sync_directory(root)
        _write(
            stage,
            {
                "schema": "com.lantai.deployment-transaction.v1",
                "state": "prepared",
                "source_sha": source_sha,
                "version": version,
                "owner": owner,
                "entries": entries,
                "directoryIdentity": {
                    "root": _DIRECTORIES.get()[root].identity,
                    "stage": directory.identity,
                },
            },
        )
        return stage


def promote(root, *, sources, owner_pid):
    root = _root(root)
    with _exclusive(root):
        stage = root / TX_NAME
        state = _read(stage)
        _assert_owner(state, owner_pid)
        _require(
            state["state"] == "prepared" and set(sources) == set(ROLES),
            "Deployment is not ready for promotion",
        )
        for role in ROLES:
            source, staged = Path(sources[role]), stage / role
            _require(
                not source.is_symlink()
                and not staged.is_symlink()
                and (
                    source.is_dir() and staged.is_dir()
                    if role == "app"
                    else source.is_file() and staged.is_file()
                ),
                "Incomplete deployment stage",
            )
            expected = fingerprint(source)
            _require(
                fingerprint(staged) == expected,
                "Staged replacement differs from the verified source",
            )
            _sync_artifact(staged)
            _require(
                fingerprint(staged) == expected,
                "Replacement changed while its contents were flushed",
            )
            entry = state["entries"][role]
            destination = root / entry["destination"]
            _require(
                _same(destination, entry["old_hash"])
                if entry["had_old"]
                else not _present(destination),
                "Previous release changed before promotion",
            )
            _require(
                not _present(stage / ("old-" + role)),
                "Backup already exists",
            )
            entry["new_hash"] = expected
        previous = _settle_app_processes(root, stage, state)
        _validate_layout(root, stage, state)
        # The wait can take seconds; source and staged bytes must still agree.
        for role in ROLES:
            _require(
                _same(Path(sources[role]), state["entries"][role]["new_hash"]),
                "Verified source changed during process settlement",
            )
        state["previous_running"] = previous
        state["state"] = "installing"
        _write(stage, state)
        for role in ROLES:
            destination = root / state["entries"][role]["destination"]
            if state["entries"][role]["had_old"]:
                _move(destination, stage / ("old-" + role))
            _move(stage / role, destination)
        state["state"] = "installed"
        _write(stage, state)


def rollback(root, *, owner_pid):
    root = _root(root)
    with _exclusive(root):
        stage = root / TX_NAME
        cleanup = _read_cleanup(root)
        if cleanup is not None:
            _assert_owner(cleanup["state"], owner_pid)
            _require(
                cleanup["state"]["state"] == "rolled-back",
                "Accepted deployment cannot be rolled back",
            )
            _resume_cleanup(root, cleanup)
            return
        if not _present(stage):
            return
        state = _read(stage)
        _assert_owner(state, owner_pid)
        _require(
            state["state"] != "accepted",
            "Accepted deployment cannot be rolled back",
        )
        _restore(root, stage, state)


def finish(root, *, owner_pid, receipt):
    root = _root(root)
    with _exclusive(root):
        stage = root / TX_NAME
        cleanup = _read_cleanup(root)
        if cleanup is not None:
            _assert_owner(cleanup["state"], owner_pid)
            _require(
                cleanup["state"]["state"] == "accepted"
                and cleanup["state"]["receipt"]
                == str(Path(os.path.abspath(receipt))),
                "Finish differs from retained acceptance",
            )
            _resume_cleanup(root, cleanup)
            return
        state = _read(stage)
        _assert_owner(state, owner_pid)
        _require(
            state["state"] in {"installed", "accepted"},
            "Deployment has not finished installing",
        )
        _require(
            all(
                _same(root / e["destination"], e["new_hash"])
                for e in state["entries"].values()
            ),
            "Installed release differs from verified bytes",
        )
        receipt = Path(os.path.abspath(receipt))
        _receipt_path(root, state, str(receipt))
        if state["state"] == "accepted":
            _require(
                state["receipt"] == str(receipt),
                "Finish differs from retained acceptance",
            )
            _clean_stage(stage)
            return
        accepted = dict(
            state,
            state="accepted",
            receipt=str(receipt),
            processes_settled=True,
        )
        _receipt_preflight(root, accepted)
        _settle_app_processes(root, stage, state, cleanup=True)
        _validate_layout(root, stage, state)
        _receipt_preflight(root, accepted)
        _write(stage, accepted)
        _clean_stage(stage)


def previous_running(root, *, owner_pid):
    root = _root(root)
    with _exclusive(root):
        stage = root / TX_NAME
        cleanup = _read_cleanup(root)
        state = cleanup["state"] if cleanup is not None else _read(stage)
        _assert_owner(state, owner_pid)
        owners = (
            _decode_process_owners({"schema": 1, "owners": cleanup["owners"]})
            if cleanup is not None
            else _process_owners(stage)
        )
        return state.get("previous_running", False) or any(
            origin == "previous" and owner.had_instance
            for origin, owner in owners
        )


def launch_intent(root, *, owner_pid):
    root = _root(root)
    with _exclusive(root):
        stage = root / TX_NAME
        state = _read(stage)
        _assert_owner(state, owner_pid)
        _require(
            state["state"] == "installed" and not state.get("launch_started"),
            "Launch requires an installed, unstarted transaction",
        )
        _validate_layout(root, stage, state)
        state.update(launch_started=True, launch_registered=False)
        _write(stage, state)


def register_launch(root, *, owner_pid, timeout=30.0):
    root = _root(root)
    with _exclusive(root):
        stage = root / TX_NAME
        state = _read(stage)
        _assert_owner(state, owner_pid)
        _require(
            state["state"] == "installed"
            and state.get("launch_started")
            and not state.get("launch_registered"),
            "Launch has no unregistered intent",
        )
        _validate_layout(root, stage, state)
        deadline = time.monotonic() + timeout
        while True:
            try:
                owner = process_stop.bootstrap(
                    root / state["entries"]["app"]["destination"],
                )
                if owner.had_instance and owner.nonce:
                    break
            except process_stop.ProcessStopError:
                pass  # Electron can precede its verified backend briefly.
            if time.monotonic() >= deadline:
                raise ValueError(
                    "New App instance identity is unproved; preserving journal and backups",
                )
            time.sleep(0.1)
        retained = _process_owners(stage)
        retained.append(("launch", owner))
        _save_process_owners(stage, retained)
        _validate_layout(root, stage, state)
        state["launch_registered"] = True
        _write(stage, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "begin",
            "promote",
            "rollback",
            "finish",
            "previous-running",
            "launch-intent",
            "register-launch",
        ),
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--owner-pid", required=True, type=int)
    parser.add_argument("--app")
    parser.add_argument("--dmg")
    parser.add_argument("--zip")
    parser.add_argument("--source-sha")
    parser.add_argument("--version")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.command == "begin":
        stage = begin(
            args.root,
            destinations={r: getattr(args, r) for r in ROLES},
            source_sha=args.source_sha or "",
            version=args.version or "",
            owner_pid=args.owner_pid,
        )
        print(stage)
    elif args.command == "promote":
        promote(
            args.root,
            sources={r: getattr(args, r) for r in ROLES},
            owner_pid=args.owner_pid,
        )
    elif args.command == "rollback":
        rollback(args.root, owner_pid=args.owner_pid)
    elif args.command == "previous-running":
        print(
            "1"
            if previous_running(args.root, owner_pid=args.owner_pid)
            else "0",
        )
    elif args.command == "launch-intent":
        launch_intent(args.root, owner_pid=args.owner_pid)
    elif args.command == "register-launch":
        register_launch(args.root, owner_pid=args.owner_pid)
    else:
        _require(
            args.receipt is not None,
            "Acceptance requires a durable receipt path",
        )
        finish(args.root, owner_pid=args.owner_pid, receipt=args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
