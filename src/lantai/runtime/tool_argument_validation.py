"""Bounded schema-only diagnostics for correcting tool arguments.

Validation remains authoritative and never rewrites an argument. In particular,
oneOf errors must not echo a whole slide or hide the actionable leaf constraint.
"""

from __future__ import annotations

import json
from collections import OrderedDict, deque
from itertools import islice
from typing import Any, Iterable

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for


def _schema_value(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    return encoded if len(encoded) <= 240 else encoded[:237] + "..."


def _argument_path(path: Iterable[Any]) -> str:
    result = "arguments"
    for component in path:
        if isinstance(component, int):
            result += f"[{component}]"
        elif str(component).isidentifier():
            result += f".{component}"
        else:
            result += f"[{_schema_value(str(component))}]"
    return result if len(result) <= 240 else result[:237] + "..."


def _relevant_errors(error: ValidationError) -> Iterable[ValidationError]:
    if error.validator not in {"oneOf", "anyOf"} or not error.context:
        yield error
        return

    branches: dict[int, list[ValidationError]] = {}
    for child in error.context:
        branch = child.schema_path[0]
        if isinstance(branch, int):
            branches.setdefault(branch, []).append(child)
    # A failed const discriminator (e.g. layout_id or kind) identifies a
    # different alternative, not a constraint to impose on the selected one.
    # Do not guess between multiple remaining alternatives.
    candidates = [
        children
        for children in branches.values()
        if not any(
            child.validator == "const"
            or (child.validator == "type" and not child.path)
            for child in children
        )
    ]
    if len(candidates) == 1:
        for child in candidates[0]:
            yield from _relevant_errors(child)
    else:
        yield error


def _constraint(error: ValidationError) -> str:
    keyword = error.validator
    expected = _schema_value(error.validator_value)
    if keyword == "required":
        missing = [
            key for key in error.validator_value
            if isinstance(error.instance, dict) and key not in error.instance
        ]
        return f"missing required properties {_schema_value(missing)}"
    if keyword == "additionalProperties":
        allowed = list(error.schema.get("properties", {}))
        return (
            "unexpected properties; permitted properties "
            f"{_schema_value(allowed)}"
        )
    if keyword == "type":
        return f"must have type {expected}"
    if keyword in {"enum", "const"}:
        return f"must match {keyword} {expected}"
    if keyword in {"oneOf", "anyOf"}:
        quantifier = "exactly one" if keyword == "oneOf" else "at least one"
        return f"must match {quantifier} permitted schema ({keyword})"
    # Never use ValidationError.message: several validators include repr of
    # the entire instance, which can contain large/private authored content.
    return f"must satisfy {keyword}={expected}"


def tool_argument_schema_feedback(
    schema: dict[str, Any],
    arguments: dict[str, Any],
) -> str | None:
    """Return limited, page-fair diagnostics, or None for valid arguments."""

    validator = validator_for(schema)(schema)
    issues: OrderedDict[
        tuple[str, str], tuple[tuple[Any, ...], str]
    ] = OrderedDict()
    capped = False
    for error_index, error in enumerate(validator.iter_errors(arguments)):
        if error_index >= 128:
            capped = True
            break
        for leaf in islice(_relevant_errors(error), 129):
            path = tuple(leaf.absolute_path)
            detail = _constraint(leaf)
            issues.setdefault((_argument_path(path), detail), (path, detail))
            if len(issues) >= 128:
                capped = True
                break
        if capped:
            break
    if not issues:
        return None

    # Show at least one violation from each array item before filling the
    # remainder; a dense first page must not conceal all later page errors.
    scopes: OrderedDict[tuple[Any, ...], deque[tuple[str, str]]] = OrderedDict()
    for (argument_path, detail), (path, _) in issues.items():
        first_index = next(
            (
                index for index, component in enumerate(path)
                if isinstance(component, int)
            ),
            None,
        )
        scope = path[:first_index + 1] if first_index is not None else ()
        scopes.setdefault(scope, deque()).append((argument_path, detail))
    lines: list[str] = []
    while scopes and len(lines) < 32:
        for scope in list(scopes):
            argument_path, detail = scopes[scope].popleft()
            line = f"{argument_path}: {detail}"
            if sum(len(item) + 1 for item in lines) + len(line) > 5200:
                capped = True
                scopes.clear()
                break
            lines.append(line)
            if not scopes[scope]:
                del scopes[scope]
            if len(lines) >= 32:
                break
    omitted = capped or bool(scopes)
    suffix = (
        " Additional violations omitted; validate the entire argument object."
        if omitted else ""
    )
    return (
        "JSON Schema validation failed:\n"
        + "\n".join(lines)
        + "\nCorrect the listed fields while preserving other arguments; "
        "do not repeat the unchanged invalid call."
        + suffix
    )
