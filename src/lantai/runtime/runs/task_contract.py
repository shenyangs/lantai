# -*- coding: utf-8 -*-
"""Compile chat intent into the authoritative Runtime task contract."""

from __future__ import annotations

from .models import (
    RuntimeBudget,
    RuntimeCheckpointPolicy,
    RuntimeFailurePolicy,
    RuntimeTaskComplexity,
    RuntimeTaskCommand,
    RuntimeTaskContract,
)


def normalize_task_complexity(value: str | None, work_method_id: str) -> RuntimeTaskComplexity:
    """Return the explicit execution tier, never a keyword-derived tier."""

    method = str(work_method_id or "").strip().lower()
    if method == "sustained_execution":
        return "sustained"
    return "standard"


def budget_for_complexity(complexity: RuntimeTaskComplexity) -> RuntimeBudget:
    """Return loop-safety policy without imposing a healthy-work quota.

    Complexity and work-method choices describe how the agent should work;
    they must not silently cap a user's task by turns, tools, elapsed time, or
    aggregate tokens.  Termination is driven by semantic no-progress and
    repeated-failure guards instead.
    """

    return RuntimeBudget(
        max_no_progress_rounds=3,
        max_same_failure_count=3,
    )


def budget_for_work_method(
    *,
    method_id: str,
    complexity: RuntimeTaskComplexity,
    max_steps: int | None = None,
    max_tool_calls: int | None = None,
    max_wall_seconds: float | None = None,
    max_no_progress_rounds: int | None = None,
) -> RuntimeBudget:
    """Compile semantic loop guards from the selected work method.

    Legacy work-method resource fields remain accepted for persisted-config
    compatibility, but they are deliberately not converted into hard runtime
    ceilings.
    """

    baseline = budget_for_complexity(complexity)
    no_progress = int(
        max_no_progress_rounds or baseline.max_no_progress_rounds or 3
    )
    return baseline.model_copy(
        update={
            "max_no_progress_rounds": no_progress,
        }
    )


def build_chat_task_contract(
    *,
    objective: str,
    client_turn_id: str | None,
    complexity: str | None,
    work_method_id: str | None,
    run_id: str | None = None,
    attempt_id: str | None = None,
    acceptance: list[str] | tuple[str, ...] = (),
    work_method_max_steps: int | None = None,
    work_method_max_tool_calls: int | None = None,
    work_method_max_wall_seconds: float | None = None,
    work_method_max_no_progress_rounds: int | None = None,
) -> RuntimeTaskContract:
    method_id = str(work_method_id or "standard").strip().lower() or "standard"
    normalized_complexity = normalize_task_complexity(complexity, method_id)
    budget = budget_for_work_method(
        method_id=method_id,
        complexity=normalized_complexity,
        max_steps=work_method_max_steps,
        max_tool_calls=work_method_max_tool_calls,
        max_wall_seconds=work_method_max_wall_seconds,
        max_no_progress_rounds=work_method_max_no_progress_rounds,
    )
    return RuntimeTaskContract(
        run_id=run_id,
        attempt_id=attempt_id,
        client_turn_id=client_turn_id,
        complexity=normalized_complexity,
        work_method_id=method_id,
        objective=objective,
        acceptance=tuple(
            str(item).strip() for item in acceptance if str(item).strip()
        ),
        budget=budget,
        failure_policy=RuntimeFailurePolicy(
            max_same_failure_count=budget.max_same_failure_count or 3,
            max_no_progress_rounds=budget.max_no_progress_rounds or 3,
        ),
        checkpoint_policy=RuntimeCheckpointPolicy(),
    )


def revise_chat_task_contract(
    contract: RuntimeTaskContract,
    *,
    client_turn_id: str,
    command: str,
) -> RuntimeTaskContract:
    """Append one durable steer without replacing the original task facts."""

    command_id = str(client_turn_id or "").strip()
    command_text = str(command or "").strip()
    if not command_id:
        raise ValueError("client_turn_id must not be empty")
    if not command_text:
        raise ValueError("command must not be empty")
    for existing in contract.commands:
        if existing.client_turn_id != command_id:
            continue
        if existing.text != command_text:
            raise ValueError(
                "client_turn_id already identifies a different task command"
            )
        return contract
    revision = contract.revision + 1
    task_command = RuntimeTaskCommand(
        revision=revision,
        client_turn_id=command_id,
        text=command_text,
    )
    return contract.model_copy(
        update={
            "client_turn_id": command_id,
            "revision": revision,
            "commands": (*contract.commands, task_command),
        }
    )


def task_contract_effective_objective(contract: RuntimeTaskContract) -> str:
    """Render the current model objective while retaining the base request."""

    sections = [contract.objective]
    sections.extend(
        (
            f"[steer revision={command.revision} "
            f"client_turn_id={command.client_turn_id}]\n{command.text}"
        )
        for command in contract.commands
    )
    return "\n\n".join(sections)


__all__ = [
    "budget_for_complexity",
    "budget_for_work_method",
    "build_chat_task_contract",
    "normalize_task_complexity",
    "revise_chat_task_contract",
    "task_contract_effective_objective",
]
