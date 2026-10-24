import json

import pytest
from fastapi import HTTPException

from lantai.app.wiki import core


def test_page_delete_is_recoverable_and_can_be_restored(tmp_path):
    core.write_wiki_page(
        tmp_path, "queries/research.md", "# Research\n\nKeep this knowledge.\n"
    )

    removed = core.delete_wiki_page(tmp_path, "queries/research.md")

    assert removed["deleted"] is True
    assert not (tmp_path / ".wiki/pages/queries/research.md").exists()
    trash = core.list_wiki_trash(tmp_path)
    assert [(item.kind, item.title) for item in trash] == [("page", "Research")]

    restored = core.restore_wiki_trash_item(tmp_path, trash[0].id)
    assert restored["kind"] == "page"
    assert core.read_wiki_page(tmp_path, "queries/research.md").content.endswith(
        "Keep this knowledge.\n"
    )
    assert core.list_wiki_trash(tmp_path) == []


def test_unified_trash_lists_sources_and_pages_and_purges_only_archived_items(tmp_path):
    source = core.import_wiki_source_upload(
        tmp_path, "evidence.txt", b"Original evidence"
    )
    source_path = source["imported"][0]["path"]
    core.write_wiki_page(tmp_path, "queries/note.md", "# Note\n\nUser note.\n")
    core.archive_wiki_source(tmp_path, source_path)
    core.delete_wiki_page(tmp_path, "queries/note.md")

    trash = core.list_wiki_trash(tmp_path)
    assert {item.kind for item in trash} == {"source", "page"}
    page = next(item for item in trash if item.kind == "page")
    assert core.purge_wiki_trash_item(tmp_path, page.id) == {
        "deleted": True,
        "id": page.id,
    }
    assert {item.kind for item in core.list_wiki_trash(tmp_path)} == {"source"}
    with pytest.raises(HTTPException) as missing:
        core.restore_wiki_trash_item(tmp_path, page.id)
    assert missing.value.status_code == 404


def test_page_archive_rejects_internal_files_and_path_escape(tmp_path):
    (tmp_path / "AGENTS.md").write_text("secret")
    with pytest.raises(HTTPException):
        core.archive_wiki_page(tmp_path, "AGENTS.md")
    with pytest.raises(HTTPException):
        core.purge_wiki_trash_item(tmp_path, "../outside")
    assert (tmp_path / "AGENTS.md").read_text() == "secret"


def test_page_restore_refuses_to_overwrite_a_new_page(tmp_path):
    core.write_wiki_page(tmp_path, "queries/note.md", "# First\n")
    archive_id = core.archive_wiki_page(tmp_path, "queries/note.md")["archive_id"]
    core.write_wiki_page(tmp_path, "queries/note.md", "# Replacement\n")
    with pytest.raises(HTTPException) as conflict:
        core.restore_wiki_trash_item(tmp_path, archive_id)
    assert conflict.value.status_code == 409
    assert core.read_wiki_page(tmp_path, "queries/note.md").title == "Replacement"


def test_restore_cannot_follow_a_manipulated_manifest_outside_its_archive(tmp_path):
    core.write_wiki_page(tmp_path, "queries/archived.md", "# Archived\n")
    archive_id = core.archive_wiki_page(tmp_path, "queries/archived.md")["archive_id"]
    core.write_wiki_page(tmp_path, "queries/live.md", "# Live\n")
    manifest = tmp_path / ".wiki/trash" / archive_id / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["trash_path"] = ".wiki/pages/queries/live.md"
    manifest.write_text(json.dumps(payload))

    with pytest.raises(HTTPException) as blocked:
        core.restore_wiki_trash_item(tmp_path, archive_id)
    assert blocked.value.status_code == 404
    assert core.read_wiki_page(tmp_path, "queries/live.md").title == "Live"


def test_restore_rejects_a_non_object_manifest(tmp_path):
    core.write_wiki_page(tmp_path, "queries/archived.md", "# Archived\n")
    archive_id = core.archive_wiki_page(tmp_path, "queries/archived.md")["archive_id"]
    manifest = tmp_path / ".wiki/trash" / archive_id / "manifest.json"
    manifest.write_text("[]")
    with pytest.raises(HTTPException) as malformed:
        core.restore_wiki_trash_item(tmp_path, archive_id)
    assert malformed.value.status_code == 404
