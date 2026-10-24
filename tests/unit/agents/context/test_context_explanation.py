# -*- coding: utf-8 -*-
"""Context explanation tests."""

from lantai.agents.context.explanation import build_context_explanation


def test_manifest_categories_and_compaction_reasons_are_explained():
    result = build_context_explanation(
        stats={
            "messages_tokens": 300,
            "compressed_summary_tokens": 80,
        },
        manifest={
            "categories": {
                "system": {
                    "estimated": 100,
                    "upper_bound": 110,
                    "method": "estimated",
                },
                "history": {
                    "estimated": 300,
                    "upper_bound": 320,
                    "method": "estimated",
                },
                "tool_result": {
                    "estimated": 200,
                    "upper_bound": 210,
                    "method": "exact",
                },
            },
            "selected_categories": ["system", "history", "tool_result"],
            "discarded": {"wiki-1": "budget", "wiki-2": "redundant"},
            "actual_prompt_tokens": 590,
            "context_recovery": {"reason": "provider_context_limit"},
        },
    )

    categories = {item["key"]: item for item in result["categories"]}
    assert categories["history"]["estimated_tokens"] == 220
    assert categories["summary"]["estimated_tokens"] == 80
    assert result["count_source"] == "provider"
    assert result["compaction"]["budget_discarded"] == 1
    assert result["compaction"]["provider_recovery"] is True
    assert "安全重试" in result["compaction"]["explanation"]


def test_memory_estimate_is_used_before_first_model_manifest():
    result = build_context_explanation(
        stats={
            "messages_tokens": 120,
            "compressed_summary_tokens": 30,
        },
        manifest=None,
    )

    assert result["count_source"] == "memory_estimate"
    assert [item["key"] for item in result["categories"]] == [
        "history",
        "summary",
    ]
    assert result["compaction"]["reason_codes"] == ["history_compacted"]


def test_explanation_never_contains_manifest_payload_content():
    result = build_context_explanation(
        stats={"messages_tokens": 1, "compressed_summary_tokens": 0},
        manifest={
            "categories": {
                "current_message": {
                    "estimated": 1,
                    "upper_bound": 1,
                    "method": "exact",
                },
            },
            "payload_hash": "hash-only",
            "selected": ["secret prompt content"],
        },
    )

    assert "secret prompt content" not in str(result)

