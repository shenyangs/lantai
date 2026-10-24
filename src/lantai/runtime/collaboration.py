# -*- coding: utf-8 -*-
"""Typed contract for one Shen-Xingzhi-hosted collaboration room."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CollaborationMemberContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: str
    agent_id: str
    label: str
    assignment: str
    depends_on: list[str] = Field(default_factory=list, max_length=3)
    minimum_output_chars: int = Field(default=0, ge=0, le=100_000)
    required_output_markers: list[str] = Field(
        default_factory=list,
        max_length=12,
    )
    effective_permissions: Literal["read_only"] = "read_only"
    can_spawn_collaboration: Literal[False] = False
    can_write_external_state: Literal[False] = False


class CollaborationHostContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = "default"
    display_name: str = "沈行知"
    role: Literal["host_and_final_owner"] = "host_and_final_owner"
    responsibilities: list[str] = Field(
        default_factory=lambda: [
            "staff",
            "delegate",
            "moderate",
            "reconcile",
            "verify",
            "deliver",
            "close",
        ],
    )


class CollaborationRoundPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds: int = Field(default=2, ge=1, le=2)
    allow_nested_collaboration: Literal[False] = False
    member_write_access: Literal[False] = False
    replacement_limit: int = Field(default=1, ge=0, le=1)


class CollaborationConvergencePolicy(BaseModel):
    """Public stopping agreement for every read-only member run."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["enforce"] = "enforce"
    max_no_progress_rounds: int = Field(default=2, ge=1, le=4)
    semantic_review_limit: Literal[1] = 1
    reserve_synthesis_rounds: Literal[1] = 1
    max_research_calls: int = Field(default=10, ge=1, le=40)
    max_read_calls: int = Field(default=14, ge=1, le=48)
    hard_timeout_seconds: int = Field(default=300, ge=60, le=3600)
    saturated_outcome: Literal["best_effort_or_checkpoint"] = (
        "best_effort_or_checkpoint"
    )


class CollaborationSynthesisContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: Literal["parent_agent"] = "parent_agent"
    conflict_policy: Literal["surface_conflicts", "prefer_stronger_evidence"] = (
        "surface_conflicts"
    )
    required_sections: list[str] = Field(
        default_factory=lambda: [
            "agreements",
            "conflicts",
            "unknowns",
            "evidence_gaps",
            "next_steps",
        ],
    )


class CollaborationContract(BaseModel):
    """Public, serializable agreement that constrains a collaboration batch."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    contract_id: str
    goal: str
    why_parallel: str
    effective_permissions: Literal["read_only"] = "read_only"
    participation_mode: Literal["autonomous", "checkpoint", "interactive"] = (
        "autonomous"
    )
    protocol: Literal["parallel_consultation", "roundtable"] = (
        "parallel_consultation"
    )
    host: CollaborationHostContract = Field(
        default_factory=CollaborationHostContract,
    )
    round_policy: CollaborationRoundPolicy = Field(
        default_factory=CollaborationRoundPolicy,
    )
    convergence: CollaborationConvergencePolicy = Field(
        default_factory=CollaborationConvergencePolicy,
    )
    members: list[CollaborationMemberContract] = Field(min_length=2, max_length=4)
    acceptance_criteria: list[str] = Field(min_length=1)
    synthesis: CollaborationSynthesisContract = Field(
        default_factory=CollaborationSynthesisContract,
    )

    def public_dump(self) -> dict[str, object]:
        """Return the public contract without member-private assignments."""

        value = self.model_dump(mode="json")
        for member in value.get("members", []):
            if isinstance(member, dict):
                member.pop("assignment", None)
        return value


def build_collaboration_contract(
    *,
    contract_id: str,
    goal: str,
    why_parallel: str,
    tasks: list[dict[str, Any]],
    acceptance_criteria: list[str] | None = None,
    conflict_policy: Literal[
        "surface_conflicts",
        "prefer_stronger_evidence",
    ] = "surface_conflicts",
    participation_mode: Literal[
        "autonomous",
        "checkpoint",
        "interactive",
    ] = "autonomous",
    protocol: Literal["parallel_consultation", "roundtable"] = (
        "parallel_consultation"
    ),
) -> CollaborationContract:
    normalized_goal = str(goal or "").strip() or "并行收集独立专业判断"
    normalized_reason = str(why_parallel or "").strip() or "成员任务相互独立且只读"
    criteria = [
        str(item).strip()
        for item in (acceptance_criteria or [])
        if str(item).strip()
    ] or ["每个成员给出结论与依据", "父 Agent 显式汇总分歧和证据缺口"]
    return CollaborationContract(
        contract_id=contract_id,
        goal=normalized_goal,
        why_parallel=normalized_reason,
        participation_mode=participation_mode,
        protocol=protocol,
        members=[
            CollaborationMemberContract(
                member_id=f"member-{index + 1}",
                agent_id=task["to_agent"],
                label=task["label"],
                assignment=task["text"],
                depends_on=list(task.get("depends_on") or []),
                minimum_output_chars=int(task.get("minimum_output_chars") or 0),
                required_output_markers=list(
                    task.get("required_output_markers") or []
                ),
            )
            for index, task in enumerate(tasks)
        ],
        acceptance_criteria=criteria,
        synthesis=CollaborationSynthesisContract(conflict_policy=conflict_policy),
    )
