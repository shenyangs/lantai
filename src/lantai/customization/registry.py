"""Machine-readable single source of truth for customizable fields."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import RegistryItem

_ITEM_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class RegistryError(ValueError):
    """Raised when the bundled or imported registry is invalid."""


class CustomizationRegistry:
    def __init__(self, *, schema_version: int, items: list[RegistryItem]) -> None:
        self.schema_version = schema_version
        self.items = items
        self._by_id = {item.id: item for item in items}
        if len(self._by_id) != len(items):
            raise RegistryError("Customization Registry contains duplicate IDs")
        for item in items:
            if not _ITEM_ID.fullmatch(item.id):
                raise RegistryError(f"Invalid customization item ID: {item.id!r}")
            self.validate_value(item, item.factory_value)
            if item.merge_strategy == "immutable" and item.risk != "forbidden":
                raise RegistryError(
                    f"Immutable customization item must be forbidden: {item.id}"
                )

    @classmethod
    def load(cls, path: Path) -> "CustomizationRegistry":
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"Unable to read Customization Registry: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise RegistryError("Customization Registry must contain an items array")
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            items=[RegistryItem.model_validate(value) for value in payload["items"]],
        )

    @classmethod
    def load_bundled(cls) -> "CustomizationRegistry":
        return cls.load(Path(__file__).with_name("data") / "registry.json")

    def get(self, item_id: str) -> RegistryItem:
        try:
            return self._by_id[item_id]
        except KeyError as exc:
            raise RegistryError(f"Unknown customization item: {item_id}") from exc

    def factory_values(self) -> dict[str, Any]:
        return {item.id: deepcopy(item.factory_value) for item in self.items}

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "items": [
                item.model_dump(mode="json")
                for item in self.items
                if item.export_policy != "private"
            ],
        }

    @staticmethod
    def validate_value(item: RegistryItem, value: Any) -> Any:
        type_ok = {
            "boolean": lambda candidate: isinstance(candidate, bool),
            "string": lambda candidate: isinstance(candidate, str),
            "number": lambda candidate: (
                isinstance(candidate, (int, float))
                and not isinstance(candidate, bool)
            ),
            "string_array": lambda candidate: (
                isinstance(candidate, list)
                and all(isinstance(entry, str) for entry in candidate)
            ),
            "object": lambda candidate: isinstance(candidate, dict),
        }[item.value_type](value)
        if not type_ok:
            raise RegistryError(
                f"Invalid value for {item.id}: expected {item.value_type}"
            )
        if item.enum is not None and value not in item.enum:
            raise RegistryError(
                f"Invalid value for {item.id}: expected one of {item.enum!r}"
            )
        if item.value_type == "number":
            numeric = float(value)
            if item.minimum is not None and numeric < item.minimum:
                raise RegistryError(f"Value for {item.id} is below its minimum")
            if item.maximum is not None and numeric > item.maximum:
                raise RegistryError(f"Value for {item.id} exceeds its maximum")
        if item.id == "appearance.design_tokens":
            if len(value) > 120:
                raise RegistryError("Too many appearance design tokens")
            for name, token in value.items():
                if not re.fullmatch(r"--lt-[a-z0-9-]+", str(name)):
                    raise RegistryError(f"Invalid Lantai design token: {name!r}")
                if not isinstance(token, str) or not token.strip() or len(token) > 256:
                    raise RegistryError(f"Invalid value for design token {name!r}")
                lowered = token.casefold()
                if any(part in lowered for part in ("url(", "@import", "expression(")):
                    raise RegistryError(f"Unsafe value for design token {name!r}")
        if item.id == "automation.quiet_hours":
            allowed = {"start", "end", "timezone"}
            if set(value) - allowed or any(
                not isinstance(entry, str) for entry in value.values()
            ):
                raise RegistryError("Invalid automation quiet-hours object")
        return deepcopy(value)
