from __future__ import annotations

import pytest

from lantai.presentation.models import DeckIR, DeckSlide, TextObject
from lantai.presentation.natural_edit import plan_text_edit


def _deck() -> DeckIR:
    return DeckIR(
        id="edit-deck",
        title="编辑验证",
        slides=[
            DeckSlide(
                id="slide-a",
                layout_id="cover",
                objects=[
                    TextObject(
                        id="title-a",
                        role="title",
                        slot="title",
                        text="旧标题",
                    ),
                    TextObject(
                        id="subtitle-a",
                        role="body",
                        slot="subtitle",
                        text="保留这句",
                    ),
                ],
            ),
            DeckSlide(
                id="slide-b",
                layout_id="closing",
                objects=[
                    TextObject(
                        id="title-b",
                        role="title",
                        slot="title",
                        text="不能越界修改",
                    )
                ],
            ),
        ],
    )


def test_title_instruction_changes_only_the_target_slide() -> None:
    deck = _deck()
    changed, replacements = plan_text_edit(
        deck,
        "slide-a",
        "把本页标题改为：更清楚的新标题",
    )

    assert replacements[0].object_id == "title-a"
    assert replacements[0].text == "更清楚的新标题"
    assert changed.slides[0].objects[0].text == "更清楚的新标题"
    assert changed.slides[0].objects[1].text == "保留这句"
    assert changed.slides[1] == deck.slides[1]
    assert deck.slides[0].objects[0].text == "旧标题"


def test_quoted_instruction_replaces_matching_text_inside_target_slide() -> None:
    changed, replacements = plan_text_edit(
        _deck(),
        "slide-a",
        "将“保留”替换为“继续保留”",
    )

    assert replacements[0].object_id == "subtitle-a"
    assert changed.slides[0].objects[1].text == "继续保留这句"


@pytest.mark.parametrize(
    "instruction, message",
    [
        ("让它更高级", "安全映射"),
        ("标题改为旧标题", "没有产生实际变化"),
        ("将“并不存在”改为“新文”", "未找到"),
    ],
)
def test_unresolved_or_noop_instruction_fails_closed(
    instruction: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        plan_text_edit(_deck(), "slide-a", instruction)
