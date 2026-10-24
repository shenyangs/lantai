# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib

import pytest

import lantai.app.officecli_safe_bridge as bridge
from lantai.app.officecli_safe_bridge import (
    OfficeCliSafetyError,
    cleanup_officecli_transaction,
    commit_officecli_transaction,
    prepare_officecli_mcp_call,
)


def test_read_only_commands_pass_without_a_transaction(tmp_path):
    source = tmp_path / "report.docx"
    source.write_bytes(b"source")

    assert prepare_officecli_mcp_call(
        {"command": ["view", str(source), "text"]},
    ) is None


def test_write_commands_target_an_isolated_copy_and_commit_atomically(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bridge, "WORKING_DIR", tmp_path / "working")
    source = tmp_path / "report.docx"
    source.write_bytes(b"before")
    before_hash = hashlib.sha256(b"before").hexdigest()

    transaction = prepare_officecli_mcp_call(
        {"command": ["set", str(source), "/body/p[1]", "--prop", "bold=true"]},
    )

    assert transaction is not None
    assert transaction.arguments["command"][1] != str(source)
    assert transaction.working_path.read_bytes() == b"before"
    transaction.working_path.write_bytes(b"after")
    receipt = commit_officecli_transaction(transaction)

    assert source.read_bytes() == b"after"
    assert receipt.before_sha256 == before_hash
    assert receipt.after_sha256 == hashlib.sha256(b"after").hexdigest()
    assert receipt.atomic is True
    assert receipt.backup_path
    assert bridge.Path(receipt.backup_path).read_bytes() == b"before"
    cleanup_officecli_transaction(transaction)
    assert not transaction.temporary_dir.exists()


def test_source_hash_change_aborts_commit_without_overwriting(tmp_path):
    source = tmp_path / "book.xlsx"
    source.write_bytes(b"baseline")
    transaction = prepare_officecli_mcp_call(
        {"command": ["set", str(source), "/Sheet1/A1", "--prop", "value=2"]},
    )
    assert transaction is not None
    transaction.working_path.write_bytes(b"agent-result")
    source.write_bytes(b"external-change")

    with pytest.raises(OfficeCliSafetyError, match="source changed"):
        commit_officecli_transaction(transaction)

    assert source.read_bytes() == b"external-change"
    cleanup_officecli_transaction(transaction)


@pytest.mark.parametrize("verb", ["open", "save", "close", "watch", "install", "plugins"])
def test_bypass_and_install_verbs_are_blocked(verb, tmp_path):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"deck")

    with pytest.raises(OfficeCliSafetyError, match="blocked"):
        prepare_officecli_mcp_call({"command": [verb, str(source)]})


def test_create_uses_a_temporary_output_and_preserves_new_file_semantics(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(bridge, "WORKING_DIR", tmp_path / "working")
    target = tmp_path / "new.pptx"
    transaction = prepare_officecli_mcp_call(
        {"command": ["create", str(target)]},
    )
    assert transaction is not None
    assert transaction.existed is False
    transaction.working_path.write_bytes(b"new-deck")

    receipt = commit_officecli_transaction(transaction)

    assert target.read_bytes() == b"new-deck"
    assert receipt.backup_path == ""
    cleanup_officecli_transaction(transaction)
