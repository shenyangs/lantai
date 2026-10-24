# -*- coding: utf-8 -*-
"""Resolve the effective work method from explicit and model-owned intent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .input import RuntimeInput

_MODEL_WORK_METHOD_CONFIDENCE = 0.65
_KNOWN_WORK_METHODS = frozenset(
    {
        "standard",
        "deep_research",
        "sustained_execution",
        "collaboration",
        "private_editor",
    }
)
_MODEL_UPGRADABLE_WORK_METHODS = _KNOWN_WORK_METHODS - {"private_editor"}


def _runtime_metadata(runtime_input: RuntimeInput) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    metadata.update(dict(runtime_input.request.metadata or {}))
    metadata.update(dict(runtime_input.metadata or {}))
    return metadata


def effective_work_method_id_from_metadata(metadata: Mapping[str, Any]) -> str:
    """Return explicit selection, or a confident model-authored upgrade.

    A non-standard selector is authoritative.  The auxiliary model may upgrade
    the default method when the user explicitly requests a named execution
    form in prose; Runtime never performs keyword classification here.
    """

    raw_contract = metadata.get("task_contract")
    contract = raw_contract if isinstance(raw_contract, Mapping) else {}
    selected = str(contract.get("work_method_id") or "standard").strip().lower()
    if selected not in _KNOWN_WORK_METHODS:
        selected = "standard"
    if selected != "standard":
        return selected

    raw_intelligence = metadata.get("turn_intelligence")
    intelligence = (
        raw_intelligence if isinstance(raw_intelligence, Mapping) else {}
    )
    try:
        confidence = float(intelligence.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    requested = str(
        intelligence.get("requested_work_method_id") or ""
    ).strip().lower()
    if (
        intelligence.get("status") == "classified"
        and confidence >= _MODEL_WORK_METHOD_CONFIDENCE
        and requested in _MODEL_UPGRADABLE_WORK_METHODS
        and requested != "standard"
    ):
        return requested
    return selected


def effective_work_method_id(runtime_input: RuntimeInput) -> str:
    return effective_work_method_id_from_metadata(_runtime_metadata(runtime_input))


def durable_artifact_required_from_metadata(metadata: Mapping[str, Any]) -> bool:
    """Project a confident model-authored file-delivery requirement."""

    raw_intelligence = metadata.get("turn_intelligence")
    intelligence = (
        raw_intelligence if isinstance(raw_intelligence, Mapping) else {}
    )
    try:
        confidence = float(intelligence.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(
        intelligence.get("status") == "classified"
        and confidence >= _MODEL_WORK_METHOD_CONFIDENCE
        and intelligence.get("durable_artifact_intent") in {"create", "edit"}
    )


def durable_artifact_required(runtime_input: RuntimeInput) -> bool:
    return durable_artifact_required_from_metadata(_runtime_metadata(runtime_input))


def collaboration_is_required(runtime_input: RuntimeInput) -> bool:
    return effective_work_method_id(runtime_input) == "collaboration"


__all__ = [
    "collaboration_is_required",
    "durable_artifact_required",
    "durable_artifact_required_from_metadata",
    "effective_work_method_id",
    "effective_work_method_id_from_metadata",
]
