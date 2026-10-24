from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "scripts" / "check_lantai_2_memory_ownership_regression.sh"


def test_memory_ownership_gate_covers_every_release_invariant_surface() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    for required in (
        "test_memory_ledger_golden.py",
        "test_canonical_ledger.py",
        "test_memory_policy.py",
        "test_layered_memory.py",
        "test_structured_user_memory.py",
        "test_user_understanding_integration.py",
        "test_memory_pack.py",
        "test_memory_migration.py",
        "test_memory_maintenance.py",
        "test_memory_usage_trace.py",
        "test_workspace_memory_facts.py",
        "test_workspace_memory_pack.py",
    ):
        assert required in source
