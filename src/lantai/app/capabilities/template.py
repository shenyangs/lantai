# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Any

from .models import CapabilityParameter, CapabilityStep

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


def find_placeholders(template: str) -> set[str]:
    return {match.group(1) for match in _PLACEHOLDER_RE.finditer(template or "")}


def build_parameter_values(
    parameters: list[CapabilityParameter],
    provided: dict[str, Any],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    missing: list[str] = []
    for parameter in parameters:
        if parameter.name in provided:
            values[parameter.name] = provided[parameter.name]
        elif parameter.default is not None:
            values[parameter.name] = parameter.default
        elif parameter.required:
            missing.append(parameter.label or parameter.name)

    extra = {key: value for key, value in provided.items() if key not in values}
    values.update(extra)
    if missing:
        raise ValueError(f"缺少必填参数：{', '.join(missing)}")
    return values


def _resolve_value(values: dict[str, Any], key: str) -> tuple[bool, Any]:
    current: Any = values
    for part in key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def render_template(template: str, values: dict[str, Any]) -> str:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        found, value = _resolve_value(values, key)
        if not found:
            missing.add(key)
            return match.group(0)
        return "" if value is None else str(value)

    rendered = _PLACEHOLDER_RE.sub(replace, template or "")
    if missing:
        raise ValueError(f"模板缺少参数值：{', '.join(sorted(missing))}")
    return rendered


def validate_step_parameters(step: CapabilityStep, values: dict[str, Any]) -> None:
    missing = [name for name in step.required_parameters if name not in values]
    if missing:
        raise ValueError(f"步骤「{step.title}」缺少参数：{', '.join(missing)}")
