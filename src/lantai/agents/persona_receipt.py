# -*- coding: utf-8 -*-
"""Inert, turn-local evidence from the existing Persona preparation seam.

The receipt records values that Persona preparation has already materialized.
It never calls a provider, compiler, serializer, model hook, or custom
container iterator.  Private values stay in memory and are hidden from repr.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Final, Literal


PersonaPreparationSourceMode = Literal[
    "disabled",
    "runner_override",
    "agent_provider",
    "absent",
]

_SOURCE_MODES: Final = frozenset(
    {"disabled", "runner_override", "agent_provider", "absent"}
)


@dataclass(frozen=True, slots=True, repr=False)
class FrozenPersonaReceiptValue:
    """Tagged, deeply detached representation of exact built-in values."""

    kind: str
    value: Any

    def __post_init__(self) -> None:
        if type(self.kind) is not str:
            raise TypeError("Persona receipt value kind requires an exact string")
        if self.kind == "null":
            valid = self.value is None
        elif self.kind == "bool":
            valid = type(self.value) is bool
        elif self.kind == "int":
            valid = type(self.value) is int
        elif self.kind in {"float", "string"}:
            valid = type(self.value) is str
        elif self.kind == "object":
            valid = type(self.value) is tuple and all(
                type(row) is tuple
                and len(row) == 2
                and type(row[0]) is str
                and type(row[1]) is FrozenPersonaReceiptValue
                for row in self.value
            )
        elif self.kind in {"list", "tuple"}:
            valid = type(self.value) is tuple and all(
                type(item) is FrozenPersonaReceiptValue for item in self.value
            )
        else:
            raise ValueError("Unsupported Persona receipt value kind")
        if not valid:
            raise TypeError("Persona receipt value payload is invalid")

    def __repr__(self) -> str:
        count = len(self.value) if type(self.value) is tuple else 1
        return (
            "FrozenPersonaReceiptValue("
            f"kind={self.kind!r}, private_value=True, item_count={count!r})"
        )


def freeze_persona_receipt_value(value: Any) -> FrozenPersonaReceiptValue:
    """Detach exact JSON-like built-ins without invoking extensible hooks."""

    value_type = type(value)
    if value is None:
        return FrozenPersonaReceiptValue("null", None)
    if value_type is bool:
        return FrozenPersonaReceiptValue("bool", value)
    if value_type is int:
        return FrozenPersonaReceiptValue("int", value)
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError("Persona receipt numbers must be finite")
        return FrozenPersonaReceiptValue("float", repr(value))
    if value_type is str:
        return FrozenPersonaReceiptValue("string", value)
    if value_type is dict:
        rows: list[tuple[str, FrozenPersonaReceiptValue]] = []
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("Persona receipt object keys require exact strings")
            rows.append((key, freeze_persona_receipt_value(item)))
        return FrozenPersonaReceiptValue("object", tuple(rows))
    if value_type is list:
        return FrozenPersonaReceiptValue(
            "list",
            tuple(freeze_persona_receipt_value(item) for item in value),
        )
    if value_type is tuple:
        return FrozenPersonaReceiptValue(
            "tuple",
            tuple(freeze_persona_receipt_value(item) for item in value),
        )
    raise TypeError("Persona receipt values require exact built-in containers")


def _canonical_frozen_value(value: FrozenPersonaReceiptValue) -> dict[str, Any]:
    if type(value) is not FrozenPersonaReceiptValue:
        raise TypeError("Persona receipt digest requires an exact frozen value")
    if value.kind == "object":
        return {
            "kind": "object",
            "value": [
                [key, _canonical_frozen_value(item)] for key, item in value.value
            ],
        }
    if value.kind in {"list", "tuple"}:
        return {
            "kind": value.kind,
            "value": [_canonical_frozen_value(item) for item in value.value],
        }
    return {"kind": value.kind, "value": value.value}


def persona_receipt_value_sha256(value: FrozenPersonaReceiptValue) -> str:
    encoded = json.dumps(
        _canonical_frozen_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class PersonaPreparationReceipt:
    """Detached output-consistency receipt for one prepared Persona turn."""

    source_mode: PersonaPreparationSourceMode
    language: str
    turn_key: FrozenPersonaReceiptValue
    subject_context: FrozenPersonaReceiptValue
    subject_continuity_prompt: str
    perceptible_presence_prompt: str

    def __post_init__(self) -> None:
        if type(self.source_mode) is not str or self.source_mode not in _SOURCE_MODES:
            raise ValueError("Unsupported Persona preparation source mode")
        if any(
            type(value) is not str
            for value in (
                self.language,
                self.subject_continuity_prompt,
                self.perceptible_presence_prompt,
            )
        ):
            raise TypeError("Persona receipt prompt fields require exact strings")
        if type(self.turn_key) is not FrozenPersonaReceiptValue or type(
            self.subject_context
        ) is not FrozenPersonaReceiptValue:
            raise TypeError("Persona receipt values must be deeply frozen")

    def __repr__(self) -> str:
        return (
            "PersonaPreparationReceipt("
            f"source_mode={self.source_mode!r}, "
            f"language_present={bool(self.language)!r}, "
            f"subject_prompt_present={bool(self.subject_continuity_prompt)!r}, "
            f"presence_prompt_present={bool(self.perceptible_presence_prompt)!r}, "
            "private_values=True)"
        )


def capture_persona_preparation_receipt(
    *,
    source_mode: PersonaPreparationSourceMode,
    language: str,
    turn_key: tuple[tuple[str, str], ...],
    subject_context: dict[str, Any],
    subject_continuity_prompt: str,
    perceptible_presence_prompt: str,
) -> PersonaPreparationReceipt:
    """Capture already-produced values without recomputing Persona output."""

    if type(turn_key) is not tuple or any(
        type(row) is not tuple
        or len(row) != 2
        or type(row[0]) is not str
        or type(row[1]) is not str
        for row in turn_key
    ):
        raise TypeError("Persona receipt turn key requires exact built-in tuples")
    if type(subject_context) is not dict:
        raise TypeError("Persona receipt subject context requires an exact built-in dict")
    return PersonaPreparationReceipt(
        source_mode=source_mode,
        language=language,
        turn_key=freeze_persona_receipt_value(turn_key),
        subject_context=freeze_persona_receipt_value(subject_context),
        subject_continuity_prompt=subject_continuity_prompt,
        perceptible_presence_prompt=perceptible_presence_prompt,
    )


__all__ = [
    "FrozenPersonaReceiptValue",
    "PersonaPreparationReceipt",
    "PersonaPreparationSourceMode",
    "capture_persona_preparation_receipt",
    "freeze_persona_receipt_value",
    "persona_receipt_value_sha256",
]
