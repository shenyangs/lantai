# -*- coding: utf-8 -*-
"""Runtime filters for builtin skills superseded by native tools."""

from __future__ import annotations

import json

from lantai.agents.skills_manager import resolve_effective_skills


def _write_workspace_skill(
    workspace_dir,
    *,
    name: str,
    source: str,
) -> None:
    skill_dir = workspace_dir / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# test\n", encoding="utf-8")
    (workspace_dir / "skill.json").write_text(
        json.dumps(
            {
                "skills": {
                    name: {
                        "enabled": True,
                        "channels": ["all"],
                        "source": source,
                    },
                },
            },
        ),
        encoding="utf-8",
    )


def test_retired_builtin_image_skill_is_not_injected_into_runtime(tmp_path) -> None:
    builtin_workspace = tmp_path / "builtin"
    custom_workspace = tmp_path / "custom"
    _write_workspace_skill(
        builtin_workspace,
        name="lantai-image-assets",
        source="builtin",
    )
    _write_workspace_skill(
        custom_workspace,
        name="lantai-image-assets",
        source="customized",
    )

    assert resolve_effective_skills(builtin_workspace, "console") == []
    assert resolve_effective_skills(custom_workspace, "console") == [
        "lantai-image-assets",
    ]
