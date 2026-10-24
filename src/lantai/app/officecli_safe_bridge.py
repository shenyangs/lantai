# -*- coding: utf-8 -*-
"""Transactional safety boundary for OfficeCLI MCP write operations."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..constant import WORKING_DIR


READ_ONLY_VERBS = frozenset(
    {
        "help",
        "load_skill",
        "view",
        "get",
        "query",
        "validate",
        "raw",
        "dump",
    },
)
WRITE_VERBS = frozenset(
    {
        "create",
        "merge",
        "set",
        "add",
        "remove",
        "move",
        "swap",
        "refresh",
        "raw-set",
        "add-part",
        "batch",
        "import",
    },
)
BLOCKED_VERBS = frozenset(
    {
        "open",
        "save",
        "close",
        "watch",
        "unwatch",
        "plugins",
        "skills",
        "install",
        "mcp",
    },
)
SUPPORTED_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})


class OfficeCliSafetyError(RuntimeError):
    """The OfficeCLI request cannot cross the transactional boundary safely."""


@dataclass(frozen=True)
class OfficeCliTransaction:
    arguments: dict[str, Any]
    verb: str
    target_path: Path
    working_path: Path
    temporary_dir: Path
    baseline_sha256: str | None
    existed: bool


@dataclass(frozen=True)
class OfficeCliDeliveryReceipt:
    target_path: str
    backup_path: str
    before_sha256: str
    after_sha256: str
    validation: str = "OpenXML validate passed"
    atomic: bool = True

    def format_for_tool(self) -> str:
        backup = self.backup_path or "(new file; no prior version)"
        before = self.before_sha256 or "(new file)"
        return (
            "Lantai Office delivery receipt\n"
            f"- target: {self.target_path}\n"
            f"- validation: {self.validation}\n"
            f"- atomic replace: {'yes' if self.atomic else 'no'}\n"
            f"- before sha256: {before}\n"
            f"- after sha256: {self.after_sha256}\n"
            f"- rollback backup: {backup}"
        )


_LOCKS_GUARD = threading.Lock()
_COMMIT_LOCKS: dict[str, threading.Lock] = {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_argv(arguments: dict[str, Any]) -> list[str]:
    raw = arguments.get("command")
    if isinstance(raw, str):
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            raise OfficeCliSafetyError(f"Invalid OfficeCLI command: {exc}") from exc
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        argv = list(raw)
    else:
        raise OfficeCliSafetyError("OfficeCLI command must be a string or string array")
    if argv and Path(argv[0]).name.lower() == "officecli":
        argv = argv[1:]
    if not argv:
        raise OfficeCliSafetyError("OfficeCLI command is empty")
    return argv


def _target_argument_index(verb: str, argv: list[str]) -> int:
    if verb == "merge":
        if len(argv) < 3:
            raise OfficeCliSafetyError("OfficeCLI merge requires template and output paths")
        return 2
    if len(argv) < 2:
        raise OfficeCliSafetyError(f"OfficeCLI {verb} requires a document path")
    return 1


def prepare_officecli_mcp_call(
    arguments: dict[str, Any] | None,
) -> OfficeCliTransaction | None:
    """Rewrite a mutating OfficeCLI MCP command onto an isolated working copy."""

    original = dict(arguments or {})
    argv = _command_argv(original)
    verb = argv[0].lower()
    if verb in READ_ONLY_VERBS:
        return None
    if verb in BLOCKED_VERBS:
        raise OfficeCliSafetyError(
            f"OfficeCLI '{verb}' is blocked in Agent mode; use a read command or one "
            "transactional write command instead.",
        )
    if verb not in WRITE_VERBS:
        raise OfficeCliSafetyError(
            f"OfficeCLI verb '{verb}' is not registered as a real safe capability",
        )

    target_index = _target_argument_index(verb, argv)
    requested = Path(argv[target_index]).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    target = requested.resolve(strict=False)
    if target.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise OfficeCliSafetyError("OfficeCLI writes support only .docx, .xlsx and .pptx")
    existed = target.is_file()
    if not existed and verb not in {"create", "merge"}:
        raise OfficeCliSafetyError(f"Office target does not exist: {target}")
    if existed and not target.is_file():
        raise OfficeCliSafetyError(f"Office target is not a regular file: {target}")
    if not target.parent.is_dir():
        raise OfficeCliSafetyError(f"Office target directory does not exist: {target.parent}")

    temporary_dir = Path(tempfile.mkdtemp(prefix="lantai_office_txn_"))
    working = temporary_dir / target.name
    baseline = _sha256(target) if existed else None
    if existed:
        shutil.copy2(target, working)
    argv[target_index] = str(working)
    rewritten = dict(original)
    rewritten["command"] = argv
    return OfficeCliTransaction(
        arguments=rewritten,
        verb=verb,
        target_path=target,
        working_path=working,
        temporary_dir=temporary_dir,
        baseline_sha256=baseline,
        existed=existed,
    )


def commit_officecli_transaction(
    transaction: OfficeCliTransaction,
) -> OfficeCliDeliveryReceipt:
    """Hash-check, back up, and atomically publish a validated working copy."""

    if not transaction.working_path.is_file():
        raise OfficeCliSafetyError("OfficeCLI did not produce a deliverable file")
    key = str(transaction.target_path)
    with _LOCKS_GUARD:
        lock = _COMMIT_LOCKS.setdefault(key, threading.Lock())
    with lock:
        current_hash = _sha256(transaction.target_path) if transaction.target_path.is_file() else None
        if current_hash != transaction.baseline_sha256:
            raise OfficeCliSafetyError(
                "The Office source changed while the Agent was working; the result was not committed.",
            )
        after_hash = _sha256(transaction.working_path)
        backup_path = ""
        if transaction.existed:
            backup_dir = WORKING_DIR / "backups" / "officecli"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / (
                f"{int(time.time())}-{uuid.uuid4().hex[:8]}-{transaction.target_path.name}"
            )
            shutil.copy2(transaction.target_path, backup)
            backup_path = str(backup)

        stage = transaction.target_path.parent / (
            f".{transaction.target_path.name}.lantai-stage-{uuid.uuid4().hex}"
        )
        try:
            shutil.copy2(transaction.working_path, stage)
            with stage.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(stage, transaction.target_path)
        finally:
            stage.unlink(missing_ok=True)

        return OfficeCliDeliveryReceipt(
            target_path=str(transaction.target_path),
            backup_path=backup_path,
            before_sha256=transaction.baseline_sha256 or "",
            after_sha256=after_hash,
        )


def cleanup_officecli_transaction(transaction: OfficeCliTransaction) -> None:
    shutil.rmtree(transaction.temporary_dir, ignore_errors=True)
