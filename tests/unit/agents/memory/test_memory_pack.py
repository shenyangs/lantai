from __future__ import annotations

import zipfile
import json
from pathlib import Path

import pytest

from lantai.agents.memory.memory_pack import (
    MEMORY_PACK_ADOPT_CONFIRMATION,
    MemoryPackError,
    adopt_memory_pack,
    export_memory_pack,
    inspect_memory_pack,
)
from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger


def _seed_memory(root: Path, marker: str) -> None:
    (root / "MEMORY.md").write_text(f"# {marker}\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "2026-07-31.md").write_text(marker, encoding="utf-8")
    (root / "user-memory").mkdir()
    (root / "user-memory" / "entries.json").write_text(
        '{"entries":[]}',
        encoding="utf-8",
    )


def test_memory_pack_export_is_checksummed_and_source_aware(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _seed_memory(source, "source-memory")
    pack = tmp_path / "memory.lantai-memory.zip"

    report = export_memory_pack(source, pack, agent_id="writer")
    inspected = inspect_memory_pack(pack)

    assert report.valid is True
    assert inspected.valid is True
    assert inspected.source_agent_id == "writer"
    assert inspected.categories == ["daily_memory", "root_memory", "user_memory"]
    assert inspected.file_count == 3
    assert inspected.schema_version == 2
    assert inspected.contains_original_sources is False


def test_memory_pack_reports_original_source_snapshots(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    ledger = CanonicalMemoryLedger(source)
    ledger.record_event("请记住我偏好短回答", source_role="user", event_id="event_1")
    pack = tmp_path / "memory.zip"

    exported = export_memory_pack(source, pack, agent_id="writer")
    inspected = inspect_memory_pack(pack)

    assert exported.contains_original_sources is True
    assert inspected.original_source_categories == ["memory_ledger"]
    with zipfile.ZipFile(pack) as archive:
        assert json.loads(archive.read("memory-pack.json"))["contains_original_sources"] is True


def test_memory_pack_cannot_restore_locally_deleted_original_source(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for root in (source, target):
        ledger = CanonicalMemoryLedger(root)
        event = ledger.record_event("请记住我偏好短回答", source_role="user", event_id="event_1")
        ledger.capture_fact("偏好短回答", memory_type="style", event=event,
                            explicit_user_request=True)
    pack = tmp_path / "old-memory.zip"
    export_memory_pack(source, pack, agent_id="agent")
    CanonicalMemoryLedger(target).delete_source_event("event_1")

    preview = adopt_memory_pack(pack, target, agent_id="agent", dry_run=True)
    assert preview.decision_conflicts
    with pytest.raises(MemoryPackError, match="已删除"):
        adopt_memory_pack(pack, target, agent_id="agent", dry_run=False,
                          confirmation=MEMORY_PACK_ADOPT_CONFIRMATION)
    assert CanonicalMemoryLedger(target).list_events() == []


def test_memory_pack_cannot_restore_locally_forgotten_fact(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for root in (source, target):
        ledger = CanonicalMemoryLedger(root)
        event = ledger.record_event("请记住我偏好短回答", source_role="user")
        fact = ledger.capture_fact("偏好短回答", memory_type="style", event=event,
                                   explicit_user_request=True)
        if root == target:
            ledger.forget(fact.id)
    pack = tmp_path / "old-memory.zip"
    export_memory_pack(source, pack, agent_id="agent")

    preview = adopt_memory_pack(pack, target, agent_id="agent", dry_run=True)
    assert preview.decision_conflicts
    with pytest.raises(MemoryPackError, match="已纠正"):
        adopt_memory_pack(pack, target, agent_id="agent", dry_run=False,
                          confirmation=MEMORY_PACK_ADOPT_CONFIRMATION)
    assert CanonicalMemoryLedger(target).confirmed_facts() == []


def test_memory_pack_carries_forward_local_deletion_tombstones(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    CanonicalMemoryLedger(source).record_event("另一条消息", event_id="other")
    local = CanonicalMemoryLedger(target)
    local.record_event("要删除的消息", event_id="deleted")
    local.delete_source_event("deleted")
    pack = tmp_path / "memory.zip"
    export_memory_pack(source, pack, agent_id="agent")

    preview = adopt_memory_pack(pack, target, agent_id="agent", dry_run=True)
    assert preview.decision_conflicts == []
    adopt_memory_pack(pack, target, agent_id="agent", dry_run=False,
                      confirmation=MEMORY_PACK_ADOPT_CONFIRMATION)
    assert CanonicalMemoryLedger(target).record_event(
        "要删除的消息", event_id="deleted"
    ) is None


def test_memory_pack_rejects_legacy_list_entries_containing_locally_forgotten_content(
    tmp_path: Path,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "user-memory").mkdir()
    (source / "user-memory" / "entries.json").write_text(
        json.dumps([{"id": "old", "content": "用户偏好短回答", "status": "active"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (target / "layered-memory").mkdir()
    (target / "layered-memory" / "state.json").write_text(
        json.dumps({"forgotten_content": ["偏好短回答"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    pack = tmp_path / "old-memory.zip"
    export_memory_pack(source, pack, agent_id="agent")

    preview = adopt_memory_pack(pack, target, agent_id="agent", dry_run=True)
    assert "记忆包的旧记忆索引含本机已纠正的内容" in preview.decision_conflicts
    with pytest.raises(MemoryPackError, match="已纠正"):
        adopt_memory_pack(
            pack, target, agent_id="agent", dry_run=False,
            confirmation=MEMORY_PACK_ADOPT_CONFIRMATION,
        )
    assert not (target / "user-memory" / "entries.json").exists()


def test_memory_pack_accepts_legacy_list_atoms_when_no_decision_conflicts(
    tmp_path: Path,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    atoms_path = source / "layered-memory" / "atoms" / "atoms.json"
    atoms_path.parent.mkdir(parents=True)
    atoms_path.write_text(
        json.dumps([{"id": "atom_1", "content": "喜欢长文章", "status": "active"}], ensure_ascii=False),
        encoding="utf-8",
    )
    pack = tmp_path / "old-memory.zip"
    export_memory_pack(source, pack, agent_id="agent")

    preview = adopt_memory_pack(pack, target, agent_id="agent", dry_run=True)
    assert preview.decision_conflicts == []
    adopted = adopt_memory_pack(
        pack, target, agent_id="agent", dry_run=False,
        confirmation=MEMORY_PACK_ADOPT_CONFIRMATION,
    )
    assert adopted.adopted is True
    assert json.loads((target / "layered-memory" / "atoms" / "atoms.json").read_text(encoding="utf-8"))[0]["id"] == "atom_1"


def test_memory_pack_rejects_markdown_containing_locally_forgotten_content(
    tmp_path: Path,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "MEMORY.md").write_text("用户偏好短回答。", encoding="utf-8")
    (target / "layered-memory").mkdir()
    (target / "layered-memory" / "state.json").write_text(
        json.dumps({"forgotten_content": ["偏好短回答"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    pack = tmp_path / "old-memory.zip"
    export_memory_pack(source, pack, agent_id="agent")

    preview = adopt_memory_pack(pack, target, agent_id="agent", dry_run=True)
    assert "记忆包的旧记忆正文含本机已纠正的内容" in preview.decision_conflicts


def test_memory_pack_inspection_rejects_changed_payload(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _seed_memory(source, "source-memory")
    pack = tmp_path / "memory.lantai-memory.zip"
    export_memory_pack(source, pack, agent_id="writer")

    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(pack, "a") as archive:
            archive.writestr("payload/root_memory/MEMORY.md", "tampered")

    with pytest.raises(MemoryPackError):
        inspect_memory_pack(pack)


def test_memory_pack_adoption_requires_dry_run_and_keeps_recovery_pack(
    tmp_path: Path,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _seed_memory(source, "source-memory")
    _seed_memory(target, "target-memory")
    pack = tmp_path / "memory.lantai-memory.zip"
    export_memory_pack(source, pack, agent_id="writer")

    preview = adopt_memory_pack(
        pack,
        target,
        agent_id="reader",
        dry_run=True,
    )
    assert preview.dry_run is True
    assert preview.would_replace == [
        "daily_memory",
        "root_memory",
        "user_memory",
    ]
    assert "target-memory" in (target / "MEMORY.md").read_text(encoding="utf-8")

    with pytest.raises(MemoryPackError, match="确认"):
        adopt_memory_pack(pack, target, agent_id="reader", dry_run=False)

    adopted = adopt_memory_pack(
        pack,
        target,
        agent_id="reader",
        dry_run=False,
        confirmation=MEMORY_PACK_ADOPT_CONFIRMATION,
    )
    assert adopted.adopted is True
    assert Path(adopted.recovery_pack).is_file()
    assert "source-memory" in (target / "MEMORY.md").read_text(encoding="utf-8")


def test_memory_pack_adoption_removes_stale_understanding_when_pack_omits_it(
    tmp_path: Path,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _seed_memory(source, "source-memory")
    _seed_memory(target, "target-memory")
    stale = target / "user-understanding"
    stale.mkdir()
    (stale / "projection.json").write_text(
        '{"atoms":[{"statement":"old profile"}]}',
        encoding="utf-8",
    )
    pack = tmp_path / "memory.lantai-memory.zip"
    export_memory_pack(source, pack, agent_id="writer")

    preview = adopt_memory_pack(pack, target, agent_id="reader", dry_run=True)
    assert "user_understanding" in preview.would_remove

    adopt_memory_pack(
        pack,
        target,
        agent_id="reader",
        dry_run=False,
        confirmation=MEMORY_PACK_ADOPT_CONFIRMATION,
    )
    assert not stale.exists()


def test_memory_pack_adoption_rolls_back_current_category_on_swap_failure(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _seed_memory(source, "source-memory")
    _seed_memory(target, "target-memory")
    pack = tmp_path / "memory.lantai-memory.zip"
    export_memory_pack(source, pack, agent_id="writer")
    original_rename = Path.rename

    def fail_staged_user_memory(source_path: Path, destination: Path):
        if (
            source_path.name == "user_memory"
            and ".memory-adopt-" in source_path.parent.name
            and Path(destination).name == "user-memory"
        ):
            raise OSError("injected staged category swap failure")
        return original_rename(source_path, destination)

    monkeypatch.setattr(Path, "rename", fail_staged_user_memory)

    with pytest.raises(OSError, match="injected"):
        adopt_memory_pack(
            pack,
            target,
            agent_id="reader",
            dry_run=False,
            confirmation=MEMORY_PACK_ADOPT_CONFIRMATION,
        )

    assert "target-memory" in (target / "MEMORY.md").read_text(encoding="utf-8")
    assert "target-memory" in (target / "memory" / "2026-07-31.md").read_text(
        encoding="utf-8",
    )
    assert (target / "user-memory" / "entries.json").is_file()
