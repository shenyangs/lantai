"""Temporary red behavior locks; all fixtures stay inside pytest tmp_path."""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

import lantai.local_models.llamacpp as module
from lantai.local_models.download_manager import DownloadTaskResult, DownloadTaskStatus
from lantai.local_models.llamacpp import LlamaCppBackend


def test_tar_parent_path_is_rejected_before_any_extraction(tmp_path: Path):
    archive = tmp_path / "package.tar.gz"
    destination = tmp_path / "install-stage"
    destination.mkdir()
    with tarfile.open(archive, "w:gz") as handle:
        data = b"untrusted archive bytes"
        entry = tarfile.TarInfo("../outside-stage.txt")
        entry.size = len(data)
        handle.addfile(entry, io.BytesIO(data))
    error = None
    try:
        LlamaCppBackend._extract_archive(archive, destination)
    except Exception as exc:
        error = exc
    assert not (tmp_path / "outside-stage.txt").exists(), "archive escaped the extraction directory"
    assert error is not None, "unsafe archive must be explicitly rejected"


@pytest.mark.parametrize("entry_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_tar_external_link_is_rejected(tmp_path: Path, entry_type):
    archive = tmp_path / "package.tar.gz"
    destination = tmp_path / "install-stage"
    destination.mkdir()
    outside = tmp_path / "private-fixture.txt"
    outside.write_text("fixture content outside extraction root")
    with tarfile.open(archive, "w:gz") as handle:
        entry = tarfile.TarInfo("llama-server")
        entry.type = entry_type
        entry.linkname = str(outside)
        handle.addfile(entry)
    error = None
    try:
        LlamaCppBackend._extract_archive(archive, destination)
    except Exception as exc:
        error = exc
    assert error is not None, "archive links must be rejected before copying their targets"
    assert not (destination / "llama-server").exists()
    assert outside.read_text() == "fixture content outside extraction root"


def test_failed_install_retains_the_previous_installation(tmp_path: Path, monkeypatch):
    backend = object.__new__(LlamaCppBackend)
    staging = tmp_path / "stage"
    final = tmp_path / "installed"
    staging.mkdir()
    final.mkdir()
    (staging / "llama-server").write_text("candidate fixture")
    old_binary = final / "llama-server"
    old_binary.write_text("previous working fixture")

    def fail_move(*args, **kwargs):
        raise OSError("injected install publication failure")

    monkeypatch.setattr(module.shutil, "move", fail_move)
    result, _ = backend._finalize_download_result(
        DownloadTaskResult(status=DownloadTaskStatus.COMPLETED, local_path=str(staging)),
        staging_dir=staging,
        final_dir=final,
    )
    assert result.status == DownloadTaskStatus.FAILED
    assert old_binary.is_file(), "failed replacement removed the previous installation"
    assert old_binary.read_text() == "previous working fixture"
    assert staging.is_dir()
