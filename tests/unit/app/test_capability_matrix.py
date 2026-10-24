# -*- coding: utf-8 -*-
"""Behavior locks for the cross-edition capability matrix."""

from __future__ import annotations

from lantai.app.capabilities.policy import build_capability_matrix


REQUIRED_CAPABILITIES = {
    "chat_runtime",
    "filesystem_read",
    "filesystem_write",
    "filesystem_external_grant",
    "browser_read_navigation",
    "browser_write",
    "browser_upload_download",
    "shell_python_subprocess",
    "mcp_plugin_network",
    "background_scheduled_execution",
    "persistent_resume",
}


def test_capability_matrix_covers_runtime_migration_required_surfaces() -> None:
    matrix = build_capability_matrix(distribution="developer_id")

    assert matrix.distribution == "developer_id"
    assert REQUIRED_CAPABILITIES.issubset(matrix.entries)
    assert {entry.mode for entry in matrix.entries.values()} <= {
        "enabled",
        "read_only",
        "interactive_only",
        "disabled",
    }
    assert all(entry.reason_code for entry in matrix.entries.values())


def test_app_store_capability_matrix_has_machine_readable_degradations() -> None:
    matrix = build_capability_matrix(distribution="app_store")

    assert matrix.entries["chat_runtime"].mode == "enabled"
    assert matrix.entries["browser_read_navigation"].mode == "read_only"
    assert matrix.entries["browser_read_navigation"].reason_code == "mas_browser_read_only"
    assert matrix.entries["browser_write"].mode == "disabled"
    assert matrix.entries["browser_write"].reason_code == "mas_browser_write_disabled"
    assert matrix.entries["browser_upload_download"].mode == "interactive_only"
    assert (
        matrix.entries["browser_upload_download"].reason_code
        == "mas_security_scoped_bookmark_required"
    )
    assert matrix.entries["shell_python_subprocess"].mode == "disabled"
    assert matrix.entries["persistent_resume"].mode == "interactive_only"
