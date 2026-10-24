#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop one installed desktop instance, proving ownership and final settlement.

The private proof retains the instance nonce for interrupted cleanup. Public
receipts contain no nonce, argv, environment, or PID. An argv path is never an
ownership credential. Uncertain identity or incomplete scans fail closed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import plistlib
import stat
import sys
import time
import uuid


def _load_process_probe():
    # This fixed source helper also works before the source environment is synced.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from lantai.security import desktop_process_probe

    return desktop_process_probe


probe = _load_process_probe()


class ProcessStopError(RuntimeError):
    """A safe, finite reason code; never a third-party exception message."""


@dataclass(frozen=True)
class Owner:
    app: dict
    roots: list[dict]
    backends: list[dict]
    nonce: str | None
    had_instance: bool


def _fingerprint(path: Path, *, directory=False, optional=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise ProcessStopError("bundle-identity-missing") from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or path.is_symlink():
        raise ProcessStopError("bundle-identity-invalid")
    return [
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]


def app_binding(app: Path) -> dict:
    app = Path(os.path.abspath(app))
    if app.is_symlink() or app.resolve() != app:
        raise ProcessStopError("bundle-path-is-indirect")
    folder = _fingerprint(app, directory=True, optional=True)
    executable = app.stem
    plist_path = app / "Contents/Info.plist"
    plist_identity = None
    if folder is not None:
        plist_identity = _fingerprint(plist_path)
        with plist_path.open("rb") as handle:
            metadata = plistlib.load(handle)
        executable = metadata.get("CFBundleExecutable")
        if (
            metadata.get("CFBundleIdentifier") != "com.lantai.app"
            or not isinstance(executable, str)
            or not executable
            or Path(executable).name != executable
            or executable in (".", "..")
        ):
            raise ProcessStopError("bundle-metadata-invalid")
    root = app / "Contents/MacOS" / executable
    backend = (
        app
        / "Contents/Resources/lantai-server/Lantai Backend.app/Contents/MacOS/lantai-server"
    )
    return {
        "path": str(app),
        "folder": folder,
        "plist": plist_identity,
        "root_path": str(root),
        "root_file": _fingerprint(root, optional=folder is None),
        "backend_path": str(backend),
        "backend_file": _fingerprint(backend, optional=True),
    }


def _validate_binding(binding):
    if app_binding(Path(binding["path"])) != binding:
        raise ProcessStopError("bundle-identity-changed")


def _same_user(process) -> bool:
    return process.uids().effective == os.geteuid()


def scan_app(binding: dict) -> list[dict]:
    result = []
    paths = {binding["root_path"]: "root", binding["backend_path"]: "backend"}
    for pid in probe.psutil.pids():
        try:
            process = probe.psutil.Process(pid)
            if not _same_user(process) or process.status() in (
                probe.psutil.STATUS_ZOMBIE,
                probe.psutil.STATUS_DEAD,
            ):
                continue
            birth = str(process.create_time())
            executable = process.exe()
            kind = paths.get(executable)
            if (
                kind is None
                and Path(binding["path"]) in Path(executable).parents
            ):
                kind = "helper"
            if kind is not None and process.is_running():
                record = {"pid": pid, "birth": birth, "kind": kind}
                if kind == "helper":
                    record.update(
                        exe=executable,
                        file=_fingerprint(Path(executable)),
                    )
                result.append(record)
        except probe.psutil.NoSuchProcess:
            continue
        # AccessDenied must not turn an incomplete inventory into empty success.
    return result


def _nonce(record, *, vanished_ok=False):
    try:
        process = probe.psutil.Process(record["pid"])
        if (
            str(process.create_time()) != record["birth"]
            or not process.is_running()
            or process.status()
            in (probe.psutil.STATUS_ZOMBIE, probe.psutil.STATUS_DEAD)
        ):
            if vanished_ok:
                return None
            raise ProcessStopError("instance-changed-during-bootstrap")
        value = process.environ().get(probe.OWNER_ENV)
        if not process.is_running():
            if vanished_ok:
                return None
            raise ProcessStopError("instance-changed-during-bootstrap")
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ProcessStopError("backend-instance-identity-missing")
        return value
    except probe.psutil.NoSuchProcess:
        if vanished_ok:
            return None
        raise ProcessStopError("instance-changed-during-bootstrap") from None


def _bootstrap_retained_owner(
    app,
    binding,
    records,
    roots,
    backends,
    helpers,
    nonces,
    retained,
):
    # A durable first inventory can outlive the backend/parent. Admit
    # only its exact roots/helpers; a new instance gets no permission
    # from another process's saved proof.
    for owner in retained:
        locate_owner(owner, [app])
    known = {
        (r["pid"], r["birth"]): (owner, r)
        for owner in retained
        for r in owner.roots
    }
    known_nonces = {owner.nonce for owner in retained}
    if (
        len(known_nonces) != 1
        or None in known_nonces
        or not nonces <= known_nonces
    ):
        raise ProcessStopError("new-backend-instance")
    for record in roots + helpers:
        saved = known.get((record["pid"], record["birth"]))
        if saved is None or record["kind"] != saved[1]["kind"]:
            raise ProcessStopError("new-desktop-instance")
        owner, original = saved
        if record["kind"] == "helper":
            relocated = str(
                app / Path(original["exe"]).relative_to(owner.app["path"]),
            )
            if (
                record["exe"] != relocated
                or record["file"] != original["file"]
            ):
                raise ProcessStopError("desktop-helper-file-changed")
    _validate_binding(binding)
    if not records:
        return Owner(binding, [], [], None, False)
    saved_backends = list(backends)
    for owner in retained:
        for backend in owner.backends:
            if backend not in saved_backends:
                saved_backends.append(backend)
    return Owner(
        binding,
        roots + helpers,
        saved_backends,
        next(iter(known_nonces)),
        True,
    )


def bootstrap(app: Path, *, retained=()) -> Owner:
    try:
        binding = app_binding(app)
        records = scan_app(binding)
        roots = [record for record in records if record["kind"] == "root"]
        backends = [
            record for record in records if record["kind"] == "backend"
        ]
        helpers = [record for record in records if record["kind"] == "helper"]
        if len(roots) > 1:
            raise ProcessStopError("multiple-desktop-instances")
        nonces = {_nonce(record) for record in backends}
        if len(nonces) > 1:
            raise ProcessStopError("multiple-backend-instances")
        if retained:
            return _bootstrap_retained_owner(
                app,
                binding,
                records,
                roots,
                backends,
                helpers,
                nonces,
                retained,
            )
        if roots and not backends:
            raise ProcessStopError("desktop-backend-identity-unavailable")
        if roots:
            root = roots[0]
            for backend in backends:
                ancestors = probe.psutil.Process(backend["pid"]).parents()
                if not any(
                    p.pid == root["pid"]
                    and str(p.create_time()) == root["birth"]
                    for p in ancestors
                ):
                    raise ProcessStopError("backend-is-not-desktop-child")
        anchors = {(r["pid"], r["birth"]) for r in roots + backends}
        for helper in helpers:
            ancestors = probe.psutil.Process(helper["pid"]).parents()
            if not any(
                (p.pid, str(p.create_time())) in anchors for p in ancestors
            ):
                raise ProcessStopError("desktop-helper-ownership-unproved")
        if helpers and not backends:
            raise ProcessStopError("desktop-backend-identity-unavailable")
        _validate_binding(binding)
        return Owner(
            binding,
            roots + helpers,
            backends,
            next(iter(nonces), None),
            bool(records),
        )
    except (probe.psutil.Error, OSError, ValueError, TypeError, KeyError):
        raise ProcessStopError("instance-bootstrap-unproved") from None


def _root_signal(owner: Owner, record: dict, name: str, *, binding=None):
    binding = binding or owner.app
    try:
        process = probe.psutil.Process(record["pid"])
        if str(process.create_time()) != record["birth"]:
            return
        if process.status() in (
            probe.psutil.STATUS_ZOMBIE,
            probe.psutil.STATUS_DEAD,
        ):
            return
        original = record.get("exe", owner.app["root_path"])
        relocated = str(
            Path(binding["path"])
            / Path(original).relative_to(owner.app["path"]),
        )
        if not _same_user(process) or process.exe() not in {
            original,
            relocated,
        }:
            raise ProcessStopError("desktop-root-identity-changed")
        _validate_binding(binding)
        if (
            record["kind"] == "helper"
            and _fingerprint(Path(relocated)) != record["file"]
        ):
            raise ProcessStopError("desktop-helper-file-changed")
        # psutil's mutations recheck the PID/birth pair before sending a signal.
        if name == "SIGKILL":
            process.kill()
        else:
            import signal

            process.send_signal(signal.SIGTERM)
    except probe.psutil.NoSuchProcess:
        return


def _validate_running_instances(owner, instances, registered_roots):
    for record in instances:
        if record["kind"] in ("root", "helper"):
            if (record["pid"], record["birth"]) not in registered_roots:
                raise ProcessStopError("new-desktop-instance")
        else:
            nonce = _nonce(record, vanished_ok=True)
            if nonce is not None and (
                owner.nonce is None or nonce != owner.nonce
            ):
                raise ProcessStopError("new-backend-instance")


def _live_registered_roots(owner):
    roots = []
    for record in owner.roots:
        try:
            process = probe.psutil.Process(record["pid"])
            if (
                str(process.create_time()) == record["birth"]
                and process.is_running()
                and process.status()
                not in (probe.psutil.STATUS_ZOMBIE, probe.psutil.STATUS_DEAD)
            ):
                roots.append(record)
        except probe.psutil.NoSuchProcess:
            continue
    return roots


def settle(
    owner: Owner,
    *,
    term_timeout=3.0,
    kill_timeout=2.0,
    binding=None,
    registered_only=False,
) -> None:
    """Return only after two complete empty scans and all registered root exits."""
    started = time.monotonic()
    term_end = started + term_timeout
    end = term_end + kill_timeout
    sent_term = set()
    empty_scans = 0
    registered_roots = {
        (record["pid"], record["birth"]) for record in owner.roots
    }
    binding = binding or owner.app
    try:
        while True:
            _validate_binding(binding)
            # libproc may keep the original executable path after a rename.
            # A registered root must be checked directly by PID and birth.
            instances = [] if registered_only else scan_app(binding)
            _validate_running_instances(owner, instances, registered_roots)
            records = (
                [
                    record
                    for record in probe.snapshot(owner.nonce)
                    if record["owned"] and record["alive"]
                ]
                if owner.nonce is not None
                else []
            )
            roots = _live_registered_roots(owner)
            if not records and not roots:
                empty_scans += 1
                if empty_scans >= 2:
                    _validate_binding(binding)
                    return
            else:
                empty_scans = 0
            now = time.monotonic()
            if now >= end:
                raise ProcessStopError("owned-processes-unsettled")
            name = "SIGTERM" if now < term_end else "SIGKILL"
            requests = []
            for record in records:
                key = (record["pid"], record["birth"])
                if name == "SIGTERM" and key in sent_term:
                    continue
                requests.append(
                    {
                        "pid": record["pid"],
                        "birth": record["birth"],
                        "signal": name,
                    },
                )
                sent_term.add(key)
            if requests:
                _validate_binding(binding)
                probe.signal_owned(owner.nonce, requests)
            for root in roots:
                key = (root["pid"], root["birth"])
                if name == "SIGTERM" and key in sent_term:
                    continue
                _root_signal(owner, root, name, binding=binding)
                sent_term.add(key)
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
    except (probe.psutil.Error, OSError, ValueError, TypeError, KeyError):
        raise ProcessStopError("process-settlement-unproved") from None


def locate_owner(owner: Owner, candidates: list[Path]) -> dict:
    """Relocation is authorized only by the same bundle inode and file identity."""
    original = owner.app
    matches = []
    for candidate in candidates:
        try:
            current = app_binding(candidate)
        except (ProcessStopError, OSError, ValueError, TypeError):
            continue  # An unrelated incomplete copy cannot authorize relocation.
        if (
            original["folder"] is not None
            and current["folder"] is not None
            and current["folder"][:2] == original["folder"][:2]
            and all(
                current[key] == original[key]
                for key in ("plist", "root_file", "backend_file")
            )
        ):
            matches.append(current)
    if len(matches) != 1:
        raise ProcessStopError("registered-bundle-relocation-unproved")
    return matches[0]


def settle_registered(
    owner: Owner,
    candidates: list[Path],
    *,
    term_timeout=3.0,
    kill_timeout=2.0,
):
    """Settle the retained identity without adopting a new target instance."""
    binding = locate_owner(owner, candidates)
    settle(
        owner,
        binding=binding,
        registered_only=True,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )


def ensure_absent(app: Path):
    """An incomplete staged copy can be removed only after two complete scans.

    No process can be adopted or signalled without valid bundle metadata.
    """
    app = Path(os.path.abspath(app))
    if app.resolve() != app or app.is_symlink():
        raise ProcessStopError("bundle-path-is-indirect")
    before = _fingerprint(app, directory=True, optional=True)
    try:
        for _ in range(2):
            for pid in probe.psutil.pids():
                try:
                    process = probe.psutil.Process(pid)
                    if (
                        _same_user(process)
                        and process.status()
                        not in (
                            probe.psutil.STATUS_ZOMBIE,
                            probe.psutil.STATUS_DEAD,
                        )
                        and app in Path(process.exe()).parents
                        and process.is_running()
                    ):
                        raise ProcessStopError(
                            "incomplete-bundle-has-live-process",
                        )
                except probe.psutil.NoSuchProcess:
                    continue
            time.sleep(0.05)
        if _fingerprint(app, directory=True, optional=True) != before:
            raise ProcessStopError("bundle-identity-changed")
    except probe.psutil.Error:
        raise ProcessStopError(
            "incomplete-bundle-inventory-unproved",
        ) from None


def _ensure_owner_absent(owner):
    for record in owner.roots:
        try:
            process = probe.psutil.Process(record["pid"])
            if (
                str(process.create_time()) == record["birth"]
                and process.is_running()
                and process.status()
                not in (probe.psutil.STATUS_ZOMBIE, probe.psutil.STATUS_DEAD)
            ):
                raise ProcessStopError("cleanup-registered-process-still-live")
        except probe.psutil.NoSuchProcess:
            continue
    if owner.nonce and any(
        r["alive"] and r["owned"] for r in probe.snapshot(owner.nonce)
    ):
        raise ProcessStopError("cleanup-owned-process-still-live")


def ensure_cleanup_absent(stage: Path, owners):
    """Resume deletion only with complete empty inventories, without signals.

    After partial deletion a bundle cannot grant new signal authority. Retained
    PID/birth and nonce checks also cover renamed or detached old executables.
    The accepted target's Owner is deliberately excluded by the caller.
    """
    try:
        for _ in range(2):
            for owner in owners:
                _ensure_owner_absent(owner)
            ensure_absent(stage)
    except (probe.psutil.Error, OSError, ValueError, TypeError, KeyError):
        raise ProcessStopError("cleanup-inventory-unproved") from None


def _private_parent(path: Path):
    if path.parent.resolve() != path.parent:
        raise ProcessStopError("proof-directory-is-indirect")
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ProcessStopError("proof-directory-is-not-private")
    return fd


def _read_private(path: Path):
    directory = _private_parent(path)
    try:
        try:
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 1024 * 1024
            ):
                raise ProcessStopError("proof-file-is-not-private")
            return json.load(handle)
    finally:
        os.close(directory)


def _write_private(path: Path, payload):
    directory = _private_parent(path)
    temporary = ".process-proof-" + uuid.uuid4().hex
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _validate_proof_app(app):
    if not isinstance(app, dict) or set(app) != {
        "path",
        "folder",
        "plist",
        "root_path",
        "root_file",
        "backend_path",
        "backend_file",
    }:
        raise ProcessStopError("proof-bundle-invalid")
    for key in ("path", "root_path", "backend_path"):
        if (
            not isinstance(app[key], str)
            or not Path(app[key]).is_absolute()
            or str(Path(os.path.abspath(app[key]))) != app[key]
        ):
            raise ProcessStopError("proof-bundle-invalid")
    if Path(app["root_path"]).parent != Path(
        app["path"],
    ) / "Contents/MacOS" or app["backend_path"] != str(
        Path(app["path"])
        / "Contents/Resources/lantai-server/Lantai Backend.app/Contents/MacOS/lantai-server",
    ):
        raise ProcessStopError("proof-bundle-invalid")
    for key in ("folder", "plist", "root_file", "backend_file"):
        fp = app[key]
        if fp is not None and (
            not isinstance(fp, list)
            or len(fp) != 5
            or any(type(v) is not int or v < 0 for v in fp)
        ):
            raise ProcessStopError("proof-bundle-invalid")


def _validate_proof_helper(app, record):
    if not isinstance(record.get("exe"), str):
        raise ProcessStopError("proof-helper-invalid")
    if (
        Path(app["path"]) not in Path(record["exe"]).parents
        or str(Path(os.path.abspath(record["exe"]))) != record["exe"]
    ):
        raise ProcessStopError("proof-helper-invalid")
    if (
        not isinstance(record.get("file"), list)
        or len(record["file"]) != 5
        or any(type(v) is not int or v < 0 for v in record["file"])
    ):
        raise ProcessStopError("proof-helper-invalid")


def _validate_proof_records(app, roots, backends):
    for records, kinds in (
        (roots, {"root", "helper"}),
        (backends, {"backend"}),
    ):
        for record in records:
            if (
                not isinstance(record, dict)
                or record.get("kind") not in kinds
                or type(record.get("pid")) is not int
                or record["pid"] <= 0
                or not isinstance(record.get("birth"), str)
            ):
                raise ProcessStopError("proof-process-invalid")
            if record["kind"] == "helper":
                _validate_proof_helper(app, record)


def owner_from_dict(value: dict) -> Owner:
    """Validate private proof data without adopting any process it describes."""
    try:
        if not isinstance(value, dict) or set(value) != {
            "app",
            "roots",
            "backends",
            "nonce",
            "had_instance",
        }:
            raise ProcessStopError("proof-owner-invalid")
        owner = Owner(**value)
        app = owner.app
        _validate_proof_app(app)
        if (
            type(owner.had_instance) is not bool
            or not isinstance(owner.roots, list)
            or not isinstance(owner.backends, list)
        ):
            raise ProcessStopError("proof-owner-invalid")
        _validate_proof_records(app, owner.roots, owner.backends)
        if owner.nonce is not None and (
            not isinstance(owner.nonce, str)
            or not owner.nonce
            or len(owner.nonce) > 256
            or not owner.backends
        ):
            raise ProcessStopError("proof-nonce-invalid")
        if (
            bool(owner.roots or owner.backends or owner.nonce)
            != owner.had_instance
            or owner.had_instance
            and owner.nonce is None
        ):
            raise ProcessStopError("proof-instance-unproved")
        return owner
    except (TypeError, ValueError, KeyError):
        raise ProcessStopError("proof-owner-invalid") from None


def stop_instance(app: Path, proof: Path, receipt: Path) -> dict:
    owner = None
    try:
        value = _read_private(proof)
        if value is None:
            owner = bootstrap(app)
            _write_private(proof, {"schema": 1, "owner": asdict(owner)})
        else:
            if value.get("schema") != 1 or not isinstance(
                value.get("owner"),
                dict,
            ):
                raise ProcessStopError("proof-schema-invalid")
            owner = owner_from_dict(value["owner"])
            if owner.app.get("path") != str(Path(os.path.abspath(app))):
                raise ProcessStopError("proof-owner-invalid")
        settle(owner)
        result = {
            "schema": 1,
            "settled": True,
            "hadInstance": owner.had_instance,
        }
        _write_private(receipt, result)
        return result
    except (
        ProcessStopError,
        probe.psutil.Error,
        OSError,
        ValueError,
        TypeError,
        KeyError,
    ):
        # Keep the first owner proof for retry; never serialize an exception.
        _write_private(
            receipt,
            {
                "schema": 1,
                "settled": False,
                "hadInstance": bool(owner and owner.had_instance),
            },
        )
        raise ProcessStopError("desktop-stop-unproved") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = stop_instance(args.app, args.proof, args.receipt)
        print("running" if result["hadInstance"] else "stopped")
        return 0
    except (ProcessStopError, OSError):
        print(
            "Error: App process ownership or final settlement is unproved; "
            "retained private proof and deployment backups require inspection.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
