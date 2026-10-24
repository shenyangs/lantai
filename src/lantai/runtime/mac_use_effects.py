# -*- coding: utf-8 -*-
"""Mac Use interpretation helpers for the Runtime effect journal."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .runs import RuntimeEffectRecord


@dataclass(frozen=True)
class MacUseEffectInterpretation:
    """Normalized effect facts needed by the Native Runtime adapter."""

    external_status: str
    resolves_external_effect_id: str
    verification_target: RuntimeEffectRecord | None
    ambiguity_reason: str | None


def _text(metadata: Mapping[str, Any], key: str, default: str = "") -> str:
    return str(metadata.get(key) or default).strip()


def interpret_mac_use_effect(
    records: Iterable[RuntimeEffectRecord],
    *,
    result_name: str,
    metadata: Mapping[str, Any],
) -> MacUseEffectInterpretation:
    """Resolve a verified probe or preserve one ambiguous GUI action."""

    external_status = _text(metadata, "external_effect_status")
    resolves_external_effect_id = _text(
        metadata,
        "resolves_external_effect_id",
    )
    resolves_scope_hash = _text(
        metadata,
        "resolves_external_effect_scope_hash",
    )
    verification_target = None
    if (
        result_name == "mac_verify"
        and resolves_external_effect_id
        and external_status in {"verified", "no_effect_verified"}
    ):
        latest_by_id: dict[str, RuntimeEffectRecord] = {}
        for record in records:
            previous = latest_by_id.get(record.effect_id)
            if previous is None or record.revision > previous.revision:
                latest_by_id[record.effect_id] = record
        expected_reason = (
            f"external_effect:{resolves_external_effect_id}:"
            f"scope:{resolves_scope_hash}"
        )
        matches = [
            record
            for record in latest_by_id.values()
            if record.status == "ambiguous"
            and record.tool_name == "mac_act"
            and record.ambiguity_reason == expected_reason
        ]
        if len(matches) == 1:
            verification_target = matches[0]

    ambiguity_reason = None
    if result_name == "mac_act" and external_status == "ambiguous":
        ambiguity_reason = (
            f"external_effect:{_text(metadata, 'external_effect_id', 'unknown')}:"
            f"scope:{_text(metadata, 'external_effect_scope_hash', 'missing')}"
        )
    return MacUseEffectInterpretation(
        external_status=external_status,
        resolves_external_effect_id=resolves_external_effect_id,
        verification_target=verification_target,
        ambiguity_reason=ambiguity_reason,
    )


__all__ = ["MacUseEffectInterpretation", "interpret_mac_use_effect"]
