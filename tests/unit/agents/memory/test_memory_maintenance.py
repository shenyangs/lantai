# -*- coding: utf-8 -*-

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import numpy as np

from lantai.agents.memory import maintenance as memory_maintenance
from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.maintenance import (
    MEMORY_CLEAR_CONFIRMATION,
    clear_all_long_term_memory,
    memory_clear_preview,
)
from lantai.retrieval.local_index import IndexedDocument, SQLiteRetrievalIndex
from lantai.retrieval.vector_index import SQLiteVectorIndex


class _MaintenanceEmbedding:
    model_name = "test/maintenance"
    dimensions = 2
    fingerprint = "test-maintenance-v1"

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_every_memory_layer(root: Path) -> None:
    (root / "MEMORY.md").write_text("# Long memory\n- 用户偏好简洁回答\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "2026-07-27.md").write_text("今天的重要记忆", encoding="utf-8")
    (root / "memory_file_metadata.json").write_text("{}", encoding="utf-8")

    _write_json(
        root / "user-memory" / "entries.json",
        {"version": 1, "entries": [{"id": "entry-1", "content": "结构化记忆"}]},
    )
    _write_json(
        root / "user-memory" / "profile_summary.json",
        {"summary": "用户画像"},
    )
    _write_json(
        root / "user-understanding" / "projection.json",
        {
            "version": 1,
            "atoms": [{"id": "understanding-1", "statement": "用户正在做人格化优化"}],
        },
    )
    _write_json(
        root / "user-understanding" / "opportunities.json",
        {
            "version": 1,
            "opportunities": [
                {"id": "opportunity-1", "content": "优化完成后主动跟进测评"},
            ],
        },
    )
    _write_json(
        root / "persona" / "subject-continuity" / "default.json",
        {
            "version": 1,
            "reflection": {
                "attention": "用户正在做人格化优化",
                "self_observation": "我不想再用兜底句糊弄他",
            },
        },
    )

    _write_json(
        root / "memory-ledger" / "facts.json",
        {"version": 2, "facts": [{
            "id": "fact-1", "content": "canonical 记忆", "memory_type": "workflow",
            "status": "confirmed", "event_ids": ["event-1"], "confidence": 1.0,
        }]},
    )
    _write_json(
        root / "memory-ledger" / "projections.json",
        {"version": 2, "facts": [{"fact_id": "fact-1"}]},
    )
    (root / "memory-ledger" / "events.jsonl").write_text(
        '{"id":"event-1","content":"canonical 记忆"}\n',
        encoding="utf-8",
    )
    _write_json(
        root / "memory-ledger" / "last-recall.json",
        {"query": "偏好", "items": [{"content": "简洁回答"}]},
    )
    _write_json(
        root / "memory-ledger" / "migration-v2.json",
        {"version": "memory-governance-v2", "applied": True},
    )
    CanonicalMemoryLedger(root).set_preferences(
        runtime_enabled=False,
        capture_enabled=True,
    )

    _write_json(
        root / "layered-memory" / "atoms" / "atoms.json",
        {"atoms": [{"id": "atom-1", "content": "原子记忆"}]},
    )
    (root / "layered-memory" / "atoms" / "atoms.jsonl").write_text(
        '{"id":"atom-1"}\n',
        encoding="utf-8",
    )
    _write_json(
        root / "layered-memory" / "scenes" / "index.json",
        {"scenes": [{"scene_name": "project"}]},
    )
    (root / "layered-memory" / "scenes" / "project.md").write_text(
        "# Project scene",
        encoding="utf-8",
    )
    (root / "layered-memory" / "persona.md").write_text(
        "# Persona\n用户画像",
        encoding="utf-8",
    )
    _write_json(
        root / "layered-memory" / "system2" / "summary.json",
        {
            "models": [{"key": "preference"}],
            "open_intentions": [{"id": "intent-1"}],
            "evolution_chains": [{"id": "chain-1"}],
        },
    )
    (root / "layered-memory" / "system2" / "system2.md").write_text(
        "# System2",
        encoding="utf-8",
    )
    (root / "layered-memory" / "conversations").mkdir(parents=True)
    (root / "layered-memory" / "conversations" / "2026-07-27.jsonl").write_text(
        '{"id":"l0-1","content":"聊天副本一"}\n'
        '{"id":"l0-2","content":"聊天副本二"}\n',
        encoding="utf-8",
    )
    _write_json(
        root / "layered-memory" / "state.json",
        {
            "seen_l0": ["message-hash-1", "message-hash-2"],
            "seen_offload": ["tool-hash-1"],
            "forgotten_content": ["旧墓碑"],
            "last_system2_digest": "stale",
        },
    )
    (root / "layered-memory" / "offload").mkdir(parents=True)
    (root / "layered-memory" / "offload" / "keep.txt").write_text(
        "active tool cache",
        encoding="utf-8",
    )

    (root / "sessions").mkdir()
    (root / "sessions" / "chat.json").write_text(
        '{"messages":["聊天原文必须保留"]}',
        encoding="utf-8",
    )
    (root / "backup").mkdir()
    (root / "backup" / "memory_backup_20260727.md").write_text(
        "旧自动备份",
        encoding="utf-8",
    )
    (root / "memory-migration-backups" / "old").mkdir(parents=True)
    (root / "memory-migration-backups" / "old" / "facts.json").write_text(
        "旧迁移备份",
        encoding="utf-8",
    )
    (root / "memory-clear-backups" / "old").mkdir(parents=True)
    (root / "memory-clear-backups" / "old" / "manifest.json").write_text(
        "旧清理备份",
        encoding="utf-8",
    )
    _write_json(
        root / "memory-imports" / "import_1234567890abcdef1234567890abcdef.json",
        {
            "status": "committed",
            "candidates": [{"content": "迁移记忆"}],
        },
    )
    _write_json(
        root
        / "memory-dreaming"
        / "runs"
        / "dream_1234567890abcdef1234567890abcdef.json",
        {"run_id": "dream_1234567890abcdef1234567890abcdef"},
    )
    (root / "memory-dreaming" / "DREAMS.md").write_text(
        "# 澜台梦境日记\n",
        encoding="utf-8",
    )

    index = SQLiteRetrievalIndex(root / "memory-retrieval.sqlite")
    index.upsert(
        [
            IndexedDocument(
                source_id="memory:fact-1",
                source_kind="memory",
                title="偏好",
                content="用户偏好简洁回答",
                citation="memory-ledger/facts.json#fact-1",
            ),
        ],
    )


def test_preview_and_clear_all_memory_with_backup(tmp_path: Path) -> None:
    _seed_every_memory_layer(tmp_path)

    preview = memory_clear_preview(tmp_path, agent_id="default")

    assert preview["counts"] == {
        "structured_entries": 1,
        "canonical_facts": 1,
        "canonical_events": 1,
        "layered_atoms": 1,
        "layered_conversations": 2,
        "scenes": 1,
        "system2_models": 3,
        "markdown_files": 2,
        "import_batches": 1,
        "dreaming_runs": 1,
        "user_understanding_atoms": 1,
        "proactive_opportunities": 1,
        "subject_continuity_states": 1,
        "backup_files": 3,
    }
    assert preview["active_items"] == 17
    assert preview["total_items"] == 20
    assert preview["chat_history_preserved"] is True

    result = clear_all_long_term_memory(
        tmp_path,
        confirmation=MEMORY_CLEAR_CONFIRMATION,
        create_backup=True,
        agent_id="default",
    )

    backup_dir = Path(result["backup_dir"])
    assert result["cleared"] is True
    assert result["backup_created"] is True
    assert backup_dir.is_dir()
    assert (backup_dir / "manifest.json").is_file()
    assert (backup_dir / "MEMORY.md").is_file()
    assert (backup_dir / "user-memory" / "entries.json").is_file()
    assert (backup_dir / "user-understanding" / "projection.json").is_file()
    assert (backup_dir / "user-understanding" / "opportunities.json").is_file()
    assert (
        backup_dir / "persona" / "subject-continuity" / "default.json"
    ).is_file()
    assert (backup_dir / "memory-imports").is_dir()
    assert (backup_dir / "memory-dreaming" / "DREAMS.md").is_file()
    assert not (tmp_path / "memory-clear-backups" / "old").exists()
    assert not (tmp_path / "memory-migration-backups").exists()
    assert not (tmp_path / "backup" / "memory_backup_20260727.md").exists()

    assert not (tmp_path / "MEMORY.md").exists()
    assert not (tmp_path / "memory").exists()
    assert not (tmp_path / "memory_file_metadata.json").exists()
    assert not (tmp_path / "memory-imports").exists()
    assert not (tmp_path / "memory-dreaming").exists()
    assert not (tmp_path / "user-understanding").exists()
    assert not (tmp_path / "persona" / "subject-continuity").exists()
    assert _read_entries(tmp_path / "user-memory" / "entries.json") == []
    assert _read_entries(tmp_path / "memory-ledger" / "facts.json", "facts") == []
    assert _read_entries(tmp_path / "memory-ledger" / "projections.json", "facts") == []
    assert not (tmp_path / "memory-ledger" / "events.jsonl").exists()
    assert not (tmp_path / "memory-ledger" / "last-recall.json").exists()
    assert _read_entries(tmp_path / "layered-memory" / "atoms" / "atoms.json", "atoms") == []
    assert _read_entries(tmp_path / "layered-memory" / "scenes" / "index.json", "scenes") == []
    assert not (tmp_path / "layered-memory" / "conversations").exists()
    assert not (tmp_path / "layered-memory" / "system2").exists()
    assert not (tmp_path / "layered-memory" / "persona.md").exists()

    state = json.loads(
        (tmp_path / "layered-memory" / "state.json").read_text(encoding="utf-8"),
    )
    assert state["seen_l0"] == ["message-hash-1", "message-hash-2"]
    assert state["seen_offload"] == ["tool-hash-1"]
    assert state["forgotten_content"] == []
    assert "last_system2_digest" not in state
    assert (tmp_path / "layered-memory" / "offload" / "keep.txt").is_file()
    assert (tmp_path / "sessions" / "chat.json").read_text(encoding="utf-8")
    assert _read_entries(
        tmp_path / "memory-ledger" / "migration-v2.json",
        "",
    ) == {"version": "memory-governance-v2", "applied": True}
    assert CanonicalMemoryLedger(tmp_path).get_preferences() == {
        "runtime_enabled": False,
        "capture_enabled": True,
    }
    assert SQLiteRetrievalIndex(tmp_path / "memory-retrieval.sqlite").search("简洁") == []
    assert memory_clear_preview(tmp_path)["active_items"] == 0


def test_clear_rejects_wrong_confirmation_without_mutation(tmp_path: Path) -> None:
    _seed_every_memory_layer(tmp_path)

    with pytest.raises(ValueError, match=MEMORY_CLEAR_CONFIRMATION):
        clear_all_long_term_memory(
            tmp_path,
            confirmation="删除",
            create_backup=False,
        )

    assert (tmp_path / "MEMORY.md").is_file()
    assert SQLiteRetrievalIndex(tmp_path / "memory-retrieval.sqlite").search("简洁")


def test_clear_removes_the_semantic_projection_immediately(tmp_path: Path) -> None:
    vector = SQLiteVectorIndex(
        tmp_path / "memory-retrieval-vectors.sqlite",
        embedding=_MaintenanceEmbedding(),
    )
    vector.upsert(
        [
            IndexedDocument(
                source_id="memory:semantic",
                source_kind="memory",
                title="偏好",
                content="预算分析逐项列出报价。",
                citation="memory#semantic",
            ),
        ],
    )

    clear_all_long_term_memory(
        tmp_path,
        confirmation=MEMORY_CLEAR_CONFIRMATION,
        create_backup=False,
    )

    assert SQLiteVectorIndex(
        tmp_path / "memory-retrieval-vectors.sqlite",
        embedding=_MaintenanceEmbedding(),
    ).record_count == 0


def test_clear_without_backup_removes_previous_clear_backups(tmp_path: Path) -> None:
    _seed_every_memory_layer(tmp_path)

    result = clear_all_long_term_memory(
        tmp_path,
        confirmation=MEMORY_CLEAR_CONFIRMATION,
        create_backup=False,
    )

    assert result["backup_created"] is False
    assert result["backup_dir"] == ""
    assert not (tmp_path / "memory-clear-backups").exists()
    assert (tmp_path / "sessions" / "chat.json").is_file()


def test_clear_includes_configured_daily_memory_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        memory_maintenance,
        "load_agent_config",
        lambda _agent_id: SimpleNamespace(
            running=SimpleNamespace(daily_memory_dir="daily-notes"),
        ),
    )
    (tmp_path / "daily-notes").mkdir()
    (tmp_path / "daily-notes" / "2026-07-27.md").write_text(
        "配置目录中的每日记忆",
        encoding="utf-8",
    )

    preview = memory_clear_preview(tmp_path, agent_id="custom")
    result = clear_all_long_term_memory(
        tmp_path,
        confirmation=MEMORY_CLEAR_CONFIRMATION,
        create_backup=False,
        agent_id="custom",
    )

    assert preview["counts"]["markdown_files"] == 1
    assert result["cleared"] is True
    assert not (tmp_path / "daily-notes").exists()


def _read_entries(path: Path, key: str = "entries"):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if key == "":
        return payload
    return payload[key]
