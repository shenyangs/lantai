import type { RuntimeEvent, RuntimeEventUsage } from "./chat";

export type { RuntimeEvent };

export type RuntimeRunStatus =
  | "running"
  | "completed"
  | "failed"
  | "interrupted"
  | "stalled"
  | "idle"
  | "unknown";

export type RuntimeRunSource =
  | "chat"
  | "tool"
  | "approval"
  | "mission"
  | "browser"
  | "capability"
  | "pipeline"
  | "cron"
  | "reminder"
  | "heartbeat"
  | "image"
  | "skills"
  | "system";

export interface RuntimeUsageSummary extends RuntimeEventUsage {
  input_tokens: number;
  output_tokens: number;
  cache_tokens: number;
  context_tokens: number;
  cost_points: number;
  tool_calls: number;
  artifact_bytes: number;
}

export interface RuntimeRunSummary {
  id: string;
  display_id: string;
  source: RuntimeRunSource;
  attempt_id?: string | null;
  title: string;
  summary: string;
  status: RuntimeRunStatus;
  authority_status?: string;
  transport_status?: string;
  revision?: number | null;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  model?: string | null;
  chat_id?: string | null;
  session_id?: string | null;
  workspace_dir?: string | null;
  lease_owner?: string | null;
  lease_expires_at?: string | null;
  heartbeat_at?: string | null;
  release_edition?: string | null;
  current_step?: Record<string, unknown> | null;
  budget?: Record<string, unknown>;
  checkpoint?: Record<string, unknown> | null;
  pending_approval?: boolean;
  ambiguous_effect?: boolean;
  evaluation_summary?: Record<string, unknown> | null;
  artifact_count?: number;
  recovery?: Record<string, unknown>;
  can_interrupt: boolean;
  can_retry: boolean;
  usage: RuntimeUsageSummary;
  last_event_summary: string;
  event_count: number;
  projection_source?: string;
  debug_refs: Record<string, unknown>;
}

export interface RuntimeRunsResponse {
  total: number;
  items: RuntimeRunSummary[];
}

export type RuntimeTaskLane =
  | "pending"
  | "active"
  | "needs_user"
  | "done"
  | "needs_action";

export type RuntimeTaskOutcome =
  | "queued"
  | "in_progress"
  | "needs_input"
  | "delivered"
  | "incomplete"
  | "blocked"
  | "unknown";

export type RuntimeAutonomyOperation =
  | "automatic_resume"
  | "model_call"
  | "local_read"
  | "local_write_new"
  | "local_modify_existing"
  | "network_read"
  | "gui"
  | "external_delivery";

export type RuntimeAutoResumeCondition =
  | "network_recovery"
  | "backend_restart"
  | "app_reopen";

export type RuntimeAutonomyRequiredCapability =
  | "renderer"
  | "keep_awake"
  | "result_verifier";

export type RuntimeAutonomyPreflightCapability =
  | "user_grant"
  | "workspace_access"
  | "disk_capacity"
  | "provider_authentication"
  | "model_protocol"
  | "model_context"
  | "renderer"
  | "keep_awake"
  | "gui"
  | "result_verifier";

export interface RuntimeAutonomyPreflightCheck {
  schema_version?: 1;
  check_id: string;
  capability: RuntimeAutonomyPreflightCapability;
  status: "passed" | "blocked";
  checked_at: string;
  message: string;
  evidence: Record<string, string | number | boolean | null>;
}

export interface RuntimeModelPriceBound {
  schema_version?: 1;
  provider_id: string;
  model_id: string;
  pricing_version: string;
  max_dispatch_micro_cny: number;
  upper_bound_proven: true;
}

export interface RuntimeAutonomyGrant {
  schema_version?: 1;
  grant_id: string;
  approved: true;
  issued_by: "user";
  run_id: string;
  chat_id: string;
  workspace_id: string;
  target_task_revision: number;
  issued_at: string;
  starts_at: string;
  expires_at: string;
  allowed_operations: RuntimeAutonomyOperation[];
  allowed_paths: string[];
  allowed_provider_ids: string[];
  allowed_model_ids: string[];
  allow_network: boolean;
  allow_gui: boolean;
  allow_external_delivery: boolean;
  auto_resume_conditions: RuntimeAutoResumeCondition[];
  required_capabilities?: RuntimeAutonomyRequiredCapability[];
  preflight_checks?: RuntimeAutonomyPreflightCheck[];
  budget_currency?: "CNY" | null;
  max_cost_micro_cny?: number | null;
  model_price_bounds: RuntimeModelPriceBound[];
  revision: number;
  revocation_generation: number;
  revoked_at?: string | null;
  revocation_reason?: string | null;
}

export interface RuntimeProgressMilestone {
  milestone_id: string;
  statement: string;
  evidence_refs: string[];
}

export interface RuntimeProgressCostSummary {
  currency: "CNY";
  limit_micro_cny?: number | null;
  reserved_micro_cny: number;
  committed_micro_cny: number;
  settled_count: number;
  pending_count: number;
  ambiguous_count: number;
  overrun_count: number;
}

export interface RuntimeProgressEffectSummary {
  status_counts: Record<string, number>;
  unresolved_effect_ids: string[];
  ambiguous_effect_ids: string[];
}

export interface RuntimeProgressReport {
  schema_version?: 1;
  report_id: string;
  kind: "deadline_snapshot" | "terminal" | "manual";
  generated_at: string;
  run_id: string;
  attempt_id: string;
  task_revision?: number | null;
  contract_revision: number;
  status: string;
  terminal: boolean;
  objective: string;
  completed_milestones: RuntimeProgressMilestone[];
  acceptance_results: Record<string, unknown>[];
  open_acceptance_items: string[];
  artifact_refs: string[];
  blocked_reasons: string[];
  user_decisions: string[];
  effect_summary: RuntimeProgressEffectSummary;
  cost_summary: RuntimeProgressCostSummary;
  active_seconds?: number | null;
  elapsed_seconds?: number | null;
  wait_seconds?: number | null;
  unknown_facts: string[];
  grant_id?: string | null;
  grant_revision?: number | null;
  grant_expires_at?: string | null;
  grant_revoked?: boolean | null;
  wake_key?: string | null;
  wait_reason?: string | null;
  narrative_status: "not_requested" | "generated" | "failed";
  narrative?: string | null;
  narrative_error?: string | null;
}

export interface RuntimeTaskBoardItem {
  id: string;
  display_id: string;
  title: string;
  summary: string;
  source: RuntimeRunSource | string;
  lane: RuntimeTaskLane;
  outcome_status: RuntimeTaskOutcome;
  authority_status: string;
  transport_status: string;
  delivery_status: string;
  is_history: boolean;
  pending_approval: boolean;
  ambiguous_effect: boolean;
  next_action?: string | null;
  agent_id?: string | null;
  chat_id?: string | null;
  session_id?: string | null;
  workspace_dir?: string | null;
  attempt_id?: string | null;
  revision?: number | null;
  created_at?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
  ended_at?: string | null;
  current_step?: Record<string, unknown> | null;
  checkpoint?: Record<string, unknown> | null;
  budget: Record<string, unknown>;
  usage: RuntimeUsageSummary;
  recovery: Record<string, unknown>;
  autonomy_grant?: RuntimeAutonomyGrant | null;
  progress_report?: RuntimeProgressReport | null;
  completion_evidence?: Record<string, unknown> | null;
  can_start: boolean;
  can_interrupt: boolean;
  can_retry: boolean;
  can_resume: boolean;
  can_open_chat: boolean;
  event_count: number;
  last_event_summary: string;
  debug_refs: Record<string, unknown>;
}

export interface RuntimeTaskBoardCounts {
  pending: number;
  active: number;
  needs_user: number;
  done: number;
  needs_action: number;
  history: number;
}

export interface RuntimeTaskBoardResponse {
  total: number;
  visible_total: number;
  generated_at: string;
  counts: RuntimeTaskBoardCounts;
  items: RuntimeTaskBoardItem[];
}

export interface CreateRuntimeTaskRequest {
  objective: string;
  title?: string | null;
  acceptance?: string[];
  work_method_id?: string;
  client_task_id: string;
  auto_start?: boolean;
}

export interface CreateRuntimeTaskResponse {
  created: boolean;
  run_id: string;
  chat_id: string;
  session_id: string;
  status: string;
  revision: number;
  start?: RuntimeTaskStartResponse | null;
}

export interface RuntimeTaskStartResponse {
  run_id: string;
  chat_id: string;
  session_id: string;
  status: string;
  already_running: boolean;
  accepted: boolean;
}

export interface RuntimeRunEventsResponse {
  run_id: string;
  attempt_id?: string | null;
  workspace_dir?: string | null;
  after_seq: number;
  latest_seq: number;
  limit?: number | null;
  next_after_seq?: number | null;
  has_more?: boolean;
  events: RuntimeEvent[];
}

export type RuntimeContinueMode = "reconnect" | "retry" | "resume" | "takeover";

export type RuntimeEffectResolution =
  | "mark_ambiguous"
  | "probe_committed"
  | "safe_to_retry"
  | "compensated"
  | "human_resolved";

export type RuntimeEffectRepairAction =
  | "rebuild_checkpoint"
  | "rewrite_terminal";

export type RuntimeEffectTerminalStatus =
  | "succeeded"
  | "failed"
  | "cancelled"
  | "stalled";

export interface RuntimeRunControlRequest {
  expected_attempt_id?: string | null;
  expected_revision?: number | null;
  chat_id?: string | null;
  session_id?: string | null;
  workspace_dir?: string | null;
}

export interface RuntimeAutonomyGrantRequest
  extends RuntimeRunControlRequest {
  approved: true;
  expires_at: string;
  allowed_operations: RuntimeAutonomyOperation[];
  allowed_paths?: string[];
  allowed_provider_ids?: string[];
  allowed_model_ids?: string[];
  allow_network?: boolean;
  allow_gui?: boolean;
  allow_external_delivery?: boolean;
  auto_resume_conditions?: RuntimeAutoResumeCondition[];
  required_capabilities?: RuntimeAutonomyRequiredCapability[];
  budget_currency?: "CNY" | null;
  max_cost_micro_cny?: number | null;
  model_price_bounds?: RuntimeModelPriceBound[];
  reason: string;
}

export interface RuntimeAutonomyRevokeRequest
  extends RuntimeRunControlRequest {
  reason: string;
}

export interface RuntimeBudget {
  max_turns?: number | null;
  max_steps?: number | null;
  max_tool_calls?: number | null;
  max_seconds?: number | null;
  max_tokens?: number | null;
  max_cost_points?: number | null;
  max_artifact_bytes?: number | null;
  deadline_at?: string | null;
}

export interface RuntimeRunBudgetUpdateRequest extends RuntimeRunControlRequest {
  budget: RuntimeBudget;
  changed_by?: "user" | "system" | "policy" | "runtime";
  reason: string;
}

export interface RuntimeRunBudgetUpdateResponse extends RuntimeRunActionResponse {
  changed: boolean;
  previous_revision?: number;
  budget: RuntimeBudget;
  budget_event?: {
    seq: number;
    type: string;
    event_hash: string;
    previous_budget_hash: string;
    next_budget_hash: string;
  };
}

export interface RuntimeEffectResolutionRequest extends RuntimeRunControlRequest {
  resolution: RuntimeEffectResolution;
  reason?: string | null;
  result_hash?: string | null;
  expected_effect_revision?: number | null;
}

export interface RuntimeEffectRepairRequest extends RuntimeRunControlRequest {
  action: RuntimeEffectRepairAction;
  reason?: string | null;
  expected_effect_revision?: number | null;
  checkpoint_id?: string | null;
  checkpoint_seq?: number | null;
  checkpoint_summary?: string | null;
  artifact_id?: string | null;
  event_id?: string | null;
  terminal_status?: RuntimeEffectTerminalStatus;
}

export interface RuntimeRunActionResponse {
  accepted?: boolean;
  stopped?: boolean;
  queued?: boolean;
  reconnected?: boolean;
  paused?: boolean;
  resumed?: boolean;
  archived?: boolean;
  deleted?: boolean;
  mode?: RuntimeContinueMode;
  run_id: string;
  attempt_id?: string;
  revision?: number;
  workspace_dir?: string;
  artifact_cascade?: Record<string, unknown>;
  runtime_archive?: Record<string, unknown>;
  runtime_delete?: Record<string, unknown>;
  file_grant_lifecycle?: Record<string, unknown>;
  attempt_prepared?: boolean;
  previous_attempt_id?: string;
  delete_artifacts?: boolean;
  delete_approval?: Record<string, unknown>;
  chat_id?: string;
  session_id?: string;
  reason?: string;
  latest_seq?: number;
  status?: string;
  browser_status?: string;
  text?: string | null;
}

export interface RuntimeAutonomyGrantResponse
  extends RuntimeRunActionResponse {
  changed: boolean;
  autonomy_grant: RuntimeAutonomyGrant | null;
}

export interface RuntimeRunExportRequest extends RuntimeRunControlRequest {
  approved: boolean;
  reason: string;
  exported_by?: string | null;
  version_info?: Record<string, string>;
}

export interface RuntimeRunExportTraceEvent {
  event_id: string;
  seq: number;
  type: string;
  status?: string | null;
  source: string;
  summary: string;
  payload?: Record<string, unknown>;
  usage?: Record<string, unknown> | null;
  created_at?: string | null;
}

export interface RuntimeRunExportPackage {
  schema_version: number;
  exported_at: string;
  exported_by: string;
  export_reason: string;
  contract: Record<string, unknown>;
  runtime_run: Record<string, unknown>;
  trace_events: RuntimeRunExportTraceEvent[];
  latest_evaluation?: Record<string, unknown> | null;
  artifact_manifest: {
    schema_version: number;
    manifest_hash: string;
    artifacts: Record<string, unknown>[];
  };
  retention_summary: {
    artifact_count: number;
    retention_policies: Record<string, number>;
    contract_retention_days: number;
    export_requires_approval: boolean;
    delete_artifacts_on_contract_delete: boolean;
  };
  version_info?: Record<string, string>;
}

export interface RuntimeRunExportResponse extends RuntimeRunActionResponse {
  exported: boolean;
  export_approval?: {
    approved: boolean;
    reason: string;
    exported_by: string;
  };
  package: RuntimeRunExportPackage;
}

export interface RuntimeEffectResolutionResponse extends RuntimeRunActionResponse {
  resolved: boolean;
  idempotent?: boolean;
  effect_id: string;
  effect_revision: number;
  effect_status: string;
  applied_actions?: string[];
}

export interface RuntimeEffectRepairResponse extends RuntimeRunActionResponse {
  repaired: boolean;
  idempotent?: boolean;
  action: RuntimeEffectRepairAction;
  effect_id: string;
  effect_revision: number;
  effect_status: string;
  previous_revision?: number;
  checkpoint?: Record<string, unknown>;
}

export interface RuntimeRunContinueRequest extends RuntimeRunControlRequest {
  mode?: RuntimeContinueMode | null;
  after_seq?: number;
  stage_label?: string | null;
}

export interface RuntimeRunDeleteRequest extends RuntimeRunControlRequest {
  approved: boolean;
  reason: string;
  delete_artifacts?: boolean;
}

export interface PreviewHarnessFinding {
  severity: "critical" | "major" | "minor" | string;
  category: string;
  message: string;
  evidence?: string;
  url?: string | null;
}

export interface PreviewNetworkError {
  url: string;
  status?: number | null;
  error?: string;
}

export interface PreviewHarnessReport {
  url: string;
  fetch_url: string;
  ok: boolean;
  summary: string;
  status_code?: number | null;
  content_type?: string;
  duration_ms?: number;
  body_bytes?: number;
  body_text_length?: number;
  root_selector_present?: boolean;
  white_screen_likely?: boolean;
  browser_engine?: string;
  screenshot_path?: string | null;
  console_errors?: string[];
  page_errors?: string[];
  network_errors?: PreviewNetworkError[];
  findings?: PreviewHarnessFinding[];
}

export interface RuntimePreviewInspectRequest {
  url: string;
  run_id?: string | null;
  chat_id?: string | null;
  session_id?: string | null;
  workspace_dir?: string | null;
  stage_label?: string | null;
  record_event?: boolean;
}
