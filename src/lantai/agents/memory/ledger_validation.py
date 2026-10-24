"""Storage validation for canonical evidence, without changing admission policy."""

from __future__ import annotations

import json
import math
from dataclasses import MISSING, fields
from typing import Literal, get_args, get_origin, get_type_hints


def object_payload(value: str, *, versions: set[int]) -> dict:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("expected object")
    version = payload.get("version", 1)
    if type(version) is not int or version not in versions:
        raise ValueError("unsupported version")
    return payload


def string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def record_payload(raw: object, record_type: type) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("expected record")
    if "version" in raw and (type(raw["version"]) is not int or raw["version"] not in {1, 2}):
        raise ValueError("unsupported record version")
    hints = get_type_hints(record_type)
    payload: dict = {}
    for item in fields(record_type):
        if item.name not in raw:
            if item.default is MISSING and item.default_factory is MISSING:
                raise ValueError("missing required field")
            continue
        value = raw[item.name]
        hint = hints[item.name]
        origin = get_origin(hint)
        valid = True
        if hint is str:
            valid = isinstance(value, str)
        elif hint is int:
            valid = type(value) is int
        elif hint is float:
            valid = type(value) in {int, float} and math.isfinite(value)
        elif origin is Literal:
            valid = value in get_args(hint)
        elif origin is list:
            element_type = get_args(hint)[0]
            valid = isinstance(value, list) and all(isinstance(element, element_type) for element in value)
        if not valid:
            raise ValueError("invalid field type")
        payload[item.name] = value
    if not payload.get("id") or not payload.get("content"):
        raise ValueError("missing record identity or content")
    return payload
