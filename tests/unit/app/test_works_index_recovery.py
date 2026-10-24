# -*- coding: utf-8 -*-
from __future__ import annotations

# Recovery tests intentionally exercise transactional helpers and fault seams.
# pylint: disable=protected-access

import errno
import json
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.app import office_conversion, work_covers, works as works_store
from lantai.app.capabilities.artifacts import load_artifact_manifest
from lantai.app.work_covers import generate_ai_cover
from lantai.app.works import (
    WORKS_SCHEMA_VERSION,
    WorksIndexUnavailable,
    add_work,
    derive_work_to_pdf,
    inspect_works_index,
    list_works,
    preview_works_index_recovery,
    remove_work,
    restore_works_index,
    set_work_cover,
    works_index_path,
)
from lantai.config.config import ImageGenerationConfig, ModelSlotConfig


def _add_text_work(workspace: Path, name: str, contents: str):
    source = workspace / "sources" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(contents, encoding="utf-8")
    return add_work(workspace, source_path=source, allowed_roots=[workspace])


def test_corrupt_index_blocks_add_and_preserves_index_and_managed_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = _add_text_work(workspace, "original.txt", "original")
    index_path = works_index_path(workspace)
    corrupt_bytes = b'{"schema_version": 5, "works": ['
    index_path.write_bytes(corrupt_bytes)
    managed_before = sorted(
        path.relative_to(workspace).as_posix()
        for path in (workspace / ".lantai" / "works" / "files").rglob("*")
    )

    with pytest.raises(WorksIndexUnavailable) as error:
        _add_text_work(workspace, "new.txt", "new")

    assert error.value.code == "works_index_invalid_json"
    assert index_path.read_bytes() == corrupt_bytes
    assert Path(original.path).read_text(encoding="utf-8") == "original"
    assert (
        sorted(
            path.relative_to(workspace).as_posix()
            for path in (workspace / ".lantai" / "works" / "files").rglob("*")
        )
        == managed_before
    )


def test_partial_index_blocks_delete_without_dropping_rows_or_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = _add_text_work(workspace, "original.txt", "original")
    index_path = works_index_path(workspace)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["works"].append({"id": "broken-row"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    index_path.write_bytes(raw)

    with pytest.raises(WorksIndexUnavailable) as error:
        remove_work(workspace, original.id)

    assert error.value.code == "works_index_incompatible_records"
    assert index_path.read_bytes() == raw
    assert Path(original.path).is_file()


def test_future_schema_and_missing_index_with_assets_are_not_treated_as_empty(
    tmp_path: Path,
) -> None:
    future_workspace = tmp_path / "future"
    future_workspace.mkdir()
    _add_text_work(future_workspace, "old.txt", "old")
    future_index = works_index_path(future_workspace)
    payload = json.loads(future_index.read_text(encoding="utf-8"))
    payload["schema_version"] = WORKS_SCHEMA_VERSION + 1
    future_bytes = json.dumps(payload, ensure_ascii=False).encode()
    future_index.write_bytes(future_bytes)

    with pytest.raises(WorksIndexUnavailable) as future_error:
        list_works(future_workspace)
    assert future_error.value.code == "works_index_future_schema"
    assert future_index.read_bytes() == future_bytes

    missing_workspace = tmp_path / "missing"
    missing_workspace.mkdir()
    old = _add_text_work(missing_workspace, "old.txt", "old")
    missing_index = works_index_path(missing_workspace)
    missing_index.unlink()

    with pytest.raises(WorksIndexUnavailable) as missing_error:
        _add_text_work(missing_workspace, "new.txt", "new")
    assert missing_error.value.code == "works_index_missing_with_assets"
    assert Path(old.path).is_file()
    assert not missing_index.exists()


def test_missing_index_with_history_is_not_reinitialized_after_emptying(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old = _add_text_work(workspace, "old.txt", "old")
    remove_work(workspace, old.id)
    index_path = works_index_path(workspace)
    assert list_works(workspace) == []
    assert any(
        (workspace / ".lantai" / "works" / "index-backups").glob(
            "index-*.json",
        ),
    )
    index_path.unlink()

    with pytest.raises(WorksIndexUnavailable) as error:
        _add_text_work(workspace, "new.txt", "new")

    assert error.value.code == "works_index_missing_with_history"
    assert not index_path.exists()


def test_supported_old_schema_and_unknown_fields_survive_a_normal_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = _add_text_work(workspace, "original.txt", "original")
    index_path = works_index_path(workspace)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["schema_version"] = WORKS_SCHEMA_VERSION - 1
    payload["future_top_level_hint"] = {"keep": True}
    payload["works"][0]["future_record_hint"] = ["keep"]
    index_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    added = _add_text_work(workspace, "second.txt", "second")

    updated = json.loads(index_path.read_text(encoding="utf-8"))
    assert updated["schema_version"] == WORKS_SCHEMA_VERSION
    assert updated["future_top_level_hint"] == {"keep": True}
    rows = {row["id"]: row for row in updated["works"]}
    assert rows[original.id]["future_record_hint"] == ["keep"]
    assert added.id in rows


def test_last_good_snapshot_supports_preview_confirm_atomic_restore_and_resume(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = _add_text_work(workspace, "first.txt", "first")
    second = _add_text_work(workspace, "second.txt", "second")
    index_path = works_index_path(workspace)
    corrupt_bytes = b"not-json"
    index_path.write_bytes(corrupt_bytes)

    diagnosis = inspect_works_index(workspace)
    assert diagnosis.status == "recovery_required"
    assert diagnosis.code == "works_index_invalid_json"
    assert diagnosis.current_digest
    assert diagnosis.backups

    preview = preview_works_index_recovery(
        workspace,
        snapshot_digest=diagnosis.backups[0].digest,
    )
    assert preview.confirmation_required is True
    assert preview.current_digest == diagnosis.current_digest
    assert [item.id for item in preview.snapshot_works] == [first.id]
    assert second.id in preview.orphaned_managed_ids

    restored = restore_works_index(
        workspace,
        expected_current_digest=preview.current_digest,
        snapshot_digest=preview.snapshot_digest,
    )
    assert restored.status == "restored"
    assert [item.id for item in list_works(workspace)] == [first.id]
    assert Path(second.path).is_file()

    repeated = restore_works_index(
        workspace,
        expected_current_digest=preview.current_digest,
        snapshot_digest=preview.snapshot_digest,
    )
    assert repeated.status == "already_restored"

    third = _add_text_work(workspace, "third.txt", "third")
    assert {item.id for item in list_works(workspace)} == {first.id, third.id}
    assert index_path.read_bytes() != corrupt_bytes


def test_restore_cas_refuses_bytes_changed_after_preview(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _add_text_work(workspace, "first.txt", "first")
    _add_text_work(workspace, "second.txt", "second")
    index_path = works_index_path(workspace)
    index_path.write_bytes(b"first-corrupt-state")
    diagnosis = inspect_works_index(workspace)
    preview = preview_works_index_recovery(
        workspace,
        snapshot_digest=diagnosis.backups[0].digest,
    )
    changed_bytes = b"second-corrupt-state"
    index_path.write_bytes(changed_bytes)

    with pytest.raises(WorksIndexUnavailable) as error:
        restore_works_index(
            workspace,
            expected_current_digest=preview.current_digest,
            snapshot_digest=preview.snapshot_digest,
        )

    assert error.value.code == "works_index_changed"
    assert index_path.read_bytes() == changed_bytes


def test_two_recovery_actors_are_serialized_and_idempotent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _add_text_work(workspace, "first.txt", "first")
    _add_text_work(workspace, "second.txt", "second")
    works_index_path(workspace).write_bytes(b"corrupt")
    diagnosis = inspect_works_index(workspace)
    preview = preview_works_index_recovery(
        workspace,
        snapshot_digest=diagnosis.backups[0].digest,
    )

    def recover():
        return restore_works_index(
            workspace,
            expected_current_digest=preview.current_digest,
            snapshot_digest=preview.snapshot_digest,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: recover(), range(2)))

    assert sorted(result.status for result in results) == [
        "already_restored",
        "restored",
    ]
    assert len(list_works(workspace)) == 1


def test_concurrent_writers_do_not_lose_either_work(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sources = []
    for index in range(2):
        source = workspace / "sources" / f"source-{index}.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"payload-{index}", encoding="utf-8")
        sources.append(source)

    def add(source: Path):
        return add_work(
            workspace,
            source_path=source,
            allowed_roots=[workspace],
            source_chat_id="chat-concurrent",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(add, sources))

    persisted = list_works(workspace)
    assert {item.id for item in persisted} == {item.id for item in created}
    assert {
        Path(item.path).read_text(encoding="utf-8") for item in persisted
    } == {
        "payload-0",
        "payload-1",
    }


@pytest.mark.parametrize(
    ("crash_point", "expected_returncode", "expected_bytes"),
    [
        ("before_replace", 91, b"old-index"),
        ("after_replace", 92, b"new-index"),
    ],
)
def test_atomic_index_publish_survives_process_kill_without_torn_bytes(
    tmp_path: Path,
    crash_point: str,
    expected_returncode: int,
    expected_bytes: bytes,
) -> None:
    target = tmp_path / "isolated" / "index.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-index")
    code = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from lantai.app import works

        target = Path({str(target)!r})
        if {crash_point!r} == "before_replace":
            works.os.replace = lambda _source, _target: os._exit(91)
        else:
            works._fsync_directory = lambda _path: os._exit(92)
        works._write_bytes_atomic_no_follow(target, b"new-index")
        """,
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    assert completed.returncode == expected_returncode
    assert target.read_bytes() == expected_bytes


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (PermissionError("denied"), "works_index_permission_denied"),
        (OSError(errno.EIO, "injected I/O failure"), "works_index_io_error"),
    ],
)
def test_index_read_failures_are_not_treated_as_an_empty_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    expected_code: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = _add_text_work(workspace, "original.txt", "original")
    index_path = works_index_path(workspace)
    index_bytes = index_path.read_bytes()
    real_read = works_store._read_bytes_no_follow

    def fail_index_read(path: Path) -> bytes:
        if path == index_path:
            raise failure
        return real_read(path)

    monkeypatch.setattr(works_store, "_read_bytes_no_follow", fail_index_read)

    with pytest.raises(WorksIndexUnavailable) as error:
        _add_text_work(workspace, "new.txt", "new")

    assert error.value.code == expected_code
    assert index_path.read_bytes() == index_bytes
    assert Path(original.path).read_text(encoding="utf-8") == "original"


def test_index_inspection_reports_managed_asset_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _add_text_work(workspace, "original.txt", "original")
    managed_root = workspace / ".lantai" / "works" / "files"
    real_iterdir = Path.iterdir

    def fail_managed_iterdir(path: Path):
        if path == managed_root:
            raise OSError(errno.EIO, "injected asset directory failure")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_managed_iterdir)

    diagnosis = inspect_works_index(workspace)

    assert diagnosis.status == "unavailable"
    assert diagnosis.code == "works_index_assets_io_error"
    assert diagnosis.diagnostic_id


def test_index_inspection_does_not_hide_backup_directory_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _add_text_work(workspace, "original.txt", "original")
    backup_root = workspace / ".lantai" / "works" / "index-backups"
    real_glob = Path.glob

    def fail_backup_glob(path: Path, pattern: str):
        if path == backup_root:
            raise OSError(errno.EIO, "injected backup directory failure")
        return real_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", fail_backup_glob)

    diagnosis = inspect_works_index(workspace)

    assert diagnosis.status == "unavailable"
    assert diagnosis.code == "works_index_backup_io_error"
    assert diagnosis.diagnostic_id


def test_existing_last_good_backup_read_failure_blocks_every_writer_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = _add_text_work(workspace, "original.txt", "original")
    index_path = works_index_path(workspace)
    index_bytes = index_path.read_bytes()
    digest = works_store._sha256_bytes(index_bytes)
    backup_path = works_store._backup_path(workspace, digest)
    works_store.ensure_works_index_writable(workspace)
    assert backup_path.read_bytes() == index_bytes
    real_read = works_store._read_bytes_no_follow

    def fail_backup_read(path: Path) -> bytes:
        if path == backup_path:
            raise OSError(errno.EIO, "injected existing backup read failure")
        return real_read(path)

    monkeypatch.setattr(works_store, "_read_bytes_no_follow", fail_backup_read)

    with pytest.raises(WorksIndexUnavailable) as error:
        _add_text_work(workspace, "candidate.txt", "candidate")

    assert error.value.code == "works_index_backup_failed"
    assert index_path.read_bytes() == index_bytes
    assert Path(original.path).read_text(encoding="utf-8") == "original"


def test_failed_index_commit_preserves_one_candidate_and_retry_reuses_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = _add_text_work(workspace, "original.txt", "original")
    index_path = works_index_path(workspace)
    index_bytes = index_path.read_bytes()
    real_write = works_store._write_index

    def fail_commit(*_args, **_kwargs) -> None:
        raise WorksIndexUnavailable(
            "works_index_write_failed",
            "injected commit failure",
        )

    monkeypatch.setattr(works_store, "_write_index", fail_commit)
    source = workspace / "sources" / "candidate.txt"
    source.write_text("candidate", encoding="utf-8")

    with pytest.raises(WorksIndexUnavailable) as error:
        add_work(
            workspace,
            source_path=source,
            allowed_roots=[workspace],
            source_chat_id="chat-1",
            source_run_id="run-1",
        )

    candidate_path = Path(error.value.candidate_path or "")
    assert error.value.code == "works_index_write_failed"
    assert candidate_path.read_text(encoding="utf-8") == "candidate"
    assert index_path.read_bytes() == index_bytes
    assert [item.id for item in list_works(workspace)] == [original.id]
    first_manifest = load_artifact_manifest(workspace)
    assert len(first_manifest["artifacts"]) == 2

    monkeypatch.setattr(works_store, "_write_index", real_write)
    resumed = add_work(
        workspace,
        source_path=source,
        allowed_roots=[workspace],
        source_chat_id="chat-1",
        source_run_id="run-1",
    )

    assert Path(resumed.path) == candidate_path
    assert len(load_artifact_manifest(workspace)["artifacts"]) == 2
    assert (
        len(list((workspace / ".lantai" / "works" / "files").iterdir())) == 2
    )


def test_corrupt_artifact_manifest_blocks_add_without_overwriting_or_copying(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sources" / "candidate.txt"
    source.parent.mkdir(parents=True)
    source.write_text("candidate", encoding="utf-8")
    manifest_path = workspace / ".lantai" / "artifacts" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    corrupt_bytes = b'{"schema_version": 1, "artifacts": ['
    manifest_path.write_bytes(corrupt_bytes)

    with pytest.raises(WorksIndexUnavailable) as error:
        add_work(
            workspace,
            source_path=source,
            allowed_roots=[workspace],
            source_chat_id="chat-1",
            source_run_id="run-1",
        )

    assert error.value.code == "artifact_manifest_invalid_json"
    assert manifest_path.read_bytes() == corrupt_bytes
    managed_root = workspace / ".lantai" / "works" / "files"
    assert not managed_root.exists() or not list(managed_root.iterdir())


def test_registration_failure_preserves_candidate_and_retry_reuses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sources" / "candidate final.txt"
    source.parent.mkdir(parents=True)
    source.write_text("candidate", encoding="utf-8")
    real_register = works_store.register_artifact
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "injected artifact registration failure")
        return real_register(*args, **kwargs)

    monkeypatch.setattr(works_store, "register_artifact", fail_once)
    with pytest.raises(WorksIndexUnavailable) as error:
        add_work(
            workspace,
            source_path=source,
            allowed_roots=[workspace],
            source_chat_id="chat-1",
            source_run_id="run-1",
        )

    candidate_path = Path(error.value.candidate_path or "")
    assert error.value.code == "artifact_manifest_write_failed"
    assert candidate_path.read_text(encoding="utf-8") == "candidate"
    assert not works_index_path(workspace).exists()
    assert (
        len(list((workspace / ".lantai" / "works" / "files").iterdir())) == 1
    )

    resumed = add_work(
        workspace,
        source_path=source,
        allowed_roots=[workspace],
        source_chat_id="chat-1",
        source_run_id="run-1",
    )

    assert Path(resumed.path) == candidate_path
    assert Path(resumed.path).name == source.name
    assert (
        len(list((workspace / ".lantai" / "works" / "files").iterdir())) == 1
    )
    assert len(load_artifact_manifest(workspace)["artifacts"]) == 1


def test_first_work_commit_failure_retry_uses_exact_artifact_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sources" / "candidate.txt"
    source.parent.mkdir(parents=True)
    source.write_text("candidate", encoding="utf-8")
    index_path = works_index_path(workspace)
    real_write = works_store._write_index

    def fail_commit(*_args, **_kwargs) -> None:
        raise WorksIndexUnavailable(
            "works_index_write_failed",
            "injected first commit failure",
        )

    monkeypatch.setattr(works_store, "_write_index", fail_commit)
    with pytest.raises(WorksIndexUnavailable) as error:
        add_work(
            workspace,
            source_path=source,
            allowed_roots=[workspace],
            source_chat_id="chat-1",
            source_run_id="run-1",
        )

    candidate_path = Path(error.value.candidate_path or "")
    assert candidate_path.read_text(encoding="utf-8") == "candidate"
    assert not index_path.exists()
    assert len(load_artifact_manifest(workspace)["artifacts"]) == 1

    monkeypatch.setattr(works_store, "_write_index", real_write)
    resumed = add_work(
        workspace,
        source_path=source,
        allowed_roots=[workspace],
        source_chat_id="chat-1",
        source_run_id="run-1",
    )

    assert Path(resumed.path) == candidate_path
    assert [item.id for item in list_works(workspace)] == [resumed.id]
    assert len(load_artifact_manifest(workspace)["artifacts"]) == 1
    assert (
        len(list((workspace / ".lantai" / "works" / "files").iterdir())) == 1
    )


def test_enospc_during_index_publish_keeps_original_index_and_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = _add_text_work(workspace, "original.txt", "original")
    index_path = works_index_path(workspace)
    index_bytes = index_path.read_bytes()
    real_atomic_write = works_store._write_bytes_atomic_no_follow

    def fail_index_publish(path: Path, payload: bytes) -> None:
        if path == index_path:
            raise OSError(errno.ENOSPC, "injected no space")
        real_atomic_write(path, payload)

    monkeypatch.setattr(
        works_store,
        "_write_bytes_atomic_no_follow",
        fail_index_publish,
    )

    with pytest.raises(WorksIndexUnavailable) as error:
        _add_text_work(workspace, "candidate.txt", "candidate")

    assert error.value.code == "works_index_write_failed"
    assert Path(error.value.candidate_path or "").is_file()
    assert index_path.read_bytes() == index_bytes
    assert Path(original.path).is_file()


def test_cover_commit_failure_preserves_old_cover_and_generated_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    work = _add_text_work(workspace, "report.txt", "report")
    old_cover = workspace / "old.png"
    old_cover.write_bytes(b"old-cover")
    covered = set_work_cover(
        workspace,
        work.id,
        source_path=old_cover,
        mime_type="image/png",
    )
    old_managed_cover = Path(covered.cover_path or "")
    real_write = works_store._write_index

    def fail_commit(*_args, **_kwargs) -> None:
        raise WorksIndexUnavailable(
            "works_index_write_failed",
            "injected commit failure",
        )

    monkeypatch.setattr(works_store, "_write_index", fail_commit)
    new_cover = workspace / "new.png"
    new_cover.write_bytes(b"new-cover")

    with pytest.raises(WorksIndexUnavailable) as error:
        set_work_cover(
            workspace,
            work.id,
            source_path=new_cover,
            mime_type="image/png",
        )

    candidate = Path(error.value.candidate_path or "")
    assert candidate.read_bytes() == b"new-cover"
    assert old_managed_cover.read_bytes() == b"old-cover"
    persisted = next(
        item for item in list_works(workspace) if item.id == work.id
    )
    assert persisted.cover_path == str(old_managed_cover)

    monkeypatch.setattr(works_store, "_write_index", real_write)
    resumed = set_work_cover(
        workspace,
        work.id,
        source_path=candidate,
        mime_type="image/png",
    )
    assert Path(resumed.cover_path or "") == candidate
    assert not old_managed_cover.exists()


def test_corrupt_index_blocks_derivation_before_converter_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "deck.pptx"
    source.write_bytes(b"presentation")
    work = add_work(workspace, source_path=source, allowed_roots=[workspace])
    works_index_path(workspace).write_bytes(b"broken")
    calls = 0

    def forbidden_conversion(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("converter must not run")

    monkeypatch.setattr(
        office_conversion,
        "convert_office_document",
        forbidden_conversion,
    )

    with pytest.raises(WorksIndexUnavailable):
        derive_work_to_pdf(workspace, work.id)
    assert calls == 0


async def test_corrupt_index_blocks_cover_before_paid_provider_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    work = _add_text_work(workspace, "report.txt", "report")
    works_index_path(workspace).write_bytes(b"broken")
    provider_calls = 0

    def forbidden_manager():
        nonlocal provider_calls
        provider_calls += 1
        return SimpleNamespace()

    monkeypatch.setattr(
        work_covers.ProviderManager,
        "get_instance",
        forbidden_manager,
    )
    config = ImageGenerationConfig(
        models=[ModelSlotConfig(provider_id="image", model="cover-v1")],
        works_cover_enabled=True,
        works_cover_model=ModelSlotConfig(
            provider_id="image",
            model="cover-v1",
        ),
    )

    with pytest.raises(WorksIndexUnavailable):
        await generate_ai_cover(workspace, work, config)
    assert provider_calls == 0
