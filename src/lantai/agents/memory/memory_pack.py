# -*- coding: utf-8 -*-
"""Portable, checksummed memory packs backed by Lantai's memory model.

The pack contains durable memory sources, including original messages retained
as memory evidence. Complete chat transcripts, credentials, retrieval indexes,
tool-result offload caches, and runtime state are excluded.
Adoption is replace-based, requires an explicit confirmation, creates a full
recovery pack first, and swaps each memory root atomically.
"""

from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...backup.integrity import (
    BackupFileIntegrity,
    BackupIntegrityError,
    verify_zip_integrity,
    write_bytes_with_integrity,
    write_integrity_manifest,
    write_path_with_integrity,
)
from ...retrieval.local_index import SQLiteRetrievalIndex
from ...utils.authoritative_store import write_authoritative
from .canonical_ledger import CanonicalMemoryLedger, _normalize as _normalize_fact
from .maintenance import daily_memory_dir
from .locking import memory_state_lock, memory_transaction_journal
from ...utils.path_transaction import path_transaction

MEMORY_PACK_SCHEMA_VERSION = 2
MEMORY_PACK_ADOPT_CONFIRMATION = "采用记忆包"
MEMORY_PACK_MAX_FILES = 20_000
MEMORY_PACK_MAX_TOTAL_BYTES = 256 * 1024 * 1024
MEMORY_PACK_MAX_FILE_BYTES = 32 * 1024 * 1024

_MANIFEST_PATH = "memory-pack.json"
_PAYLOAD_PREFIX = "payload/"
_CATEGORY_ORDER = (
    "root_memory",
    "daily_memory",
    "user_memory",
    "memory_ledger",
    "layered_memory",
    "dreaming",
    "user_understanding",
    "metadata",
)
_ADOPTION_LOCK = threading.RLock()


class MemoryPackError(ValueError):
    """Raised when a memory pack is unsafe, invalid, or not confirmed."""


class MemoryPackManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = MEMORY_PACK_SCHEMA_VERSION
    product: Literal["lantai"] = "lantai"
    created_at: str
    source_agent_id: str
    categories: list[str]
    sensitive_personal_data: bool = True
    semantics: Literal["replace"] = "replace"
    contains_original_sources: bool = False
    original_source_categories: list[str] = Field(default_factory=list)


class MemoryPackReport(BaseModel):
    valid: bool = True
    schema_version: int = MEMORY_PACK_SCHEMA_VERSION
    source_agent_id: str
    created_at: str
    categories: list[str] = Field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    dry_run: bool = True
    adopted: bool = False
    would_replace: list[str] = Field(default_factory=list)
    would_remove: list[str] = Field(default_factory=list)
    recovery_pack: str = ""
    warnings: list[str] = Field(default_factory=list)
    contains_original_sources: bool = False
    original_source_categories: list[str] = Field(default_factory=list)
    decision_conflicts: list[str] = Field(default_factory=list)


def _original_source_category(member: str) -> str:
    if member == "payload/memory_ledger/events.jsonl":
        return "memory_ledger"
    if member.startswith("payload/layered_memory/conversations/") and member.endswith(".jsonl"):
        return "layered_memory"
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_paths(root: Path, agent_id: str) -> dict[str, Path]:
    return {
        "root_memory": root / "MEMORY.md",
        "daily_memory": daily_memory_dir(root, agent_id),
        "user_memory": root / "user-memory",
        "memory_ledger": root / "memory-ledger",
        "layered_memory": root / "layered-memory",
        "dreaming": root / "memory-dreaming",
        # This is a persisted user-facing understanding/feedback projection.
        # Replacing the source memories while leaving the old projection in
        # place would make the previous profile survive an adoption.
        "user_understanding": root / "user-understanding",
        "metadata": root / "memory_file_metadata.json",
    }


def _skip_source_file(path: Path, *, category: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return True
    if path.name.startswith(".") or path.name.endswith((".lock", ".tmp")):
        return True
    if ".corrupt-" in path.name and path.name.endswith(".bin"):
        return True
    if category == "layered_memory" and "offload" in path.parts:
        return True
    return False


def _iter_category_files(source: Path, category: str):
    if source.is_symlink() or not source.exists():
        return
    if source.is_file():
        if not _skip_source_file(source, category=category):
            yield source, source.name
        return
    for path in sorted(source.rglob("*")):
        if _skip_source_file(path, category=category):
            continue
        yield path, path.relative_to(source).as_posix()


def export_memory_pack(
    working_dir: Path | str,
    destination: Path | str,
    *,
    agent_id: str,
) -> MemoryPackReport:
    """Export one coherent, settled memory state while blocking adoption/writers."""

    root = Path(working_dir).expanduser().resolve(strict=False)
    with memory_state_lock(root):
        CanonicalMemoryLedger(root).read_snapshot()
        return _export_memory_pack_locked(root, destination, agent_id=agent_id)


def _export_memory_pack_locked(
    working_dir: Path | str,
    destination: Path | str,
    *,
    agent_id: str,
) -> MemoryPackReport:

    root = Path(working_dir).expanduser().resolve(strict=False)
    output = Path(destination).expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    entries: list[BackupFileIntegrity] = []
    categories: list[str] = []
    file_count = 0
    total_bytes = 0
    original_source_categories: set[str] = set()
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for category, source in _source_paths(root, agent_id).items():
                category_count = 0
                for path, relative in _iter_category_files(source, category):
                    size = path.stat().st_size
                    if size > MEMORY_PACK_MAX_FILE_BYTES:
                        raise MemoryPackError(
                            f"记忆文件过大，无法打包：{path.name}",
                        )
                    file_count += 1
                    total_bytes += size
                    if file_count > MEMORY_PACK_MAX_FILES:
                        raise MemoryPackError("记忆包文件数量超过限制")
                    if total_bytes > MEMORY_PACK_MAX_TOTAL_BYTES:
                        raise MemoryPackError("记忆包总大小超过限制")
                    member = f"{_PAYLOAD_PREFIX}{category}/{relative}"
                    original_category = _original_source_category(member)
                    if original_category:
                        original_source_categories.add(original_category)
                    write_path_with_integrity(
                        archive,
                        path,
                        member,
                        entries,
                    )
                    category_count += 1
                if category_count:
                    categories.append(category)

            manifest = MemoryPackManifest(
                created_at=_now_iso(),
                source_agent_id=agent_id,
                categories=sorted(categories),
                contains_original_sources=bool(original_source_categories),
                original_source_categories=sorted(original_source_categories),
            )
            write_bytes_with_integrity(
                archive,
                manifest.model_dump_json(indent=2).encode("utf-8"),
                _MANIFEST_PATH,
                entries,
            )
            write_integrity_manifest(archive, entries)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return inspect_memory_pack(output)


def _validated_archive(pack_path: Path) -> tuple[MemoryPackManifest, int, int, list[str]]:
    if not pack_path.is_file():
        raise MemoryPackError("记忆包不存在")
    if pack_path.stat().st_size > MEMORY_PACK_MAX_TOTAL_BYTES:
        raise MemoryPackError("记忆包压缩文件超过大小限制")
    try:
        with zipfile.ZipFile(pack_path, "r") as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > MEMORY_PACK_MAX_FILES + 2:
                raise MemoryPackError("记忆包文件数量超过限制")
            total_uncompressed = sum(info.file_size for info in infos)
            if total_uncompressed > MEMORY_PACK_MAX_TOTAL_BYTES:
                raise MemoryPackError("记忆包展开后超过大小限制")
            if any(info.file_size > MEMORY_PACK_MAX_FILE_BYTES for info in infos):
                raise MemoryPackError("记忆包包含超大文件")
            integrity = verify_zip_integrity(archive, required=True)
            if _MANIFEST_PATH not in archive.namelist():
                raise MemoryPackError("记忆包缺少 memory-pack.json")
            manifest = MemoryPackManifest.model_validate_json(
                archive.read(_MANIFEST_PATH),
            )
            unknown_categories = set(manifest.categories) - set(_CATEGORY_ORDER)
            if unknown_categories:
                raise MemoryPackError("记忆包包含不支持的记忆分类")
            payload_categories: set[str] = set()
            original_source_categories: set[str] = set()
            for info in infos:
                name = info.filename.replace("\\", "/")
                if name in {_MANIFEST_PATH, "integrity.json"}:
                    continue
                parts = Path(name).parts
                if (
                    len(parts) < 3
                    or parts[0] != "payload"
                    or parts[1] not in _CATEGORY_ORDER
                    or ".." in parts
                ):
                    raise MemoryPackError(f"记忆包成员路径无效：{name}")
                payload_categories.add(parts[1])
                original_category = _original_source_category(name)
                if original_category:
                    original_source_categories.add(original_category)
            if payload_categories != set(manifest.categories):
                raise MemoryPackError("记忆包分类清单与实际文件不一致")
            if manifest.schema_version == 2 and (
                manifest.contains_original_sources != bool(original_source_categories)
                or manifest.original_source_categories != sorted(original_source_categories)
            ):
                raise MemoryPackError("记忆包原文来源清单与实际文件不一致")
            return manifest, integrity.file_count - 1, integrity.total_bytes, sorted(original_source_categories)
    except MemoryPackError:
        raise
    except (BackupIntegrityError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise MemoryPackError(f"记忆包校验失败：{exc}") from exc


def inspect_memory_pack(pack_path: Path | str) -> MemoryPackReport:
    """Verify a pack without changing any active memory."""

    manifest, file_count, total_bytes, source_categories = _validated_archive(
        Path(pack_path).expanduser().resolve(strict=False),
    )
    return MemoryPackReport(
        schema_version=manifest.schema_version,
        source_agent_id=manifest.source_agent_id,
        created_at=manifest.created_at,
        categories=sorted(manifest.categories),
        file_count=file_count,
        total_bytes=total_bytes,
        contains_original_sources=bool(source_categories),
        original_source_categories=source_categories,
    )


def _extract_stage(pack_path: Path, stage: Path) -> None:
    with zipfile.ZipFile(pack_path, "r") as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith(_PAYLOAD_PREFIX) or info.is_dir():
                continue
            relative = Path(name).relative_to("payload")
            destination = (stage / relative).resolve(strict=False)
            if not destination.is_relative_to(stage.resolve(strict=False)):
                raise MemoryPackError("记忆包成员试图逃逸暂存目录")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)


def _staged_category_path(stage: Path, category: str) -> Path:
    category_root = stage / category
    if category in {"root_memory", "metadata"}:
        filename = "MEMORY.md" if category == "root_memory" else "memory_file_metadata.json"
        return category_root / filename
    return category_root


def _read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MemoryPackError(f"无法检查记忆决策文件：{path.name}") from exc
    if not isinstance(value, dict):
        raise MemoryPackError(f"记忆决策文件格式无效：{path.name}")
    return value


def _read_items(path: Path, key: str) -> list[dict]:
    """Read both current wrapped and legacy bare-list memory projections."""

    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MemoryPackError(f"无法检查记忆决策文件：{path.name}") from exc
    items = value.get(key, []) if isinstance(value, dict) else value
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise MemoryPackError(f"记忆决策文件格式无效：{path.name}")
    return items


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise MemoryPackError(f"无法检查记忆来源文件：{path.name}") from exc
    if not all(isinstance(value, dict) for value in values):
        raise MemoryPackError(f"记忆来源文件格式无效：{path.name}")
    return values


def _fact_digest(content: str) -> str:
    return hashlib.sha256(_normalize_fact(content).encode("utf-8")).hexdigest()


def _contains_negative_content(content: str, negative_content: list[str]) -> bool:
    normalized = _normalize_fact(content)
    return any(
        len(term) >= 4 and term in normalized
        for value in negative_content
        if (term := _normalize_fact(value))
    )


def _local_decisions(root: Path) -> tuple[set[str], set[str], set[str], list[str]]:
    snapshot = CanonicalMemoryLedger(root).read_snapshot()
    deleted = snapshot["deleted_sources"]
    fact_hashes = set(str(item) for item in deleted.get("fact_hashes", []))
    negative_content: list[str] = []
    for fact in snapshot["facts"]:
        if isinstance(fact, dict) and fact.get("status") in {"forgotten", "rejected", "superseded"}:
            content = str(fact.get("content") or "")
            if content and content != "[来源已删除]":
                fact_hashes.add(_fact_digest(content))
                negative_content.append(content)
    state = _read_object(root / "layered-memory" / "state.json")
    for content in state.get("forgotten_content", []):
        if isinstance(content, str) and content.strip():
            fact_hashes.add(_fact_digest(content))
            negative_content.append(content)
    return (
        set(str(item) for item in deleted.get("event_ids", [])),
        fact_hashes,
        set(str(item) for item in state.get("deleted_l0", [])),
        negative_content,
    )


def _decision_conflicts(root: Path, stage: Path) -> list[str]:
    deleted_events, blocked_facts, deleted_l0, negative_content = _local_decisions(root)
    conflicts: list[str] = []
    imported = CanonicalMemoryLedger(stage, ledger_dir=stage / "memory_ledger").read_snapshot()
    events = imported["events"]
    if any(str(event.get("id")) in deleted_events for event in events):
        conflicts.append("记忆包含本机已删除的原文来源")
    facts = imported["facts"]
    if any(
        isinstance(fact, dict)
        and fact.get("status") not in {"forgotten", "rejected", "superseded", "expired"}
        and (
            _fact_digest(str(fact.get("content") or "")) in blocked_facts
            or _contains_negative_content(str(fact.get("content") or ""), negative_content)
        )
        for fact in facts
    ):
        conflicts.append("记忆包含本机已纠正或遗忘的事实")
    if any(
        isinstance(fact, dict)
        and set(str(item) for item in fact.get("event_ids", [])) & deleted_events
        and fact.get("status") not in {"forgotten", "rejected", "superseded", "expired"}
        for fact in facts
    ):
        conflicts.append("记忆包中的事实引用了本机已删除的原文来源")
    conversation_dir = stage / "layered_memory" / "conversations"
    if any(
        str(row.get("id")) in deleted_l0
        for path in conversation_dir.glob("*.jsonl")
        for row in _read_rows(path)
    ):
        conflicts.append("记忆包含本机已删除的对话原文片段")
    entries = _read_items(stage / "user_memory" / "entries.json", "entries")
    atoms = _read_items(stage / "layered_memory" / "atoms" / "atoms.json", "atoms")
    if any(
        isinstance(item, dict)
        and item.get("status") not in {"archived", "deleted"}
        and (
            _fact_digest(str(item.get("content") or "")) in blocked_facts
            or _contains_negative_content(str(item.get("content") or ""), negative_content)
        )
        for item in [*entries, *atoms]
    ):
        conflicts.append("记忆包的旧记忆索引含本机已纠正的内容")
    if negative_content:
        source_text_paths = [stage / "root_memory" / "MEMORY.md"]
        source_text_paths.extend((stage / "daily_memory").rglob("*.md"))
        for path in source_text_paths:
            if not path.is_file():
                continue
            normalized_text = _normalize_fact(path.read_text(encoding="utf-8"))
            if _contains_negative_content(normalized_text, negative_content):
                conflicts.append("记忆包的旧记忆正文含本机已纠正的内容")
                break
    return list(dict.fromkeys(conflicts))


def _carry_local_decisions(root: Path, stage: Path) -> None:
    deleted_events, blocked_facts, deleted_l0, _ = _local_decisions(root)
    staged_ledger_path = stage / "memory_ledger" / "deleted-sources.json"
    staged_ledger = CanonicalMemoryLedger(stage, ledger_dir=stage / "memory_ledger").read_snapshot()["deleted_sources"]
    if deleted_events or blocked_facts or staged_ledger_path.exists():
        staged_ledger_path.parent.mkdir(parents=True, exist_ok=True)
        staged_ledger.update({
            "version": 2,
            "event_ids": sorted(set(str(item) for item in staged_ledger.get("event_ids", [])) | deleted_events),
            "fact_hashes": sorted(set(str(item) for item in staged_ledger.get("fact_hashes", [])) | blocked_facts),
        })
        write_authoritative(staged_ledger_path, json.dumps(staged_ledger, ensure_ascii=False, indent=2).encode("utf-8"))
    source_state = _read_object(root / "layered-memory" / "state.json")
    staged_state_path = stage / "layered_memory" / "state.json"
    staged_state = _read_object(staged_state_path)
    if deleted_l0 or source_state.get("forgotten_content") or staged_state:
        staged_state_path.parent.mkdir(parents=True, exist_ok=True)
        for key in ("deleted_l0", "forgotten_content"):
            staged_state[key] = list(dict.fromkeys([
                *[str(item) for item in staged_state.get(key, [])],
                *[str(item) for item in source_state.get(key, [])],
            ]))
        staged_state_path.write_text(json.dumps(staged_state, ensure_ascii=False, indent=2), encoding="utf-8")


def _replace_categories_atomically(
    targets: dict[str, Path],
    stage: Path,
) -> list[str]:
    root = targets["memory_ledger"].parent
    replacements = []
    for category in _CATEGORY_ORDER:
        target = targets[category]
        if target.is_symlink():
            raise MemoryPackError(f"目标记忆路径是符号链接：{target.name}")
        staged = _staged_category_path(stage, category)
        replacements.append((target, staged if staged.exists() else None))
    with path_transaction(memory_transaction_journal(root), replacements):
        CanonicalMemoryLedger(root).read_snapshot()
    return []


def adopt_memory_pack(
    pack_path: Path | str,
    working_dir: Path | str,
    *,
    agent_id: str,
    dry_run: bool = True,
    confirmation: str = "",
) -> MemoryPackReport:
    """Preview or atomically adopt a verified memory pack."""

    pack = Path(pack_path).expanduser().resolve(strict=False)
    root = Path(working_dir).expanduser().resolve(strict=False)
    report = inspect_memory_pack(pack)
    targets = _source_paths(root, agent_id)
    present = set(report.categories)
    report.would_replace = sorted(
        category
        for category, target in targets.items()
        if category in present and target.exists()
    )
    report.would_remove = sorted(
        category
        for category, target in targets.items()
        if category not in present and target.exists()
    )
    if not dry_run and confirmation != MEMORY_PACK_ADOPT_CONFIRMATION:
        raise MemoryPackError(
            f"请先预演，并输入“{MEMORY_PACK_ADOPT_CONFIRMATION}”确认采用",
        )

    with memory_state_lock(root), _ADOPTION_LOCK:
        stage = Path(
            tempfile.mkdtemp(
                prefix=f".{root.name}.memory-adopt-",
                dir=root.parent,
            ),
        )
        warnings: list[str] = []
        try:
            _extract_stage(pack, stage)
            conflicts = _decision_conflicts(root, stage)
            report.decision_conflicts = conflicts
            if dry_run:
                return report
            if conflicts:
                raise MemoryPackError("；".join(conflicts))
            _carry_local_decisions(root, stage)
            backup_dir = root / "memory-adoption-backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            recovery_pack = backup_dir / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + f"-{uuid.uuid4().hex[:8]}.lantai-memory.zip"
            )
            export_memory_pack(root, recovery_pack, agent_id=agent_id)
            warnings.extend(_replace_categories_atomically(targets, stage))
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        try:
            CanonicalMemoryLedger(root).rebuild_projections()
            SQLiteRetrievalIndex(root / "memory-retrieval.sqlite").rebuild([])
            for suffix in ("", "-wal", "-shm"):
                (root / f"memory-retrieval-vectors.sqlite{suffix}").unlink(
                    missing_ok=True,
                )
        except Exception as exc:
            warnings.append(f"记忆已采用，但派生索引需要重启后重建：{exc}")

    return report.model_copy(
        update={
            "dry_run": False,
            "adopted": True,
            "recovery_pack": str(recovery_pack),
            "warnings": warnings,
        },
    )
