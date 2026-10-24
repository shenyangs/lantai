#!/usr/bin/env python3
"""Re-evaluate recorded real-model persona answers against the current suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_SUITE = (
    ROOT
    / "src"
    / "lantai"
    / "agents"
    / "md_files"
    / "constitutions"
    / "shen_xingzhi-scenarios-zh.yaml"
)


def review(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep recorded answers intact and refresh only suite evaluation fields."""

    from lantai.agents.persona.benchmark import (  # noqa: PLC0415
        evaluate_persona_response,
        load_builtin_persona_scenarios,
    )

    scenarios = {item.id: item for item in load_builtin_persona_scenarios("zh")}
    reviewed_results: list[dict[str, Any]] = []
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        scenario_id = str(item.get("scenario_id") or "")
        answer = str(item.get("answer") or "")
        scenario = scenarios.get(scenario_id)
        if scenario is None or not answer:
            raise ValueError(f"Recorded scenario is unavailable: {scenario_id!r}")
        evaluation = evaluate_persona_response(scenario, answer)
        reviewed = dict(item)
        reviewed["recorded_evaluation"] = {
            "passed": item.get("passed") is True,
            "missing_required_groups": item.get("missing_required_groups") or [],
            "forbidden_matches": item.get("forbidden_matches") or [],
        }
        reviewed["passed"] = evaluation.passed
        reviewed["missing_required_groups"] = [
            list(group) for group in evaluation.missing_required_groups
        ]
        reviewed["forbidden_matches"] = list(evaluation.forbidden_matches)
        reviewed_results.append(reviewed)

    passed = sum(item["passed"] is True for item in reviewed_results)
    result = dict(payload)
    result.update(
        {
            "schema": "com.lantai.source-persona-positive-smoke-reviewed.v1",
            "source_schema": payload.get("schema"),
            "reviewed_at": time.time(),
            "scenario_suite": {
                "path": str(SCENARIO_SUITE.relative_to(ROOT)),
                "sha256": hashlib.sha256(SCENARIO_SUITE.read_bytes()).hexdigest(),
            },
            "ok": passed == len(reviewed_results) and bool(reviewed_results),
            "passed": passed,
            "total": len(reviewed_results),
            "results": reviewed_results,
        },
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    result = review(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": result["ok"],
                "passed": result["passed"],
                "total": result["total"],
                "evidence": str(args.output),
            },
            ensure_ascii=False,
        ),
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
