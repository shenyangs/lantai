# -*- coding: utf-8 -*-
"""Tests for lightweight skill index helpers."""

from __future__ import annotations

import json

from lantai.app.routers.skills import _build_workspace_skill_index


def test_workspace_skill_index_excludes_full_content(tmp_path):
    skills_dir = tmp_path / "skills" / "hello"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        """---
name: hello
description: Say hello
version: 1.2.3
metadata:
  lantai:
    emoji: 👋
---

# Hello

This is the long body that should not be returned by the index.
""",
        encoding="utf-8",
    )
    (tmp_path / "skill.json").write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "version": 1,
                "skills": {
                    "hello": {
                        "enabled": True,
                        "channels": ["console"],
                        "source": "customized",
                        "tags": ["demo"],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    index = _build_workspace_skill_index(tmp_path)

    assert len(index) == 1
    item = index[0]
    assert item.name == "hello"
    assert item.description == "Say hello"
    assert item.version == "1.2.3"
    assert item.enabled is True
    assert item.channels == ["console"]
    assert item.tags == ["demo"]
    assert not hasattr(item, "content")
