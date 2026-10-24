# -*- coding: utf-8 -*-
"""Preview, back up, and clear every durable memory projection.

Chat transcripts are deliberately outside this module's deletion scope.  The
layered L0 copies are removed, while their ``seen_l0`` hashes are retained so
old transcript messages are not immediately imported as new memories again.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ...config.config import load_agent_config
from ...retrieval.local_index import SQLiteRetrievalIndex
from .canonical_ledger import CanonicalMemoryLedger
from .layered_memory import LayeredMemoryStore
from .migration import MIGRATION_VERSION
from .structured_user_memory import StructuredUserMemoryStore, now_iso
from .locking import memory_state_lock, memory_transaction_journal
from ...utils.path_transaction import path_transaction


MEMORY_CLEAR_CONFIRMATION = "删除全部记忆"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _json_list_count(path: Path, key: str) -> int:
    payload = _read_json(path, {})
    if isinstance(payload, dict):
        payload = payload.get(key, [])
    return len(payload) if isinstance(payload, list) else 0


def _jsonl_count(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def _glob_file_count(root: Path, pattern: str) -> int:
    if not root.exists():
        return 0
    return sum(1 for path in root.glob(pattern) if path.is_file())


def _backup_file_count(root: Path) -> int:
    total = _glob_file_count(root / "backup", "memory_backup_*.md")
    for directory in (
        root / "memory-migration-backups",
        root / "memory-clear-backups",
    ):
        total += _glob_file_count(directory, "**/*")
    return total


def daily_memory_dir(root: Path, agent_id: str) -> Path:
    """Resolve the configured daily-memory directory inside the workspace."""

    try:
        configured = str(load_agent_config(agent_id).running.daily_memory_dir)
    except Exception:
        configured = "memory"
    resolved_root = root.resolve()
    candidate = root / configured
    try:
        resolved_candidate = candidate.resolve()
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return resolved_root / "memory"
    if resolved_candidate == resolved_root:
        return resolved_root / "memory"
    return resolved_candidate


def memory_clear_preview(
    working_dir: str | Path,
    *,
    agent_id: str = "default",
) -> dict[str, Any]:
    """Count all durable memory artifacts without mutating the workspace."""

    root = Path(working_dir).resolve()
    user_store = StructuredUserMemoryStore(root)
    ledger = CanonicalMemoryLedger(root)
    layered = LayeredMemoryStore(root, agent_id, user_store)

    daily_dir = daily_memory_dir(root, agent_id)
    markdown_files = int((root / "MEMORY.md").is_file())
    markdown_files += _glob_file_count(root / "memory", "**/*.md")
    if daily_dir != root / "memory":
        markdown_files += _glob_file_count(daily_dir, "**/*.md")
    conversation_records = sum(
        _jsonl_count(path)
        for path in layered.conversation_dir.glob("*.jsonl")
        if path.is_file()
    )
    system2 = _read_json(layered.system2_summary_path, {})
    system2_models = 0
    if isinstance(system2, dict):
        for key in ("models", "open_intentions", "evolution_chains"):
            value = system2.get(key, [])
            if isinstance(value, list):
                system2_models += len(value)

    counts = {
        "structured_entries": _json_list_count(user_store.entries_path, "entries"),
        "canonical_facts": _json_list_count(ledger.facts_path, "facts"),
        "canonical_events": _jsonl_count(ledger.events_path),
        "layered_atoms": _json_list_count(layered.atoms_path, "atoms"),
        "layered_conversations": conversation_records,
        "scenes": _json_list_count(layered.scene_index_path, "scenes"),
        "system2_models": system2_models,
        "markdown_files": markdown_files,
        "import_batches": _glob_file_count(root / "memory-imports", "import_*.json"),
        "dreaming_runs": _glob_file_count(
            root / "memory-dreaming" / "runs",
            "dream_*.json",
        ),
        "user_understanding_atoms": _json_list_count(
            root / "user-understanding" / "projection.json",
            "atoms",
        ),
        "proactive_opportunities": _json_list_count(
            root / "user-understanding" / "opportunities.json",
            "opportunities",
        ),
        "subject_continuity_states": _glob_file_count(
            root / "persona" / "subject-continuity",
            "*.json",
        ),
        "backup_files": _backup_file_count(root),
    }
    active_items = sum(
        count for name, count in counts.items() if name != "backup_files"
    )
    return {
        "counts": counts,
        "active_items": active_items,
        "backup_files": counts["backup_files"],
        "total_items": active_items + counts["backup_files"],
        "chat_history_preserved": True,
        "runtime_cache_preserved": True,
        "confirmation_text": MEMORY_CLEAR_CONFIRMATION,
    }


@contextmanager
def _clear_lock(root: Path) -> Iterator[None]:
    with memory_state_lock(root):
        yield


def _next_backup_dir(root: Path) -> Path:
    parent = root / "memory-clear-backups"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = parent / timestamp
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{timestamp}-{suffix}"
        suffix += 1
    return candidate


def _copy_path(source: Path, destination: Path) -> None:
    if not source.exists() and not source.is_symlink():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def _create_active_backup(
    root: Path,
    backup_dir: Path,
    *,
    agent_id: str,
    preview: dict[str, Any],
    preferences: dict[str, bool],
) -> None:
    """Back up active memory sources, excluding older backup archives."""

    backup_dir.mkdir(parents=True, exist_ok=False)
    sources = (
        root / "MEMORY.md",
        root / "memory",
        daily_memory_dir(root, agent_id),
        root / "user-memory",
        root / "memory-ledger",
        root / "memory-imports",
        root / "memory-dreaming",
        root / "user-understanding",
        root / "persona" / "subject-continuity",
        root / "layered-memory" / "conversations",
        root / "layered-memory" / "atoms",
        root / "layered-memory" / "scenes",
        root / "layered-memory" / "system2",
        root / "layered-memory" / "persona.md",
        root / "layered-memory" / "state.json",
        root / "memory_file_metadata.json",
        root / "memory-retrieval.sqlite",
        root / "memory-retrieval.sqlite-wal",
        root / "memory-retrieval.sqlite-shm",
        root / "memory-retrieval-vectors.sqlite",
        root / "memory-retrieval-vectors.sqlite-wal",
        root / "memory-retrieval-vectors.sqlite-shm",
    )
    for source in dict.fromkeys(sources):
        _copy_path(source, backup_dir / source.relative_to(root))

    # The capture switch is temporarily paused during deletion.  A restored
    # backup should contain the user's original preference, not that transient
    # maintenance state.
    CanonicalMemoryLedger(backup_dir).set_preferences(**preferences)
    _write_json_atomic(
        backup_dir / "manifest.json",
        {
            "version": 1,
            "created_at": now_iso(),
            "agent_id": agent_id,
            "preview": preview,
            "preferences": preferences,
            "scope": {
                "includes": [
                    "Markdown 长期记忆",
                    "结构化用户记忆",
                    "canonical 事实与证据",
                    "其他 AI 记忆迁移批次",
                    "Dreaming 运行审计与反思日记",
                    "L0/L1/L2 分层记忆",
                    "Persona 与 System2",
                    "稀疏用户理解与主动机会队列",
                    "沈行知隐性主体连续性状态",
                    "长期记忆检索索引",
                ],
                "excludes": [
                    "聊天记录",
                    "知识库",
                    "技能与文件",
                    "工具结果运行缓存",
                ],
            },
        },
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _remove_old_backups(root: Path, *, preserve: Path | None) -> None:
    backup_dir = root / "backup"
    if backup_dir.exists():
        for path in backup_dir.glob("memory_backup_*.md"):
            _remove_path(path)

    _remove_path(root / "memory-migration-backups")

    clear_backups = root / "memory-clear-backups"
    if clear_backups.exists():
        for path in list(clear_backups.iterdir()):
            if preserve is not None and path == preserve:
                continue
            _remove_path(path)
        try:
            clear_backups.rmdir()
        except OSError:
            pass


def _clear_active_sources(
    root: Path,
    *,
    agent_id: str,
    preferences: dict[str, bool],
) -> None:
    user_store = StructuredUserMemoryStore(root)
    ledger = CanonicalMemoryLedger(root)
    layered = LayeredMemoryStore(root, agent_id, user_store)
    daily_dir = daily_memory_dir(root, agent_id)

    state = _read_json(layered.state_path, {})
    seen_l0 = state.get("seen_l0", []) if isinstance(state, dict) else []
    seen_offload = state.get("seen_offload", []) if isinstance(state, dict) else []
    migration_marker = _read_json(ledger.root / "migration-v2.json", None)
    if not isinstance(migration_marker, dict) or (
        migration_marker.get("version") != MIGRATION_VERSION
    ):
        migration_marker = {
            "version": MIGRATION_VERSION,
            "mode": "memory_clear",
            "created_at": now_iso(),
            "cleared": True,
        }

    for path in (
        root / "MEMORY.md",
        root / "memory",
        daily_dir,
        root / "memory_file_metadata.json",
        root / "memory-imports",
        root / "memory-dreaming",
        root / "user-understanding",
        root / "persona" / "subject-continuity",
        user_store.root,
        ledger.root,
        layered.conversation_dir,
        layered.atoms_path.parent,
        layered.scenes_dir,
        layered.system2_dir,
        layered.persona_path,
    ):
        _remove_path(path)

    user_store.save_entries([])
    ledger._write_atomic(ledger.facts_path, {"version": 2, "facts": []})
    _write_json_atomic(ledger.projections_path, {"version": 2, "facts": []})
    ledger.set_preferences(**preferences)
    _write_json_atomic(ledger.root / "migration-v2.json", migration_marker)

    _write_json_atomic(layered.atoms_path, {"atoms": []})
    _write_json_atomic(layered.scene_index_path, {"scenes": []})
    _write_json_atomic(
        layered.state_path,
        {
            "seen_l0": list(dict.fromkeys(str(item) for item in seen_l0))[-8000:],
            "seen_offload": list(
                dict.fromkeys(str(item) for item in seen_offload)
            )[-8000:],
            "forgotten_content": [],
            "updated_at": now_iso(),
        },
    )

    SQLiteRetrievalIndex(root / "memory-retrieval.sqlite").rebuild([])
    for path in (
        root / "memory-retrieval-vectors.sqlite",
        root / "memory-retrieval-vectors.sqlite-wal",
        root / "memory-retrieval-vectors.sqlite-shm",
    ):
        _remove_path(path)


def clear_all_long_term_memory(
    working_dir: str | Path,
    *,
    confirmation: str,
    create_backup: bool = True,
    agent_id: str = "default",
) -> dict[str, Any]:
    """Clear durable memory while preserving chats and runtime continuity."""

    if confirmation != MEMORY_CLEAR_CONFIRMATION:
        raise ValueError(f"请输入“{MEMORY_CLEAR_CONFIRMATION}”以确认操作")

    root = Path(working_dir).resolve()
    with _clear_lock(root):
        preview = memory_clear_preview(root, agent_id=agent_id)
        ledger = CanonicalMemoryLedger(root)
        preferences = ledger.get_preferences()

        backup_dir: Path | None = None
        stage = Path(tempfile.mkdtemp(prefix=".memory-clear-stage-", dir=root.parent))
        try:
            if create_backup:
                backup_dir = _next_backup_dir(root)
                _create_active_backup(
                    root,
                    backup_dir,
                    agent_id=agent_id,
                    preview=preview,
                    preferences=preferences,
                )
            # Prepare the complete empty state without changing any live
            # authority. The stable lock already fences turn capture.
            for source in (root / "layered-memory" / "state.json", root / "memory-ledger" / "migration-v2.json"):
                _copy_path(source, stage / source.relative_to(root))
            _clear_active_sources(
                stage,
                agent_id=agent_id,
                preferences=preferences,
            )
            paths = [root / name for name in (
                "MEMORY.md", "memory", "memory_file_metadata.json", "memory-imports",
                "memory-dreaming", "user-understanding", "persona/subject-continuity",
                "user-memory", "memory-ledger", "layered-memory/conversations",
                "layered-memory/atoms", "layered-memory/scenes", "layered-memory/system2",
                "layered-memory/persona.md", "layered-memory/state.json",
                "memory-retrieval.sqlite", "memory-retrieval.sqlite-wal", "memory-retrieval.sqlite-shm",
                "memory-retrieval-vectors.sqlite", "memory-retrieval-vectors.sqlite-wal",
                "memory-retrieval-vectors.sqlite-shm",
            )]
            paths.append(daily_memory_dir(root, agent_id))
            paths.extend((root / "backup").glob("memory_backup_*.md"))
            paths.append(root / "memory-migration-backups")
            paths.extend(path for path in (root / "memory-clear-backups").glob("*") if path != backup_dir)
            # A configured daily directory may be nested in the standard one.
            paths = sorted(set(paths), key=lambda path: len(path.parts))
            targets: list[Path] = []
            for path in paths:
                if not any(path.is_relative_to(parent) for parent in targets):
                    targets.append(path)
            replacements = [(path, candidate if (candidate := stage / path.relative_to(root)).exists() else None)
                            for path in targets]
            with path_transaction(memory_transaction_journal(root), replacements):
                CanonicalMemoryLedger(root).read_snapshot()
            clear_backups = root / "memory-clear-backups"
            if clear_backups.exists() and not any(clear_backups.iterdir()):
                clear_backups.rmdir()
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        return {
            "cleared": True,
            "cleared_counts": preview["counts"],
            "cleared_items": preview["total_items"],
            "backup_created": backup_dir is not None,
            "backup_dir": str(backup_dir) if backup_dir is not None else "",
            "chat_history_preserved": True,
            "runtime_cache_preserved": True,
            "preferences": preferences,
        }
