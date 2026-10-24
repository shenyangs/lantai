# -*- coding: utf-8 -*-
"""Isolated extraction contracts for evidence-grounded user understanding."""

from types import SimpleNamespace

import pytest

from lantai.agents.persona.user_understanding import (
    extract_user_understanding_candidates,
)


@pytest.mark.asyncio
async def test_isolated_extraction_returns_a_pattern_with_the_user_source():
    async def utility_runner(**_kwargs):
        return SimpleNamespace(
            text=(
                '{"understandings":[{'
                '"kind":"decision_pattern",'
                '"statement":"用户在发布前反复修改时，可能真正担心公开后的不可逆评价。",'
                '"confidence":0.82,'
                '"evidence":"我还是想再改一遍",'
                '"future_relevance":"以后讨论发布取舍时"'
                '}]} '
            ),
        )

    candidates = await extract_user_understanding_candidates(
        agent_id="default",
        user_texts=["这个发布方案我还是想再改一遍。"],
        source_ids=["session_1:event_1"],
        utility_runner=utility_runner,
    )

    assert len(candidates) == 1
    assert candidates[0].kind == "decision_pattern"
    assert "不可逆评价" in candidates[0].statement
    assert candidates[0].evidence_ids == ("session_1:event_1",)

