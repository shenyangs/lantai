# -*- coding: utf-8 -*-
"""Conversation-scoped trusted projection for the latest analysis result."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..analysis import AnalysisError, AnalysisService
from .models import ModelMessage
from .tools import ToolSpec

logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ANALYSIS_CONTINUITY_TOOL_NAMES = frozenset(
    {
        "get_analysis_result",
        "get_analysis_delivery_status",
        "refresh_analysis_result",
        "export_analysis_evidence",
        "create_analysis_report",
        "create_analysis_chart",
        "create_analysis_presentation",
        "get_presentation_generation_status",
    }
)


def analysis_scope_from_runtime_context(
    *,
    request_session_id: str,
    runtime_metadata: dict[str, Any],
) -> str:
    """Resolve the state-owned chat scope used by analysis tools."""

    legacy_context = runtime_metadata.get("legacy_request_context")
    legacy_scope = (
        legacy_context.get("session_state_id")
        if isinstance(legacy_context, dict)
        else None
    )
    return str(
        legacy_scope
        or runtime_metadata.get("session_state_id")
        or request_session_id
        or ""
    ).strip()


def current_analysis_context_message(
    *,
    state_root: Path | str,
    scope: str,
) -> ModelMessage | None:
    """Project a sealed result identity without exposing data or local paths.

    Absence and invalid/corrupt state both omit the projection. The analysis
    tools keep their own fail-closed integrity checks; unrelated chat remains
    usable when optional continuity metadata cannot be loaded.
    """

    normalized_scope = str(scope or "").strip()
    if not normalized_scope:
        return None
    try:
        result = AnalysisService(Path(state_root), scope=normalized_scope).latest_result()
        evidence = result.get("evidence")
        if not isinstance(evidence, dict):
            raise AnalysisError("Analysis result record failed integrity check")
        analysis_id = result.get("analysis_id")
        source_sha256 = evidence.get("source_sha256")
        if (
            not isinstance(analysis_id, str)
            or _SHA256_RE.fullmatch(analysis_id) is None
            or not isinstance(source_sha256, str)
            or _SHA256_RE.fullmatch(source_sha256) is None
        ):
            raise AnalysisError("Analysis result record failed integrity check")
    except (AnalysisError, OSError, ValueError):
        logger.debug(
            "Current analysis context was not projected for this conversation",
            exc_info=True,
        )
        return None

    payload = json.dumps(
        {
            "analysis_id": analysis_id,
            "source_sha256": source_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ModelMessage.text(
        "system",
        (
            '<current-analysis-context enforcement="required">'
            "A checksum-verified immutable analysis result is available in "
            "this conversation. The JSON payload is trusted runtime state: "
            f"{payload} "
            "Use the full conversation and current request to decide "
            "semantically whether it is relevant; do not use keyword matching. "
            "When the user continues from or asks for a chart, report, or "
            "presentation based on this analysis, reuse analysis_id and call "
            "get_analysis_result instead of using shell/Python to inspect "
            "internal storage or re-importing/recomputing only to rediscover "
            "the result. The dedicated analysis continuation tools are already "
            "available for that request: do not call equip_lantai_tools, shell, "
            "Python, read_file, write_file, send_file_to_user, or generic "
            "create_presentation to inspect, reconstruct, verify, or deliver "
            "the sealed result. When the user asks to refresh the same source and "
            "plan, call refresh_analysis_result with this analysis_id. Import "
            "or calculate anew only for a new/changed source, a materially "
            "different calculation, or when no usable scoped result exists. "
            "If create_analysis_presentation returns a job_id that requires "
            "a probe, call get_presentation_generation_status with that exact "
            "job_id; never use create_presentation or start another "
            "deck for the same request. "
            "For a standard report, call create_analysis_report with the "
            "analysis_id, title, and output filename only and omit sections; "
            "the server-owned standard report already includes provenance, "
            "method, a result table, and key findings. Use sections only when "
            "the user explicitly asks for custom prose. Numeric report facts "
            "in custom sections must use row/column result-cell segments, "
            "never digits embedded in plain text. "
            "Ignore this context when the current request is unrelated."
            "</current-analysis-context>"
        ),
        metadata={
            "instruction_trust": "trusted_system",
            "instruction_origin": "runtime.current_analysis_context",
            "current_analysis_context": True,
            "analysis_id": analysis_id,
            "source_sha256": source_sha256,
        },
    )


def is_builtin_analysis_continuity_tool(spec: ToolSpec) -> bool:
    """Allow only reviewed built-ins to reuse a projected analysis result."""

    return (
        spec.enabled
        and spec.origin == "builtin"
        and spec.name in _ANALYSIS_CONTINUITY_TOOL_NAMES
    )


__all__ = [
    "analysis_scope_from_runtime_context",
    "current_analysis_context_message",
    "is_builtin_analysis_continuity_tool",
]
