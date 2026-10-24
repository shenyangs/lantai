from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_presentation_skills_route_to_clean_room_v3_entrypoint() -> None:
    for language in ("zh", "en"):
        skill = (ROOT / f"src/lantai/agents/skills/pptx-{language}/SKILL.md").read_text(
            encoding="utf-8"
        )
        wrapper = (
            ROOT
            / f"src/lantai/agents/skills/pptx-{language}/scripts/run_presentation_v3.py"
        )

        assert wrapper.is_file()
        assert "scripts/run_presentation_v3.py" in skill
        assert "Presentation Skill v3" in skill
        assert "PptxGenJS" in skill
        assert "GenOffice" in skill
        assert "render" in skill.lower() or "渲染" in skill
        assert "visual" in skill.lower() or "视觉" in skill
        assert "generate_presentation.py" not in skill
        assert not (
            ROOT / f"src/lantai/agents/skills/pptx-{language}/scripts/generate.py"
        ).exists()
        assert "Proprietary" not in skill
