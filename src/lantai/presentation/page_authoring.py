# -*- coding: utf-8 -*-
"""Fail-closed boundary between a page author and the native PPTX writer."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Sequence
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext

from pydantic import ValidationError

from .models import (
    ChartObject,
    CompositionAuthority,
    DeckPlanSlide,
    DeckSlide,
    ImageObject,
    MetricObject,
    PresentationBrief,
    PresentationJobRequest,
    ShapeObject,
    SourceCalculationProof,
    TableObject,
    TextObject,
)


class PresentationPageAuthorContentError(ValueError):
    """An authored scene violated a deterministic source-content contract."""

    def __init__(self, message: str, *, violation_code: str) -> None:
        super().__init__(message)
        self.violation_code = violation_code


class PresentationSemanticContentError(ValueError):
    """A semantic-plan result violated an authored content contract."""

    def __init__(self, message: str, *, violation_code: str) -> None:
        super().__init__(message)
        self.violation_code = violation_code


class PresentationPageAuthorSceneError(ValueError):
    """An authored scene violated native-object relationship integrity."""

    def __init__(self, message: str, *, violation_code: str) -> None:
        super().__init__(message)
        self.violation_code = violation_code


def scope_authored_page_object_ids(slide: DeckSlide) -> DeckSlide:
    """Make page-local author ids safe inside one deck.

    Page authors work on one scene at a time and naturally reuse local names
    such as ``title`` or ``background``.  DeckIR requires object identity to be
    unique across all pages, so the runtime owns this deterministic identity
    translation.  Native group and relationship references are rewritten in
    the same operation; visible content, geometry, and data remain untouched.
    """

    prefix = f"{slide.id}::"
    id_map = {
        obj.id: obj.id if obj.id.startswith(prefix) else f"{prefix}{obj.id}"
        for obj in slide.objects
    }
    if slide.template_anchors is not None:
        id_map.update({
            use.target_object_id: (
                use.target_object_id if use.target_object_id.startswith(prefix)
                else f"{prefix}{use.target_object_id}"
            )
            for use in slide.template_anchors.uses
        })
    if all(old_id == new_id for old_id, new_id in id_map.items()):
        return slide
    objects = [
        obj.model_copy(update={"id": id_map[obj.id]})
        for obj in slide.objects
    ]
    groups = [
        group.model_copy(update={
            "object_ids": [
                id_map.get(item, item) for item in group.object_ids
            ],
        })
        for group in slide.object_groups
    ]
    relationships = [
        relationship.model_copy(update={
            "source_object_ids": [
                id_map.get(item, item)
                for item in relationship.source_object_ids
            ],
            "target_object_ids": [
                id_map.get(item, item)
                for item in relationship.target_object_ids
            ],
        })
        for relationship in slide.relationships
    ]
    return slide.model_copy(update={
        "objects": objects,
        "object_groups": groups,
        "relationships": relationships,
        "template_anchors": (
            slide.template_anchors.model_copy(update={
                "uses": [use.model_copy(update={"target_object_id": id_map[use.target_object_id]})
                         for use in slide.template_anchors.uses],
            }) if slide.template_anchors is not None else None
        ),
        "scene_hash": None,
    })


def grounded_source_text(
    request: PresentationJobRequest,
    brief: PresentationBrief,
) -> str:
    """Only actual user/material prose may authorize a page's numeric claims.

    Never include storyboard, IDs, source hashes or coverage metadata: each
    can contain model-invented or incidental digits.
    """

    return "\n".join(
        [
            request.user_request,
            request.audience,
            brief.source_title,
            *brief.required_facts,
            *brief.immutable_data,
            *(
                text
                for section in brief.source_sections
                for text in (section.document_title, section.title, section.body)
            ),
        ],
    )


def _visible_text(slide: DeckSlide, *, exclude_page_marker: str | None = None) -> str:
    parts: list[str] = []
    for obj in slide.objects:
        if isinstance(obj, TextObject):
            if (
                exclude_page_marker is not None
                and obj.role == "meta"
                and obj.slot == "page-marker"
                and obj.text == exclude_page_marker
            ):
                continue
            parts.append(obj.text)
            parts.extend(run.text for run in obj.runs)
        elif isinstance(obj, MetricObject):
            parts.extend((obj.label, obj.value, obj.detail))
        elif isinstance(obj, TableObject):
            parts.extend(obj.headers)
            parts.extend(cell for row in obj.rows for cell in row)
        elif isinstance(obj, ShapeObject):
            parts.append(obj.text)
    return "\n".join(parts)


def _chart_data(chart: ChartObject) -> dict[str, object]:
    return chart.model_dump(
        mode="json",
        exclude={"id", "slot", "frame"},
    )


_SOURCE_NUMBER = re.compile(
    r"(?<![0-9A-Za-z.])"
    r"(?P<value>(?:[-−－﹣][^\S\r\n\v\f\x85\u2028\u2029]*)?"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?P<percent>(?:[^\S\r\n\v\f\x85\u2028\u2029]*[%％])?)",
)
_SOURCE_CONNECTED_ATOM = re.compile(
    r"(?<![0-9A-Za-z.])[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*"
    r"(?:[-−－﹣/][0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)+"
    r"(?![0-9A-Za-z.])",
)
_MINUS_TRANSLATION = str.maketrans("−－﹣", "---")
_NUMBER_LINE_BREAKS = "\r\n\v\f\x85\u2028\u2029"
_SOURCE_ZH_INTEGER = re.compile(
    r"(?<![零〇一二两三四五六七八九十])"
    r"(?:第)?(?P<value>[零〇一二两三四五六七八九十]{1,3})"
    r"(?=(?:个|位|周|类|轮|处|项|次|分钟|小时|季度|阶段|页|组|份|套|条|点|步|秒|天|月|年|名|人|家))",
)
_ZH_DIGIT_VALUES = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_EQUATION_NUMBER = re.compile(
    r"(?<![0-9A-Za-z.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
)
# A compound counter is one unit, not a repetition of its components. Keep
# this explicit so malformed suffixes such as 人次次 or 万元元 remain invalid.
_EQUATION_UNIT = re.compile(
    r"[%％]|[万千百亿]?元|人(?:次|天|月|年)?|公斤|千克|克|个|家|次|天|月|年|季度"
)
_QUANTITY_SUFFIX = re.compile(r"[%％]|[^\W\d_]+")
_EQUATION_OPERATORS = "-−－﹣+＋*/×÷=＝()（）"
_CALENDAR_DATE = re.compile(
    r"(?<![0-9A-Za-z_.])(?P<year>[0-9]{4})-"
    r"(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"(?![0-9A-Za-z_]|\.[0-9])"
)


def _single_quantity_suffix(text: str, position: int) -> re.Match[str] | None:
    """Read an opaque lexical suffix, not an arithmetic unit or prose claim.

    A strictly paired single quantity or compact range does not require a
    vocabulary of every possible unit. Unknown letter suffixes are indivisible
    and never establish an equation. Keep the existing repeated-known-unit
    guard instead of interpreting arbitrary words as multiple units.
    """

    suffix = _QUANTITY_SUFFIX.match(text, position)
    if suffix is None:
        return None
    unit_position = position
    unit_count = 0
    while unit_position < suffix.end():
        unit = _EQUATION_UNIT.match(text, unit_position)
        if unit is None:
            return suffix
        unit_position = unit.end()
        unit_count += 1
    return None if unit_count > 1 else suffix


def _arithmetic_operator_positions(
    tokens: list[tuple[str, int]],
) -> set[int] | None:
    """Validate one numeric expression and distinguish binary/unary signs.

    This is a syntax check, not a calculator or a source-fact inference. Both
    sides of an explicit equation must be complete before any operator is
    normalized. Parentheses must match; signs where an operand is expected
    remain unary, including after a binary minus or inside parentheses.
    A complete number or parenthesized quantity may take one supported unit.
    """

    expect_operand = True
    may_take_unit = False
    parentheses: list[str] = []
    binary_operators: set[int] = set()
    for kind, position in tokens:
        if expect_operand:
            if kind in "-−－﹣+＋":
                continue
            if kind in {"(", "（"}:
                parentheses.append(kind)
                if len(parentheses) > 64:
                    return None
                continue
            if kind != "number":
                return None
            expect_operand = False
            may_take_unit = True
        elif kind in ")）":
            opening = "(" if kind == ")" else "（"
            if not parentheses or parentheses.pop() != opening:
                return None
            may_take_unit = True
        elif kind == "unit":
            if not may_take_unit:
                return None
            may_take_unit = False
        elif kind in "-−－﹣+＋*/×÷":
            binary_operators.add(position)
            expect_operand = True
            may_take_unit = False
        else:
            return None
    if expect_operand or parentheses:
        return None
    return binary_operators


def _complete_equation_operator_positions(
    tokens: list[tuple[str, int]],
) -> set[int] | None:
    if not any(kind in {"=", "＝"} for kind, _ in tokens):
        return None
    binary_operators: set[int] = set()
    start = 0
    for index in range(len(tokens) + 1):
        if index < len(tokens) and tokens[index][0] not in {"=", "＝"}:
            continue
        positions = _arithmetic_operator_positions(tokens[start:index])
        if positions is None:
            return None
        binary_operators.update(positions)
        start = index + 1
    return binary_operators


def _numeric_parenthesis_pairs(text: str) -> dict[int, int]:
    """Index strict same-line wrappers, including prose annotations, once."""

    pairs: dict[int, int] = {}
    stack: list[tuple[str, int]] = []
    over_limit = False
    for position, char in enumerate(text):
        if char in _NUMBER_LINE_BREAKS:
            stack.clear()
            over_limit = False
        elif over_limit:
            continue
        elif char in {"(", "（"}:
            if len(stack) == 64:
                stack.clear()
                over_limit = True
            else:
                stack.append((char, position))
        elif char in {")", "）"}:
            opening = "(" if char == ")" else "（"
            if not stack or stack[-1][0] != opening:
                stack.clear()
                continue
            _, start = stack.pop()
            pairs[start] = position
            pairs[position] = start
    return pairs


def _compact_range_operator_positions(
    tokens: list[tuple[str, int]], text: str, pairs: dict[int, int],
) -> tuple[set[int], dict[int, str]]:
    """Prove each complete unsigned two-endpoint range independently.

    ``2025年-2026年(95人)`` contains a range and a separate annotation.
    Adjacent numeric atoms or prose annotations do not change its endpoints.
    The connector must touch both unsigned endpoint quantities; each may
    take one unit and strict single-atom wrappers. Strict paired wrappers
    may surround the range, with one outer unit
    only when neither endpoint already has one. Extra signs/operators,
    repeated units and incomplete equations cannot use this exception.
    No values are inferred or calculated, and no prose is classified.
    """

    if any(kind in {"=", "＝"} for kind, _ in tokens):
        # Equations require the complete-expression proof, never a local
        # range exception which could discard an incomplete remainder.
        return set(), {}

    def quantity(index: int) -> tuple[int, int, str, int]:
        start = tokens[index][1]
        number = _EQUATION_NUMBER.match(text, start)
        assert number is not None
        end = number.end()
        unit_text = ""

        def take_unit() -> None:
            nonlocal end, unit_text
            unit = _single_quantity_suffix(text, _number_space_end(text, end))
            if unit is not None and not unit_text:
                end, unit_text = unit.end(), unit.group()

        take_unit()
        for _ in range(64):
            opening = _number_space_start(text, start) - 1
            closing = _number_space_end(text, end)
            if opening < 0 or pairs.get(opening) != closing:
                break
            # Only a fully paired single quantity belongs to an endpoint.
            # A wrapper containing another operand is handled as a whole
            # range below; unary signs are never swallowed here.
            start, end = opening, closing + 1
            take_unit()
        return start, end, unit_text, number.end()

    quantities = [quantity(index) for index, (kind, _) in enumerate(tokens)
                  if kind == "number"]
    by_start = {item[0]: item for item in quantities}
    operators = {position: kind for kind, position in tokens
                 if kind in "-−－﹣"}

    positions: set[int] = set()
    percent_ends: dict[int, str] = {}
    for start, position, left_unit, left_number_end in quantities:
        right = by_start.get(position + 1)
        if position not in operators or right is None:
            continue
        _, end, right_unit, right_number_end = right
        if any(
            unit and _EQUATION_UNIT.fullmatch(unit) is None
            for unit in (left_unit, right_unit)
        ) and (not left_unit or left_unit != right_unit):
            # Opaque suffixes prove only two like-suffixed endpoints. A
            # suffix must not swallow intervening prose and turn a later
            # unary fact into a range, e.g. 2025年的增长率为-17%. This is a
            # lexical equality check, never a classification of the words.
            continue
        range_start = start
        before = _number_space_start(text, range_start) - 1
        while before >= 0 and text[before] in {"(", "（"}:
            if pairs.get(before, -1) < end:
                break
            range_start = before
            before = _number_space_start(text, range_start) - 1
        if before >= 0 and (
            (text[before] in "-−－﹣+＋*/×÷=＝(（"
             and not _is_number_list_marker(text, before, range_start))
            or (text[before] in ")）" and before not in pairs)
        ):
            continue

        after = _number_space_end(text, end)
        unit_taken = bool(left_unit or right_unit)
        # A compact 5-17% range has a single shared percent suffix. Preserve
        # its dimension on both endpoints, just as (5-17)% does; never turn
        # either percentage into an untyped ratio in planner/retry guidance.
        shared_percent = right_unit if right_unit in {"%", "％"} and not left_unit else ""
        valid = True
        while after < len(text) and text[after] in ")）":
            if after not in pairs or pairs[after] > start:
                valid = False
                break
            after = _number_space_end(text, after + 1)
            unit = _single_quantity_suffix(text, after)
            if unit is not None:
                if unit_taken:
                    if _EQUATION_UNIT.match(text, after) is not None:
                        valid = False
                        break
                    # An unknown word outside an already unit-bearing range
                    # can be trailing prose; it cannot provide a second unit.
                    break
                unit_taken = True
                if unit.group() in {"%", "％"}:
                    shared_percent = unit.group()
                after = _number_space_end(text, unit.end())
        if not valid or (
            after < len(text) and (
                text[after] in "-−－﹣+＋*/×÷=＝"
                or _EQUATION_UNIT.match(text, after) is not None
                or (text[after] in "(（" and after not in pairs)
            )
        ):
            continue
        positions.add(position)
        if shared_percent:
            percent_ends[left_number_end] = shared_percent
            percent_ends[right_number_end] = shared_percent
    return positions, percent_ends


def _calendar_date_connector_positions(
    text: str, pairs: dict[int, int],
) -> set[int]:
    """Prove only the two separators of a complete, valid ISO calendar atom.

    This does not evaluate arithmetic or infer facts. Date components still
    undergo the same per-source numeric grounding as every other number.
    An attached sign, unit, unmatched wrapper or arithmetic tail supplies no
    date proof; complete equations keep their independent syntax checks.
    """

    positions: set[int] = set()
    for match in _CALENDAR_DATE.finditer(text):
        try:
            date(
                *(int(match.group(part)) for part in ("year", "month", "day")),
            )
        except ValueError:
            continue
        start, end = match.span()
        valid = True
        # Peel only strictly paired wrappers around this date, never a
        # containing expression or a signed quantity. Keep the shared limit.
        for depth in range(65):
            opening = _number_space_start(text, start) - 1
            closing = _number_space_end(text, end)
            if opening < 0 or text[opening] not in "(（":
                break
            if depth == 64 or pairs.get(opening) != closing:
                valid = False
                break
            start, end = opening, closing + 1
        if not valid:
            continue
        before = _number_space_start(text, start) - 1
        after = _number_space_end(text, end)
        if before >= 0 and text[before] in _EQUATION_OPERATORS:
            if not _is_number_list_marker(text, before, start):
                continue
        if after < len(text) and (
            text[after] in _EQUATION_OPERATORS
            or _EQUATION_UNIT.match(text, after) is not None
        ):
            continue
        positions.update((match.start("month") - 1, match.start("day") - 1))
    return positions


def _numeric_operator_positions(
    text: str,
) -> tuple[set[int], set[int], set[int], dict[int, str]]:
    """Recognize complete equations, numeric ranges and valid calendar atoms.

    Prose and punctuation end a mathematical segment. Units alone do not
    establish arithmetic: a table's ``2025\t-17%`` stays signed. Token offsets
    let each caller normalize binary operators once without changing unary
    source facts or repeatedly scanning the whole source for each number.
    """

    pairs = _numeric_parenthesis_pairs(text)
    positions: set[int] = _calendar_date_connector_positions(text, pairs)
    boundaries: set[int] = set()
    # Both ranges and dates use unsigned connectors in source retry guidance.
    range_positions: set[int] = set(positions)
    range_percent_ends: dict[int, str] = {}

    def finish_segment(tokens: list[tuple[str, int]]) -> None:
        positions.update(_complete_equation_operator_positions(tokens) or set())
        compact_ranges, percent_ends = _compact_range_operator_positions(tokens, text, pairs)
        positions.update(compact_ranges)
        range_positions.update(compact_ranges)
        range_percent_ends.update(percent_ends)
        # A left operand is a boundary even if the remaining expression is
        # incomplete. This never authorizes normalization: it only prevents
        # a sign chain from swallowing an unproven binary operator.
        expect_operand = True
        for kind, position in tokens:
            if kind in {"number", ")", "）", "unit", "suffix"}:
                expect_operand = False
            elif kind in "-−－﹣+＋*/×÷":
                if not expect_operand:
                    boundaries.add(position)
                expect_operand = True
            elif kind in {"(", "（", "=", "＝"}:
                expect_operand = True

    offset = 0
    for line in text.splitlines(keepends=True):
        tokens: list[tuple[str, int]] = []
        index = 0
        while index < len(line):
            char = line[index]
            if char.isspace() and char not in _NUMBER_LINE_BREAKS:
                index += 1
                continue
            number = _EQUATION_NUMBER.match(line, index)
            if number is not None:
                # A fully paired quantity before a new, unconnected numeric
                # atom is its own segment, e.g. (17人)120-95=25. Never split
                # a malformed group or discard its source facts/signs.
                if (
                    tokens and tokens[-1][0] in {")", "）", "unit"}
                    and any(kind in {"(", "（"} for kind, _ in tokens)
                    and _arithmetic_operator_positions(tokens) is not None
                ):
                    finish_segment(tokens)
                    tokens = []
                tokens.append(("number", offset + index))
                index = number.end()
                continue
            unit = _EQUATION_UNIT.match(line, index)
            if unit is not None and tokens and tokens[-1][0] in {
                "number", ")", "）", "unit",
            }:
                tokens.append(("unit", offset + index))
                index = unit.end()
                continue
            # Match an opaque suffix only after a quantity. Probing every
            # prose character greedily rescans the rest of the line and
            # turns long PDF text into quadratic work.
            suffix = (
                _QUANTITY_SUFFIX.match(line, index)
                if tokens and tokens[-1][0] in {"number", ")", "）", "unit"}
                else None
            )
            if suffix is not None:
                after_suffix = _number_space_end(line, suffix.end())
                if (
                    after_suffix < len(line)
                    and line[after_suffix] in _EQUATION_OPERATORS
                ):
                    # Retain an unknown suffix at a mathematical boundary.
                    # It may belong to a single range endpoint, but the
                    # arithmetic validator must reject it. In particular,
                    # prose before '=' cannot clear an incomplete equation
                    # and accidentally authorize its preceding subtraction.
                    tokens.append(("suffix", offset + index))
                    index = suffix.end()
                    continue
            if char in _EQUATION_OPERATORS:
                position = offset + index
                if char in {"(", "（"} and position in pairs:
                    after_group = _number_space_end(text, pairs[position] + 1)
                    # A complete equation can end before a paired annotation.
                    # An operator/equality/unit after the group keeps it in
                    # the expression, so an incomplete tail cannot gain proof.
                    annotation_boundary = (
                        after_group >= len(text)
                        or (
                            text[after_group] not in "-−－﹣+＋*/×÷=＝)）"
                            and _EQUATION_UNIT.match(text, after_group) is None
                        )
                    )
                    if (
                        annotation_boundary
                        and _complete_equation_operator_positions(tokens) is not None
                        and (
                            _complete_numeric_group(text, position)
                            or _numeric_group_contains_prose(text, position, pairs[position])
                        )
                    ):
                        finish_segment(tokens)
                        tokens = []
                if (
                    char in {")", "）"} and position in pairs
                    and not any(token_position == pairs[position]
                                for _, token_position in tokens)
                ):
                    # Prose ended the segment inside a strictly paired
                    # annotation. Its closing delimiter does not belong to
                    # the following equation. Unmatched closings stay invalid.
                    finish_segment(tokens)
                    tokens = []
                else:
                    tokens.append((char, position))
            else:
                finish_segment(tokens)
                tokens = []
            index += 1
        finish_segment(tokens)
        offset += len(line)
    return positions, boundaries, range_positions, range_percent_ends


def _complete_numeric_group(text: str, opening_position: int) -> bool:
    """Check a same-line mathematical group without evaluating its value."""

    tokens: list[tuple[str, int]] = []
    parentheses: list[str] = []
    index = opening_position
    while index < len(text):
        char = text[index]
        if char in _NUMBER_LINE_BREAKS:
            return False
        if char.isspace():
            index += 1
            continue
        number = _EQUATION_NUMBER.match(text, index)
        if number is not None:
            tokens.append(("number", index))
            index = number.end()
            continue
        unit = _EQUATION_UNIT.match(text, index)
        if unit is not None and tokens and tokens[-1][0] in {
            "number", ")", "）", "unit",
        }:
            tokens.append(("unit", index))
            index = unit.end()
            continue
        if char in {"(", "（"}:
            parentheses.append(char)
            if len(parentheses) > 64:
                return False
        elif char in {")", "）"}:
            opening = "(" if char == ")" else "（"
            if not parentheses or parentheses.pop() != opening:
                return False
        elif char not in "-−－﹣+＋*/×÷":
            return False
        tokens.append((char, index))
        if not parentheses:
            return _arithmetic_operator_positions(tokens) is not None
        index += 1
    return False


def _numeric_group_contains_prose(text: str, opening: int, closing: int) -> bool:
    """Distinguish prose annotations from incomplete, purely numeric groups.

    Consume units even when repeated: ``(17人次次)`` is malformed numeric
    notation, not prose that could terminate an otherwise complete equation.
    Digits inside genuine prose annotations remain subject to fact grounding.
    """

    position = opening
    while position <= closing:
        char = text[position]
        if char.isspace() or char in _EQUATION_OPERATORS:
            position += 1
            continue
        number = _EQUATION_NUMBER.match(text, position)
        if number is not None:
            position = number.end()
            continue
        unit = _EQUATION_UNIT.match(text, position)
        if unit is not None:
            position = unit.end()
            continue
        return True
    return False


def _signed_group_prefix(text: str, opening_position: int) -> bool:
    """An incomplete signed wrapper must not fall back to a bare number."""

    position = _number_space_start(text, opening_position) - 1
    while position >= 0 and text[position] in {"(", "（"}:
        position = _number_space_start(text, position) - 1
    return position >= 0 and text[position] in "-−－﹣+＋"


def _canonical_zh_integer(value: str) -> str | None:
    """Normalize a deliberately small Chinese integer grammar to Arabic.

    Only zero through ninety-nine are accepted.  The surrounding regex also
    requires an explicit counter, duration unit, or ordinal unit, so ordinary
    words such as ``统一`` cannot accidentally authorize a numeric fact.
    """

    if value in _ZH_DIGIT_VALUES:
        return str(_ZH_DIGIT_VALUES[value])
    if value.count("十") != 1:
        return None
    tens_text, ones_text = value.split("十")
    if len(tens_text) > 1 or len(ones_text) > 1:
        return None
    if tens_text:
        tens = _ZH_DIGIT_VALUES.get(tens_text)
        if tens is None or tens == 0:
            return None
    else:
        tens = 1
    if ones_text:
        ones = _ZH_DIGIT_VALUES.get(ones_text)
        if ones is None:
            return None
    else:
        ones = 0
    return str(tens * 10 + ones)


def _source_zh_integer_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _SOURCE_ZH_INTEGER.finditer(text):
        token = _canonical_zh_integer(match.group("value"))
        if token is not None and token not in tokens:
            tokens.append(token)
    return tokens


def _is_number_list_marker(
    text: str, sign_position: int, operand_position: int,
) -> bool:
    if text[sign_position] not in "-*+" or operand_position <= sign_position + 1:
        return False
    gap = text[sign_position + 1:operand_position]
    if any(not char.isspace() or char in _NUMBER_LINE_BREAKS for char in gap):
        return False
    line_start = max(
        text.rfind(char, 0, sign_position) for char in _NUMBER_LINE_BREAKS
    ) + 1
    return not text[line_start:sign_position].strip()


def _canonical_numeric_token(text: str, match: re.Match[str]) -> str:
    """Normalize numeric notation without changing prose or crossing lines.

    An ASCII hyphen followed by space at the start of a line is a Markdown
    list marker, not a unary minus. Mathematical minus variants and an inline
    sign keep their polarity; whitespace never joins separate paragraphs.
    """

    raw = match.group("value")
    token = re.sub(r"\s+", "", raw).replace(",", "")
    list_prefix = re.match(r"-\s+", raw)
    if list_prefix is not None and _is_number_list_marker(
        text, match.start(), match.start() + list_prefix.end(),
    ):
        return token.removeprefix("-")
    return token.translate(_MINUS_TRANSLATION)


def _number_space_start(text: str, end: int) -> int:
    while end > 0 and text[end - 1].isspace() and text[end - 1] not in _NUMBER_LINE_BREAKS:
        end -= 1
    return end


def _number_space_end(text: str, start: int) -> int:
    while start < len(text) and text[start].isspace() and text[start] not in _NUMBER_LINE_BREAKS:
        start += 1
    return start


def _numeric_notation(
    text: str,
    match: re.Match[str],
    subtraction_positions: set[int],
    operator_boundaries: set[int],
    *,
    normalize_subtraction: bool = True,
    compact_range_positions: set[int] | None = None,
    range_percent_ends: dict[int, str] | None = None,
) -> tuple[str, str]:
    """Normalize the sign and percent notation of one numeric atom only.

    ``-(+95)`` and ``--(95)%`` retain their unary sign and quantity suffix.
    Strictly paired wrappers may surround a single quantity, never a compound
    expression: ``-(120 -95)`` does not flip either source operand or compute
    a result. Complete same-line equations identify binary minus; a complete
    compact numeric range preserves its two unsigned endpoint facts.
    Horizontal spaces, Markdown list markers and the 64-wrapper/sign limits
    share the existing numeric/equation boundaries.
    """

    token = _canonical_numeric_token(text, match)
    if match.start() in subtraction_positions and (
        normalize_subtraction
        or (compact_range_positions is not None and match.start() in compact_range_positions)
    ):
        token = token.removeprefix("-")
    percent = match.group("percent").strip()
    start, end = match.span()
    unit_taken = bool(percent)
    sign_count = int(token.startswith("-") and match.start() not in subtraction_positions)

    def take_unit(*, wrapper: bool = False) -> None:
        nonlocal end, percent, unit_taken
        unit_start = _number_space_end(text, end)
        known_unit = _EQUATION_UNIT.match(text, unit_start)
        unit = _single_quantity_suffix(text, unit_start) or known_unit
        if known_unit is not None and unit_taken and wrapper:
            raise PresentationPageAuthorContentError(
                "unsupported numeric notation repeated unit",
                violation_code="unsupported_numeric_claim",
            )
        if unit is not None and not unit_taken:
            end = unit.end()
            unit_taken = True
            if unit.group() in {"%", "％"}:
                percent = unit.group()

    def take_unary_signs() -> None:
        nonlocal start, token, sign_count
        while True:
            sign_position = _number_space_start(text, start) - 1
            if sign_position < 0 or text[sign_position] not in "-−－﹣+＋":
                return
            if (
                sign_position in subtraction_positions
                or _is_number_list_marker(text, sign_position, start)
            ):
                return
            if sign_position in operator_boundaries:
                if text[sign_position] in "+＋" and sign_count == 0:
                    return
                raise PresentationPageAuthorContentError(
                    "unsupported numeric notation unproven operator boundary",
                    violation_code="unsupported_numeric_claim",
                )
            sign_count += 1
            if sign_count > 64:
                raise PresentationPageAuthorContentError(
                    "unsupported numeric notation sign chain",
                    violation_code="unsupported_numeric_claim",
                )
            if text[sign_position] in "-−－﹣":
                token = (
                    token.removeprefix("-") if token.startswith("-")
                    else f"-{token}"
                )
            start = sign_position

    take_unit()
    take_unary_signs()
    for depth in range(65):
        opening_position = _number_space_start(text, start) - 1
        closing_position = _number_space_end(text, end)
        if opening_position < 0 or text[opening_position] not in {"(", "（"}:
            break
        if (
            closing_position >= len(text)
            or (text[opening_position], text[closing_position]) not in {
                ("(", ")"), ("（", "）"),
            }
        ):
            # Complete compound groups keep their operand facts unchanged.
            # Invalid signed groups cannot discard their outer unary sign.
            # Repeated inner units are also rejected on an unsigned atom;
            # ordinary prose after a number is not a mathematical wrapper.
            repeated_end = closing_position
            unit = _EQUATION_UNIT.match(text, repeated_end)
            while unit is not None:
                repeated_end = _number_space_end(text, unit.end())
                unit = _EQUATION_UNIT.match(text, repeated_end)
            repeated_unit = (
                repeated_end > closing_position
                and repeated_end < len(text)
                and text[repeated_end] in ")）"
            )
            if not _complete_numeric_group(text, opening_position) and (
                repeated_unit or _signed_group_prefix(text, opening_position)
            ):
                raise PresentationPageAuthorContentError(
                    "unsupported numeric notation malformed quantity wrapper",
                    violation_code="unsupported_numeric_claim",
                )
            break
        if depth == 64:
            raise PresentationPageAuthorContentError(
                "unsupported numeric notation nesting",
                violation_code="unsupported_numeric_claim",
            )
        start, end = opening_position, closing_position + 1
        take_unit(wrapper=True)
        take_unary_signs()
    if range_percent_ends is not None:
        percent = range_percent_ends.get(match.end("value"), percent)
    return token, percent


def _source_numeric_quantities(
    text: str, *, normalize_subtraction: bool = True,
) -> Iterator[tuple[str, str]]:
    """Read only provable numeric authority from an unchanged raw document.

    Document identifiers and extraction artifacts can contain unproven minus
    connectors. Exclude their entire connected atom, including an otherwise
    parseable first operand, so it cannot authorize a new measurement. Other
    unsupported notation contributes no numeric authority either. The original
    material remains intact; model-authored notation still uses strict readers.
    """

    positions, boundaries, range_positions, percent_ends = _numeric_operator_positions(text)
    unproven_spans = [
        atom.span()
        for atom in _SOURCE_CONNECTED_ATOM.finditer(text)
        if any(
            text[index] in "-−－﹣" and index not in positions
            for index in range(atom.start(), atom.end())
        )
    ]
    span_index = 0
    for match in _SOURCE_NUMBER.finditer(text):
        start, end = match.span("value")
        while span_index < len(unproven_spans) and unproven_spans[span_index][1] <= start:
            span_index += 1
        if span_index < len(unproven_spans) and unproven_spans[span_index][0] < end:
            continue
        try:
            yield _numeric_notation(
                text, match, positions, boundaries,
                normalize_subtraction=normalize_subtraction,
                compact_range_positions=range_positions,
                range_percent_ends=percent_ends,
            )
        except PresentationPageAuthorContentError as error:
            if error.violation_code != "unsupported_numeric_claim":
                raise


def source_numeric_tokens(text: str, *, preserve_percent: bool = False) -> list[str]:
    """Return conservative raw-material guidance, without rewriting its text."""

    quantities = _source_numeric_quantities(text, normalize_subtraction=False)
    return list(dict.fromkeys([
        *(token + (percent if preserve_percent else "") for token, percent in quantities),
        *_source_zh_integer_tokens(text),
    ]))


def _source_numeric_value_groups(text: str) -> tuple[set[Decimal], set[Decimal]]:
    """Keep bare source values separate from percentage-point values."""

    bare_values: set[Decimal] = set()
    percent_values: set[Decimal] = set()
    for token, percent in _source_numeric_quantities(text):
        number = Decimal(token)
        if percent:
            percent_values.add(number)
        else:
            bare_values.add(number)
    bare_values.update(Decimal(token) for token in _source_zh_integer_tokens(text))
    return bare_values, percent_values


def _source_numeric_values(text: str) -> set[Decimal]:
    bare_values, percent_values = _source_numeric_value_groups(text)
    # Native charts retain their existing points/ratio representation contract.
    return bare_values | percent_values | {value / 100 for value in percent_values}


def numeric_tokens(text: str, *, preserve_percent: bool = False) -> list[str]:
    """Return number tokens, optionally retaining source percentage notation.

    Existing well-formed value tokens keep their representation. Spaced and
    mathematical unary minus notation is canonicalized consistently. Retry
    guidance can also retain the source's percentage suffix.
    """

    subtraction_positions, operator_boundaries, range_positions, percent_ends = _numeric_operator_positions(text)
    quantities = (
        _numeric_notation(
            text, match, subtraction_positions, operator_boundaries,
            normalize_subtraction=False,
            compact_range_positions=range_positions,
            range_percent_ends=percent_ends,
        )
        for match in _SOURCE_NUMBER.finditer(text)
    )
    return list(dict.fromkeys([
        *(token + (percent if preserve_percent else "") for token, percent in quantities),
        *_source_zh_integer_tokens(text),
    ]))


def _contract_numeric_tokens(text: str, *, source_only: bool = False) -> list[str]:
    """Return numeric facts with binary subtraction operands normalized.

    A storyboard equation such as ``120万元 -95万元 = 25万元`` assigns the
    positive facts 120, 95, and 25 to the page.  A unary claim such as
    ``成本为 -95万元`` remains negative.  Planning, source, and authored-page
    checks must all use this same representation or a correct equation can
    reject itself at the page boundary.
    """

    tokens: list[str] = []
    if source_only:
        quantities = _source_numeric_quantities(text)
    else:
        subtraction_positions, operator_boundaries, _, percent_ends = _numeric_operator_positions(text)
        quantities = (
            _numeric_notation(
                text, match, subtraction_positions, operator_boundaries,
                range_percent_ends=percent_ends,
            )
            for match in _SOURCE_NUMBER.finditer(text)
        )
    for token, _ in quantities:
        if token not in tokens:
            tokens.append(token)
    for token in _source_zh_integer_tokens(text):
        if token not in tokens:
            tokens.append(token)
    return tokens


def _unsupported_numeric_object_paths(
    slide: DeckSlide, unsupported_numbers: set[str],
) -> list[str]:
    """Locate an existing numeric failure without returning text or object IDs.

    This is diagnostic only: the whole-page check remains authoritative. Scan
    exactly its visible fields, using the same token parser, and bound feedback.
    """

    paths: list[str] = []
    for index, obj in enumerate(slide.objects):
        prefix = f"$.objects[{index}]"
        fields: list[tuple[str, str]] = []
        if isinstance(obj, TextObject):
            fields.append((prefix + ".text", obj.text))
            fields.extend(
                (f"{prefix}.runs[{run_index}].text", run.text)
                for run_index, run in enumerate(obj.runs)
            )
        elif isinstance(obj, MetricObject):
            fields.extend(
                (prefix + "." + name, getattr(obj, name))
                for name in ("label", "value", "detail")
            )
        elif isinstance(obj, TableObject):
            fields.extend(
                (f"{prefix}.headers[{column}]", text)
                for column, text in enumerate(obj.headers)
            )
            fields.extend(
                (f"{prefix}.rows[{row_index}][{column}]", text)
                for row_index, row in enumerate(obj.rows)
                for column, text in enumerate(row)
            )
        elif isinstance(obj, ShapeObject):
            fields.append((prefix + ".text", obj.text))
        for path, text in fields:
            if unsupported_numbers.intersection(_contract_numeric_tokens(text)):
                paths.append(path)
                if len(paths) == 16:
                    return paths
    return paths


def page_allowed_numeric_tokens(planned: DeckPlanSlide) -> list[str]:
    """Return only numeric facts intentionally assigned to one storyboard.

    Runtime identifiers, slide order, source references, object geometry, and
    the rest of the deck are deliberately excluded.  Chart values are part of
    the page contract, but number-format syntax is not a factual value.
    """

    text_parts = [
        planned.title,
        planned.intent.purpose,
        planned.intent.key_message,
        *planned.intent.evidence,
        *planned.intent.required_facts,
        *planned.intent.immutable_facts,
        *planned.content_blocks,
        planned.takeaway,
    ]
    for chart in planned.charts:
        text_parts.extend(chart.categories)
        text_parts.extend(chart.series)
        text_parts.extend((chart.unit, chart.derivation))
    tokens = _contract_numeric_tokens("\n".join(text_parts))
    for chart in planned.charts:
        for values in chart.series.values():
            for value in values:
                canonical = format(Decimal(str(value)).normalize(), "f")
                if canonical in {"-0", "+0"}:
                    canonical = "0"
                if canonical not in tokens:
                    tokens.append(canonical)
    return tokens


def verified_calculation_values(
    proofs: Sequence[SourceCalculationProof],
    *,
    source_text_for: Callable[[list[str]], str],
    allowed_refs: Sequence[str],
    unit: str | None = None,
) -> set[Decimal]:
    """Recompute proposals without promoting model prose to source evidence."""

    available: set[Decimal] = set()
    allowed = set(allowed_refs)
    for proposed in proofs:
        # Revalidate even model_copy / restored callers; there is no trusted
        # persisted boolean and no expression-string execution.
        try:
            proof = SourceCalculationProof.model_validate(
                proposed.model_dump(mode="json", warnings=False),
            )
        except ValidationError:
            # ValidationError and serializer warnings can include untrusted
            # source excerpts. Preserve failure without exposing their values.
            raise ValueError("calculation proof does not match its typed schema") from None
        refs = {term.source_ref for term in proof.terms} | {proof.authorization_source_ref}
        if refs - allowed or (unit is not None and unit != proof.unit):
            raise ValueError("calculation proof exceeds source or unit scope")
        authorization_source = source_text_for([proof.authorization_source_ref])
        authorization_excerpt = "".join(proof.authorization_excerpt.split())
        if not authorization_excerpt or authorization_excerpt not in "".join(authorization_source.split()):
            raise ValueError("calculation proof authorization is not in its cited source")
        with localcontext() as context:
            context.prec = 64
            result = Decimal(0)
            for term_index, term in enumerate(proof.terms):
                source = source_text_for([term.source_ref])
                if "".join(term.source_excerpt.split()) not in "".join(source.split()):
                    error = ValueError("calculation proof input excerpt is not in its cited source")
                    # Preserve the strict decision/message. This owned index
                    # locates the failed field without retaining rejected text.
                    error.presentation_calculation_term_index = term_index
                    raise error
                bare, percent = _source_numeric_value_groups(term.source_excerpt)
                inputs = percent if term.unit == "%" else bare
                if term.unit != proof.unit or term.unit not in source or Decimal(term.value) not in inputs:
                    error = ValueError("calculation proof input value or unit is not source-grounded")
                    error.presentation_calculation_term_index = term_index
                    error.presentation_calculation_term_field = (
                        "unit" if term.unit != proof.unit or term.unit not in source else "value"
                    )
                    raise error
                result += Decimal(term.value) * term.coefficient
            if result != Decimal(proof.result):
                raise ValueError("calculation proof result does not match recomputation")
        available.add(result)
    return available


def validate_grounded_numeric_text(
    text: str,
    source_text: str,
    *,
    location: str,
    verified_values: set[Decimal] | None = None,
) -> None:
    """Reject model-authored numbers absent from the authoritative source.

    Exact source values pass. A displayed value may also be a conventional
    decimal rounding of a source value, such as 9.4% from 9.375%. This accepts
    presentation formatting without allowing the model to invent a new base
    measurement.
    """

    # A verified signed difference may be expressed as its exact magnitude in
    # prose ("decreased by 9" for -9), just like a directly cited difference.
    # Keep the signed proof unchanged: chart values and new negative claims
    # still require their exact signed authority, not this display allowance.
    verified_display_values = (verified_values or set()) | {
        abs(value) for value in (verified_values or ())
    }
    bare_source_values, percent_source_values = _source_numeric_value_groups(source_text)
    subtraction_positions, operator_boundaries, _, percent_ends = _numeric_operator_positions(text)
    for match in _SOURCE_NUMBER.finditer(text):
        token, percent = _numeric_notation(
            text, match, subtraction_positions, operator_boundaries,
            range_percent_ends=percent_ends,
        )
        value = Decimal(token)
        candidates = {value}
        if percent:
            # Only bare values may be interpreted as either points or ratios.
            # A source percentage already has a unit: 17% is not 0.17%/1700%.
            source_values = (
                percent_source_values | bare_source_values
                | {number * 100 for number in bare_source_values}
            )
        else:
            source_values = (
                bare_source_values | percent_source_values
                | {number / 100 for number in percent_source_values}
            )
        available = source_values | {abs(number) for number in source_values} | verified_display_values
        if candidates & available:
            continue
        decimal_places = len(token.rsplit(".", 1)[1]) if "." in token else 0
        quantum = Decimal(1).scaleb(-decimal_places)
        if decimal_places > 0 and any(
            source_value.quantize(quantum, rounding=ROUND_HALF_UP) in candidates
            for source_value in source_values | (verified_values or set())
        ):
            continue
        error = PresentationPageAuthorContentError(
            f"uncited storyboard number {token} in {location}",
            violation_code="unsupported_numeric_claim",
        )
        error.presentation_numeric_token = token
        error.presentation_numeric_location = location
        raise error
    source_values = (
        bare_source_values | percent_source_values
        | {number / 100 for number in percent_source_values}
    )
    available = source_values | {abs(value) for value in source_values} | verified_display_values
    for token in _source_zh_integer_tokens(text):
        if Decimal(token) in available:
            continue
        error = PresentationPageAuthorContentError(
            f"uncited storyboard number {token} in {location}",
            violation_code="unsupported_numeric_claim",
        )
        error.presentation_numeric_token = token
        error.presentation_numeric_location = location
        raise error


def validate_planned_text_sources(
    slides: list[DeckPlanSlide],
    brief: PresentationBrief,
) -> None:
    """Ground every numeric claim in a slide's own cited source sections."""

    for slide in slides:
        cited = set(slide.intent.source_refs)
        source_parts = [
            section.grounded_text
            for section in brief.source_sections
            if set(section.source_refs or [section.source_ref]).issubset(cited)
        ]
        authored_text = "\n".join(
            [
                slide.title,
                slide.intent.purpose,
                slide.intent.key_message,
                *slide.content_blocks,
                slide.takeaway,
                *(chart.derivation for chart in slide.charts),
            ]
        )
        validate_grounded_numeric_text(
            authored_text,
            "\n".join(source_parts),
            location=f"slide-{slide.order:03d}",
            verified_values=verified_calculation_values(
                [*slide.intent.calculation_proofs, *(proof for chart in slide.charts for proof in chart.calculation_proofs)],
                source_text_for=lambda refs: "\n".join(
                    section.grounded_text for section in brief.source_sections
                    if set(section.source_refs or [section.source_ref]).issubset(set(refs))
                ),
                allowed_refs=slide.intent.source_refs,
            ),
        )


def validate_planned_chart_sources(
    slides: list[DeckPlanSlide],
    brief: PresentationBrief,
) -> None:
    """Require every chart datum to be cited or exactly recomputed.

    This is deliberately conservative: a model's derivation prose does not
    authorize new numeric facts. Typed calculation proofs must pass exact
    source, same-unit and arithmetic verification before use.
    """

    for slide in slides:
        for chart in slide.charts:
            cited = set(chart.source_refs)
            source_bodies = [
                section.grounded_text
                for section in brief.source_sections
                # A merged section loses per-sentence provenance. Require all
                # its contributors, not just one, before using any value.
                if set(section.source_refs or [section.source_ref]).issubset(cited)
            ]
            available = _source_numeric_values("\n".join(source_bodies))
            available |= verified_calculation_values(
                chart.calculation_proofs,
                source_text_for=lambda refs: "\n".join(
                    section.grounded_text for section in brief.source_sections
                    if set(section.source_refs or [section.source_ref]).issubset(set(refs))
                ),
                allowed_refs=chart.source_refs,
                unit=chart.unit,
            )
            for series_name, values in chart.series.items():
                for value in values:
                    if Decimal(str(value)) not in available:
                        error = ValueError(
                            "uncited chart value "
                            f"{value} in {slide.id}/{chart.id}/{series_name}"
                        )
                        error.presentation_numeric_token = str(value)
                        error.presentation_numeric_location = (
                            f"slide-{slide.order:03d}"
                        )
                        raise error


def validate_authored_page(
    authored: DeckSlide,
    planned: DeckPlanSlide,
    scaffold: DeckSlide,
    *,
    source_text: str,
    source_brief: PresentationBrief | None = None,
) -> DeckSlide:
    """Accept explicit native geometry only with intact content and lineage."""

    slide = DeckSlide.model_validate(authored.model_dump(mode="json"))
    if slide.id != scaffold.id or slide.intent_id != scaffold.intent_id:
        raise ValueError("page author changed the slide or intent identity")
    if slide.composition_authority != CompositionAuthority.AUTHOR:
        raise ValueError("page author did not declare explicit scene authority")
    if slide.layout_id != "freeform" or not slide.objects:
        raise ValueError("page author must return a non-empty freeform scene")
    if slide.editability_mode != scaffold.editability_mode:
        raise ValueError("page author changed the editability requirement")
    if slide.source_fact_ids != scaffold.source_fact_ids:
        raise ValueError("page author changed the source fact binding")
    if slide.asset_ids != scaffold.asset_ids:
        raise ValueError("page author changed the asset binding")
    if any(isinstance(obj, ImageObject) for obj in slide.objects):
        # The current page-author input has no rights-verified asset catalog.
        raise ValueError("page author cannot introduce an unverified image")
    decorative_slots = {
        "page-marker",
        "page-number",
        "step-number",
        "sequence-number",
        "numeric-badge",
    }
    for index, obj in enumerate(slide.objects):
        if not isinstance(obj, (TextObject, ShapeObject)):
            continue
        text = obj.text
        if numeric_tokens(text) and (
            obj.slot.casefold() in decorative_slots
            or (
                isinstance(obj, TextObject)
                and obj.role.casefold() in {"page-number", "step-number"}
            )
        ):
            error = PresentationPageAuthorContentError(
                "page author introduced decorative numbering",
                violation_code="decorative_numbering",
            )
            error.presentation_numeric_object_paths = [f"$.objects[{index}].text"]
            raise error
    titles = [
        obj.text for obj in slide.objects
        if isinstance(obj, TextObject) and obj.role == "title"
    ]
    if titles != [planned.title]:
        raise ValueError("page author changed or omitted the storyboard title")
    charts = [obj for obj in slide.objects if isinstance(obj, ChartObject)]
    if sorted((_chart_data(chart) for chart in charts), key=str) != sorted(
        (_chart_data(chart) for chart in planned.charts), key=str,
    ):
        raise PresentationPageAuthorContentError(
            "page author changed or omitted planned chart data",
            violation_code="planned_chart_data_changed",
        )
    visible = re.sub(r"\s+", "", _visible_text(slide))
    missing_fact_paths = [
        f"{field_name}[{index}]"
        for field_name, facts in (
            ("required_facts", planned.intent.required_facts),
            ("immutable_facts", planned.intent.immutable_facts),
        )
        for index, fact in enumerate(facts)
        if re.sub(r"\s+", "", fact) not in visible
    ]
    if missing_fact_paths:
        error = PresentationPageAuthorContentError(
            "page author omitted one or more required visible facts",
            violation_code="required_visible_fact_omitted",
        )
        error.presentation_missing_fact_paths = missing_fact_paths
        error.presentation_detail_target_orders = [planned.order]
        raise error
    if numeric_tokens(slide.speaker_notes):
        raise PresentationPageAuthorContentError(
            "page author speaker notes must be numeric-free",
            violation_code="numeric_speaker_notes",
        )
    def calculation_source_text(refs: list[str]) -> str:
        if source_brief is not None:
            cited = set(refs)
            return "\n".join(
                section.grounded_text
                for section in source_brief.source_sections
                if set(section.source_refs or [section.source_ref]).issubset(cited)
            )
        # A standalone single-source caller can supply that exact source body.
        # A combined text body cannot establish per-source provenance.
        if set(planned.intent.source_refs) == set(refs) and len(set(refs)) == 1:
            return source_text
        raise ValueError("calculation proof requires individually cited source bodies")

    calculated = verified_calculation_values(
        [
            *planned.intent.calculation_proofs,
            *(proof for chart in planned.charts for proof in chart.calculation_proofs),
        ],
        source_text_for=calculation_source_text,
        allowed_refs=planned.intent.source_refs,
    )
    for chart in planned.charts:
        verified_calculation_values(
            chart.calculation_proofs,
            source_text_for=calculation_source_text,
            allowed_refs=chart.source_refs,
            unit=chart.unit,
        )
    source_numbers = set(_contract_numeric_tokens(source_text, source_only=True)) | {
        format(value.normalize(), "f") for value in calculated
    }
    allowed_numbers = (
        set(page_allowed_numeric_tokens(planned))
        & source_numbers
    )
    authored_text = _visible_text(slide)
    authored_numbers = set(_contract_numeric_tokens(authored_text))
    unsupported_numbers = sorted(authored_numbers - allowed_numbers)
    if unsupported_numbers:
        error = PresentationPageAuthorContentError(
            "page author introduced unsupported number(s): "
            + ", ".join(unsupported_numbers),
            violation_code="unsupported_number",
        )
        error.presentation_numeric_tokens = unsupported_numbers
        error.presentation_numeric_location = planned.id
        error.presentation_numeric_object_paths = _unsupported_numeric_object_paths(
            slide, set(unsupported_numbers),
        )
        raise error
    object_ids = {obj.id for obj in slide.objects}
    if any(
        set(group.object_ids) - object_ids for group in slide.object_groups
    ) or any(
        set(rel.source_object_ids + rel.target_object_ids) - object_ids
        for rel in slide.relationships
    ):
        raise PresentationPageAuthorSceneError(
            "page author referenced a missing native object",
            violation_code="dangling_object_reference",
        )
    scene_hash = page_scene_hash(slide)
    if slide.scene_hash is not None and slide.scene_hash != scene_hash:
        raise ValueError("page scene checkpoint hash does not match its content")
    return slide.model_copy(update={"scene_hash": scene_hash})


def page_scene_hash(slide: DeckSlide) -> str:
    """Fingerprint scene semantics, retaining pre-proof checkpoint ownership."""

    payload = slide.model_copy(update={"scene_hash": None}).model_dump(mode="json")
    for obj in payload["objects"]:
        if obj.get("kind") == "chart" and obj.get("calculation_proofs") == []:
            obj.pop("calculation_proofs")
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8"),
    ).hexdigest()
