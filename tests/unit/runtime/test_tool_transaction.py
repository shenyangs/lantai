# -*- coding: utf-8 -*-
"""Behavior and architecture locks for the S5 ToolTransaction boundary."""

# pylint: disable=protected-access

from __future__ import annotations

from pathlib import Path

import pytest

from lantai.runtime.events import RuntimeEvent
from lantai.runtime.tool_transaction import (
    ToolArgumentRepairBudget,
    ToolTransaction,
    ToolTransactionOutcome,
    validate_tool_call_batch,
)
from lantai.runtime.tools import ToolCall, ToolResult


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _event(event_type: str) -> RuntimeEvent:
    return RuntimeEvent(
        id=f"event-{event_type}",
        seq=1,
        type=event_type,
        status="running",
        source="tool",
        session_id="session-1",
        turn_id="turn-1",
    )


def test_tool_transaction_outcome_is_immutable_and_phase_explicit() -> None:
    result = ToolResult(call_id="call-1", name="lookup")
    outcome = ToolTransactionOutcome(
        phase="execute",
        event=_event("tool.started"),
        result=result,
        terminal=False,
    )

    assert outcome.phase == "execute"
    with pytest.raises((AttributeError, TypeError)):
        outcome.terminal = True  # type: ignore[misc]


def test_tool_transaction_maps_public_events_to_stable_phases() -> None:
    assert (
        ToolTransaction._phase_for_event(_event("tool.call.started"))
        == "validate"
    )
    assert (
        ToolTransaction._phase_for_event(_event("approval.requested"))
        == "authorize"
    )
    assert (
        ToolTransaction._phase_for_event(_event("approval.resolved"))
        == "authorize"
    )
    assert (
        ToolTransaction._phase_for_event(_event("tool.started")) == "execute"
    )
    assert (
        ToolTransaction._phase_for_event(_event("tool.call.delta"))
        == "execute"
    )
    assert (
        ToolTransaction._phase_for_event(_event("tool.call.completed"))
        == "project"
    )


def test_tool_batch_validation_is_atomic_and_preserves_order() -> None:
    calls = [
        ToolCall(id="valid", name="lookup", arguments={"query": "ok"}),
        ToolCall(id="invalid", name="write", arguments={}),
    ]
    validated: list[str] = []

    def normalize(call: ToolCall) -> ToolCall:
        return call.model_copy(update={"metadata": {"normalized": True}})

    def validate(call: ToolCall) -> None:
        validated.append(call.id)
        if call.id == "invalid":
            raise ValueError("missing path")

    outcome = validate_tool_call_batch(
        calls,
        normalize=normalize,
        validate=validate,
    )

    assert [call.id for call in outcome.tool_calls] == ["valid", "invalid"]
    assert all(
        call.metadata["normalized"] is True for call in outcome.tool_calls
    )
    assert validated == ["valid", "invalid"]
    assert outcome.requires_repair is True
    assert dict(outcome.errors_by_call_id) == {"invalid": "missing path"}


def test_tool_argument_repair_budget_matches_existing_two_retry_contract() -> (
    None
):
    budget = ToolArgumentRepairBudget()

    assert budget.record_failed_batch() == 1
    assert budget.exhausted is False
    assert budget.record_failed_batch() == 2
    assert budget.exhausted is False
    assert budget.record_failed_batch() == 3
    assert budget.exhausted is True


def test_native_turn_orchestrator_only_keeps_a_thin_transaction_adapter() -> (
    None
):
    native_source = (
        PROJECT_ROOT / "src/lantai/runtime/adapters/native_runtime.py"
    ).read_text(encoding="utf-8")
    transaction_source = (
        PROJECT_ROOT / "src/lantai/runtime/tool_transaction.py"
    ).read_text(encoding="utf-8")
    start = native_source.index("    async def _run_tool_call(\n")
    end = native_source.index("    async def _evaluate_policy(\n", start)
    adapter = native_source[start:end]

    assert "ToolTransaction(self)" in adapter
    assert "_execute_callable" not in adapter
    assert "_request_approval" not in adapter
    assert "_record_tool_effect" not in adapter
    assert "tool.call.completed" not in adapter
    assert len(adapter.splitlines()) < 40
    assert "class ToolTransaction:" in transaction_source
    assert "async def _execute_stages(" in transaction_source
    # S6 plus deterministic finalization recovery define the integrated
    # baseline. Keep it exact instead of granting future growth headroom.
    assert len(native_source.splitlines()) <= 5_882


def test_tool_transaction_uses_an_explicit_forward_only_runtime_port() -> None:
    transaction_source = (
        PROJECT_ROOT / "src/lantai/runtime/tool_transaction.py"
    ).read_text(encoding="utf-8")
    support_source = (
        PROJECT_ROOT / "src/lantai/runtime/native_turn_support.py"
    ).read_text(encoding="utf-8")

    assert "class ToolTransactionRuntimePort(Protocol):" in transaction_source
    assert "runtime: ToolTransactionRuntimePort" in transaction_source
    assert "from .adapters import native_runtime" not in transaction_source
    assert "host: Any" not in transaction_source
    assert "self._host" not in transaction_source
    assert "from .adapters" not in support_source
    assert "ToolTransaction" not in support_source
    assert "async def run_turn(" not in support_source


def test_tool_transaction_binds_started_effect_during_tool_execution() -> None:
    transaction_source = (
        PROJECT_ROOT / "src/lantai/runtime/tool_transaction.py"
    ).read_text(encoding="utf-8")

    assert "bind_runtime_effect_context(" in transaction_source
    assert "RuntimeEffectContext(" in transaction_source


def test_native_policy_chain_is_a_thin_domain_adapter() -> None:
    native_source = (
        PROJECT_ROOT / "src/lantai/runtime/adapters/native_runtime.py"
    ).read_text(encoding="utf-8")
    policy_chain_source = (
        PROJECT_ROOT / "src/lantai/runtime/policy_chain.py"
    ).read_text(encoding="utf-8")
    start = native_source.index("    async def _evaluate_policy(\n")
    end = native_source.index("    def _record_tool_effect_intent(\n", start)
    adapter = native_source[start:end]

    assert "evaluate_policy_chain(" in adapter
    assert "policy_context.model_copy" not in adapter
    assert "AuthorityProvenance" not in adapter
    assert "policy_middlewares" not in adapter
    assert len(adapter.splitlines()) < 25
    assert "async def evaluate_policy_chain(" in policy_chain_source
    assert "policy_context.model_copy(deep=True)" in policy_chain_source
    assert "AuthorityProvenance(" in policy_chain_source
    assert 'if selected.action == "deny":' in policy_chain_source
