from __future__ import annotations

from lantai.presentation.naming import presentation_filename, unique_presentation_path


def test_presentation_filename_uses_the_deck_topic_and_sanitizes_it() -> None:
    assert presentation_filename("会写代码的办公 Agent：来了？.pptx") == (
        "会写代码的办公 Agent-来了.pptx"
    )


def test_presentation_filename_has_a_portable_fallback() -> None:
    assert presentation_filename(" /:*? ") == "澜台演示文稿.pptx"
    assert presentation_filename("CON") == "澜台演示文稿-CON.pptx"


def test_unique_presentation_path_preserves_existing_decks(tmp_path) -> None:
    existing = tmp_path / "项目复盘.pptx"
    existing.write_bytes(b"old")

    assert unique_presentation_path(tmp_path, "项目复盘") == (
        tmp_path / "项目复盘 (2).pptx"
    )
