# -*- coding: utf-8 -*-
"""Cross-language governance for system and internal prompts."""

from lantai.agents.language_policy import (
    agent_language_support,
    canonical_prompt_language,
)
from lantai.agents.legacy_lantai_agent import build_ququ_prompt
from lantai.agents.persona.constitution import get_protected_temperament_prompt


def test_zh_and_en_are_full_while_ru_uses_complete_english_canon() -> None:
    assert agent_language_support("zh").level == "full"
    assert agent_language_support("en-US").canonical == "en"
    assert agent_language_support("ru").level == "partial"
    assert canonical_prompt_language("ru") == "en"
    assert canonical_prompt_language("unknown") == "en"


def test_ru_persona_and_ququ_do_not_mix_partial_chinese_scaffolding() -> None:
    persona = get_protected_temperament_prompt(canonical_prompt_language("ru"))
    ququ = build_ququ_prompt(baseline_selected=True, language="ru")

    assert "## Identity" in persona
    assert "Inner aside" in ququ
    assert "蛐蛐" not in ququ
    assert '<ququ-route baseline_selected="true" hard_required="true" />' in ququ
