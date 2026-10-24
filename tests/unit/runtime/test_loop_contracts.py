# -*- coding: utf-8 -*-
"""Behavior locks for structured long-running Loop contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lantai.runtime import (
    RUNTIME_LOOP_CONTRACT_SCHEMA_VERSION,
    LoopBudget,
    LoopContract,
    LoopDeliverable,
    LoopDeterministicGate,
    LoopEditionPolicy,
    LoopInput,
    LoopRubric,
    LoopRubricDimension,
    LoopStopConditions,
    compute_loop_contract_hash,
    loop_contract_with_hash,
    render_loop_contract_markdown,
)


def _contract() -> LoopContract:
    return LoopContract(
        contract_id="loop-contract-1",
        objective="Audit runtime migration regressions",
        scope=["Inspect runtime contracts", "Run deterministic gates"],
        out_of_scope=["Publish release artifacts"],
        inputs=[
            LoopInput(
                input_id="plan",
                kind="file",
                title="Runtime migration plan",
                uri="LANTAI_RUNTIME_MIGRATION_FULL_REMEDIATION_PLAN.md",
                evidence_hash="sha256:plan",
            )
        ],
        deliverables=[
            LoopDeliverable(
                deliverable_id="report",
                title="Remediation report",
                acceptance=["Lists verified gates", "Links evidence"],
            )
        ],
        acceptance=["All required gates pass", "No hidden side effects"],
        deterministic_gates=[
            LoopDeterministicGate(
                gate_id="runtime-contracts",
                name="Runtime contracts",
                kind="test",
                command="uv run pytest tests/unit/runtime -q",
                timeout_seconds=600,
            )
        ],
        rubric=LoopRubric(
            rubric_id="runtime-remediation",
            version="1",
            passing_score=4.2,
            dimensions=[
                LoopRubricDimension(
                    dimension_id="correctness",
                    name="Correctness",
                    weight=0.5,
                    target_score=4.5,
                ),
                LoopRubricDimension(
                    dimension_id="evidence",
                    name="Evidence",
                    weight=0.5,
                    target_score=4.0,
                ),
            ],
        ),
        edition_policy=LoopEditionPolicy(
            release_scope="both",
            tool_policy="enabled",
            network_policy="read_only",
            file_policy="read_only",
            browser_policy="interactive_only",
            shell_policy="disabled",
        ),
        budgets=LoopBudget(max_attempts=3, max_steps=40, max_wall_seconds=7200),
        stop_conditions=LoopStopConditions(
            rubric_score_at_least=4.2,
            max_consecutive_failures=2,
            max_no_progress_iterations=3,
        ),
        approved_by="sam",
    )


def test_loop_contract_round_trips_required_product_fields() -> None:
    contract = _contract()

    restored = LoopContract.model_validate(contract.model_dump(mode="json"))

    assert restored.schema_version == RUNTIME_LOOP_CONTRACT_SCHEMA_VERSION
    assert restored.objective == "Audit runtime migration regressions"
    assert restored.scope == ["Inspect runtime contracts", "Run deterministic gates"]
    assert restored.out_of_scope == ["Publish release artifacts"]
    assert restored.deliverables[0].acceptance == [
        "Lists verified gates",
        "Links evidence",
    ]
    assert restored.deterministic_gates[0].command == (
        "uv run pytest tests/unit/runtime -q"
    )
    assert restored.rubric.dimensions[0].dimension_id == "correctness"
    assert restored.edition_policy.release_scope == "both"
    assert restored.budgets.max_steps == 40
    assert restored.stop_conditions.max_no_progress_iterations == 3


def test_loop_contract_rejects_unstructured_or_incomplete_contracts() -> None:
    payload = _contract().model_dump(mode="json")
    payload["objective"] = " "
    with pytest.raises(ValidationError):
        LoopContract.model_validate(payload)

    payload = _contract().model_dump(mode="json")
    payload["deterministic_gates"][0]["command"] = ""
    with pytest.raises(ValidationError):
        LoopContract.model_validate(payload)

    payload = _contract().model_dump(mode="json")
    payload["deliverables"][0]["acceptance"] = []
    with pytest.raises(ValidationError):
        LoopContract.model_validate(payload)

    payload = _contract().model_dump(mode="json")
    payload["hidden_prompt"] = "not allowed"
    with pytest.raises(ValidationError):
        LoopContract.model_validate(payload)


def test_loop_contract_requires_unique_control_ids() -> None:
    payload = _contract().model_dump(mode="json")
    payload["deterministic_gates"].append(dict(payload["deterministic_gates"][0]))

    with pytest.raises(ValidationError):
        LoopContract.model_validate(payload)


def test_loop_contract_hash_ignores_existing_hash_and_markdown_is_projection() -> None:
    contract = _contract()
    hashed = loop_contract_with_hash(contract)
    stale = hashed.model_copy(update={"contract_hash": "sha256:stale"})

    assert hashed.contract_hash.startswith("sha256:")
    assert compute_loop_contract_hash(stale) == hashed.contract_hash

    markdown = render_loop_contract_markdown(stale)
    assert "# Audit runtime migration regressions" in markdown
    assert f"Contract Hash: `{hashed.contract_hash}`" in markdown
    assert "## Deterministic Gates" in markdown
    assert "uv run pytest tests/unit/runtime -q" in markdown
