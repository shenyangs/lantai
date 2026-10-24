# -*- coding: utf-8 -*-
"""Canonical Provider Normalizer exchange and differential contracts."""

from __future__ import annotations

import json
from pathlib import Path

from lantai.providers.stream_normalizer import (
    ProviderStreamNormalizer,
    delta_from_possible_cumulative_text,
    normalize_stream_chunk,
    provider_stream_profile,
)


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/providers/canonical_stream_exchange_v1.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_canonical_stream_exchange_fixture() -> None:
    for case in _fixture()["cases"]:
        profile = provider_stream_profile(
            provider_id=case["provider_id"],
            model_id=case["model_id"],
            family=case["family"],
        )
        normalizer = ProviderStreamNormalizer(profile)
        deltas = [
            normalizer.text_delta(chunk, channel=case["channel"])
            for chunk in case["chunks"]
        ]

        assert [delta for delta in deltas if delta] == case["expected_deltas"], case[
            "id"
        ]
        assert normalizer.full_text(channel=case["channel"]) == case[
            "expected_full"
        ], case["id"]


def test_canonical_snapshot_block_fixture() -> None:
    for case in _fixture()["snapshot_block_cases"]:
        profile = provider_stream_profile(
            provider_id=case["provider_id"],
            model_id=case["model_id"],
            family=case["family"],
        )
        normalized = normalize_stream_chunk(case["payload"], profile=profile)

        assert normalized.thinking_delta == case["expected_thinking"], case["id"]
        assert normalized.text_delta == case["expected_text"], case["id"]


def test_every_differential_change_is_explicitly_explained() -> None:
    """Shadow comparator: unexplained differences from the old helper fail."""

    for case in _fixture()["cases"]:
        previous = ""
        old_deltas: list[str] = []
        for chunk in case["chunks"]:
            delta, previous = delta_from_possible_cumulative_text(
                chunk,
                previous=previous,
            )
            if delta:
                old_deltas.append(delta)

        changed = old_deltas != case["expected_deltas"]
        assert changed == bool(case.get("expected_old_difference")), case["id"]

    for case in _fixture()["snapshot_block_cases"]:
        old = normalize_stream_chunk(case["payload"])
        changed = (
            old.thinking_delta != case["expected_thinking"]
            or old.text_delta != case["expected_text"]
        )
        assert changed == bool(case.get("expected_old_difference")), case["id"]
