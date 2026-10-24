# -*- coding: utf-8 -*-
"""Privacy-safe explanation of context composition and compaction."""

from __future__ import annotations

from typing import Any

_CATEGORY_ORDER = (
    "system",
    "tool_schema",
    "current_message",
    "attachment",
    "history",
    "summary",
    "stable_memory",
    "dynamic_memory",
    "wiki_evidence",
    "tool_result",
    "request_envelope",
)


def build_context_explanation(
    *,
    stats: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build counts and reason codes without exposing any context content."""

    raw_categories = manifest.get("categories") if isinstance(manifest, dict) else None
    categories: dict[str, dict[str, Any]] = {}
    if isinstance(raw_categories, dict):
        for key, value in raw_categories.items():
            if not isinstance(value, dict):
                continue
            estimated = _safe_int(value.get("estimated"))
            upper_bound = max(estimated, _safe_int(value.get("upper_bound")))
            categories[str(key)] = {
                "estimated_tokens": estimated,
                "upper_bound_tokens": upper_bound,
                "method": str(value.get("method") or "estimated"),
            }

    summary_tokens = _safe_int(stats.get("compressed_summary_tokens"))
    if categories:
        # The legacy formatter places the summary into history. Split it back
        # out for explanation while preserving the manifest total.
        history = categories.get("history")
        if summary_tokens and history:
            summary_estimate = min(summary_tokens, history["estimated_tokens"])
            summary_upper = min(summary_tokens, history["upper_bound_tokens"])
            history["estimated_tokens"] -= summary_estimate
            history["upper_bound_tokens"] -= summary_upper
            categories["summary"] = {
                "estimated_tokens": summary_estimate,
                "upper_bound_tokens": summary_upper,
                "method": "exact",
            }
    else:
        message_tokens = _safe_int(stats.get("messages_tokens"))
        if message_tokens:
            categories["history"] = {
                "estimated_tokens": message_tokens,
                "upper_bound_tokens": message_tokens,
                "method": "estimated",
            }
        if summary_tokens:
            categories["summary"] = {
                "estimated_tokens": summary_tokens,
                "upper_bound_tokens": summary_tokens,
                "method": "exact",
            }

    selected_categories = set(
        manifest.get("selected_categories") or []
        if isinstance(manifest, dict)
        else [],
    )
    total = sum(item["estimated_tokens"] for item in categories.values())
    category_rows = [
        {
            "key": key,
            **categories[key],
            "selected": not selected_categories or key in selected_categories,
            "share": (
                round(categories[key]["estimated_tokens"] / total, 4)
                if total > 0
                else 0.0
            ),
        }
        for key in sorted(
            categories,
            key=lambda item: (
                _CATEGORY_ORDER.index(item)
                if item in _CATEGORY_ORDER
                else len(_CATEGORY_ORDER),
                item,
            ),
        )
        if categories[key]["estimated_tokens"] > 0
        or categories[key]["upper_bound_tokens"] > 0
    ]

    discarded = manifest.get("discarded") if isinstance(manifest, dict) else None
    discarded_reasons = list(discarded.values()) if isinstance(discarded, dict) else []
    budget_discarded = discarded_reasons.count("budget")
    redundant_discarded = discarded_reasons.count("redundant")
    recovery = manifest.get("context_recovery") if isinstance(manifest, dict) else None
    provider_recovery = bool(
        isinstance(recovery, dict)
        and recovery.get("reason") == "provider_context_limit"
    )
    reason_codes: list[str] = []
    if summary_tokens:
        reason_codes.append("history_compacted")
    if budget_discarded:
        reason_codes.append("optional_context_over_budget")
    if redundant_discarded:
        reason_codes.append("redundant_context_removed")
    if provider_recovery:
        reason_codes.append("provider_context_limit_recovery")

    if provider_recovery:
        explanation = "模型报告了上下文上限，澜台已移除可选证据并安全重试一次。"
    elif budget_discarded:
        explanation = f"为守住智能预算，本轮有 {budget_discarded} 个可选上下文块未送入模型。"
    elif summary_tokens:
        explanation = "较早的对话已压缩为摘要；最近消息和摘要会一起参与后续回答。"
    else:
        explanation = "本轮尚未触发压缩，澜台会按模型真实窗口动态保留上下文。"

    actual_prompt_tokens = (
        _safe_int(manifest.get("actual_prompt_tokens"))
        if isinstance(manifest, dict)
        else 0
    )
    count_source = (
        "provider"
        if actual_prompt_tokens > 0
        else "manifest_estimate"
        if categories and isinstance(raw_categories, dict)
        else "memory_estimate"
    )
    return {
        "categories": category_rows,
        "count_source": count_source,
        "compaction": {
            "active": bool(reason_codes),
            "summary_tokens": summary_tokens,
            "budget_discarded": budget_discarded,
            "redundant_discarded": redundant_discarded,
            "provider_recovery": provider_recovery,
            "reason_codes": reason_codes,
            "explanation": explanation,
        },
    }


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

