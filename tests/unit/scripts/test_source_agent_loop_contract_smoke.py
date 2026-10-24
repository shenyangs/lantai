# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Behavior locks for the opt-in real Agent Loop source smoke."""

from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.source_agent_loop_contract_smoke import (
    _assert_source_credentials_unchanged,
    _configure_keyring_environment,
    _disable_keyring_writes_for_smoke,
    _fingerprint_source_credentials,
    _path_is_within,
    _staged_source_configuration,
)


def _source_fixture(tmp_path: Path) -> tuple[Path, Path]:
    working_dir = tmp_path / "working"
    secret_dir = tmp_path / "working.secret"
    provider_dir = secret_dir / "providers" / "builtin"
    working_dir.mkdir()
    provider_dir.mkdir(parents=True)
    (working_dir / "config.json").write_text(
        json.dumps({"version": 1}),
        encoding="utf-8",
    )
    (secret_dir / ".master_key").write_text("ab" * 32, encoding="utf-8")
    (secret_dir / "providers" / "active_model.json").write_text(
        json.dumps({"provider_id": "kimi-codingplan", "model": "demo"}),
        encoding="utf-8",
    )
    (provider_dir / "kimi-codingplan.json").write_text(
        json.dumps(
            {
                "id": "kimi-codingplan",
                "api_key": "ENC:test-only-ciphertext",
            },
        ),
        encoding="utf-8",
    )
    return working_dir, secret_dir


def test_source_agent_loop_smoke_disables_keyring_by_default(
    monkeypatch,
) -> None:
    monkeypatch.delenv("LANTAI_SKIP_KEYRING", raising=False)
    monkeypatch.delenv("LANTAI_DISABLE_KEYRING", raising=False)

    _configure_keyring_environment(allow_keyring=False)

    assert os.environ["LANTAI_SKIP_KEYRING"] == "1"
    assert os.environ["LANTAI_DISABLE_KEYRING"] == "1"


def test_source_agent_loop_smoke_can_explicitly_use_keyring(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LANTAI_SKIP_KEYRING", "1")
    monkeypatch.setenv("LANTAI_DISABLE_KEYRING", "1")

    _configure_keyring_environment(allow_keyring=True)

    assert "LANTAI_SKIP_KEYRING" not in os.environ
    assert "LANTAI_DISABLE_KEYRING" not in os.environ


def test_source_agent_loop_smoke_disables_keyring_writes(
    monkeypatch,
) -> None:
    from lantai.security import secret_store

    writes = []

    def record_write(value: str) -> bool:
        writes.append(value)
        return True

    monkeypatch.setattr(
        secret_store,
        "_try_keyring_set",
        record_write,
    )

    _disable_keyring_writes_for_smoke()

    assert secret_store._try_keyring_set("do-not-store") is False
    assert not writes


def test_source_agent_loop_smoke_stages_credentials_before_mutable_load(
    tmp_path: Path,
) -> None:
    working_dir, secret_dir = _source_fixture(tmp_path)
    source_fingerprints = _fingerprint_source_credentials(secret_dir)

    with _staged_source_configuration(
        source_working_dir=working_dir,
        source_secret_dir=secret_dir,
    ) as staged:
        assert staged.working_dir != working_dir
        assert staged.secret_dir != secret_dir
        assert (staged.working_dir / "config.json").is_file()
        staged_active = staged.secret_dir / "providers" / "active_model.json"
        staged_active.unlink()
        (staged.secret_dir / ".master_key").write_text(
            "cd" * 32,
            encoding="utf-8",
        )

        _assert_source_credentials_unchanged(source_fingerprints)

    assert (secret_dir / "providers" / "active_model.json").is_file()
    assert (secret_dir / ".master_key").read_text(encoding="utf-8") == (
        "ab" * 32
    )


def test_source_agent_loop_smoke_detects_source_credential_drift(
    tmp_path: Path,
) -> None:
    working_dir, secret_dir = _source_fixture(tmp_path)
    del working_dir
    source_fingerprints = _fingerprint_source_credentials(secret_dir)
    active = secret_dir / "providers" / "active_model.json"
    active.write_text("{}", encoding="utf-8")

    try:
        _assert_source_credentials_unchanged(source_fingerprints)
    except RuntimeError as exc:
        assert "active_model" in str(exc)
    else:
        raise AssertionError("source credential drift was not detected")


def test_source_agent_loop_smoke_rejects_evidence_inside_source_dirs(
    tmp_path: Path,
) -> None:
    working_dir, secret_dir = _source_fixture(tmp_path)

    assert _path_is_within(working_dir / "evidence.json", working_dir)
    assert _path_is_within(secret_dir / "evidence.json", secret_dir)
    assert not _path_is_within(tmp_path / "outside.json", working_dir)
    assert not _path_is_within(tmp_path / "outside.json", secret_dir)
