# -*- coding: utf-8 -*-
"""Behavior locks for reading enabled skill resources inside the skill sandbox."""

from __future__ import annotations

import json

import pytest

from lantai.agents.tools.read_skill import read_skill
from lantai.config.context import (
    WorkspaceContext,
    set_current_workspace_context,
    set_current_workspace_dir,
)


def _install_enabled_skill(workspace, *, enabled: bool = True) -> None:
    skill_dir = workspace / "skills" / "pptx"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create and validate PowerPoint presentations.\n"
        "---\n"
        "Read pptxgenjs.md before generating a deck.\n",
        encoding="utf-8",
    )
    (skill_dir / "pptxgenjs.md").write_text(
        "# PptxGenJS\nGenerate, render, inspect, then deliver the pptx.\n",
        encoding="utf-8",
    )
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "render.py").write_text(
        "print('rendered')\n",
        encoding="utf-8",
    )
    (workspace / "skill.json").write_text(
        json.dumps(
            {
                "skills": {
                    "pptx": {
                        "enabled": enabled,
                        "channels": ["all"],
                        "source": "builtin",
                    },
                },
            },
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_read_skill_loads_manifest_and_root_resource(tmp_path) -> None:
    _install_enabled_skill(tmp_path)
    set_current_workspace_dir(tmp_path)
    try:
        manifest = await read_skill("pptx")
        guide = await read_skill("pptx", "pptxgenjs.md")
    finally:
        set_current_workspace_dir(None)

    assert manifest.is_error is False
    assert "Create and validate PowerPoint" in manifest.content[0]["text"]
    assert "list_skill_resources" in manifest.content[0]["text"]
    assert "read_skill(skill_name, file_path)" in manifest.content[0]["text"]
    assert "copy_skill_resource" in manifest.content[0]["text"]
    assert "run_skill_script" in manifest.content[0]["text"]
    assert "JSON array of separate strings" in manifest.content[0]["text"]
    assert 'arguments=["--flag", "value"]' in manifest.content[0]["text"]
    assert "scripts/render.py" in manifest.content[0]["text"]
    assert str(tmp_path) not in manifest.content[0]["text"]
    assert guide.is_error is False
    assert "Generate, render, inspect" in guide.content[0]["text"]


@pytest.mark.asyncio
async def test_read_skill_rejects_disabled_skill_and_traversal(tmp_path) -> None:
    _install_enabled_skill(tmp_path, enabled=False)
    set_current_workspace_dir(tmp_path)
    try:
        disabled = await read_skill("pptx")
        traversal = await read_skill("pptx", "../secret.txt")
    finally:
        set_current_workspace_dir(None)

    assert disabled.is_error is True
    assert "not enabled" in disabled.content[0]["text"]
    assert disabled.metadata == {
        "error_code": "skill_not_enabled",
        "deterministic_failure": True,
    }
    assert traversal.is_error is True
    assert "safe relative path" in traversal.content[0]["text"]
    assert traversal.metadata == {
        "error_code": "invalid_skill_resource_path",
        "deterministic_failure": True,
    }


@pytest.mark.asyncio
async def test_read_skill_marks_missing_resource_as_deterministic(tmp_path) -> None:
    _install_enabled_skill(tmp_path)
    set_current_workspace_dir(tmp_path)
    try:
        response = await read_skill("pptx", "pptx")
    finally:
        set_current_workspace_dir(None)

    assert response.is_error is True
    assert "was not found" in response.content[0]["text"]
    assert response.metadata == {
        "error_code": "skill_resource_not_found",
        "deterministic_failure": True,
    }


@pytest.mark.asyncio
async def test_read_skill_uses_agent_home_from_topic_workspace_context(
    tmp_path,
) -> None:
    agent_home = tmp_path / "agent"
    topic_workspace = agent_home / "topic-workspaces" / "chat-1"
    topic_workspace.mkdir(parents=True)
    _install_enabled_skill(agent_home)
    set_current_workspace_context(
        WorkspaceContext(
            agent_home=agent_home,
            state_root=topic_workspace,
            project_root=topic_workspace,
        ),
    )
    try:
        response = await read_skill("pptx")
    finally:
        set_current_workspace_context(None)

    assert response.is_error is False
    assert "Create and validate PowerPoint" in response.content[0]["text"]
