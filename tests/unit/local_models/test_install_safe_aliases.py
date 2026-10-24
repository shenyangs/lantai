"""Pinned-only finite alias admission through the actual public pipeline.

Archive bytes, platform headers and server command results are synthetic. The
pure trusted-asset seam fixes archive hash/size; neither extraction, link
admission, health admission nor promotion/recovery is replaced.
"""
from __future__ import annotations

import gzip
import io
import stat
import tarfile
from pathlib import Path

import pytest

from tests.unit.local_models.test_source_install_regressions import (
    DownloadTaskStatus,
    backend,
    finite_staged_health_command,
    forbid_real_network_and_execution,
    good_archive,
    install,
    prepare,
    run_worker,
    static_header,
)
from tests.unit.local_models.test_install_crash_recovery import regular_file_bytes


TOP = "llama-b8744/"
LIB_BYTES = b"synthetic-library-content-no-executable-body"


def aliased_archive(instance, kind, outside):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, value in {"llama-server": static_header(instance.os_name, instance.arch),
                            "llama-cli": static_header(instance.os_name, instance.arch),
                            "libfixture.0.0.8744.dylib": LIB_BYTES}.items():
            entry = tarfile.TarInfo(TOP + name)
            entry.size, entry.mode = len(value), 0o755
            archive.addfile(entry, io.BytesIO(value))
        entries = []
        if kind == "internal_symlink_chain":
            entries = [("libfixture.0.dylib", tarfile.SYMTYPE, "libfixture.0.0.8744.dylib"),
                       ("libfixture.dylib", tarfile.SYMTYPE, "libfixture.0.dylib")]
        elif kind == "internal_hardlink":
            entries = [("libfixture.dylib", tarfile.LNKTYPE, TOP + "libfixture.0.0.8744.dylib")]
        elif kind == "outside_relative":
            entries = [("libfixture.dylib", tarfile.SYMTYPE, "../outside-owned-fixture/sentinel")]
        elif kind == "absolute":
            entries = [("libfixture.dylib", tarfile.SYMTYPE, str(outside / "sentinel"))]
        elif kind == "cycle":
            entries = [("libfixture.dylib", tarfile.SYMTYPE, "libfixture.0.dylib"),
                       ("libfixture.0.dylib", tarfile.SYMTYPE, "libfixture.dylib")]
        elif kind == "missing":
            entries = [("libfixture.dylib", tarfile.SYMTYPE, "lib-not-in-archive.dylib")]
        else:
            raise AssertionError("unknown finite alias fixture")
        for name, link_type, target in entries:
            alias = tarfile.TarInfo(TOP + name)
            alias.type, alias.linkname, alias.mode = link_type, target, 0o755
            archive.addfile(alias)
    return gzip.compress(output.getvalue(), mtime=0)


def public_alias_candidate(tmp_path, monkeypatch, kind):
    instance = backend(tmp_path / "bin", os_name="macos", arch="arm64")
    _, _, previous = install(monkeypatch, instance, good_archive(instance, marker=b"old-before-alias"))
    assert previous.status == DownloadTaskStatus.COMPLETED
    assert instance.check_llamacpp_installation() == (True, "")
    old_bytes = regular_file_bytes(instance.target_dir)
    outside = tmp_path / "outside-owned-fixture"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"alias-must-not-touch")
    data = aliased_archive(instance, kind, outside)
    spec, asset, fields = prepare(monkeypatch, instance, data)
    assert fields["sha256"] == asset.sha256 and fields["size_bytes"] == len(data)
    worker = run_worker(monkeypatch, spec, data)
    result, _ = spec.task.finalize(worker)
    return instance, spec, result, old_bytes, outside


@pytest.mark.parametrize("kind", ["internal_symlink_chain", "internal_hardlink"])
def test_pinned_internal_alias_materializes_regular_verified_file(tmp_path, monkeypatch, kind):
    instance, spec, result, _, outside = public_alias_candidate(tmp_path, monkeypatch, kind)
    assert result.status == DownloadTaskStatus.COMPLETED, "legitimate pinned archive alias made normal installation unavailable"
    names = ["libfixture.dylib", "libfixture.0.0.8744.dylib"]
    if kind == "internal_symlink_chain":
        names.append("libfixture.0.dylib")
    for name in names:
        path = instance.target_dir / name
        assert stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
        assert path.read_bytes() == LIB_BYTES
    assert instance.check_llamacpp_installation() == (True, "")
    spec.task.run_cleanup()
    assert not Path(spec.task.payload["staging_dir"]).exists()
    assert (outside / "sentinel").read_bytes() == b"alias-must-not-touch"


@pytest.mark.parametrize("kind", ["outside_relative", "absolute", "cycle", "missing"])
def test_even_pinned_unsafe_alias_cannot_replace_previous_verified_install(tmp_path, monkeypatch, kind):
    instance, spec, result, old_bytes, outside = public_alias_candidate(tmp_path, monkeypatch, kind)
    assert result.status == DownloadTaskStatus.FAILED, "trusted hash did not make an unsafe alias target safe"
    assert result.error, "unsafe alias rejection needs an observable diagnostic"
    assert regular_file_bytes(instance.target_dir) == old_bytes, "unsafe alias candidate changed previous receipt or files"
    assert instance.check_llamacpp_installation() == (True, "")
    spec.task.run_cleanup()
    assert not Path(spec.task.payload["staging_dir"]).exists()
    assert (outside / "sentinel").read_bytes() == b"alias-must-not-touch"
