#!/usr/bin/env python3
"""Stage and optionally promote a versioned Lantai platform Prompt Bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lantai.agents.prompt import build_platform_prompt_bundle
from lantai.agents.prompt_bundle import PromptBundleRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-dir", type=Path, required=True)
    parser.add_argument("--gate-evidence", type=Path, required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument(
        "--rollout",
        choices=("candidate", "canary", "stable"),
        default="candidate",
    )
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--compare-build-id")
    args = parser.parse_args()

    gate = json.loads(args.gate_evidence.read_text(encoding="utf-8"))
    gate_payload = {
        "status": gate.get("status"),
        "evidence_id": str(args.gate_evidence),
    }
    build = build_platform_prompt_bundle(args.language)
    registry = PromptBundleRegistry(args.registry_dir)
    path = registry.stage(build, gate_evidence=gate_payload)
    result: dict[str, object] = {
        "ok": True,
        "prompt_build_id": build.manifest["prompt_build_id"],
        "manifest_sha256": build.manifest["prompt_sha256"],
        "bundle_path": str(path),
        "rollout": "candidate",
        "content_logged": False,
    }
    if args.compare_build_id:
        result["diff"] = registry.diff(
            args.compare_build_id,
            build.manifest["prompt_build_id"],
        )
    if args.promote:
        state = registry.promote(
            build.manifest["prompt_build_id"],
            rollout=args.rollout,
            gate_evidence=gate_payload if args.rollout == "stable" else None,
        )
        result["rollout"] = state["rollout"]
        result["previous_build_id"] = state.get("previous_build_id")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
