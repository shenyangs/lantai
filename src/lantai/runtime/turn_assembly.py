# -*- coding: utf-8 -*-
"""Immutable contracts for one fully prepared Native Runtime turn.

This module is deliberately pure.  It freezes values that have already been
prepared elsewhere; it does not read agent state, call a model, execute a tool,
or perform I/O.  The full snapshot is private runtime input.  Only
``privacy_evidence`` is suitable for logs and release evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final


TURN_ASSEMBLY_SNAPSHOT_SCHEMA_V1: Final = (
    "com.lantai.runtime.turn-assembly-snapshot.v1"
)

# Ordering is part of the contract.  A future schema may append a surface, but
# v1 producers must not silently reorder or omit a protected intelligence seam.
REQUIRED_TURN_ASSEMBLY_SURFACES: Final = (
    "prompt",
    "persona",
    "memory_context",
    "skills",
    "attachments",
    "tools",
    "request_policy",
    "compatibility",
)

def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class FrozenJsonObject(Mapping[str, Any]):
    """Insertion-ordered, immutable JSON object with deeply frozen values."""

    _items: tuple[tuple[str, Any], ...]

    def __post_init__(self) -> None:
        if type(self._items) is not tuple:
            raise TypeError("Frozen turn assembly objects require a built-in tuple")
        seen: set[str] = set()
        frozen_items: list[tuple[str, Any]] = []
        for entry in self._items:
            if type(entry) is not tuple or len(entry) != 2:
                raise TypeError("Frozen turn assembly entries must be key-value tuples")
            key, value = entry
            if type(key) is not str:
                raise TypeError("Turn assembly mapping keys must be built-in strings")
            if key in seen:
                raise ValueError("Turn assembly mapping keys must be unique")
            seen.add(key)
            frozen_items.append((key, freeze_turn_assembly_value(value)))
        object.__setattr__(self, "_items", tuple(frozen_items))

    def __repr__(self) -> str:
        item_count: int | str = (
            len(self._items) if type(self._items) is tuple else "invalid"
        )
        return f"FrozenJsonObject(item_count={item_count!r})"

    def __getitem__(self, key: str) -> Any:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def items(self) -> tuple[tuple[str, Any], ...]:  # type: ignore[override]
        return self._items

    def to_plain(self) -> dict[str, Any]:
        """Return a detached mutable copy for an eventual adapter boundary."""

        return {
            key: thaw_turn_assembly_value(value)
            for key, value in self._items
        }


def freeze_turn_assembly_value(value: Any) -> Any:
    """Copy and recursively freeze one JSON-compatible prepared value.

    Callables and unordered containers are rejected instead of inspected or
    invoked.  This keeps snapshot construction deterministic and side-effect
    free by construction.
    """

    value_type = type(value)
    if value is None or value_type in {str, bool, int}:
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError("Turn assembly numbers must be finite")
        return value
    if value_type is FrozenJsonObject:
        return value
    if value_type is dict:
        return FrozenJsonObject(tuple(value.items()))
    if value_type in {list, tuple}:
        return tuple(freeze_turn_assembly_value(item) for item in value)
    if value_type in {set, frozenset}:
        raise TypeError("Turn assembly values cannot contain unordered sets")
    raise TypeError("Turn assembly values require inert built-in JSON data")


def thaw_turn_assembly_value(value: Any) -> Any:
    """Return a detached JSON-compatible copy of a frozen snapshot value."""

    if type(value) is FrozenJsonObject:
        return value.to_plain()
    if type(value) is tuple:
        return [thaw_turn_assembly_value(item) for item in value]
    return value


def _canonical_frozen_value(value: Any) -> Any:
    """Encode type and insertion order explicitly for stable hashing."""

    if type(value) is FrozenJsonObject:
        return {
            "$object": [
                [key, _canonical_frozen_value(item)]
                for key, item in value.items()
            ],
        }
    if type(value) is tuple:
        return {"$array": [_canonical_frozen_value(item) for item in value]}
    if value is None:
        return {"$null": True}
    if type(value) is bool:
        return {"$bool": value}
    if type(value) is int:
        return {"$int": value}
    if type(value) is float:
        return {"$float": repr(value)}
    if type(value) is str:
        return {"$string": value}
    raise TypeError("Unsupported frozen turn assembly value")


def _top_level_item_count(value: Any) -> int:
    if type(value) in {FrozenJsonObject, tuple}:
        return len(value)
    return 1


def _top_level_order_tokens(value: Any) -> list[str]:
    if type(value) is FrozenJsonObject:
        return list(value)
    if type(value) is tuple:
        return [f"item:{index}" for index in range(len(value))]
    return ["scalar"]


@dataclass(frozen=True, slots=True, repr=False)
class TurnAssemblySurface:
    """One protected turn-assembly surface and its stable source identities."""

    name: str
    value: Any
    source_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.name) is not str:
            raise TypeError("Turn assembly surface names must be built-in strings")
        if self.name not in REQUIRED_TURN_ASSEMBLY_SURFACES:
            raise ValueError("Turn assembly surface name is not allowed by schema v1")
        if type(self.source_ids) is not tuple or type(self.item_ids) is not tuple:
            raise TypeError("Turn assembly identities must use built-in tuples")
        if any(type(item) is not str for item in (*self.source_ids, *self.item_ids)):
            raise TypeError("Turn assembly identities must be built-in strings")
        source_ids = self.source_ids
        item_ids = self.item_ids
        if any(not item for item in (*source_ids, *item_ids)):
            raise ValueError("Turn assembly source and item identities cannot be empty")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Turn assembly source identities must be unique")
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("Turn assembly item identities must be unique")
        object.__setattr__(self, "value", freeze_turn_assembly_value(self.value))

    @property
    def content_sha256(self) -> str:
        return _sha256_json(_canonical_frozen_value(self.value))

    @property
    def item_count(self) -> int:
        return len(self.item_ids) or _top_level_item_count(self.value)

    @property
    def order_sha256(self) -> str:
        return _sha256_json(
            list(self.item_ids) or _top_level_order_tokens(self.value),
        )

    def privacy_evidence(self) -> dict[str, Any]:
        """Return telemetry-safe hashes and counts, never private values or IDs."""

        return {
            "surface": self.name,
            "content_sha256": self.content_sha256,
            "item_count": self.item_count,
            "item_identity_sha256": _sha256_json(list(self.item_ids)),
            "order_sha256": self.order_sha256,
            "source_count": len(self.source_ids),
            "source_identity_sha256": _sha256_json(list(self.source_ids)),
        }

    def __repr__(self) -> str:
        name = (
            self.name
            if type(self.name) is str
            and self.name in REQUIRED_TURN_ASSEMBLY_SURFACES
            else "invalid"
        )
        source_count: int | str = (
            len(self.source_ids) if type(self.source_ids) is tuple else "invalid"
        )
        return (
            "TurnAssemblySurface("
            f"name={name!r}, "
            "private_value=True, "
            f"source_count={source_count!r}"
            ")"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TurnAssemblySnapshot:
    """Versioned immutable private input for one Native Runtime turn."""

    snapshot_id: str
    turn_id: str
    surfaces: tuple[TurnAssemblySurface, ...]
    schema_version: str = TURN_ASSEMBLY_SNAPSHOT_SCHEMA_V1

    def __post_init__(self) -> None:
        if type(self.snapshot_id) is not str or type(self.turn_id) is not str:
            raise TypeError("Turn assembly snapshot identities must be built-in strings")
        if not self.snapshot_id or not self.turn_id:
            raise ValueError("Turn assembly snapshot and turn identities are required")
        if type(self.schema_version) is not str:
            raise TypeError("Turn assembly schema version must be a built-in string")
        if self.schema_version != TURN_ASSEMBLY_SNAPSHOT_SCHEMA_V1:
            raise ValueError("Unsupported turn assembly schema version")
        if type(self.surfaces) is not tuple:
            raise TypeError("Turn assembly snapshots require a built-in surface tuple")
        surfaces = self.surfaces
        if any(type(surface) is not TurnAssemblySurface for surface in surfaces):
            raise TypeError("Turn assembly snapshots require TurnAssemblySurface values")
        names = tuple(surface.name for surface in surfaces)
        if len(set(names)) != len(names):
            raise ValueError("Turn assembly surface names must be unique")
        required = REQUIRED_TURN_ASSEMBLY_SURFACES
        if names != required:
            raise ValueError(
                "Turn assembly v1 requires exactly the protected surfaces in "
                "canonical order: "
                + ", ".join(required),
            )

    def surface(self, name: str) -> TurnAssemblySurface:
        for surface in self.surfaces:
            if surface.name == name:
                return surface
        raise KeyError(name)

    @property
    def assembly_sha256(self) -> str:
        """Hash private content, identity and order without exposing any of them."""

        return _sha256_json(
            {
                "schema_version": self.schema_version,
                "surfaces": [
                    {
                        "name": surface.name,
                        "value": _canonical_frozen_value(surface.value),
                        "source_ids": list(surface.source_ids),
                        "item_ids": list(surface.item_ids),
                    }
                    for surface in self.surfaces
                ],
            },
        )

    def privacy_evidence(self) -> dict[str, Any]:
        """Return the only representation permitted in ordinary telemetry."""

        return {
            "schema_version": self.schema_version,
            "snapshot_identity_sha256": _sha256_json(self.snapshot_id),
            "turn_identity_sha256": _sha256_json(self.turn_id),
            "assembly_sha256": self.assembly_sha256,
            "surface_count": len(self.surfaces),
            "surface_order_sha256": _sha256_json(
                [surface.name for surface in self.surfaces],
            ),
            "surfaces": [
                surface.privacy_evidence() for surface in self.surfaces
            ],
        }

    def __repr__(self) -> str:
        schema = (
            TURN_ASSEMBLY_SNAPSHOT_SCHEMA_V1
            if type(self.schema_version) is str
            and self.schema_version == TURN_ASSEMBLY_SNAPSHOT_SCHEMA_V1
            else "invalid"
        )
        surface_count: int | str = (
            len(self.surfaces) if type(self.surfaces) is tuple else "invalid"
        )
        return (
            "TurnAssemblySnapshot("
            f"schema_version={schema!r}, "
            "private_identities=True, "
            f"surface_count={surface_count!r}"
            ")"
        )


def build_turn_assembly_snapshot(
    *,
    snapshot_id: str,
    turn_id: str,
    surfaces: tuple[TurnAssemblySurface, ...] | list[TurnAssemblySurface],
) -> TurnAssemblySnapshot:
    """Freeze already-prepared surfaces without consulting any external owner."""

    if type(surfaces) not in {tuple, list}:
        raise TypeError("Turn assembly surfaces require a built-in list or tuple")

    return TurnAssemblySnapshot(
        snapshot_id=snapshot_id,
        turn_id=turn_id,
        surfaces=surfaces if type(surfaces) is tuple else tuple(surfaces),
    )


__all__ = [
    "FrozenJsonObject",
    "REQUIRED_TURN_ASSEMBLY_SURFACES",
    "TURN_ASSEMBLY_SNAPSHOT_SCHEMA_V1",
    "TurnAssemblySnapshot",
    "TurnAssemblySurface",
    "build_turn_assembly_snapshot",
    "freeze_turn_assembly_value",
    "thaw_turn_assembly_value",
]
