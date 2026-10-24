# -*- coding: utf-8 -*-
"""Tests for encrypted MCP connector persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from lantai.security.mcp_credentials import (
    protect_agent_mcp_credentials,
    reveal_agent_mcp_credentials,
)
from lantai.security.secret_store import SecretDecryptionError, is_encrypted


@pytest.fixture(autouse=True)
def _isolate_master_key(tmp_path: Path, monkeypatch):
    import lantai.security.secret_store as secret_store

    monkeypatch.setattr(secret_store, "_cached_master_key", bytes.fromhex("ab" * 32))
    monkeypatch.setattr(secret_store, "_cached_fernet", None)
    monkeypatch.setattr(secret_store, "_get_secret_dir", lambda: tmp_path)


def _agent_config() -> dict:
    return {
        "id": "default",
        "mcp": {
            "clients": {
                "docs": {
                    "headers": {"Authorization": "Bearer static-secret"},
                    "env": {"API_KEY": "env-secret", "EMPTY": ""},
                    "oauth": {
                        "client_secret": "client-secret",
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                    },
                },
            },
        },
    }


def test_mcp_credentials_roundtrip_and_do_not_mutate_input():
    raw = _agent_config()
    protected, changed = protect_agent_mcp_credentials(raw)

    assert changed is True
    assert raw["mcp"]["clients"]["docs"]["env"]["API_KEY"] == "env-secret"
    connector = protected["mcp"]["clients"]["docs"]
    assert is_encrypted(connector["headers"]["Authorization"])
    assert is_encrypted(connector["env"]["API_KEY"])
    assert connector["env"]["EMPTY"] == ""
    assert is_encrypted(connector["oauth"]["refresh_token"])
    assert reveal_agent_mcp_credentials(protected) == raw


def test_already_protected_credentials_are_not_reencrypted():
    once, changed = protect_agent_mcp_credentials(_agent_config())
    twice, changed_again = protect_agent_mcp_credentials(once)

    assert changed is True
    assert changed_again is False
    assert twice == once


def test_corrupt_connector_ciphertext_fails_closed():
    raw = _agent_config()
    raw["mcp"]["clients"]["docs"]["oauth"]["access_token"] = "ENC:broken"

    with pytest.raises(SecretDecryptionError):
        reveal_agent_mcp_credentials(raw)

