# -*- coding: utf-8 -*-
"""Behavior locks for the R3 Legacy intelligence interface contract."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_legacy_intelligence_interface.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_legacy_intelligence_interface",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_r3_interface_contract_matches_product_callsites() -> None:
    module = _load_module()

    report = module.check_contract(ROOT, module.DEFAULT_CONTRACT)

    assert report["passed"] is True
    assert report["issues"] == {
        "missing_consumers": [],
        "unlisted_agent_surface": [],
        "forbidden_execution_surface_observed": [],
        "missing_forbidden_members": [],
        "missing_snapshot_categories": [],
        "missing_snapshot_nodes": [],
        "missing_compatibility_evidence": [],
        "policy_violations": [],
    }


def test_r3_interface_contract_rejects_forbidden_product_execution(
    tmp_path: Path,
) -> None:
    module = _load_module()
    product = tmp_path / "product.py"
    product.write_text("await agent.reply([])\n", encoding="utf-8")
    contract = json.loads(module.DEFAULT_CONTRACT.read_text(encoding="utf-8"))
    contract["consumer_paths"] = [product.name]
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    report = module.check_contract(tmp_path, contract_path)

    assert report["passed"] is False
    assert report["issues"]["forbidden_execution_surface_observed"] == [
        "reply"
    ]


def test_r3_interface_contract_rejects_unreviewed_agent_member(
    tmp_path: Path,
) -> None:
    module = _load_module()
    product = tmp_path / "product.py"
    product.write_text("agent.new_magic()\n", encoding="utf-8")
    contract = json.loads(module.DEFAULT_CONTRACT.read_text(encoding="utf-8"))
    contract["consumer_paths"] = [product.name]
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    report = module.check_contract(tmp_path, contract_path)

    assert report["passed"] is False
    assert report["issues"]["unlisted_agent_surface"] == ["new_magic"]
