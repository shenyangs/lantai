import sqlite3
import zipfile
from types import SimpleNamespace

import pytest

from lantai.app.reading.store import ReadingStore
from lantai.app.reading.managed import ManagedRSSHub
from lantai.backup._ops.create_helpers import add_agent_workspaces


@pytest.mark.parametrize("sanitized", [False, True])
def test_reading_backup_contains_committed_wal_data_without_journals(
    tmp_path, sanitized
):
    workspace = tmp_path / "workspace"
    store = ReadingStore(workspace)
    entry_id = store.ingest([{"url": "https://example.com/a", "title": "Saved"}])[0]
    writer = sqlite3.connect(store.path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "UPDATE entries SET starred=1,body='committed content' WHERE id=?",
            (entry_id,),
        )
        writer.commit()
        archive = tmp_path / "backup.zip"
        integrity = []
        with zipfile.ZipFile(archive, "w") as output:
            assert add_agent_workspaces(
                output,
                [("reader", SimpleNamespace(workspace_dir=workspace))],
                integrity_entries=integrity,
                sanitized=sanitized,
            )
        with zipfile.ZipFile(archive) as saved:
            names = saved.namelist()
            assert len(names) == 1 and names[0].endswith("reading/library.sqlite3")
            restored = tmp_path / "restored.sqlite3"
            restored.write_bytes(saved.read(names[0]))
        with sqlite3.connect(restored) as db:
            assert db.execute(
                "SELECT starred,body FROM entries WHERE id=?", (entry_id,)
            ).fetchone() == (1, "committed content")
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert len(integrity) == 1 and integrity[0].path == names[0]
    finally:
        writer.close()


def test_runtime_components_are_outside_workspaces_and_shared_between_agents(tmp_path):
    cache = tmp_path / "cache"
    first = ManagedRSSHub(tmp_path / "a", cache_root=cache)
    second = ManagedRSSHub(tmp_path / "b", cache_root=cache)
    assert first.directory == second.directory
    assert first.root != second.root
    assert not first.directory.is_relative_to(tmp_path / "a")
