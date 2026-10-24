# -*- coding: utf-8 -*-
"""Helpers for creating backups: agents, global config, secrets, skill pool."""
from __future__ import annotations

import json
import logging
import re
import zipfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .._utils.constants import (
    PREFIX_CONFIG,
    PREFIX_SECRETS,
    PREFIX_SKILL_POOL,
    PREFIX_WORKSPACES,
)
from ...constant import CONFIG_FILE, SECRET_DIR, WORKING_DIR
from ..integrity import (
    BackupFileIntegrity,
    write_bytes_with_integrity,
    write_path_with_integrity,
)

logger = logging.getLogger(__name__)

REDACTED_SECRET_VALUE = "__LANTAI_REDACTED_SECRET__"
REDACTED_FILE_BYTES = b"__LANTAI_REDACTED_FILE__\n"
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|refresh_?token|token|secret|"
    r"client_?secret|password|passwd|authorization|auth|bearer|cookie|"
    r"credential|private_?key|webhook|dsn|connection_?string)(?:$|_)",
    re.IGNORECASE,
)
_SENSITIVE_FILE = re.compile(
    r"(?:^|/)(?:\.env(?:\..*)?|\.npmrc|\.pypirc|\.netrc|"
    r"\.git-credentials|auth(?:\..*)?|credentials?(?:\..*)?|"
    r"secrets?(?:\..*)?|cookies?(?:\..*)?|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\..*)?|"
    r"[^/]+\.(?:pem|key|p12|pfx))$",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    rb"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    rb"client[_-]?secret|private[_-]?key|password|passwd|authorization|"
    rb"auth|bearer|cookie|credential|webhook(?:[_-]?url)?|"
    rb"(?:bot|auth)[_-]?token|token|dsn|connection[_-]?string)"
    rb"\s*[\"']?\s*[:=]\s*[\"']?"
    rb"(?!(?:none|null|false|true|redacted)\b)[A-Za-z0-9_./+=-]{8,}",
    re.IGNORECASE,
)
_SECRET_SIGNATURE = re.compile(
    rb"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----|"
    rb"(?:sk|ghp|gho|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}|"
    rb"AKIA[0-9A-Z]{16}|"
    rb"AIza[0-9A-Za-z_-]{20,}|"
    rb"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{12,}|"
    rb"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]{4,}@",
    re.IGNORECASE,
)
_SECRET_SCAN_CHUNK = 1024 * 1024
_SECRET_SCAN_OVERLAP = 512
_STRUCTURED_TEXT_SUFFIXES = {".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}
_STRUCTURED_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:-\s*)?[\"']?)(?P<key>[A-Za-z0-9_.-]+)"
    r"(?P<separator>[\"']?\s*[:=]\s*)(?P<value>.*?)(?P<newline>\r?\n)?$"
)


def _is_sensitive_key(key: object) -> bool:
    raw = str(key)
    # Preserve word boundaries for camelCase/PascalCase credential keys such
    # as apiKey, accessToken and clientSecret before applying the matcher.
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    separated = re.sub(r"[^A-Za-z0-9]+", "_", separated)
    return bool(_SENSITIVE_KEY.search(separated))


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED_SECRET_VALUE
            if _is_sensitive_key(key)
            else _redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str) and _SECRET_SIGNATURE.search(
        value.encode("utf-8")
    ):
        return REDACTED_SECRET_VALUE
    return value


def _contains_secret_material(path: Path) -> bool:
    """Scan a regular file for common credential assignments and signatures."""
    try:
        tail = b""
        with path.open("rb") as handle:
            while chunk := handle.read(_SECRET_SCAN_CHUNK):
                sample = tail + chunk
                if _SECRET_ASSIGNMENT.search(
                    sample
                ) or _SECRET_SIGNATURE.search(sample):
                    return True
                tail = sample[-_SECRET_SCAN_OVERLAP:]
    except OSError:
        # The caller already handles unreadable files consistently with the
        # existing backup flow; this scanner must not turn a read race into a
        # false claim that content was redacted.
        return False
    return False


def _redact_structured_text(path: Path) -> bytes | None:
    if path.suffix.lower() not in _STRUCTURED_TEXT_SUFFIXES:
        return None
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    changed = False
    output: list[str] = []
    for line in source.splitlines(keepends=True):
        match = _STRUCTURED_ASSIGNMENT.match(line)
        if match and _is_sensitive_key(match.group("key")):
            output.append(
                f"{match.group('prefix')}{match.group('key')}"
                f"{match.group('separator')}{REDACTED_SECRET_VALUE}"
                f"{match.group('newline') or ''}"
            )
            changed = True
        else:
            output.append(line)
    if not changed:
        return None
    return "".join(output).encode("utf-8")


def sanitized_file_payload(path: Path, relative_path: str) -> bytes | None:
    """Return redacted bytes or ``None`` when a file is unchanged."""
    if path.is_symlink() or _SENSITIVE_FILE.search(
        relative_path.replace("\\", "/")
    ):
        return REDACTED_FILE_BYTES
    if path.suffix.lower() == ".json":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            value = None
        if value is not None:
            redacted = _redact_json(value)
            if redacted != value:
                return json.dumps(
                    redacted,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
    structured = _redact_structured_text(path)
    if structured is not None:
        remaining = structured.replace(
            REDACTED_SECRET_VALUE.encode("utf-8"),
            b"redacted",
        )
        if _SECRET_ASSIGNMENT.search(remaining) or _SECRET_SIGNATURE.search(
            remaining
        ):
            return REDACTED_FILE_BYTES
        return structured
    if _contains_secret_material(path):
        return REDACTED_FILE_BYTES
    return None


def add_agent_workspaces(
    zf: zipfile.ZipFile,
    valid_agents: list[tuple[str, Any]],
    progress_callback=None,
    stop_event=None,
    integrity_entries: list[BackupFileIntegrity] | None = None,
    sanitized: bool = False,
) -> bool:
    """Add each agent's workspace directory to the zip.

    Returns:
        False if stop_event was set (cancelled), True otherwise.
    """
    total = len(valid_agents)
    logger.info(
        "Backing up %d agent(s): %s",
        total,
        [aid for aid, _ in valid_agents],
    )

    for i, (aid, ref) in enumerate(valid_agents):
        if stop_event and stop_event.is_set():
            return False

        if progress_callback:
            progress_callback(i, total, aid)

        ws = Path(ref.workspace_dir).expanduser().resolve()
        if ws.is_dir():
            file_count = 0
            for entry in sorted(ws.rglob("*")):
                if entry.is_file():
                    try:
                        entry.resolve().relative_to(ws)
                    except (OSError, ValueError):
                        logger.warning(
                            "Skipping workspace file outside root: %s",
                            entry,
                        )
                        continue
                    rel = entry.relative_to(ws).as_posix()
                    databases = {"reading/library.sqlite3", ".lantai/research/index.sqlite3"}
                    if any(rel == database + suffix for database in databases
                           for suffix in ("-wal", "-shm", "-journal")):
                        continue
                    arcname = f"{PREFIX_WORKSPACES}{aid}/{rel}"
                    snapshot = nullcontext(entry)
                    if rel in databases:
                        from ...app.reading.backup import reading_snapshot

                        snapshot = reading_snapshot(entry, stop_event)
                    try:
                        with snapshot as source:
                            payload = (
                                sanitized_file_payload(source, rel)
                                if sanitized
                                else None
                            )
                            if payload is not None and integrity_entries is not None:
                                write_bytes_with_integrity(
                                    zf, payload, arcname, integrity_entries
                                )
                            elif payload is not None:
                                zf.writestr(arcname, payload)
                            elif integrity_entries is None:
                                zf.write(source, arcname)
                            else:
                                write_path_with_integrity(
                                    zf,
                                    source,
                                    arcname,
                                    integrity_entries,
                                )
                    except InterruptedError:
                        return False
                    file_count += 1
            logger.debug(
                "Agent '%s': %d file(s) added from %s",
                aid,
                file_count,
                ws,
            )
        else:
            logger.warning(
                "Agent '%s' workspace directory not found: %s",
                aid,
                ws,
            )

    return not (stop_event and stop_event.is_set())


def add_global_config(
    zf: zipfile.ZipFile,
    integrity_entries: list[BackupFileIntegrity] | None = None,
    sanitized: bool = False,
) -> None:
    """Add the global config file to the zip."""
    cfg_src = WORKING_DIR / CONFIG_FILE
    if cfg_src.is_file():
        payload = (
            sanitized_file_payload(cfg_src, CONFIG_FILE) if sanitized else None
        )
        if payload is not None and integrity_entries is not None:
            write_bytes_with_integrity(
                zf, payload, PREFIX_CONFIG, integrity_entries
            )
        elif payload is not None:
            zf.writestr(PREFIX_CONFIG, payload)
        elif integrity_entries is None:
            zf.write(cfg_src, PREFIX_CONFIG)
        else:
            write_path_with_integrity(
                zf,
                cfg_src,
                PREFIX_CONFIG,
                integrity_entries,
            )
        logger.info("Global config added to backup: %s", cfg_src)
    else:
        logger.warning(
            "include_global_config=True but config file not found: %s",
            cfg_src,
        )


def add_secrets(
    zf: zipfile.ZipFile,
    stop_event=None,
    integrity_entries: list[BackupFileIntegrity] | None = None,
) -> bool:
    """Add all files from the secrets directory to the zip.

    Returns ``False`` if *stop_event* was set before or during the operation
    (cancelled), ``True`` otherwise.
    """
    if not SECRET_DIR.is_dir():
        logger.warning("Secrets directory not found: %s", SECRET_DIR)
        return True
    file_count = 0
    for entry in sorted(SECRET_DIR.rglob("*")):
        if stop_event and stop_event.is_set():
            return False
        if entry.is_file():
            arcname = (
                f"{PREFIX_SECRETS}{entry.relative_to(SECRET_DIR).as_posix()}"
            )
            if integrity_entries is None:
                zf.write(entry, arcname)
            else:
                write_path_with_integrity(
                    zf,
                    entry,
                    arcname,
                    integrity_entries,
                )
            file_count += 1
    logger.info(
        "Secrets backed up: %d file(s) from %s",
        file_count,
        SECRET_DIR,
    )
    return True


def add_skill_pool(
    zf: zipfile.ZipFile,
    stop_event=None,
    integrity_entries: list[BackupFileIntegrity] | None = None,
    sanitized: bool = False,
) -> bool:
    """Add all files from the skill pool directory to the zip.

    Returns ``False`` if *stop_event* was set before or during the operation
    (cancelled), ``True`` otherwise.
    """
    from ...agents.skills_manager import get_skill_pool_dir

    skill_pool_dir = get_skill_pool_dir()
    if not skill_pool_dir.is_dir():
        logger.warning("Skill pool directory not found: %s", skill_pool_dir)
        return True
    logger.info("Backing up skill pool from %s", skill_pool_dir)
    file_count = 0
    for entry in sorted(skill_pool_dir.rglob("*")):
        if stop_event and stop_event.is_set():
            return False
        if entry.is_file():
            rel = entry.relative_to(skill_pool_dir).as_posix()
            arcname = f"{PREFIX_SKILL_POOL}{rel}"
            logger.debug("  Adding %s", arcname)
            payload = sanitized_file_payload(entry, rel) if sanitized else None
            if payload is not None and integrity_entries is not None:
                write_bytes_with_integrity(
                    zf, payload, arcname, integrity_entries
                )
            elif payload is not None:
                zf.writestr(arcname, payload)
            elif integrity_entries is None:
                zf.write(entry, arcname)
            else:
                write_path_with_integrity(
                    zf,
                    entry,
                    arcname,
                    integrity_entries,
                )
            file_count += 1
    logger.info("Skill pool backed up: %d file(s)", file_count)
    return True


def add_files_to_zip(
    zf: zipfile.ZipFile,
    meta,
    progress_callback=None,
    stop_event=None,
    valid_agents=None,
    integrity_entries: list[BackupFileIntegrity] | None = None,
) -> list[str]:
    """Add files to zip based on backup scope.

    Args:
        zf: ZipFile object to write to
        meta: Backup metadata with scope information
        progress_callback: Optional callback(current_index, total, agent_id)
        stop_event: Optional threading.Event to support cancellation
        valid_agents: Pre-computed ``(aid, ref)`` pairs to back up.
                      Empty list when meta.scope.include_agents is False.

    Returns:
        List of agent IDs that were backed up, or empty list if cancelled
    """
    if valid_agents is None:
        valid_agents = []

    if valid_agents and not add_agent_workspaces(
        zf,
        valid_agents,
        progress_callback,
        stop_event,
        integrity_entries,
        sanitized=bool(getattr(meta.scope, "sanitized", False)),
    ):
        return []

    if meta.scope.include_global_config:
        add_global_config(
            zf,
            integrity_entries,
            sanitized=bool(getattr(meta.scope, "sanitized", False)),
        )
    if meta.scope.include_secrets:
        if not add_secrets(zf, stop_event, integrity_entries):
            return []
    if meta.scope.include_skill_pool:
        if not add_skill_pool(
            zf,
            stop_event,
            integrity_entries,
            sanitized=bool(getattr(meta.scope, "sanitized", False)),
        ):
            return []

    return [aid for aid, _ in valid_agents]
