# -*- coding: utf-8 -*-
"""Provider stream payload fixture contracts."""

from __future__ import annotations

import json
from pathlib import Path

from lantai.providers.stream_normalizer import (
    ProviderStreamNormalizer,
    normalize_provider_stream_payload,
    normalize_stream_chunk,
    provider_stream_profile,
)


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/providers/stream_payload_contracts.json"


def _fixture_cases() -> list[dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return list(payload["cases"])


def _tool_call_summary(raw_calls: list[object]) -> list[dict]:
    calls: list[dict] = []
    for raw in raw_calls:
        if isinstance(raw, dict):
            function = raw.get("function")
            if isinstance(function, dict):
                calls.append(
                    {
                        "index": raw.get("index"),
                        "id": raw.get("id"),
                        "name": function.get("name"),
                        "arguments": function.get("arguments"),
                    },
                )
            else:
                calls.append(dict(raw))
            continue

        function = getattr(raw, "function", None)
        calls.append(
            {
                "index": getattr(raw, "index", None),
                "id": getattr(raw, "id", None),
                "name": getattr(function, "name", None),
                "arguments": getattr(function, "arguments", None),
            },
        )
    return [
        {key: value for key, value in call.items() if value is not None}
        for call in calls
    ]


def test_provider_stream_payload_fixture_cases_are_normalized() -> None:
    for case in _fixture_cases():
        chunk = normalize_provider_stream_payload(
            case["payload"],
            family=case["family"],
        )
        expected = case["expected"]

        for key in ("text_delta", "thinking_delta", "finish_reason", "error", "done"):
            if key in expected:
                assert getattr(chunk, key) == expected[key], case["id"]

        if "usage_delta" in expected:
            assert chunk.usage_delta == expected["usage_delta"], case["id"]

        if "tool_call_delta" in expected:
            assert _tool_call_summary(chunk.tool_call_delta) == expected[
                "tool_call_delta"
            ], case["id"]


def test_provider_normalizer_detects_cumulative_snapshots_after_prefix_growth() -> None:
    normalizer = ProviderStreamNormalizer()

    assert normalizer.text_delta("想", channel="thinking") == "想"
    assert normalizer.text_delta("想明", channel="thinking") == "明"
    assert normalizer.text_delta("想明", channel="thinking") == ""
    assert normalizer.full_text(channel="thinking") == "想明"


def test_provider_normalizer_preserves_identical_legal_delta_tokens() -> None:
    normalizer = ProviderStreamNormalizer()

    assert normalizer.text_delta("哈") == "哈"
    assert normalizer.text_delta("哈") == "哈"
    assert normalizer.full_text() == "哈哈"


def test_minimax_profile_deduplicates_only_same_snapshot_blocks() -> None:
    minimax_profile = provider_stream_profile(
        provider_id="minimax-cn",
        model_id="MiniMax-M3",
    )
    ordinary_profile = provider_stream_profile(
        provider_id="anthropic",
        model_id="claude-test",
        family="anthropic",
    )
    payload = {
        "content": [
            {"type": "thinking", "thinking": "检查一次"},
            {"type": "thinking", "thinking": "检查一次"},
            {"type": "text", "text": "有意重复"},
            {"type": "text", "text": "有意重复"},
        ],
    }

    minimax = normalize_stream_chunk(payload, profile=minimax_profile)
    ordinary = normalize_stream_chunk(payload, profile=ordinary_profile)

    assert minimax.thinking_delta == "检查一次"
    assert minimax.text_delta == "有意重复"
    assert ordinary.thinking_delta == "检查一次\n检查一次"
    assert ordinary.text_delta == "有意重复\n有意重复"
