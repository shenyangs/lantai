"""Five finite public-pipeline crash recovery locks, entirely synthetic/offline.

Real owned directory renames happen before a BaseException simulates abrupt
process exit. Recovery is a fresh public installation check, not a mocked
private recovery function. No executable body or network is permitted.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tests.unit.local_models.test_source_install_regressions import (
    DownloadTaskStatus,
    _promotion_path_matches,
    backend,
    finite_staged_health_command,
    forbid_real_network_and_execution,
    good_archive,
    install,
    prepare,
    run_worker,
    static_header,
    trust_fixture,
)


class SimulatedProcessExit(BaseException):
    """Ordinary exception handlers must not silently turn this into rollback."""


def regular_file_bytes(directory):
    result = {}
    for path in directory.rglob("*"):
        if stat.S_ISREG(path.lstat().st_mode):
            result[str(path.relative_to(directory))] = path.read_bytes()
    return result


def interrupted_install(tmp_path, monkeypatch, boundary):
    parent = tmp_path / "owned-install-parent"
    parent.mkdir()
    instance = backend(parent / "bin")
    # A cold backend can independently resolve one immutable synthetic asset.
    # Distinct old/candidate directory identities and receipt nonces distinguish
    # installations even when their official archive bytes are identical.
    data = good_archive(instance, marker=b"crash-recovery-trusted-bytes")
    _, _, previous = install(monkeypatch, instance, data)
    assert previous.status == DownloadTaskStatus.COMPLETED
    assert instance.check_llamacpp_installation() == (True, "")
    old_bytes = regular_file_bytes(instance.target_dir)
    spec, _, _ = prepare(monkeypatch, instance, data)
    worker = run_worker(monkeypatch, spec, data)
    assert worker.status == DownloadTaskStatus.COMPLETED, "validated staged candidate required before crash injection"
    stage = Path(spec.task.payload["staging_dir"])
    candidate_bytes = regular_file_bytes(stage)
    assert old_bytes != candidate_bytes, "receipts must distinguish old and candidate owners"
    backup = parent / (".llamacpp-backup-" + spec.task.payload["job_nonce"])
    journal = parent / instance._transaction_name(instance.target_dir.name)
    parent_stat = parent.stat(follow_symlinks=False)
    parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
    expected_src, expected_dst = ((instance.target_dir, backup) if boundary == "old_to_backup"
                                  else (stage, instance.target_dir))
    actual_rename = os.rename
    hits = []

    def exit_after_real_rename(src, dst, *args, **kwargs):
        matches = (_promotion_path_matches(src, expected_src, parent_identity,
                                            dir_fd=kwargs.get("src_dir_fd"))
                   and _promotion_path_matches(dst, expected_dst, parent_identity,
                                               dir_fd=kwargs.get("dst_dir_fd")))
        returned = actual_rename(src, dst, *args, **kwargs)
        if matches:
            hits.append(boundary)
            raise SimulatedProcessExit("synthetic process exit after actual owned rename")
        return returned

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "rename", exit_after_real_rename)
        with pytest.raises(SimulatedProcessExit):
            spec.task.finalize(worker)
    assert hits == [boundary], "fault must occur once, after the actual requested rename"
    assert journal.is_file(), "crash must leave durable recovery ownership"
    assert regular_file_bytes(backup) == old_bytes
    if boundary == "old_to_backup":
        assert not instance.target_dir.exists()
        assert regular_file_bytes(stage) == candidate_bytes
    else:
        assert not stage.exists()
        assert regular_file_bytes(instance.target_dir) == candidate_bytes
    return dict(instance=instance, data=data, spec=spec, stage=stage, backup=backup,
                journal=journal, old_bytes=old_bytes, candidate_bytes=candidate_bytes)


def cold_check(monkeypatch, interrupted):
    cold = backend(interrupted["instance"].target_dir)
    trust_fixture(monkeypatch, cold, interrupted["data"])
    return cold.check_llamacpp_installation()


@pytest.mark.parametrize("boundary", ["old_to_backup", "stage_to_final"])
def test_fresh_public_check_recovers_actual_post_rename_crash(tmp_path, monkeypatch, boundary):
    state = interrupted_install(tmp_path, monkeypatch, boundary)
    assert cold_check(monkeypatch, state) == (True, "")
    expected = state["old_bytes"] if boundary == "old_to_backup" else state["candidate_bytes"]
    assert regular_file_bytes(state["instance"].target_dir) == expected
    assert not state["stage"].exists(), "recovery left its owned stage"
    assert not state["backup"].exists(), "recovery left its owned backup"
    assert not state["journal"].exists(), "successful recovery did not retire transaction"


def test_tampered_promoted_candidate_recovers_previous_exact_receipt(tmp_path, monkeypatch):
    state = interrupted_install(tmp_path, monkeypatch, "stage_to_final")
    state["instance"].executable.write_bytes(static_header("linux", "x64", b"post-promotion-tampering"))
    assert cold_check(monkeypatch, state) == (True, "")
    assert regular_file_bytes(state["instance"].target_dir) == state["old_bytes"], "recovery did not preserve exact previous bytes and receipt"
    assert not state["stage"].exists()
    assert not state["backup"].exists()
    assert not state["journal"].exists()


def test_foreign_backup_replacement_is_not_deleted_or_blessed(tmp_path, monkeypatch):
    state = interrupted_install(tmp_path, monkeypatch, "stage_to_final")
    parked_old = state["backup"].with_name("parked-original-old-install")
    state["backup"].rename(parked_old)
    state["backup"].mkdir()
    sentinel = state["backup"] / "foreign-owned-fixture-sentinel"
    sentinel.write_bytes(b"must-not-delete")
    before = state["backup"].stat(follow_symlinks=False)
    verified, diagnostic = cold_check(monkeypatch, state)
    assert not verified and diagnostic, "foreign backup identity must block automatic transaction settlement"
    after = state["backup"].stat(follow_symlinks=False)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert sentinel.read_bytes() == b"must-not-delete"
    assert regular_file_bytes(parked_old) == state["old_bytes"]
    assert regular_file_bytes(state["instance"].target_dir) == state["candidate_bytes"]
    assert state["journal"].is_file(), "blocked identity settlement discarded its recovery evidence"


def test_parent_exchange_preserves_outside_sentinel_and_owned_recovery(tmp_path, monkeypatch):
    state = interrupted_install(tmp_path, monkeypatch, "stage_to_final")
    parent = state["instance"].target_dir.parent
    parked_parent = parent.with_name("parked-owned-install-parent")
    outside = tmp_path / "outside-owned-fixture"
    (outside / "bin").mkdir(parents=True)
    (outside / "bin" / "sentinel").write_bytes(b"outside-must-retain")
    outside_bytes = regular_file_bytes(outside)
    owned_bytes = regular_file_bytes(parent)
    parent.rename(parked_parent)
    parent.symlink_to(outside, target_is_directory=True)
    verified, diagnostic = cold_check(monkeypatch, state)
    assert not verified and diagnostic, "exchanged parent was followed during recovery"
    try:
        state["spec"].task.run_cleanup()
    except Exception:
        # Rejecting the exchanged ancestor is allowed; following it is not.
        pass
    assert regular_file_bytes(outside) == outside_bytes
    assert regular_file_bytes(parked_parent) == owned_bytes, "failed recovery or cleanup destroyed its original owned data"
    parent.unlink()
    parked_parent.rename(parent)
    assert cold_check(monkeypatch, state) == (True, "")
    assert regular_file_bytes(state["instance"].target_dir) == state["candidate_bytes"]
    assert not state["backup"].exists()
    assert not state["journal"].exists()
    assert regular_file_bytes(outside) == outside_bytes
