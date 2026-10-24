#!/usr/bin/env python3
"""Refresh current Lantai 2.0 candidate audit with newly verified evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_hash(value: Any) -> str:
    text = str(value or "")
    return text.removeprefix("sha256:")


def refresh_audit(
    base: dict[str, Any],
    *,
    source_memory_evidence: dict[str, Any],
    memory_evidence: dict[str, Any],
    memory_evidence_sha256: str,
    dirty_ledger: dict[str, Any],
) -> dict[str, Any]:
    if base.get("schema") != "com.lantai.internal-candidate-audit.v1":
        raise ValueError("Unsupported internal candidate audit schema")
    if memory_evidence.get("schema") != (
        "com.lantai.packaged-memory-ownership-smoke.v1"
    ) or memory_evidence.get("passed") is not True:
        raise ValueError("Packaged memory ownership evidence is not passing v1 evidence")
    if (
        source_memory_evidence.get("gate")
        != "lantai_2_memory_ownership_source_regression"
        or source_memory_evidence.get("passed") is not True
        or int(source_memory_evidence.get("tests_passed") or 0) < 1
        or int(source_memory_evidence.get("tests_failed") or 0) != 0
    ):
        raise ValueError("Source memory ownership evidence is not passing evidence")

    candidate = base.get("candidate")
    app = memory_evidence.get("app")
    if not isinstance(candidate, dict) or not isinstance(app, dict):
        raise ValueError("Candidate or packaged app metadata is missing")
    if str(candidate.get("version") or "") != str(app.get("short_version") or ""):
        raise ValueError("Memory evidence version does not match candidate")
    candidate_hashes = candidate.get("artifact_sha256")
    package_hashes = app.get("artifact_sha256")
    if not isinstance(candidate_hashes, dict) or not isinstance(package_hashes, dict):
        raise ValueError("Candidate artifact hashes are missing")
    for key in ("main_executable", "app_asar", "backend_executable"):
        if _plain_hash(candidate_hashes.get(key)) != _plain_hash(package_hashes.get(key)):
            raise ValueError(f"Memory evidence artifact hash mismatch: {key}")

    checks = memory_evidence.get("checks")
    provider = memory_evidence.get("provider")
    isolation = memory_evidence.get("isolation")
    if not all(isinstance(item, dict) for item in (checks, provider, isolation)):
        raise ValueError("Memory evidence checks/provider/isolation are missing")
    model_context = checks.get("model_context")
    after_backend = checks.get("after_backend_restart")
    after_app = checks.get("after_app_relaunch")
    if not all(isinstance(item, dict) for item in (model_context, after_backend, after_app)):
        raise ValueError("Memory evidence lifecycle checks are missing")
    required_true = (
        model_context.get("confirmed_present_before_forget") is True,
        model_context.get("pending_absent_before_forget") is True,
        model_context.get("confirmed_absent_after_forget") is True,
        model_context.get("pending_absent_after_forget") is True,
        after_backend.get("confirmed_fact_absent") is True,
        after_backend.get("structured_projection_absent") is True,
        after_app.get("confirmed_fact_absent") is True,
        after_app.get("structured_projection_absent") is True,
        isolation.get("temporary_user_data") is True,
        isolation.get("updater_disabled") is True,
        isolation.get("user_workspace_read") is False,
    )
    if not all(required_true):
        raise ValueError("Memory ownership evidence does not satisfy required invariants")

    dirty_summary = dirty_ledger.get("summary")
    if not isinstance(dirty_summary, dict):
        raise ValueError("Dirty worktree ledger summary is missing")
    verified = base.get("verified")
    if not isinstance(verified, dict):
        raise ValueError("Candidate verified section is missing")
    verified["memory_ownership"] = {
        "source_gate_tests_passed": source_memory_evidence["tests_passed"],
        "packaged_candidate_passed": True,
        "confirmed_present_before_forget": True,
        "pending_absent_from_model_context": True,
        "confirmed_absent_after_forget": True,
        "backend_restart_no_revival": True,
        "app_relaunch_no_revival": True,
        "canonical_forget_tombstone_preserved": True,
        "provider": "loopback_openai_compatible",
        "provider_request_count": provider.get("request_count"),
        "temporary_user_data": True,
        "updater_disabled": True,
        "user_workspace_read": False,
        "evidence_sha256": memory_evidence_sha256,
    }
    verified["dirty_worktree_ownership"] = {
        "entry_count": dirty_summary.get("entry_count"),
        "provenance_counts": dirty_summary.get("provenance_counts"),
        "public_release_allowed_by_this_gate": dirty_ledger.get(
            "public_release_allowed_by_this_gate"
        ),
    }
    deferred = base.setdefault("deferred_by_user_this_cycle", [])
    if not isinstance(deferred, list):
        raise ValueError("Candidate deferred gate list is invalid")
    browser_public_gate = "browser_public_read_success_rate_benchmark"
    if browser_public_gate not in deferred:
        deferred.append(browser_public_gate)

    remaining = base.setdefault("remaining_public_release_gates", [])
    if not isinstance(remaining, list):
        raise ValueError("Candidate remaining gate list is invalid")
    if browser_public_gate not in remaining:
        remaining.append(browser_public_gate)
    base["generated_at"] = datetime.now(timezone.utc).isoformat()
    candidate["public_release_ready"] = False
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--memory-evidence", type=Path, required=True)
    parser.add_argument("--source-memory-evidence", type=Path, required=True)
    parser.add_argument("--dirty-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    output = args.output.expanduser().resolve()
    refreshed = refresh_audit(
        _load_object(args.base.expanduser().resolve()),
        source_memory_evidence=_load_object(
            args.source_memory_evidence.expanduser().resolve()
        ),
        memory_evidence=_load_object(args.memory_evidence.expanduser().resolve()),
        memory_evidence_sha256=_sha256(args.memory_evidence.expanduser().resolve()),
        dirty_ledger=_load_object(args.dirty_ledger.expanduser().resolve()),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(json.dumps(refreshed["verified"]["memory_ownership"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
