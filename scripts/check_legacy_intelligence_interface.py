#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the R3 Legacy intelligence facade without executing user work."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = (
    ROOT
    / "docs"
    / "remediation"
    / "evidence"
    / "legacy-intelligence-interface-r3-contract-2026-08-31.json"
)
REQUIRED_SNAPSHOTS = {
    "prompt",
    "persona",
    "memory",
    "skills",
    "attachments",
    "tool_catalog",
    "session_state",
}
REQUIRED_FORBIDDEN = {"__call__", "reply", "_reasoning", "_acting"}


def _rooted_at_agent(node: ast.AST) -> bool:
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return isinstance(current, ast.Name) and current.id == "agent"


def _first_agent_member(node: ast.Attribute) -> str | None:
    current: ast.AST = node
    first: str | None = None
    while isinstance(current, ast.Attribute):
        first = current.attr
        current = current.value
    if isinstance(current, ast.Name) and current.id == "agent":
        return first
    return None


def _agent_members(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    members: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _rooted_at_agent(node):
            member = _first_agent_member(node)
            if member:
                members.add(member)
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "getattr":
            continue
        if len(node.args) < 2:
            continue
        target, name = node.args[:2]
        if (
            isinstance(target, ast.Name)
            and target.id == "agent"
            and isinstance(name, ast.Constant)
            and isinstance(name.value, str)
        ):
            members.add(name.value)
    return members


def _node_id_exists(root: Path, node_id: str) -> bool:
    file_name, separator, function_name = node_id.partition("::")
    test_path = root / file_name
    if not separator or not test_path.is_file() or not function_name:
        return False
    tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
        for node in ast.walk(tree)
    )


def check_contract(root: Path, contract_path: Path) -> dict[str, Any]:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    allowed = {
        str(item.get("member") or "")
        for item in contract.get("allowed_surface") or []
        if isinstance(item, dict)
    }
    forbidden = {
        str(item) for item in contract.get("forbidden_execution_surface") or []
    }
    observed_by_path: dict[str, list[str]] = {}
    missing_consumers: list[str] = []
    for relative in contract.get("consumer_paths") or []:
        source_path = root / str(relative)
        if not source_path.is_file():
            missing_consumers.append(str(relative))
            continue
        observed_by_path[str(relative)] = sorted(_agent_members(source_path))
    observed = {
        member for members in observed_by_path.values() for member in members
    }
    unlisted = sorted(observed - allowed)
    forbidden_observed = sorted(observed & forbidden)

    snapshots = contract.get("behavior_snapshots")
    if not isinstance(snapshots, dict):
        snapshots = {}
    missing_snapshot_categories = sorted(REQUIRED_SNAPSHOTS - set(snapshots))
    missing_snapshot_nodes = sorted(
        node_id
        for category in REQUIRED_SNAPSHOTS
        for node_id in snapshots.get(category, [])
        if not _node_id_exists(root, str(node_id))
    )
    missing_forbidden = sorted(REQUIRED_FORBIDDEN - forbidden)
    policy = contract.get("policy") if isinstance(contract.get("policy"), dict) else {}
    policy_violations = sorted(
        key
        for key in ("new_production_facade", "new_runtime_adapter", "new_feature_flag")
        if policy.get(key) is not False
    )
    compatibility = contract.get("remaining_compatibility_evidence")
    missing_compatibility = []
    for key in ("model_formatter", "command_handler", "legacy_toolkit"):
        values = compatibility.get(key) if isinstance(compatibility, dict) else None
        if not isinstance(values, list) or not values:
            missing_compatibility.append(key)
            continue
        if any(not (root / str(value)).exists() for value in values):
            missing_compatibility.append(key)

    issues = {
        "missing_consumers": missing_consumers,
        "unlisted_agent_surface": unlisted,
        "forbidden_execution_surface_observed": forbidden_observed,
        "missing_forbidden_members": missing_forbidden,
        "missing_snapshot_categories": missing_snapshot_categories,
        "missing_snapshot_nodes": missing_snapshot_nodes,
        "missing_compatibility_evidence": missing_compatibility,
        "policy_violations": policy_violations,
    }
    return {
        "schema": "com.lantai.legacy-intelligence-interface-r3-check.v1",
        "contract": str(contract_path),
        "observed_by_path": observed_by_path,
        "observed_surface": sorted(observed),
        "allowed_surface": sorted(allowed),
        "forbidden_execution_surface": sorted(forbidden),
        "issues": issues,
        "passed": not any(issues.values()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = check_contract(ROOT, args.contract.expanduser())
    except Exception as exc:  # noqa: BLE001 - CLI diagnostics
        print(f"Legacy intelligence interface check failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
