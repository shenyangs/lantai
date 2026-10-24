from __future__ import annotations

import re
from pathlib import Path

from lantai.agents.skills_manager import _validate_skill_content


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = PROJECT_ROOT / "src" / "lantai" / "agents" / "skills"

NEW_SKILLS = {
    "open-source-repository-research-zh": "open-source-repository-research",
    "co-thinking-zh": "co-thinking",
}


def test_research_and_cothinking_skills_are_valid_builtin_packages() -> None:
    for folder, expected_name in NEW_SKILLS.items():
        skill_md = SKILLS_ROOT / folder / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        name, description = _validate_skill_content(content)

        assert name == expected_name
        assert len(description) >= 30
        assert "[TODO" not in content
        assert not re.search(r"(?im)^\s*TODO(?:\s|:)", content)


def test_new_skill_relative_markdown_links_resolve() -> None:
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for folder in NEW_SKILLS:
        for markdown in (SKILLS_ROOT / folder).rglob("*.md"):
            content = markdown.read_text(encoding="utf-8")
            for raw_target in link_pattern.findall(content):
                target = raw_target.split("#", 1)[0].strip().strip("<>")
                if not target or target.startswith(
                    ("http://", "https://", "mailto:", "data:")
                ):
                    continue
                assert (markdown.parent / target).resolve().exists(), (
                    f"broken link in {markdown}: {raw_target}"
                )


def test_open_source_research_keeps_public_client_and_server_distinct() -> None:
    content = (
        SKILLS_ROOT / "open-source-repository-research-zh" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "未取得的服务端实现" in content
    assert "不得声称已掌握后端源码" in content
    assert "不运行安装脚本" in content


def test_co_thinking_requires_explicit_selection_and_user_synthesis() -> None:
    content = (SKILLS_ROOT / "co-thinking-zh" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "只在用户明确选择共思时使用" in content
    assert "AI 可以在用户说完后" in content
    assert "得到用户明确确认" in content
