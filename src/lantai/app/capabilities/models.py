# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


TaskRunSource = Literal["chat", "cron", "image", "capability", "pipeline"]
TaskRunStatus = Literal[
    "dispatched",
    "acked",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "stalled",
    "blocked",
    "cancelled",
]
TaskStepKind = Literal[
    "message",
    "tool_call",
    "tool_result",
    "artifact",
    "approval",
    "delivery",
    "error",
]
TaskStepStatus = Literal[
    "running",
    "succeeded",
    "failed",
    "skipped",
    "blocked",
    "stalled",
]
EvaluationSeverity = Literal["critical", "major", "minor"]
EvaluationConclusion = Literal["pass", "needs_revision", "blocked"]
RiskLevel = Literal["low", "medium", "high"]
ArtifactType = Literal[
    "file",
    "image",
    "markdown",
    "html",
    "docx",
    "xlsx",
    "wiki_page",
    "email_draft",
]
ParameterType = Literal["text", "number", "boolean", "select"]
CapabilityStepMode = Literal["fixed", "dynamic"]
CapabilityStepKind = Literal[
    "agent_prompt",
    "skill_prompt",
    "browser_prompt",
    "gui_prompt",
    "delivery",
]
DeliveryTargetKind = Literal["chat", "workspace_file", "wiki_draft"]
GuiTarget = Literal["browser", "desktop"]
ReplayPolicy = Literal["full"]
CollaborationPrimaryAsset = Literal["skill", "capability", "both"]
EvolutionProposalStatus = Literal["pending", "accepted", "dismissed", "rolled_back"]
EvolutionRecommendedForm = Literal[
    "extend_existing",
    "capability",
    "skill",
    "automation",
    "skip",
]


class TaskCost(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    image_credits_used: int | None = None
    tool_calls: int | None = None
    duration_ms: int | None = None


class EvaluationSummary(BaseModel):
    conclusion: EvaluationConclusion
    summary: str = ""
    critical_count: int = 0
    major_count: int = 0
    minor_count: int = 0


class EvaluationFinding(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    severity: EvaluationSeverity
    code: str
    title: str
    detail: str = ""
    step_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    schema_version: Literal[1] = 1
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_run_id: str
    evaluator: str = "fast_fail_gate"
    evaluator_version: str = "1"
    rubric_id: str | None = None
    rubric_version: str = "1"
    attempt_id: str | None = None
    evidence_sha256: str | None = None
    conclusion: EvaluationConclusion
    summary: str = ""
    gate_result: EvaluationConclusion | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    findings: list[EvaluationFinding] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class TaskStep(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    index: int = 0
    kind: TaskStepKind
    title: str
    detail: str | None = None
    status: TaskStepStatus = "running"
    tool_name: str | None = None
    risk_level: RiskLevel | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None


class Artifact(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: ArtifactType = "file"
    title: str
    path: str | None = None
    url: str | None = None
    mime_type: str | None = None
    size: int | None = None
    sha256: str | None = None
    producer: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None
    step_id: str | None = None
    effect_id: str | None = None
    classification: str = "internal"
    retention: str = "task_lifetime"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class TaskRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str
    source: TaskRunSource
    chat_id: str | None = None
    session_id: str | None = None
    title: str
    user_intent: str | None = None
    status: TaskRunStatus = "running"
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    failure_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    evaluation_summary: EvaluationSummary | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    cost: TaskCost | None = None


class CapabilityParameter(BaseModel):
    name: str
    label: str | None = None
    type: ParameterType = "text"
    required: bool = True
    default: Any | None = None
    description: str | None = None
    options: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("parameter name cannot be empty")
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError("parameter name must be alphanumeric")
        return name


class DeliveryTarget(BaseModel):
    type: DeliveryTargetKind = "chat"
    path: str | None = None
    title: str | None = None


class CapabilityRiskPolicy(BaseModel):
    require_approval_for: list[RiskLevel] = Field(default_factory=lambda: ["high"])


class CapabilityStep(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    index: int = 0
    title: str
    mode: CapabilityStepMode = "dynamic"
    kind: CapabilityStepKind = "agent_prompt"
    instruction_template: str
    required_parameters: list[str] = Field(default_factory=list)
    skill_name: str | None = None
    profile_id: str | None = None
    target_domains: list[str] = Field(default_factory=list)
    access: Literal["read", "write", "sensitive"] | None = None
    requires_manual_confirmation: bool = False
    gui_target: GuiTarget | None = None
    replay_policy: ReplayPolicy | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    risk_level: RiskLevel = "low"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Capability(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""
    version: int = 1
    enabled: bool = False
    tags: list[str] = Field(default_factory=list)
    source_run_id: str | None = None
    parameters: list[CapabilityParameter] = Field(default_factory=list)
    steps: list[CapabilityStep] = Field(default_factory=list)
    delivery_defaults: list[DeliveryTarget] = Field(
        default_factory=lambda: [DeliveryTarget(type="chat")]
    )
    risk_policy: CapabilityRiskPolicy = Field(default_factory=CapabilityRiskPolicy)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SuggestedSkillDraft(BaseModel):
    name: str
    description: str = ""
    content: str
    tags: list[str] = Field(default_factory=list)
    reason: str = ""
    attach_to_step_index: int | None = None


class CollaborationDraftRequest(BaseModel):
    chat_id: str = Field(min_length=1)
    anchor_run_id: str = Field(min_length=1)
    anchor_response_id: str | None = None


class CollaborationScope(BaseModel):
    chat_id: str
    anchor_run_id: str
    anchor_response_id: str | None = None
    message_count: int = 0
    turn_count: int = 0
    tool_call_count: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    truncated: bool = False
    redacted_count: int = 0


class CollaborationInsights(BaseModel):
    summary: str = ""
    reusable_goal: str = ""
    corrections: list[str] = Field(default_factory=list)
    decision_rules: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    tool_strategy: list[str] = Field(default_factory=list)
    output_expectations: list[str] = Field(default_factory=list)


class CapabilityDraft(BaseModel):
    capability: Capability
    source_run: TaskRun
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggested_skills: list[SuggestedSkillDraft] = Field(default_factory=list)
    primary_asset: CollaborationPrimaryAsset = "capability"
    scope: CollaborationScope | None = None
    insights: CollaborationInsights | None = None
    evolution_proposal_id: str | None = None
    matched_capability_id: str | None = None


class EvolutionEvidence(BaseModel):
    run_id: str
    title: str
    source: TaskRunSource
    occurred_at: datetime


class EvolutionProposal(BaseModel):
    id: str
    fingerprint: str
    title: str
    rationale: str
    recommended_form: EvolutionRecommendedForm
    status: EvolutionProposalStatus = "pending"
    repeat_count: int = Field(ge=2)
    evidence: list[EvolutionEvidence] = Field(min_length=2)
    matched_capability_id: str | None = None
    capability_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    reviewed_at: datetime | None = None


class EvolutionProposalReviewRequest(BaseModel):
    status: EvolutionProposalStatus
    capability_id: str | None = None


class CapabilityRunRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    parameters: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    user_id: str | None = None
    channel: str | None = "console"
    delivery_targets: list[DeliveryTarget] | None = None
    approved_risks: list[RiskLevel] = Field(default_factory=list)
    manual_gui_ack: bool = False
    suppress_delivery: bool = False


class CapabilityRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    capability_id: str
    capability_name: str
    task_run_id: str
    status: TaskRunStatus = "running"
    parameters: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    output_text: str | None = None
    error: str | None = None


class PipelineNode(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    index: int = 0
    title: str
    capability_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    position: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PipelineEdge(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    target: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class PipelineRuntime(BaseModel):
    max_parallelism: int = Field(default=2, ge=1, le=4)
    timeout_seconds: int | None = Field(default=None, ge=1)


class Pipeline(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""
    version: int = 1
    enabled: bool = False
    tags: list[str] = Field(default_factory=list)
    parameters: list[CapabilityParameter] = Field(default_factory=list)
    nodes: list[PipelineNode] = Field(default_factory=list)
    edges: list[PipelineEdge] = Field(default_factory=list)
    delivery_defaults: list[DeliveryTarget] = Field(
        default_factory=lambda: [DeliveryTarget(type="chat")]
    )
    runtime: PipelineRuntime = Field(default_factory=PipelineRuntime)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PipelineRunRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    parameters: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    user_id: str | None = None
    channel: str | None = "console"
    delivery_targets: list[DeliveryTarget] | None = None
    approved_risks: list[RiskLevel] = Field(default_factory=list)
    manual_gui_ack: bool = False


class PipelineRunNode(BaseModel):
    node_id: str
    title: str
    capability_id: str
    capability_run_id: str | None = None
    task_run_id: str | None = None
    status: TaskRunStatus = "running"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    output_text: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    error: str | None = None


class PipelineRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    pipeline_id: str
    pipeline_name: str
    task_run_id: str
    status: TaskRunStatus = "running"
    parameters: dict[str, Any] = Field(default_factory=dict)
    nodes: list[PipelineRunNode] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    output_text: str | None = None
    error: str | None = None


class CapabilitiesFile(BaseModel):
    version: int = 1
    capabilities: list[Capability] = Field(default_factory=list)


class PipelinesFile(BaseModel):
    version: int = 1
    pipelines: list[Pipeline] = Field(default_factory=list)


class TaskRunsFile(BaseModel):
    version: int = 1
    runs: list[TaskRun] = Field(default_factory=list)


class EvolutionProposalsFile(BaseModel):
    version: int = 1
    proposals: list[EvolutionProposal] = Field(default_factory=list)
