# -*- coding: utf-8 -*-
"""Static locks for the single transcript reconciliation authority."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
VENDOR_ROOT = ROOT / "console" / "src" / "vendor"
CHAT_ROOT = ROOT / "console" / "src" / "pages" / "Chat"


def test_transcript_authority_heuristics_live_only_in_reconciler() -> None:
    reconciler = (VENDOR_ROOT / "lantaiTranscriptReconciler.ts").read_text(
        encoding="utf-8",
    )
    renderer = (VENDOR_ROOT / "lantaiChat.ts").read_text(encoding="utf-8")

    for definition in (
        "const incomingSnapshotInvertsVisibleTimeline",
        "const hasTrailingEmptyOptimisticAssistant",
        "const incomingContainsEveryVisibleUser",
        "const reconcileRouteHydration",
    ):
        assert definition in reconciler
        assert definition not in renderer

    assert "createTranscriptReconciler" in renderer
    assert "transcriptReconciler.reconcileProjection" in renderer


def test_live_bridge_and_route_hydration_share_reconciler_authority() -> None:
    bridge = (
        CHAT_ROOT / "components" / "RuntimeEventStreamBridge.tsx"
    ).read_text(encoding="utf-8")

    assert "chooseActiveSessionMessages" in bridge
    assert "durableTranscriptHydrationIsRequired" in bridge
    assert "function durableHydrationIsStillRequired" not in bridge
    assert "function hasCanonicalHistoryForLatestUser" not in bridge
