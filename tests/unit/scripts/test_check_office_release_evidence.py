from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "check_office_release_evidence.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_office_release_evidence", SCRIPT_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_roundtrip_waiver_must_be_known_and_explained() -> None:
    module = _load_module()
    waiver = {"id": "unknown", "reason": "because"}
    results = [{"suffix": ".xlsx", "waivers": [waiver]}]

    with pytest.raises(AssertionError, match="unknown or unexplained waiver"):
        module._validate_roundtrip_waivers(
            {"status": "passed_with_waiver", "waivers": [waiver]}, results
        )


def test_roundtrip_waiver_must_match_xlsx_disk_readback() -> None:
    module = _load_module()
    waiver = {
        "id": "genoffice.xlsx.native_number_input",
        "reason": "User deferred the GenOffice editor update",
        "observed_value": "123.5",
        "observed_type": "s",
    }
    evidence = {"status": "passed_with_waiver", "waivers": [waiver]}
    results = [
        {
            "suffix": ".xlsx",
            "waivers": [waiver],
            "after_state": {"b2": "123.5", "b2_type": "s"},
        }
    ]

    assert module._validate_roundtrip_waivers(evidence, results) == [waiver]

    results[0]["after_state"]["b2"] = "unexpected"
    with pytest.raises(AssertionError, match="contradicts disk readback"):
        module._validate_roundtrip_waivers(evidence, results)
