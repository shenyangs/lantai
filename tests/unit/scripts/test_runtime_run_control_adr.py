# -*- coding: utf-8 -*-
"""Guard the Runtime run-control convergence ADR."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ADR = ROOT / "docs/adr/ADR-runtime-run-control-convergence-zh.md"

REQUIRED_FRAGMENTS = [
    "状态：Accepted",
    "TaskRun snapshot",
    "RuntimeEvent",
    "EffectRecord",
    "EvaluationReport",
    "Run",
    "Attempt",
    "Turn",
    "Step",
    "Effect",
    "状态枚举",
    "终态优先级",
    "fencing",
    "Mission",
    "BrowserTask",
    "Capability/Pipeline",
    "Adapter",
    "schema_version",
    "迁移",
    "回滚",
    "官网版",
    "MAS",
]


def test_runtime_run_control_adr_exists() -> None:
    assert ADR.exists()


def test_runtime_run_control_adr_covers_required_decisions() -> None:
    text = ADR.read_text(encoding="utf-8")

    for fragment in REQUIRED_FRAGMENTS:
        assert fragment in text

    assert "不得创建新的 loop state 目录" in text
