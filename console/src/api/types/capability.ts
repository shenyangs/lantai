export type TaskRunSource =
  | "chat"
  | "cron"
  | "image"
  | "capability"
  | "pipeline";
export type TaskRunStatus =
  | "dispatched"
  | "acked"
  | "running"
  | "succeeded"
  | "failed"
  | "timed_out"
  | "stalled"
  | "blocked"
  | "cancelled";
export type EvaluationSeverity = "critical" | "major" | "minor";
export type EvaluationConclusion = "pass" | "needs_revision" | "blocked";
export type RiskLevel = "low" | "medium" | "high";
export type ArtifactType =
  | "file"
  | "image"
  | "markdown"
  | "html"
  | "docx"
  | "xlsx"
  | "wiki_page"
  | "email_draft";

export interface TaskStep {
  id: string;
  index: number;
  kind:
    | "message"
    | "tool_call"
    | "tool_result"
    | "artifact"
    | "approval"
    | "delivery"
    | "error";
  title: string;
  detail?: string | null;
  status:
    | "running"
    | "succeeded"
    | "failed"
    | "skipped"
    | "blocked"
    | "stalled";
  tool_name?: string | null;
  risk_level?: RiskLevel | null;
  metadata?: Record<string, unknown>;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface Artifact {
  id: string;
  type: ArtifactType;
  title: string;
  path?: string | null;
  url?: string | null;
  mime_type?: string | null;
  size?: number | null;
  created_at: string;
}

export interface TaskRun {
  id: string;
  agent_id: string;
  source: TaskRunSource;
  chat_id?: string | null;
  session_id?: string | null;
  title: string;
  user_intent?: string | null;
  status: TaskRunStatus;
  started_at: string;
  ended_at?: string | null;
  last_heartbeat_at?: string | null;
  failure_reason?: string | null;
  metadata?: Record<string, unknown>;
  evaluation_summary?: EvaluationSummary | null;
  steps: TaskStep[];
  artifacts: Artifact[];
  cost?: {
    prompt_tokens?: number | null;
    completion_tokens?: number | null;
    image_credits_used?: number | null;
    tool_calls?: number | null;
    duration_ms?: number | null;
  } | null;
}

export interface EvaluationSummary {
  conclusion: EvaluationConclusion;
  summary: string;
  critical_count: number;
  major_count: number;
  minor_count: number;
}

export interface EvaluationFinding {
  id: string;
  severity: EvaluationSeverity;
  code: string;
  title: string;
  detail: string;
  step_id?: string | null;
  metadata?: Record<string, unknown>;
}

export interface EvaluationReport {
  id: string;
  task_run_id: string;
  evaluator: string;
  conclusion: EvaluationConclusion;
  summary: string;
  findings: EvaluationFinding[];
  created_at: string;
}

export interface CapabilityParameter {
  name: string;
  label?: string | null;
  type: "text" | "number" | "boolean" | "select";
  required: boolean;
  default?: unknown;
  description?: string | null;
  options?: string[];
}

export interface DeliveryTarget {
  type: "chat" | "workspace_file" | "wiki_draft";
  path?: string | null;
  title?: string | null;
}

export interface CapabilityStep {
  id: string;
  index: number;
  title: string;
  mode: "fixed" | "dynamic";
  kind:
    | "agent_prompt"
    | "skill_prompt"
    | "browser_prompt"
    | "gui_prompt"
    | "delivery";
  instruction_template: string;
  required_parameters: string[];
  skill_name?: string | null;
  profile_id?: string | null;
  target_domains?: string[];
  access?: "read" | "write" | "sensitive" | null;
  requires_manual_confirmation?: boolean;
  gui_target?: "browser" | "desktop" | null;
  replay_policy?: "full" | null;
  timeout_seconds?: number | null;
  risk_level: RiskLevel;
  metadata?: Record<string, unknown>;
}

export interface Capability {
  id: string;
  name: string;
  description: string;
  version: number;
  enabled: boolean;
  tags: string[];
  source_run_id?: string | null;
  parameters: CapabilityParameter[];
  steps: CapabilityStep[];
  delivery_defaults: DeliveryTarget[];
  risk_policy: {
    require_approval_for: RiskLevel[];
  };
  created_at: string;
  updated_at: string;
}

export interface CapabilityDraft {
  capability: Capability;
  source_run: TaskRun;
  evidence: string[];
  warnings: string[];
  suggested_skills?: SuggestedSkillDraft[];
  primary_asset?: "skill" | "capability" | "both";
  scope?: CollaborationScope | null;
  insights?: CollaborationInsights | null;
  evolution_proposal_id?: string | null;
  matched_capability_id?: string | null;
}

export type EvolutionProposalStatus =
  | "pending"
  | "accepted"
  | "dismissed"
  | "rolled_back";

export type EvolutionRecommendedForm =
  | "extend_existing"
  | "capability"
  | "skill"
  | "automation"
  | "skip";

export interface EvolutionEvidence {
  run_id: string;
  title: string;
  source: TaskRunSource;
  occurred_at: string;
}

export interface EvolutionProposal {
  id: string;
  fingerprint: string;
  title: string;
  rationale: string;
  recommended_form: EvolutionRecommendedForm;
  status: EvolutionProposalStatus;
  repeat_count: number;
  evidence: EvolutionEvidence[];
  matched_capability_id?: string | null;
  capability_id?: string | null;
  created_at: string;
  reviewed_at?: string | null;
}

export interface CollaborationDraftRequest {
  chat_id: string;
  anchor_run_id: string;
  anchor_response_id?: string | null;
}

export interface CollaborationScope {
  chat_id: string;
  anchor_run_id: string;
  anchor_response_id?: string | null;
  message_count: number;
  turn_count: number;
  tool_call_count: number;
  started_at?: string | null;
  ended_at?: string | null;
  truncated: boolean;
  redacted_count: number;
}

export interface CollaborationInsights {
  summary: string;
  reusable_goal: string;
  corrections: string[];
  decision_rules: string[];
  failure_modes: string[];
  tool_strategy: string[];
  output_expectations: string[];
}

export interface CollaborationDraftStageEvent {
  type: "stage";
  step: string;
  status: "running" | "completed" | "failed";
  title: string;
  detail: string;
  elapsed_seconds?: number;
}

export interface CollaborationDraftResultEvent {
  type: "result";
  operation_id: string;
  draft: CapabilityDraft;
}

export interface CollaborationDraftErrorEvent {
  type: "error";
  message: string;
}

export type CollaborationDraftStreamEvent =
  | CollaborationDraftStageEvent
  | CollaborationDraftResultEvent
  | CollaborationDraftErrorEvent;

export interface SuggestedSkillDraft {
  name: string;
  description: string;
  content: string;
  tags: string[];
  reason: string;
  attach_to_step_index?: number | null;
}

export interface CapabilityRunRequest {
  parameters: Record<string, unknown>;
  session_id?: string | null;
  user_id?: string | null;
  channel?: string | null;
  delivery_targets?: DeliveryTarget[] | null;
  approved_risks?: RiskLevel[];
  manual_gui_ack?: boolean;
  suppress_delivery?: boolean;
}

export interface CapabilityRun {
  id: string;
  capability_id: string;
  capability_name: string;
  task_run_id: string;
  status: TaskRunStatus;
  parameters: Record<string, unknown>;
  started_at: string;
  ended_at?: string | null;
  artifacts: Artifact[];
  output_text?: string | null;
  error?: string | null;
}

export interface CapabilityPolicy {
  app_store_distribution: boolean;
  gui_steps_enabled: boolean;
  gui_steps_manual_only: boolean;
}

export interface PipelineNode {
  id: string;
  index: number;
  title: string;
  capability_id: string;
  parameters: Record<string, unknown>;
  position?: Record<string, number>;
  metadata?: Record<string, unknown>;
}

export interface PipelineEdge {
  id: string;
  source: string;
  target: string;
  metadata?: Record<string, unknown>;
}

export interface PipelineRuntime {
  max_parallelism: number;
  timeout_seconds?: number | null;
}

export interface Pipeline {
  id: string;
  name: string;
  description: string;
  version: number;
  enabled: boolean;
  tags: string[];
  parameters: CapabilityParameter[];
  nodes: PipelineNode[];
  edges: PipelineEdge[];
  delivery_defaults: DeliveryTarget[];
  runtime: PipelineRuntime;
  created_at: string;
  updated_at: string;
}

export interface PipelineRunRequest {
  parameters: Record<string, unknown>;
  session_id?: string | null;
  user_id?: string | null;
  channel?: string | null;
  delivery_targets?: DeliveryTarget[] | null;
  approved_risks?: RiskLevel[];
  manual_gui_ack?: boolean;
}

export interface PipelineRunNode {
  node_id: string;
  title: string;
  capability_id: string;
  capability_run_id?: string | null;
  task_run_id?: string | null;
  status: TaskRunStatus;
  started_at?: string | null;
  ended_at?: string | null;
  output_text?: string | null;
  artifacts: Artifact[];
  error?: string | null;
}

export interface PipelineRun {
  id: string;
  pipeline_id: string;
  pipeline_name: string;
  task_run_id: string;
  status: TaskRunStatus;
  parameters: Record<string, unknown>;
  nodes: PipelineRunNode[];
  started_at: string;
  ended_at?: string | null;
  artifacts: Artifact[];
  output_text?: string | null;
  error?: string | null;
}
