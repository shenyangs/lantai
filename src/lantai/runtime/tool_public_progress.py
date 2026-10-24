# -*- coding: utf-8 -*-
"""Provider-facing schema and parsing for model-authored work progress."""

from __future__ import annotations

import re
from typing import Any, Sequence

from .models import ModelContentBlock
from .tools import ToolCall, ToolSpec


PUBLIC_PROGRESS_ARGUMENT = "_lantai_public_progress"
PUBLIC_PROGRESS_ARGUMENT_ALIASES = frozenset({"_public_progress"})


def collapse_repeated_public_progress(value: str) -> str:
    """Collapse an exact repeated progress sentence without rewriting it."""

    text = value.strip()
    match = re.fullmatch(r"(.+?)\1+", text, flags=re.DOTALL)
    return match.group(1).strip() if match else text


def tool_specs_with_public_progress_contract(
    specs: list[ToolSpec],
) -> list[ToolSpec]:
    """Add one presentation-only choice to object-shaped provider schemas."""

    projected: list[ToolSpec] = []
    for spec in specs:
        if bool(spec.metadata.get("progressive_tool_loader")):
            projected.append(spec)
            continue
        schema = spec.input_schema
        properties = schema.get("properties")
        if schema.get("type") != "object" or not isinstance(properties, dict):
            projected.append(spec)
            continue
        if PUBLIC_PROGRESS_ARGUMENT in properties:
            raise ValueError(
                f"tool {spec.name!r} uses reserved Runtime presentation field "
                f"{PUBLIC_PROGRESS_ARGUMENT!r}"
            )
        required = schema.get("required")
        required_fields = (
            [item for item in required if isinstance(item, str)]
            if isinstance(required, list)
            else []
        )
        projected.append(
            spec.model_copy(
                update={
                    "input_schema": {
                        **schema,
                        "properties": {
                            **properties,
                            PUBLIC_PROGRESS_ARGUMENT: {
                                "type": "string",
                                "description": (
                                    "Model-authored public work update: one brief "
                                    "nonrepeating confirmed fact, unfinished point, or current "
                                    "action when the work-presence contract applies; "
                                    "otherwise an empty string."
                                ),
                            },
                        },
                        "required": [*required_fields, PUBLIC_PROGRESS_ARGUMENT],
                    },
                }
            )
        )
    return projected


def without_public_progress_argument(tool_call: ToolCall) -> ToolCall:
    """Remove the presentation-only field before validation and execution."""

    arguments = dict(tool_call.arguments)
    reserved_arguments = {
        PUBLIC_PROGRESS_ARGUMENT,
        *PUBLIC_PROGRESS_ARGUMENT_ALIASES,
    }
    if not reserved_arguments.intersection(arguments):
        return tool_call
    for argument in reserved_arguments:
        arguments.pop(argument, None)
    return ToolCall(
        id=tool_call.id,
        name=tool_call.name,
        arguments=arguments,
        metadata=dict(tool_call.metadata),
    )


def without_public_progress_block_data(value: dict[str, Any]) -> dict[str, Any]:
    """Remove the field from a tool block before public event projection."""

    data = dict(value)
    for key in ("arguments", "input"):
        arguments = data.get(key)
        if not isinstance(arguments, dict):
            continue
        cleaned = dict(arguments)
        cleaned.pop(PUBLIC_PROGRESS_ARGUMENT, None)
        for alias in PUBLIC_PROGRESS_ARGUMENT_ALIASES:
            cleaned.pop(alias, None)
        data[key] = cleaned
    return data


def model_tool_public_progress(
    content: Sequence[ModelContentBlock],
    *,
    allow_structured: bool = True,
) -> tuple[str, list[str], list[str]] | None:
    """Read public text and its concrete tool identities without using thinking."""

    first_tool_index = next(
        (
            index
            for index, block in enumerate(content)
            if block.type == "tool_call"
            and isinstance(block.data, dict)
            and str(block.data.get("name") or "").strip()
        ),
        None,
    )
    if first_tool_index is None:
        return None
    public_parts = [
        block.text
        for block in content[:first_tool_index]
        if block.type == "text"
        and isinstance(block.text, str)
        and block.text.strip()
    ]
    tool_blocks = [
        block
        for block in content[first_tool_index:]
        if block.type == "tool_call" and isinstance(block.data, dict)
    ]
    if tool_blocks and all(
        str(block.data.get("name") or "") == "equip_lantai_tools"
        for block in tool_blocks
    ):
        return None
    structured_parts: list[str] = []
    for block in tool_blocks if allow_structured else ():
        arguments = block.data.get("arguments")
        if not isinstance(arguments, dict):
            arguments = block.data.get("input")
        candidate = (
            arguments.get(PUBLIC_PROGRESS_ARGUMENT)
            if isinstance(arguments, dict)
            else None
        )
        if isinstance(candidate, str) and candidate.strip():
            structured_parts.append(candidate.strip())
    public_text = (
        structured_parts[0]
        if structured_parts
        else "\n".join(public_parts).strip()
    )
    public_text = collapse_repeated_public_progress(public_text)
    tool_call_ids = [
        call_id
        for block in tool_blocks
        if (
            call_id := str(
                block.data.get("id") or block.data.get("call_id") or ""
            ).strip()
        )
    ]
    if not public_text or not tool_call_ids:
        return None
    return public_text, tool_call_ids, public_parts


__all__ = [
    "PUBLIC_PROGRESS_ARGUMENT",
    "PUBLIC_PROGRESS_ARGUMENT_ALIASES",
    "collapse_repeated_public_progress",
    "model_tool_public_progress",
    "tool_specs_with_public_progress_contract",
    "without_public_progress_argument",
    "without_public_progress_block_data",
]
