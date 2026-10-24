from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.check_foundation_experience_gate import (
    GateError,
    compare_with_baseline,
    load_policy,
    run_protected_verifications,
    validate_policy,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts/foundation-experience/contracts.json"
INCIDENTS = ROOT / "contracts/foundation-experience/incidents.json"


def _current_policy() -> tuple[dict[str, object], dict[str, object]]:
    return load_policy(CONTRACTS, INCIDENTS)


def test_repository_foundation_experience_policy_is_release_blocking() -> None:
    contracts, incidents = _current_policy()

    report = validate_policy(contracts, incidents, root=ROOT)

    assert report["ok"] is True
    assert report["preservation_principle"] == "PRESERVE-001"
    assert report["protected_contract_count"] >= 10
    assert report["incident_count"] >= 5
    assert set(report["covered_surfaces"]) >= {
        "chat",
        "reasoning",
        "tools",
        "run-control",
        "files",
        "artifacts",
        "sessions",
        "workspace",
        "collaboration",
        "release",
    }


def test_snow_leopard_ii_incidents_are_permanently_registered() -> None:
    _, incidents = _current_policy()
    registered = {
        str(item.get("id") or "") for item in incidents["incidents"]
    }
    source = json.loads(
        (ROOT / incidents["source_fixture"]).read_text(encoding="utf-8")
    )
    replayable = {
        str(item.get("incident_id") or "")
        for item in source["incidents"]
    }
    expected = {
        "provider-repeated-snapshot-projection-v1",
        "route-hydration-phantom-run-v1",
        "generated-artifact-missing-card-v1",
        "malformed-tool-argument-protocol-leak-v1",
        "ququ-private-marker-leak-v1",
    }

    assert expected <= registered
    assert expected <= replayable


def test_protected_contract_cannot_be_removed() -> None:
    baseline_contracts, baseline_incidents = _current_policy()
    candidate_contracts = copy.deepcopy(baseline_contracts)
    candidate_contracts["contracts"] = candidate_contracts["contracts"][1:]

    with pytest.raises(GateError, match="removed protected contract"):
        compare_with_baseline(
            candidate_contracts,
            baseline_incidents,
            baseline_contracts,
            baseline_incidents,
        )


def test_protected_contract_cannot_be_weakened() -> None:
    baseline_contracts, baseline_incidents = _current_policy()
    candidate_contracts = copy.deepcopy(baseline_contracts)
    protected = candidate_contracts["contracts"][0]
    protected["severity"] = "P2"
    protected["release_blocking"] = False
    protected["forbidden_regressions"] = protected["forbidden_regressions"][1:]
    protected["verification"] = protected["verification"][1:]

    with pytest.raises(GateError) as captured:
        compare_with_baseline(
            candidate_contracts,
            baseline_incidents,
            baseline_contracts,
            baseline_incidents,
        )

    message = str(captured.value)
    assert "lowered severity" in message
    assert "disabled release blocking" in message
    assert "removed forbidden regression" in message
    assert "removed verification" in message


def test_registered_incident_cannot_be_removed_or_detached() -> None:
    baseline_contracts, baseline_incidents = _current_policy()
    candidate_incidents = copy.deepcopy(baseline_incidents)
    candidate_incidents["incidents"] = candidate_incidents["incidents"][1:]

    with pytest.raises(GateError, match="removed registered incident"):
        compare_with_baseline(
            baseline_contracts,
            candidate_incidents,
            baseline_contracts,
            baseline_incidents,
        )


def test_strengthening_policy_is_allowed(tmp_path: Path) -> None:
    baseline_contracts, baseline_incidents = _current_policy()
    candidate_contracts = copy.deepcopy(baseline_contracts)
    extra_test = tmp_path / "extra_test.py"
    extra_test.write_text("# additional protection\n", encoding="utf-8")
    candidate_contracts["contracts"][0]["forbidden_regressions"].append(
        "newly_discovered_regression"
    )
    candidate_contracts["contracts"][0]["verification"].append(
        {"kind": "unit", "path": str(extra_test)}
    )

    report = compare_with_baseline(
        candidate_contracts,
        baseline_incidents,
        baseline_contracts,
        baseline_incidents,
    )

    assert report["ok"] is True
    assert report["changes"] == "additive_or_equal"


def test_invalid_policy_fails_closed(tmp_path: Path) -> None:
    contracts, incidents = _current_policy()
    broken = copy.deepcopy(contracts)
    broken["contracts"][0]["verification"] = [
        {"kind": "unit", "path": "tests/does-not-exist.py"}
    ]

    with pytest.raises(GateError, match="verification path does not exist"):
        validate_policy(broken, incidents, root=ROOT)


def test_policy_json_is_deterministic_and_has_no_duplicate_ids() -> None:
    contracts = json.loads(CONTRACTS.read_text(encoding="utf-8"))
    incidents = json.loads(INCIDENTS.read_text(encoding="utf-8"))

    contract_ids = [item["id"] for item in contracts["contracts"]]
    incident_ids = [item["id"] for item in incidents["incidents"]]
    assert len(contract_ids) == len(set(contract_ids))
    assert len(incident_ids) == len(set(incident_ids))
    assert CONTRACTS.read_text(encoding="utf-8").endswith("\n")
    assert INCIDENTS.read_text(encoding="utf-8").endswith("\n")


def test_foundation_gate_is_wired_into_local_preflight_and_release() -> None:
    preflight = (ROOT / "scripts/vibe_coding_preflight.py").read_text(encoding="utf-8")
    release_script = (ROOT / "build_and_deploy.sh").read_text(encoding="utf-8")

    assert "check_foundation_experience_gate.py" in preflight
    assert '"--run-verifications"' in preflight
    assert 'category="foundation_experience"' in preflight
    assert "validate_foundation_experience_gate" in release_script
    assert 'scripts/check_foundation_experience_gate.py' in release_script
    assert 'scripts/check_foundation_experience_gate.py" --run-verifications' in release_script
    release_entry = release_script.split("resolve_release_metadata\n", maxsplit=1)[1]
    assert release_entry.index("validate_foundation_experience_gate\n") < release_entry.index(
        "configure_distribution_signing\n"
    )


def test_foundation_gate_ci_compares_the_pull_request_baseline_fail_closed() -> None:
    workflow = (
        ROOT / ".github/workflows/foundation-experience-gate.yml"
    ).read_text(encoding="utf-8")

    assert "Foundation Experience Preservation Gate" in workflow
    assert "fetch-depth: 0" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.before" in workflow
    assert "--base-ref" in workflow
    assert "--run-verifications" in workflow
    assert "npm ci" in workflow
    assert 'pip install -e ".[dev]"' in workflow
    assert "paths:" not in workflow
    assert "continue-on-error" not in workflow


def test_preservation_mechanism_requires_owner_review() -> None:
    codeowners = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")

    assert "/contracts/foundation-experience/ @shenyangs" in codeowners
    assert "/scripts/check_foundation_experience_gate.py @shenyangs" in codeowners
    assert "/.github/workflows/foundation-experience-gate.yml @shenyangs" in codeowners


def test_protected_verifications_use_native_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    contracts, _ = _current_policy()
    calls: list[tuple[list[str], Path]] = []

    class Result:
        returncode = 0

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> Result:
        assert check is False
        calls.append((command, cwd))
        return Result()

    monkeypatch.setattr(
        "scripts.check_foundation_experience_gate.subprocess.run",
        fake_run,
    )

    report = run_protected_verifications(contracts, root=ROOT)

    assert report["python_checks"] >= 10
    assert report["frontend_checks"] >= 2
    assert len(calls) == 2
    assert calls[0][0][1:3] == ["-m", "pytest"]
    assert calls[0][1] == ROOT
    assert calls[1][0][:4] == ["npm", "run", "test:run", "--"]
    assert calls[1][1] == ROOT / "console"
