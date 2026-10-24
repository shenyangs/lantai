from lantai.presentation.reference import (
    GOLDEN_TASKS,
    PATTERN_CELLS,
    REFERENCE_SOURCES,
    SCORING_RUBRIC,
    validate_reference_pack,
)


def test_reference_pack_release_constraints() -> None:
    validate_reference_pack()
    assert 30 <= len(REFERENCE_SOURCES) <= 50
    assert 120 <= len(PATTERN_CELLS) <= 180
    assert len(GOLDEN_TASKS) == 60
    assert sum(SCORING_RUBRIC.values()) == 100


def test_reference_pack_is_clean_room_metadata_only() -> None:
    assert all(source.usage == "pattern-study-only" for source in REFERENCE_SOURCES)
    assert not any(source.distributable_asset for source in REFERENCE_SOURCES)
    assert len({cell.id for cell in PATTERN_CELLS}) == len(PATTERN_CELLS)

