# -*- coding: utf-8 -*-
"""Tests for bundling OfficeCLI into frozen desktop backend builds."""

import importlib.util
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "pack"
    / "officecli_bundle.py"
)
SPEC = importlib.util.spec_from_file_location("officecli_bundle", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
officecli_bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(officecli_bundle)


def _write_fake_officecli(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "if os.environ.get('OFFICECLI_SKIP_UPDATE') != '1':\n"
        "    raise SystemExit('OFFICECLI_SKIP_UPDATE not set')\n"
        "print('officecli 9.9.9')\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_attribution_files(root: Path) -> tuple[Path, Path]:
    license_path = root / "LICENSE"
    notice_path = root / "NOTICE"
    license_path.write_text("Apache License\nVersion 2.0\n", encoding="utf-8")
    notice_path.write_text("Copyright 2026 OfficeCLI\n", encoding="utf-8")
    return license_path, notice_path


def test_bundle_officecli_copies_to_backend_bin_and_smokes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(officecli_bundle.shutil, "which", lambda _: None)
    source = tmp_path / "source-officecli"
    backend_dir = tmp_path / "lantai-server"
    _write_fake_officecli(source)

    dest = officecli_bundle.bundle_officecli(
        backend_dir,
        required=True,
        env={
            "LANTAI_ALLOW_EXTERNAL_OFFICECLI": "1",
            "LANTAI_ALLOW_UNVERIFIED_OFFICECLI": "1",
            "LANTAI_OFFICECLI_BINARY": str(source),
            "LANTAI_OFFICECLI_SHA256": _sha256(source),
            "LANTAI_OFFICECLI_VERSION": "9.9.9",
            "LANTAI_OFFICECLI_ARCH": "local-script",
            "LANTAI_OFFICECLI_LICENSE": "Apache-2.0",
            "LANTAI_OFFICECLI_SOURCE_URL": "https://example.invalid/officecli",
        },
    )

    assert dest == backend_dir / "bin" / "officecli"
    assert os.access(dest, os.X_OK)
    assert officecli_bundle.smoke_test_officecli(dest) == "officecli 9.9.9"


def test_release_resolver_rejects_path_fallback_by_default(monkeypatch, tmp_path):
    path_binary = tmp_path / "path-officecli"
    _write_fake_officecli(path_binary)
    monkeypatch.setattr(
        officecli_bundle.shutil,
        "which",
        lambda name: str(path_binary) if name == "officecli" else None,
    )

    with pytest.raises(PermissionError, match="PATH"):
        officecli_bundle.resolve_officecli_source(
            required=True,
            env={"LANTAI_DISABLE_VENDORED_OFFICECLI": "1"},
        )


def test_external_officecli_path_requires_explicit_allowance(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(officecli_bundle.shutil, "which", lambda _: None)
    source = tmp_path / "external-officecli"
    _write_fake_officecli(source)

    with pytest.raises(PermissionError, match="external"):
        officecli_bundle.resolve_officecli_source(
            required=True,
            env={
                "LANTAI_OFFICECLI_BINARY": str(source),
                "LANTAI_DISABLE_VENDORED_OFFICECLI": "1",
            },
        )


def test_external_officecli_hash_mismatch_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(officecli_bundle.shutil, "which", lambda _: None)
    source = tmp_path / "external-officecli"
    _write_fake_officecli(source)

    with pytest.raises(RuntimeError, match="SHA256"):
        officecli_bundle.bundle_officecli(
            tmp_path / "lantai-server",
            required=True,
            env={
                "LANTAI_ALLOW_EXTERNAL_OFFICECLI": "1",
                "LANTAI_OFFICECLI_BINARY": str(source),
                "LANTAI_OFFICECLI_SHA256": "0" * 64,
                "LANTAI_OFFICECLI_VERSION": "9.9.9",
                "LANTAI_OFFICECLI_ARCH": "local-script",
                "LANTAI_OFFICECLI_LICENSE": "Apache-2.0",
                "LANTAI_OFFICECLI_SOURCE_URL": "https://example.invalid/officecli",
                "LANTAI_DISABLE_VENDORED_OFFICECLI": "1",
            },
            smoke=False,
        )


def test_local_path_bundle_writes_unreleasable_provenance(
    monkeypatch,
    tmp_path,
):
    path_binary = tmp_path / "path-officecli"
    backend_dir = tmp_path / "lantai-server"
    _write_fake_officecli(path_binary)
    monkeypatch.setattr(
        officecli_bundle.shutil,
        "which",
        lambda name: str(path_binary) if name == "officecli" else None,
    )

    dest = officecli_bundle.bundle_officecli(
        backend_dir,
        required=True,
        env={
            "LANTAI_ALLOW_PATH_OFFICECLI_FOR_LOCAL_BUILD": "1",
            "LANTAI_ALLOW_UNVERIFIED_OFFICECLI": "1",
            "LANTAI_DISABLE_VENDORED_OFFICECLI": "1",
        },
    )

    assert dest == backend_dir / "bin" / "officecli"
    provenance = json.loads((backend_dir / "bin" / "officecli.provenance.json").read_text())
    assert provenance["source_kind"] == "PATH"
    assert provenance["source_url"] == ""
    assert provenance["sha256"] == _sha256(path_binary)
    assert provenance["releasable"] is False


def test_bundle_accepts_a_developer_id_standalone_binary_without_app_assessment(
    monkeypatch,
    tmp_path,
):
    """A signed CLI is not an app bundle, so spctl cannot assess it as one."""
    source = tmp_path / "officecli"
    _write_fake_officecli(source)
    license_path, notice_path = _write_attribution_files(tmp_path)
    monkeypatch.setattr(officecli_bundle.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        officecli_bundle,
        "_codesign_summary",
        lambda _: ("developer-id", "52JQX2HUSC"),
    )
    monkeypatch.setattr(officecli_bundle, "_spctl_status", lambda _: "not-applicable")

    dest = officecli_bundle.bundle_officecli(
        tmp_path / "lantai-server",
        required=True,
        env={
            "LANTAI_ALLOW_EXTERNAL_OFFICECLI": "1",
            "LANTAI_OFFICECLI_BINARY": str(source),
            "LANTAI_OFFICECLI_SHA256": _sha256(source),
            "LANTAI_OFFICECLI_VERSION": "9.9.9",
            "LANTAI_OFFICECLI_ARCH": "local-script",
            "LANTAI_OFFICECLI_LICENSE": "Apache-2.0",
            "LANTAI_OFFICECLI_SOURCE_URL": "https://example.invalid/officecli",
            "LANTAI_OFFICECLI_LICENSE_FILE": str(license_path),
            "LANTAI_OFFICECLI_NOTICE_FILE": str(notice_path),
        },
    )

    provenance = json.loads((dest.parent / "officecli.provenance.json").read_text())
    assert provenance["releasable"] is True
    assert provenance["signature_status"] == "developer-id"
    assert provenance["spctl_status"] == "not-applicable"
    assert provenance["license_sha256"] == _sha256(license_path)
    assert provenance["notice_sha256"] == _sha256(notice_path)
    assert (dest.parent / "officecli-LICENSE.txt").read_text() == license_path.read_text()
    assert (dest.parent / "officecli-NOTICE.txt").read_text() == notice_path.read_text()


def test_releasable_external_officecli_requires_license_and_notice(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "officecli"
    _write_fake_officecli(source)
    monkeypatch.setattr(officecli_bundle.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        officecli_bundle,
        "_codesign_summary",
        lambda _: ("developer-id", "52JQX2HUSC"),
    )
    monkeypatch.setattr(officecli_bundle, "_spctl_status", lambda _: "not-applicable")

    with pytest.raises(RuntimeError, match="LICENSE"):
        officecli_bundle.bundle_officecli(
            tmp_path / "lantai-server",
            required=True,
            env={
                "LANTAI_ALLOW_EXTERNAL_OFFICECLI": "1",
                "LANTAI_OFFICECLI_BINARY": str(source),
                "LANTAI_OFFICECLI_SHA256": _sha256(source),
                "LANTAI_OFFICECLI_VERSION": "9.9.9",
                "LANTAI_OFFICECLI_ARCH": "local-script",
                "LANTAI_OFFICECLI_LICENSE": "Apache-2.0",
                "LANTAI_OFFICECLI_SOURCE_URL": "https://example.invalid/officecli",
            },
        )


def test_bundle_officecli_skips_when_optional_and_missing(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(officecli_bundle.shutil, "which", lambda _: None)

    dest = officecli_bundle.bundle_officecli(
        tmp_path / "lantai-server",
        required=False,
        env={"LANTAI_DISABLE_VENDORED_OFFICECLI": "1"},
    )

    assert dest is None


def test_bundle_officecli_required_reports_missing_env_path(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(officecli_bundle.shutil, "which", lambda _: None)
    missing = tmp_path / "missing-officecli"

    with pytest.raises(FileNotFoundError, match="LANTAI_OFFICECLI_BINARY"):
        officecli_bundle.resolve_officecli_source(
            required=True,
            env={
                "LANTAI_ALLOW_EXTERNAL_OFFICECLI": "1",
                "LANTAI_OFFICECLI_BINARY": str(missing),
                "LANTAI_DISABLE_VENDORED_OFFICECLI": "1",
            },
        )


def test_cli_skips_an_invalid_optional_candidate_but_required_stays_closed(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "officecli"
    source.write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(
        officecli_bundle,
        "resolve_officecli_source",
        lambda **_: (source, "vendored:test"),
    )
    monkeypatch.setattr(
        officecli_bundle,
        "bundle_officecli",
        lambda *_, **__: (_ for _ in ()).throw(RuntimeError("not release-signed")),
    )

    assert officecli_bundle.main([str(tmp_path / "optional")]) == 0
    assert (
        officecli_bundle.main([str(tmp_path / "required"), "--required"])
        == 1
    )


def test_official_backend_build_requires_officecli_before_electron_packaging():
    """Official desktop builds must fail before the expensive app signing step."""

    build_script = (
        Path(__file__).resolve().parents[3] / "electron" / "build-python.py"
    ).read_text(encoding="utf-8")

    assert '_backend_console_smoke_distribution() == "official"' in build_script
    assert 'cmd.append("--required")' in build_script
