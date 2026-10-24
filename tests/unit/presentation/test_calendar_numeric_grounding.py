# -*- coding: utf-8 -*-
"""Calendar punctuation must not become negative facts or prove arithmetic."""

import pytest

from lantai.presentation.page_authoring import (
    PresentationPageAuthorContentError,
    numeric_tokens,
    validate_grounded_numeric_text,
)


@pytest.mark.parametrize(
    "date_text,tokens",
    [
        ("2026-10-06", ["2026", "10", "06"]),
        ("2024-02-29", ["2024", "02", "29"]),
        ("0001-01-01", ["0001", "01"]),
        ("9999-12-31", ["9999", "12", "31"]),
    ],
)
@pytest.mark.parametrize(
    "wrapper",
    [
        "{}",
        "发布于{}，资料已归档。",
        "- {} 发布资料",
        "({})",
        "（{}）",
        "{}.",
    ],
)
def test_complete_valid_calendar_dates_preserve_unsigned_facts(
    date_text,
    tokens,
    wrapper,
):
    text = wrapper.format(date_text)
    assert numeric_tokens(text) == tokens
    assert numeric_tokens(text, preserve_percent=True) == tokens
    validate_grounded_numeric_text(
        text,
        f"资料发布日期为{date_text}。",
        location="date-test",
    )


@pytest.mark.parametrize(
    "text",
    [
        "2026-02-29",
        "2024-02-30",
        "2026-13-06",
        "2026-00-06",
        "0000-01-01",
        "2026-10-00",
        "2026-10-32",
        "2026-10-06+",
        "2026-10-06=",
        "2026-10-06=7+",
        "2026-10-06 +",
        "(2026-10-06)+",
        "-2026-10-06",
        "-(2026-10-06)",
        "+(2026-10-06)",
        "2026-10-06%",
        "2026-10-06.5",
        "2026-10-060",
        "12026-10-06",
    ],
)
def test_invalid_calendar_or_incomplete_expression_does_not_gain_date_proof(
    text,
):
    with pytest.raises(PresentationPageAuthorContentError) as exc:
        numeric_tokens(text)
    assert exc.value.violation_code == "unsupported_numeric_claim"


@pytest.mark.parametrize("claim", ["变动为-6", "变动为-10", "2026-11-06"])
def test_date_connectors_never_authorize_negative_or_unsourced_values(claim):
    with pytest.raises(PresentationPageAuthorContentError) as exc:
        validate_grounded_numeric_text(
            claim,
            "资料发布日期为2026-10-06。",
            location="date-test",
        )
    assert exc.value.violation_code == "unsupported_numeric_claim"
