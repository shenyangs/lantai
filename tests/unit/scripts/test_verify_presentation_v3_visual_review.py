from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "verify_presentation_v3_visual_review.py"
)
SPEC = importlib.util.spec_from_file_location("visual_review_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence(tmp_path: Path) -> tuple[Path, Path]:
    decks = []
    reviews = []
    for index in range(10):
        deck_id = f"deck-{index}"
        folder = tmp_path / deck_id
        folder.mkdir()
        pptx = folder / "presentation.pptx"
        montage = folder / "montage.png"
        pptx.write_bytes(f"pptx-{index}".encode())
        montage.write_bytes(f"montage-{index}".encode())
        pptx_hash = _sha256(pptx)
        montage_hash = _sha256(montage)
        decks.append(
            {
                "id": deck_id,
                "passed_deterministic_checks": True,
                "slide_count": 5,
                "pptx": str(pptx),
                "pptx_sha256": pptx_hash,
                "montage": str(montage),
            }
        )
        reviews.append(
            {
                "id": deck_id,
                "pptx_sha256": pptx_hash,
                "montage_sha256": montage_hash,
                "status": "pass",
                "issues": [],
            }
        )
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text(
        json.dumps(
            {
                "expected_deck_count": 10,
                "completed_deck_count": 10,
                "all_deterministic_checks_passed": True,
                "evidence": {
                    "independent_visual_acceptance": "not_run",
                    "semantic_model_planning": "not_run",
                },
                "blocking_gaps": [
                    "semantic model planning is not exercised by the controlled fixture benchmark",
                    "independent visual acceptance has not been recorded",
                ],
                "decks": decks,
            }
        ),
        encoding="utf-8",
    )
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "schema": "com.lantai.presentation-visual-review.v1",
                "criteria": sorted(MODULE.REQUIRED_CRITERIA),
                "decks": reviews,
            }
        ),
        encoding="utf-8",
    )
    return benchmark, review


def test_hash_bound_visual_review_passes(tmp_path: Path) -> None:
    benchmark, review = _evidence(tmp_path)
    result = MODULE.verify_visual_review(benchmark, review)
    assert result["visual_acceptance"] == "passed_hash_bound_manual_review"
    assert result["evidence"]["independent_visual_acceptance"] == "passed"
    assert result["evidence"]["semantic_model_planning"] == "not_run"
    assert result["blocking_gaps"] == [
        "semantic model planning is not exercised by the controlled fixture benchmark",
    ]
    assert len(result["decks"]) == 10
    assert all(item["manual_visual_status"] == "pass" for item in result["decks"])


def test_hash_bound_visual_review_rejects_changed_montage(tmp_path: Path) -> None:
    benchmark, review = _evidence(tmp_path)
    payload = json.loads(benchmark.read_text(encoding="utf-8"))
    Path(payload["decks"][0]["montage"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="montage hash mismatch"):
        MODULE.verify_visual_review(benchmark, review)


def test_hash_bound_visual_review_requires_all_ten_decks(tmp_path: Path) -> None:
    benchmark, review = _evidence(tmp_path)
    payload = json.loads(review.read_text(encoding="utf-8"))
    payload["decks"].pop()
    review.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="exact 10"):
        MODULE.verify_visual_review(benchmark, review)
