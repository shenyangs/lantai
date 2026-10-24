from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = ROOT / "scripts/check_runtime_v2_phase0.py"
SPEC = importlib.util.spec_from_file_location("check_runtime_v2_phase0", CHECKER_PATH)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


def _read(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


@pytest.fixture()
def phase_zero_payloads() -> tuple[dict, dict, dict, set[str]]:
    foundation = _read("contracts/foundation-experience/incidents.json")
    return (
        _read("contracts/runtime-v2/invariants.v1.json"),
        _read("contracts/runtime-v2/authority-map.v1.json"),
        _read("tests/fixtures/runtime/incidents/runtime_v2_user_reported_v1.json"),
        {item["id"] for item in foundation["incidents"]},
    )


def _validate(payloads: tuple[dict, dict, dict, set[str]]) -> dict:
    invariants, authority_map, fixture, foundation_ids = payloads
    return CHECKER.validate_contracts(
        invariants,
        authority_map,
        fixture,
        root=ROOT,
        foundation_incident_ids=foundation_ids,
    )


def test_phase_zero_source_contract_is_valid_but_phase_gate_remains_pending(
    phase_zero_payloads: tuple[dict, dict, dict, set[str]],
) -> None:
    report = _validate(phase_zero_payloads)

    assert report == {
        "source_contract_ok": True,
        "phase_ok": False,
        "phase": "RT2-00",
        "package_gate": "pending_candidate_build",
        "authority_count": 6,
        "incident_count": 8,
        "invariant_count": 16,
        "package_probe_count": 8,
    }


def test_full_cli_gate_exits_nonzero_while_source_only_gate_passes() -> None:
    assert CHECKER.main(["--json", "--source-only"]) == 0
    assert CHECKER.main(["--json"]) == 2


def test_rejects_swapped_domain_authorities_even_when_authority_set_is_unchanged(
    phase_zero_payloads: tuple[dict, dict, dict, set[str]],
) -> None:
    payloads = copy.deepcopy(phase_zero_payloads)
    domains = payloads[1]["domains"]
    domains[0]["target_authority"], domains[1]["target_authority"] = (
        domains[1]["target_authority"],
        domains[0]["target_authority"],
    )

    with pytest.raises(CHECKER.ContractError, match="domain to authority mapping"):
        _validate(payloads)


def test_rejects_empty_invariant_statement(
    phase_zero_payloads: tuple[dict, dict, dict, set[str]],
) -> None:
    payloads = copy.deepcopy(phase_zero_payloads)
    payloads[0]["invariants"][4]["statement"] = ""

    with pytest.raises(CHECKER.ContractError, match="statement must be non-empty"):
        _validate(payloads)


def test_rejects_mismatched_incident_to_probe_pairing_with_same_reference_set(
    phase_zero_payloads: tuple[dict, dict, dict, set[str]],
) -> None:
    payloads = copy.deepcopy(phase_zero_payloads)
    incidents = payloads[2]["incidents"]
    incidents[0]["package_probe_id"], incidents[1]["package_probe_id"] = (
        incidents[1]["package_probe_id"],
        incidents[0]["package_probe_id"],
    )

    with pytest.raises(CHECKER.ContractError, match="frozen one-to-one pairing"):
        _validate(payloads)


def test_rejects_empty_probe_semantic_text(
    phase_zero_payloads: tuple[dict, dict, dict, set[str]],
) -> None:
    payloads = copy.deepcopy(phase_zero_payloads)
    payloads[2]["package_probes"][5]["procedure"] = " "

    with pytest.raises(CHECKER.ContractError, match="procedure must be non-empty"):
        _validate(payloads)


def test_rejects_arbitrary_existing_file_as_incident_source_evidence(
    phase_zero_payloads: tuple[dict, dict, dict, set[str]],
) -> None:
    payloads = copy.deepcopy(phase_zero_payloads)
    payloads[2]["incidents"][0]["source_evidence"] = ["README.md"]

    with pytest.raises(CHECKER.ContractError, match="frozen semantic evidence"):
        _validate(payloads)


def test_collaboration_identity_reuses_existing_member_and_envelope_terms(
    phase_zero_payloads: tuple[dict, dict, dict, set[str]],
) -> None:
    invariants, authority_map, _, _ = phase_zero_payloads

    assert invariants["identity_envelope"]["collaboration_identity_fields"] == [
        "collaboration_id",
        "member_id",
    ]
    assert authority_map["collaboration_identity"] == {
        "specific_fields": ["collaboration_id", "member_id"],
        "execution_fields_from_envelope": ["run_id", "attempt_id"],
    }


def test_rejects_invented_collaboration_run_identity(
    phase_zero_payloads: tuple[dict, dict, dict, set[str]],
) -> None:
    payloads = copy.deepcopy(phase_zero_payloads)
    payloads[0]["identity_envelope"]["collaboration_identity_fields"].append(
        "member_run_id"
    )

    with pytest.raises(CHECKER.ContractError, match="collaboration identity fields"):
        _validate(payloads)


def test_ququ_has_fast_foreground_terminal_deadline_and_separate_observation_window(
    phase_zero_payloads: tuple[dict, dict, dict, set[str]],
) -> None:
    invariants, _, fixture, _ = phase_zero_payloads
    ququ = next(
        incident
        for incident in fixture["incidents"]
        if incident["incident_class"] == "ququ_post_answer_blocks_terminal"
    )
    probe = next(
        probe for probe in fixture["package_probes"] if probe["probe_id"] == ququ["package_probe_id"]
    )

    assert "optional_post_answer_blocks_terminal" in invariants["forbidden_patterns"]
    assert ququ["source_status"] == "source_repro_package_pending"
    assert probe["foreground_terminal_deadline_ms"] == 1000
    assert probe["sidecar_observation_window_seconds"] == 30
    assert "observation_deadline_seconds" not in probe
    assert probe["result"] == "pending_candidate_build"


def test_ququ_package_evidence_cannot_hide_slow_foreground_terminal_in_30s_window() -> None:
    evidence = {
        "schema_version": 1,
        "phase": "RT2-00",
        "result": "pass",
        "source_contract_digests": {},
        "build_provenance": {
            "candidate_commit": "a" * 40,
            "artifact_sha256": "b" * 64,
            "app_version": "test",
            "build_id": "test",
            "built_at": "2026-09-14T00:00:00Z",
            "environment": "test",
        },
        "probes": [
            {
                "probe_id": probe_id,
                "result": "pass",
                "started_at": "2026-09-14T00:00:00Z",
                "ended_at": "2026-09-14T00:00:01Z",
                "actual_behavior": "observed",
                "evidence": "test log",
                **(
                    {
                        "foreground_terminal_latency_ms": 29000,
                        "sidecar_observation_seconds": 30,
                        "scenarios": ["slow", "timeout", "failure"],
                    }
                    if probe_id == "PKG-RT2-00-08"
                    else {}
                ),
            }
            for probe_id in CHECKER.EXPECTED_PROBE_TEXT
        ],
    }

    with pytest.raises(CHECKER.ContractError, match="foreground latency"):
        CHECKER._validate_package_evidence(
            evidence,
            expected_probe_ids=list(CHECKER.EXPECTED_PROBE_TEXT),
            expected_digests={},
        )
