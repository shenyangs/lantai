#!/usr/bin/env python3
"""Bind manual visual acceptance to exact benchmark PPTX and montage bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CRITERIA = {
    "hierarchy",
    "composition",
    "alignment",
    "whitespace",
    "density",
    "typography",
    "image_quality",
    "crop",
    "chart_readability",
    "chart_semantics",
    "repetition",
    "visual_consistency",
    "narrative_transition",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def verify_visual_review(
    benchmark_path: Path,
    review_path: Path,
) -> dict[str, Any]:
    benchmark = _read_json(benchmark_path)
    review = _read_json(review_path)
    if benchmark.get("expected_deck_count") != 10:
        raise ValueError("the v3 acceptance gate requires exactly 10 decks")
    if benchmark.get("completed_deck_count") != 10:
        raise ValueError("not all benchmark decks completed")
    if benchmark.get("all_deterministic_checks_passed") is not True:
        raise ValueError("benchmark deterministic checks did not all pass")
    if set(review.get("criteria") or []) != REQUIRED_CRITERIA:
        raise ValueError("visual review criteria are incomplete or unexpected")

    benchmark_decks = {
        str(item["id"]): item for item in benchmark.get("decks") or []
    }
    review_decks = {
        str(item["id"]): item for item in review.get("decks") or []
    }
    if len(benchmark_decks) != 10 or set(benchmark_decks) != set(review_decks):
        raise ValueError("visual review must cover the exact 10 benchmark decks")

    accepted: list[dict[str, Any]] = []
    for deck_id, deck in benchmark_decks.items():
        item = review_decks[deck_id]
        if deck.get("passed_deterministic_checks") is not True:
            raise ValueError(f"deterministic checks failed for {deck_id}")
        if item.get("status") != "pass" or item.get("issues"):
            raise ValueError(f"visual review did not pass for {deck_id}")
        pptx = Path(str(deck["pptx"]))
        montage = Path(str(deck["montage"]))
        if not pptx.is_file() or not montage.is_file():
            raise ValueError(f"review evidence is missing for {deck_id}")
        actual_pptx = _sha256(pptx)
        actual_montage = _sha256(montage)
        expected_pptx = str(deck.get("pptx_sha256") or "")
        if actual_pptx != expected_pptx or item.get("pptx_sha256") != expected_pptx:
            raise ValueError(f"PPTX hash mismatch for {deck_id}")
        if item.get("montage_sha256") != actual_montage:
            raise ValueError(f"montage hash mismatch for {deck_id}")
        accepted.append(
            {
                **deck,
                "manual_visual_status": "pass",
                "manual_visual_notes": item.get("notes", ""),
                "montage_sha256": actual_montage,
            }
        )

    evidence = dict(benchmark.get("evidence") or {})
    evidence["independent_visual_acceptance"] = "passed"
    blocking_gaps = [
        gap
        for gap in benchmark.get("blocking_gaps") or []
        if gap != "independent visual acceptance has not been recorded"
    ]

    return {
        **benchmark,
        "visual_acceptance": "passed_hash_bound_manual_review",
        "evidence": evidence,
        "blocking_gaps": blocking_gaps,
        "visual_review_schema": review.get("schema"),
        "visual_reviewed_at": review.get("reviewed_at"),
        "visual_review_criteria": sorted(REQUIRED_CRITERIA),
        "decks": accepted,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("review", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify_visual_review(args.benchmark, args.review)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        "Presentation v3 visual gate passed: "
        f"{len(result['decks'])} decks, "
        f"{sum(item['slide_count'] for item in result['decks'])} slides"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
