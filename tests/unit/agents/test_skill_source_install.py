# -*- coding: utf-8 -*-
"""Behavior locks for installing Skills from ordinary local sources."""

from __future__ import annotations

import zipfile

from lantai.agents.skills_hub import install_skill_from_source
from lantai.agents.skills_manager import resolve_effective_skills


def _write_skill(skill_dir, name: str = "coffee") -> None:
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Use the installed coffee CLI safely.\n"
        "---\n"
        "Run `coffee --help` before the first real action.\n",
        encoding="utf-8",
    )


def test_install_skill_from_local_directory_is_enabled_immediately(
    tmp_path,
) -> None:
    project_root = tmp_path / "topic"
    source = project_root / "downloads" / "coffee"
    agent_home = tmp_path / "agent"
    _write_skill(source)

    result = install_skill_from_source(
        workspace_dir=agent_home,
        source="downloads/coffee",
        source_root=project_root,
        enable=True,
    )

    assert result.name == "coffee"
    assert result.enabled is True
    assert result.source_url == str(source.resolve())
    assert "coffee" in resolve_effective_skills(agent_home, "console")
    assert (agent_home / "skills" / "coffee" / "SKILL.md").is_file()


def test_install_skill_from_local_zip(tmp_path) -> None:
    project_root = tmp_path / "topic"
    source_dir = tmp_path / "source" / "coffee"
    source_zip = project_root / "coffee.zip"
    agent_home = tmp_path / "agent"
    _write_skill(source_dir)
    source_zip.parent.mkdir(parents=True)
    with zipfile.ZipFile(source_zip, "w") as archive:
        archive.write(source_dir / "SKILL.md", "coffee/SKILL.md")

    result = install_skill_from_source(
        workspace_dir=agent_home,
        source="coffee.zip",
        source_root=project_root,
        enable=True,
    )

    assert result.name == "coffee"
    assert result.enabled is True
    assert "coffee" in resolve_effective_skills(agent_home, "console")
