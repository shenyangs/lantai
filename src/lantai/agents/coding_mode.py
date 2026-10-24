# -*- coding: utf-8 -*-
"""Coding Mode prompt helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


TRUTHY_CODING_MODE_VALUES = {"1", "true", "yes", "on", "coding"}


def is_coding_mode_enabled(request_context: dict[str, Any] | None) -> bool:
    """Return whether the current turn explicitly opted into Coding Mode."""
    value = (request_context or {}).get("coding_mode")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_CODING_MODE_VALUES
    return False


def build_coding_mode_prompt(
    *,
    request_context: dict[str, Any] | None,
    runtime_workspace_dir: str | Path | None,
) -> str:
    """Build the short, Lantai-specific Coding Mode system prompt."""
    if not is_coding_mode_enabled(request_context):
        return ""

    workspace = (
        str(runtime_workspace_dir)
        or str((request_context or {}).get("workspace_dir") or "")
        or "the active Lantai workspace"
    )
    coding_context = str((request_context or {}).get("coding_context") or "").strip()
    context_hint = (
        "\n【Current Coding Context】\n"
        f"{coding_context[:8000]}\n"
        if coding_context
        else ""
    )
    coding_state = str(
        (request_context or {}).get("coding_state_snapshot") or ""
    ).strip()
    state_hint = (
        "\n【Recoverable Coding State】\n"
        f"{coding_state[:8000]}\n"
        if coding_state
        else ""
    )
    try:
        from .tools.coding import coding_tool_prompt_hint

        tool_hint = "\n" + coding_tool_prompt_hint(request_context)
    except Exception:
        tool_hint = ""
    return (
        "【Coding Mode】\n"
        f"- Work inside the active Lantai workspace: `{workspace}`.\n"
        "- Treat the workspace file tree and inline diff review as the source "
        "of truth for visible edits.\n"
        "- Inspect relevant files before changing them, keep edits scoped, and "
        "run the smallest useful verification.\n"
        "- Preserve coding state across long runs and context compaction: keep "
        "track of the active file, open tabs, dirty drafts, pending Git state, "
        "decisions, tests run, and next recovery step.\n"
        "- Prefer safe file writes and tests over broad rewrites. Never run "
        "`git reset`, `git clean`, destructive discard, or force operations "
        "unless the user explicitly asks and approval/guardrails allow it.\n"
        f"{context_hint}"
        f"{state_hint}"
        f"{tool_hint}\n"
        "- When you finish, summarize changed files, validation, and any "
        "remaining website-release, Developer ID, permission, or sandbox caveats."
    )
