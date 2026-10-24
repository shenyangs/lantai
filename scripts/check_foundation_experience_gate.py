#!/usr/bin/env python3
"""Fail closed when a protected foundation-experience contract is weakened."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACTS = ROOT / "contracts/foundation-experience/contracts.json"
DEFAULT_INCIDENTS = ROOT / "contracts/foundation-experience/incidents.json"
CONTRACT_PATH = "contracts/foundation-experience/contracts.json"
INCIDENT_PATH = "contracts/foundation-experience/incidents.json"
REQUIRED_SURFACES = {
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
SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2}
ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9-]*-\d{3}$")


class GateError(RuntimeError):
    """Raised when the preservation gate must stop a change or release."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateError(f"required policy file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GateError(f"policy root must be an object: {path}")
    return payload


def load_policy(
    contracts_path: Path = DEFAULT_CONTRACTS,
    incidents_path: Path = DEFAULT_INCIDENTS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return _read_json(contracts_path), _read_json(incidents_path)


def _items(payload: dict[str, Any], key: str, label: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise GateError(f"{label} must contain a non-empty {key} list")
    if not all(isinstance(item, dict) for item in value):
        raise GateError(f"every {label} {key} entry must be an object")
    return value


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise GateError(f"{label} must be a non-empty list")
    result = [str(item).strip() for item in value]
    if any(not item for item in result) or len(result) != len(set(result)):
        raise GateError(f"{label} must contain unique non-empty strings")
    return result


def _ids(items: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise GateError(f"{label} entry has no id")
        if item_id in indexed:
            raise GateError(f"duplicate {label} id: {item_id}")
        indexed[item_id] = item
    return indexed


def _relative_existing_path(root: Path, raw: Any, label: str) -> Path:
    value = str(raw or "").strip()
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise GateError(f"{label} must be a repository-relative safe path: {value!r}")
    resolved = root / path
    if not resolved.is_file():
        raise GateError(f"{label} path does not exist: {value}")
    return resolved


def validate_policy(
    contracts: dict[str, Any],
    incidents: dict[str, Any],
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    if contracts.get("schema_version") != 1:
        errors.append("contracts schema_version must be 1")
    if incidents.get("schema_version") != 1:
        errors.append("incidents schema_version must be 1")
    if contracts.get("preservation_principle") != "PRESERVE-001":
        errors.append("preservation_principle must be PRESERVE-001")

    try:
        required_surfaces = set(
            _strings(contracts.get("required_surfaces"), "required_surfaces")
        )
        missing_required = sorted(REQUIRED_SURFACES - required_surfaces)
        if missing_required:
            errors.append("required surfaces missing: " + ", ".join(missing_required))
        contract_items = _items(contracts, "contracts", "contracts policy")
        contract_by_id = _ids(contract_items, "contract")
    except GateError as exc:
        raise GateError(str(exc)) from exc

    covered_surfaces: set[str] = set()
    protected_count = 0
    for contract_id, contract in contract_by_id.items():
        if not ID_PATTERN.fullmatch(contract_id):
            errors.append(f"invalid contract id: {contract_id}")
        severity = str(contract.get("severity") or "")
        if severity not in SEVERITY_RANK:
            errors.append(f"{contract_id} has invalid severity: {severity}")
        if contract.get("status") != "protected":
            errors.append(f"{contract_id} must remain protected")
        else:
            protected_count += 1
        if contract.get("release_blocking") is not True:
            errors.append(f"{contract_id} must be release blocking")
        try:
            surfaces = _strings(contract.get("surfaces"), f"{contract_id}.surfaces")
            covered_surfaces.update(surfaces)
            _strings(
                contract.get("observable_invariants"),
                f"{contract_id}.observable_invariants",
            )
            _strings(
                contract.get("forbidden_regressions"),
                f"{contract_id}.forbidden_regressions",
            )
        except GateError as exc:
            errors.append(str(exc))
        verification = contract.get("verification")
        if not isinstance(verification, list) or not verification:
            errors.append(f"{contract_id}.verification must be a non-empty list")
        else:
            for index, check in enumerate(verification):
                if not isinstance(check, dict) or not str(check.get("kind") or "").strip():
                    errors.append(f"{contract_id}.verification[{index}] is invalid")
                    continue
                try:
                    _relative_existing_path(
                        root,
                        check.get("path"),
                        f"{contract_id} verification",
                    )
                except GateError as exc:
                    errors.append(str(exc))

    missing_coverage = sorted(REQUIRED_SURFACES - covered_surfaces)
    if missing_coverage:
        errors.append("protected contract coverage missing: " + ", ".join(missing_coverage))

    try:
        incident_items = _items(incidents, "incidents", "incident registry")
        incident_by_id = _ids(incident_items, "incident")
    except GateError as exc:
        raise GateError(str(exc)) from exc

    source_ids: set[str] = set()
    source_fixture = incidents.get("source_fixture")
    if source_fixture:
        try:
            source_path = _relative_existing_path(
                root, source_fixture, "incident source fixture"
            )
            source_payload = _read_json(source_path)
            source_ids = {
                str(item.get("incident_id") or "")
                for item in source_payload.get("incidents") or []
                if isinstance(item, dict)
            }
        except GateError as exc:
            errors.append(str(exc))

    for incident_id, incident in incident_by_id.items():
        if source_ids and incident_id not in source_ids:
            errors.append(f"registered incident is absent from source fixture: {incident_id}")
        try:
            contract_ids = _strings(
                incident.get("contract_ids"), f"{incident_id}.contract_ids"
            )
            unknown = sorted(set(contract_ids) - set(contract_by_id))
            if unknown:
                errors.append(
                    f"{incident_id} references unknown contracts: {', '.join(unknown)}"
                )
            _relative_existing_path(root, incident.get("fixture"), f"{incident_id} fixture")
            for target in _strings(
                incident.get("replay_targets"), f"{incident_id}.replay_targets"
            ):
                _relative_existing_path(root, target, f"{incident_id} replay target")
        except GateError as exc:
            errors.append(str(exc))

    if errors:
        raise GateError("; ".join(errors))
    return {
        "ok": True,
        "policy_id": contracts.get("policy_id"),
        "preservation_principle": contracts.get("preservation_principle"),
        "protected_contract_count": protected_count,
        "incident_count": len(incident_by_id),
        "covered_surfaces": sorted(covered_surfaces),
    }


def run_protected_verifications(
    contracts: dict[str, Any],
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Run every declared protected check, grouped by its native test runner."""

    python_paths: set[str] = set()
    frontend_paths: set[str] = set()
    for contract in _items(contracts, "contracts", "contracts policy"):
        if contract.get("status") != "protected" or contract.get("release_blocking") is not True:
            continue
        for verification in contract.get("verification") or []:
            path = str(verification.get("path") or "").strip()
            if path.startswith("console/") and Path(path).suffix in {".ts", ".tsx"}:
                frontend_paths.add(path.removeprefix("console/"))
            elif Path(path).suffix == ".py":
                python_paths.add(path)
            else:
                raise GateError(
                    "protected verification has no supported runner: "
                    f"{verification.get('kind')} {path}"
                )

    commands: list[tuple[list[str], Path]] = []
    if python_paths:
        commands.append(
            ([sys.executable, "-m", "pytest", *sorted(python_paths), "-q"], root)
        )
    if frontend_paths:
        commands.append(
            (
                ["npm", "run", "test:run", "--", *sorted(frontend_paths)],
                root / "console",
            )
        )

    completed: list[dict[str, Any]] = []
    for command, cwd in commands:
        result = subprocess.run(command, cwd=cwd, check=False)
        is_frontend = cwd.name == "console"
        completed.append(
            {
                "runner": "frontend" if is_frontend else "python",
                "checks": len(frontend_paths) if is_frontend else len(python_paths),
                "returncode": result.returncode,
            }
        )
        if result.returncode != 0:
            raise GateError(
                f"protected verification runner failed with exit code {result.returncode}: "
                + " ".join(command)
            )
    return {
        "ok": True,
        "python_checks": len(python_paths),
        "frontend_checks": len(frontend_paths),
        "runners": completed,
    }


def _canonical_set(values: Iterable[Any]) -> set[str]:
    return {
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in values
    }


def compare_with_baseline(
    candidate_contracts: dict[str, Any],
    candidate_incidents: dict[str, Any],
    baseline_contracts: dict[str, Any],
    baseline_incidents: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    candidate_by_id = _ids(
        _items(candidate_contracts, "contracts", "candidate contracts"), "contract"
    )
    baseline_by_id = _ids(
        _items(baseline_contracts, "contracts", "baseline contracts"), "contract"
    )
    for contract_id, baseline in baseline_by_id.items():
        if baseline.get("status") != "protected":
            continue
        candidate = candidate_by_id.get(contract_id)
        if candidate is None:
            errors.append(f"removed protected contract {contract_id}")
            continue
        baseline_severity = str(baseline.get("severity") or "")
        candidate_severity = str(candidate.get("severity") or "")
        if (
            baseline_severity in SEVERITY_RANK
            and candidate_severity in SEVERITY_RANK
            and SEVERITY_RANK[candidate_severity] > SEVERITY_RANK[baseline_severity]
        ):
            errors.append(
                f"{contract_id} lowered severity from {baseline_severity} to {candidate_severity}"
            )
        if baseline.get("release_blocking") is True and candidate.get(
            "release_blocking"
        ) is not True:
            errors.append(f"{contract_id} disabled release blocking")
        if candidate.get("status") != "protected":
            errors.append(f"{contract_id} is no longer protected")
        for key, description in (
            ("surfaces", "surface"),
            ("observable_invariants", "observable invariant"),
            ("forbidden_regressions", "forbidden regression"),
            ("verification", "verification"),
        ):
            baseline_values = _canonical_set(baseline.get(key) or [])
            candidate_values = _canonical_set(candidate.get(key) or [])
            for removed in sorted(baseline_values - candidate_values):
                errors.append(f"{contract_id} removed {description}: {removed}")

    candidate_incident_by_id = _ids(
        _items(candidate_incidents, "incidents", "candidate incidents"), "incident"
    )
    baseline_incident_by_id = _ids(
        _items(baseline_incidents, "incidents", "baseline incidents"), "incident"
    )
    for incident_id, baseline in baseline_incident_by_id.items():
        candidate = candidate_incident_by_id.get(incident_id)
        if candidate is None:
            errors.append(f"removed registered incident {incident_id}")
            continue
        if candidate.get("fixture") != baseline.get("fixture"):
            errors.append(f"{incident_id} changed its preserved fixture")
        for key, description in (
            ("contract_ids", "contract mapping"),
            ("replay_targets", "replay target"),
        ):
            baseline_values = _canonical_set(baseline.get(key) or [])
            candidate_values = _canonical_set(candidate.get(key) or [])
            for removed in sorted(baseline_values - candidate_values):
                errors.append(f"{incident_id} removed {description}: {removed}")

    if errors:
        raise GateError("; ".join(errors))
    return {"ok": True, "changes": "additive_or_equal"}


def _git_json(root: Path, ref: str, relative_path: str) -> dict[str, Any] | None:
    if not ref or set(ref) == {"0"}:
        return None
    result = subprocess.run(
        ["git", "show", f"{ref}:{relative_path}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(f"baseline {ref}:{relative_path} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise GateError(f"baseline {ref}:{relative_path} must be an object")
    return payload


def run_gate(
    *,
    root: Path,
    contracts_path: Path,
    incidents_path: Path,
    base_ref: str | None,
) -> dict[str, Any]:
    contracts, incidents = load_policy(contracts_path, incidents_path)
    report = validate_policy(contracts, incidents, root=root)
    effective_ref = base_ref
    if effective_ref is None:
        effective_ref = "HEAD"
    baseline_contracts = _git_json(root, effective_ref, CONTRACT_PATH)
    baseline_incidents = _git_json(root, effective_ref, INCIDENT_PATH)
    if baseline_contracts is None and baseline_incidents is None:
        report["baseline"] = "bootstrap"
        return report
    if baseline_contracts is None or baseline_incidents is None:
        raise GateError("baseline contains only part of the preservation policy")
    compare_with_baseline(
        contracts,
        incidents,
        baseline_contracts,
        baseline_incidents,
    )
    report["baseline"] = effective_ref
    report["comparison"] = "additive_or_equal"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the foundation-experience preservation ratchet."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--contracts", type=Path, default=DEFAULT_CONTRACTS)
    parser.add_argument("--incidents", type=Path, default=DEFAULT_INCIDENTS)
    parser.add_argument("--base-ref")
    parser.add_argument(
        "--run-verifications",
        action="store_true",
        help="Execute every protected Python and frontend behavior check.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    contracts_path = args.contracts.expanduser().resolve()
    incidents_path = args.incidents.expanduser().resolve()
    try:
        report = run_gate(
            root=root,
            contracts_path=contracts_path,
            incidents_path=incidents_path,
            base_ref=args.base_ref,
        )
        if args.run_verifications:
            contracts, _ = load_policy(contracts_path, incidents_path)
            report["verifications"] = run_protected_verifications(
                contracts,
                root=root,
            )
    except GateError as exc:
        payload = {"ok": False, "error": str(exc)}
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"Foundation experience gate failed: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "Foundation experience gate passed: "
            f"{report['protected_contract_count']} protected contracts, "
            f"{report['incident_count']} registered incidents, "
            f"baseline={report['baseline']}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
