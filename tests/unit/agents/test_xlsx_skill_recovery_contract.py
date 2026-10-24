# -*- coding: utf-8 -*-
"""Behavior locks for the independently implemented XLSX skill."""

from pathlib import Path


SKILLS_ROOT = Path(__file__).resolve().parents[3] / "src" / "lantai" / "agents" / "skills"


def test_xlsx_skills_define_grounded_transactional_editing() -> None:
    for locale in ("zh", "en"):
        content = (SKILLS_ROOT / f"xlsx-{locale}" / "SKILL.md").read_text(
            encoding="utf-8",
        )

        assert "version: 2.0.0" in content
        assert "license: Apache-2.0" in content
        assert "openpyxl" in content
        assert "OfficeCLI" in content
        assert "preflight.py" in content
        assert "writer" in content
        assert "--break-system-packages" not in content
