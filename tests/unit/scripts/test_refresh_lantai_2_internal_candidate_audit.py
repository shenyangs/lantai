from __future__ import annotations

from scripts.refresh_lantai_2_internal_candidate_audit import refresh_audit


def _base():
    return {
        "schema": "com.lantai.internal-candidate-audit.v1",
        "candidate": {
            "version": "2.0.0",
            "public_release_ready": False,
            "artifact_sha256": {
                "main_executable": "main",
                "app_asar": "asar",
                "backend_executable": "backend",
            },
        },
        "verified": {},
    }


def _memory():
    lifecycle = {
        "confirmed_fact_absent": True,
        "structured_projection_absent": True,
    }
    return {
        "schema": "com.lantai.packaged-memory-ownership-smoke.v1",
        "passed": True,
        "app": {
            "short_version": "2.0.0",
            "artifact_sha256": {
                "main_executable": "sha256:main",
                "app_asar": "sha256:asar",
                "backend_executable": "sha256:backend",
            },
        },
        "provider": {"request_count": 2},
        "isolation": {
            "temporary_user_data": True,
            "updater_disabled": True,
            "user_workspace_read": False,
        },
        "checks": {
            "model_context": {
                "confirmed_present_before_forget": True,
                "pending_absent_before_forget": True,
                "confirmed_absent_after_forget": True,
                "pending_absent_after_forget": True,
            },
            "after_backend_restart": lifecycle,
            "after_app_relaunch": lifecycle,
        },
    }


def _source_memory():
    return {
        "gate": "lantai_2_memory_ownership_source_regression",
        "passed": True,
        "tests_passed": 94,
        "tests_failed": 0,
    }


def test_refresh_adds_memory_and_dirty_worktree_truth() -> None:
    refreshed = refresh_audit(
        _base(),
        source_memory_evidence=_source_memory(),
        memory_evidence=_memory(),
        memory_evidence_sha256="evidence-hash",
        dirty_ledger={
            "public_release_allowed_by_this_gate": False,
            "summary": {
                "entry_count": 2825,
                "provenance_counts": {
                    "known_current_goal_closure": 23,
                    "explicit_unresolved_conflict": 2802,
                },
            },
        },
    )

    memory = refreshed["verified"]["memory_ownership"]
    assert memory["packaged_candidate_passed"] is True
    assert memory["pending_absent_from_model_context"] is True
    assert memory["updater_disabled"] is True
    assert refreshed["verified"]["dirty_worktree_ownership"] == {
        "entry_count": 2825,
        "provenance_counts": {
            "known_current_goal_closure": 23,
            "explicit_unresolved_conflict": 2802,
        },
        "public_release_allowed_by_this_gate": False,
    }
    assert "browser_public_read_success_rate_benchmark" in (
        refreshed["deferred_by_user_this_cycle"]
    )
    assert "browser_public_read_success_rate_benchmark" in (
        refreshed["remaining_public_release_gates"]
    )


def test_refresh_does_not_duplicate_browser_public_deferral() -> None:
    base = _base()
    gate = "browser_public_read_success_rate_benchmark"
    base["deferred_by_user_this_cycle"] = [gate]
    base["remaining_public_release_gates"] = [gate]

    refreshed = refresh_audit(
        base,
        source_memory_evidence=_source_memory(),
        memory_evidence=_memory(),
        memory_evidence_sha256="evidence-hash",
        dirty_ledger={"summary": {}},
    )

    assert refreshed["deferred_by_user_this_cycle"].count(gate) == 1
    assert refreshed["remaining_public_release_gates"].count(gate) == 1


def test_refresh_rejects_artifact_hash_mismatch() -> None:
    memory = _memory()
    memory["app"]["artifact_sha256"]["app_asar"] = "sha256:different"
    try:
        refresh_audit(
            _base(),
            source_memory_evidence=_source_memory(),
            memory_evidence=memory,
            memory_evidence_sha256="evidence-hash",
            dirty_ledger={"summary": {}},
        )
    except ValueError as exc:
        assert "artifact hash mismatch" in str(exc)
    else:
        raise AssertionError("hash mismatch must fail closed")
