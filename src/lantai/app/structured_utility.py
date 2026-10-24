# -*- coding: utf-8 -*-
"""Typed tasks over Lantai's isolated utility-model side channel."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Generic, TypeVar

from json_repair import repair_json
from pydantic import BaseModel, ValidationError

from .utility_model import run_utility_content_task, run_utility_text_task


ResultT = TypeVar("ResultT", bound=BaseModel)


class StructuredTaskValidationError(ValueError):
    """Raised when a utility response cannot satisfy the requested schema."""

    def __init__(
        self,
        message: str,
        *,
        validation_issues: tuple[str, ...] = (),
    ) -> None:
        self.validation_issues = validation_issues
        suffix = (
            "; schema_issues=" + ",".join(validation_issues)
            if validation_issues
            else ""
        )
        super().__init__(message + suffix)


@dataclass(frozen=True)
class StructuredUtilityResult(Generic[ResultT]):
    """Validated structured value plus safe execution metadata."""

    value: ResultT
    model_label: str
    repaired: bool


def _schema_prompt(system_prompt: str, result_model: type[BaseModel]) -> str:
    schema = json.dumps(
        result_model.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"{system_prompt.strip()}\n\n"
        "只输出一个符合该 schema 的 JSON 值。不要输出 Markdown、解释、前后缀或额外字段。\n"
        f"JSON Schema:\n{schema}"
    ).strip()


def estimate_structured_system_tokens(
    system_prompt: str,
    result_model: type[BaseModel],
) -> int:
    """Conservatively estimate the complete instruction plus JSON schema."""

    payload = _schema_prompt(system_prompt, result_model).encode("utf-8")
    return max(1, (len(payload) + 2) // 3)


def _json_candidate(raw_text: str) -> str:
    text = raw_text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return fenced.group(1).strip() if fenced else text


def _safe_validation_issues(exc: ValidationError) -> tuple[str, ...]:
    """Expose only schema-owned paths and error types, never rejected values."""

    issues: list[str] = []
    for error in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:24]:
        error_type = re.sub(
            r"[^a-z0-9_.-]+",
            "_",
            str(error.get("type") or "validation_error").lower(),
        )
        path = "$"
        for component in error.get("loc", ()):
            if isinstance(component, int):
                path += f"[{component}]"
            else:
                safe_component = re.sub(
                    r"[^A-Za-z0-9_.-]+",
                    "_",
                    str(component),
                )[:80]
                path += f".{safe_component}"
        issue = f"{error_type}@{path}"
        if issue not in issues:
            issues.append(issue)
    return tuple(issues)


def _parse_validated(
    raw_text: str,
    result_model: type[ResultT],
) -> tuple[ResultT, bool]:
    candidate = _json_candidate(raw_text)
    if not candidate:
        raise StructuredTaskValidationError("structured utility task returned empty output")
    repaired = False
    try:
        payload = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        repaired = True
        try:
            payload = repair_json(candidate, return_objects=True)
        except Exception as exc:
            raise StructuredTaskValidationError(
                "structured utility task returned invalid JSON",
            ) from exc
    try:
        return result_model.model_validate(payload), repaired
    except ValidationError as exc:
        raise StructuredTaskValidationError(
            "structured utility task output failed schema validation",
            validation_issues=_safe_validation_issues(exc),
        ) from exc


async def run_structured_utility_task(
    *,
    agent_id: str,
    system_prompt: str,
    user_prompt: str,
    purpose: str,
    result_model: type[ResultT],
    timeout_seconds: float | None = None,
    first_output_timeout_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
    allow_model_fallback: bool = True,
    expected_provider_id: str | None = None,
    require_no_api_cost: bool = False,
    max_output_tokens: int | None = None,
    usage_session_id: str | None = None,
) -> StructuredUtilityResult[ResultT] | None:
    """Run one isolated model task and expose only schema-validated output."""

    result = await run_utility_text_task(
        agent_id=agent_id,
        system_prompt=_schema_prompt(system_prompt, result_model),
        user_prompt=user_prompt,
        purpose=purpose,
        timeout_seconds=timeout_seconds,
        first_output_timeout_seconds=first_output_timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        allow_model_fallback=allow_model_fallback,
        expected_provider_id=expected_provider_id,
        require_no_api_cost=require_no_api_cost,
        max_output_tokens=max_output_tokens,
        usage_session_id=usage_session_id,
        structured_output_schema=result_model.model_json_schema(),
    )
    if result is None:
        return None
    value, repaired = _parse_validated(result.text, result_model)
    return StructuredUtilityResult(
        value=value,
        model_label=result.model_label,
        repaired=repaired,
    )


async def run_structured_utility_content_task(
    *,
    agent_id: str,
    system_prompt: str,
    user_content: str | list[dict[str, object]],
    purpose: str,
    result_model: type[ResultT],
    timeout_seconds: float | None = None,
    usage_session_id: str | None = None,
    wait_for_idle: bool = True,
) -> StructuredUtilityResult[ResultT] | None:
    """Run one isolated multimodal task and expose validated JSON only."""

    result = await run_utility_content_task(
        agent_id=agent_id,
        system_prompt=_schema_prompt(system_prompt, result_model),
        user_content=user_content,
        purpose=purpose,
        timeout_seconds=timeout_seconds,
        usage_session_id=usage_session_id,
        wait_for_idle=wait_for_idle,
    )
    if result is None:
        return None
    value, repaired = _parse_validated(result.text, result_model)
    return StructuredUtilityResult(
        value=value,
        model_label=result.model_label,
        repaired=repaired,
    )
