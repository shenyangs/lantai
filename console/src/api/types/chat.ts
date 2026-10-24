export type ChatStatus = "idle" | "running" | "stalled" | string;

export interface QuquContinuationRequest {
  request_id: string;
  chat_id: string;
  session_id: string;
  user_id: string;
  channel: string;
  source_message_id: string;
  source_message_digest: string;
  ququ_ordinal: number;
  placement: "lead" | "echo";
  response_id?: string;
  user_text?: string;
  answer_text?: string;
  ququ_text: string;
  retry_failed?: boolean;
}

export interface QuquContinuationRecord {
  continuation_id: string;
  request_id: string;
  agent_id: string;
  workspace_scope_id: string;
  chat_id: string;
  session_id: string;
  user_id: string;
  channel: string;
  source_message_id: string;
  source_message_digest: string;
  source_ququ_id: string;
  source_continuation_id?: string;
  ququ_ordinal: number;
  placement: "lead" | "echo";
  text: string;
  status: "completed" | "generating" | "failed" | string;
  created_at: string;
  updated_at: string;
}

export interface QuquContinuationResponse extends QuquContinuationRecord {
  persisted: true;
  tools_used: false;
}

export interface ChatTurnEventPreviewEvent {
  id: string;
  ts: string;
  session_id?: string | null;
  run_id?: string | null;
  client_turn_id?: string | null;
  turn_id?: string | null;
  item_id?: string | null;
  seq?: number;
  type?: string | null;
  kind: string;
  status: string;
  label: string;
  detail: string;
  usage?: Record<string, unknown> | null;
  phase?: string | null;
  trace_title?: string | null;
  trace_detail?: string | null;
  duration_ms?: number | null;
  payload?: Record<string, unknown> | null;
}

export interface ChatTurnEventPreview {
  workspace_dir?: string | null;
  summary_line?: string | null;
  events: ChatTurnEventPreviewEvent[];
}

export interface ChatRuntimeState {
  status:
    | "idle"
    | "running"
    | "stopping"
    | "completed"
    | "failed"
    | "interrupted"
    | "stalled"
    | string;
  run_id?: string | null;
  client_turn_id?: string | null;
  attempt_id?: string | null;
  outcome_status?:
    | "delivered"
    | "incomplete"
    | "needs_input"
    | "blocked"
    | "unknown"
    | string
    | null;
  delivery_status?:
    | "pending"
    | "verified"
    | "partial"
    | "not_delivered"
    | string
    | null;
  started_at?: string | null;
  updated_at?: string | null;
  summary?: string | null;
  events_preview?: ChatTurnEventPreviewEvent[];
  workspace_dir?: string | null;
  stream_epoch?: string | null;
  stream_cursor?: string | null;
  stream_latest_seq?: number | null;
  can_reconnect?: boolean;
  can_stop?: boolean;
  transport_status?:
    | "connecting"
    | "connected"
    | "reconnecting"
    | "closed"
    | string;
  transport_error?: string | null;
}

export type CollaborationMemberStatus =
  | "queued"
  | "running"
  | "retrying"
  | "waiting_resource"
  | "completed"
  | "failed"
  | "cancelled";

export interface CollaborationMemberState {
  member_id: string;
  agent_id: string;
  display_name: string;
  role: string;
  label: string;
  status: CollaborationMemberStatus;
  attempt: number;
  duration_ms: number;
  submission_ms?: number;
  remote_wait_ms?: number;
  error_code?: string | null;
  /** Authoritative child execution identity, available while work is active. */
  session_id?: string | null;
  task_id?: string | null;
  /** Stable identity owned by the execution-team contract across recovery. */
  execution_task_id?: string | null;
  run_id?: string | null;
  chat_id?: string | null;
  /** Upstream room members whose accepted results are injected before launch. */
  depends_on?: string[];
  /** Accepted public child answer; never contains private reasoning. */
  result?: string | null;
  result_truncated?: boolean;
  error_message?: string | null;
  /** Highest completed protocol round for this member. */
  round_completed?: number;
  /** Truthful second-round state; unavailable preserves the accepted round one. */
  review_status?: "running" | "completed" | "unavailable" | string | null;
  /** Scoped execution fields are present only for execution-team members. */
  execution_mode?: "scoped_execute" | string;
  permission_profile?: "delegated_workspace" | string;
  workspace_scope?: string;
  allowed_tools?: string[];
  resource_claims?: string[];
  resource_leases?: Array<{
    resource: string;
    owner_id: string;
    generation: number;
    acquired_at_epoch_seconds: number;
    expires_at_epoch_seconds: number;
  }>;
  expected_artifacts?: Array<{
    path: string;
    media_type?: string;
    minimum_bytes?: number;
  }>;
  artifacts?: Array<{
    path: string;
    media_type: string;
    size_bytes: number;
    sha256: string;
  }>;
}

export type CollaborationPhase =
  | "preparing"
  | "staffing"
  | "working"
  | "reconciling"
  | "verifying"
  | "delivering"
  | "closing"
  | "closed";

export interface CollaborationHostState {
  agent_id: string;
  display_name: string;
  role: string;
  responsibilities?: string[];
}

export interface CollaborationRoomMessage {
  message_id: string;
  member_id: string;
  display_name: string;
  kind: string;
  safe_summary: string;
  base_revision: number;
  committed_revision: number;
}

export interface CollaborationHeldDraft {
  draft_id: string;
  member_id: string;
  display_name: string;
  kind: string;
  safe_summary: string;
  base_revision: number;
  current_revision: number;
  resolution: string;
}

export interface CollaborationConflict {
  conflict_id: string;
  safe_summary: string;
  member_ids: string[];
  status: "open" | "resolved" | "deferred" | string;
  resolution_summary: string;
}

export interface CollaborationHostDecision {
  decision_id: string;
  phase: CollaborationPhase | string;
  safe_summary: string;
  reason: string;
  revision: number;
}

export interface CollaborationClosureAssessment {
  outcome:
    | "ready_for_synthesis"
    | "partial"
    | "waiting_approval"
    | "failed"
    | "cancelled"
    | string;
  deliverable_ready: boolean;
  synthesis_ready?: boolean;
  user_action_required: boolean;
  completed_members: number;
  failed_members: number;
  cancelled_members: number;
  open_conflicts: number;
  acceptance_met: string[];
  acceptance_unmet: string[];
  summary: string;
}

export interface CollaborationExecutionScope {
  root_run_id?: string | null;
  parent_run_id?: string | null;
  parent_attempt_id?: string | null;
  parent_effect_id?: string | null;
  /** Runtime control identity for pause, resume, stop, and chat navigation. */
  attempt_id?: string | null;
  runtime_generation?: number | null;
  chat_id?: string | null;
  session_id?: string | null;
  workspace_dir?: string | null;
}

export interface CollaborationExecutionBudget {
  max_member_attempts: number;
  max_wall_seconds: number;
  reserved_host_seconds: number;
  max_artifact_bytes: number;
  max_member_response_tokens: number;
}

export interface CollaborationExecutionBudgetUsage {
  member_attempts_started: number;
  verified_artifact_bytes: number;
  elapsed_wall_seconds: number;
  started_at_epoch_seconds?: number;
}

export interface CollaborationBatchSnapshot {
  schema_version?: number;
  batch_id: string;
  revision: number;
  conversation_revision?: number;
  status: "running" | "completed" | "partial" | "failed" | "cancelled" | string;
  phase?: CollaborationPhase | string;
  current_round?: number;
  participation_mode?: "autonomous" | "checkpoint" | "interactive" | string;
  protocol?: "parallel_consultation" | "roundtable" | string;
  host?: CollaborationHostState;
  goal?: string;
  why_parallel?: string;
  acceptance_criteria?: string[];
  execution_scope?: CollaborationExecutionScope;
  budget?: CollaborationExecutionBudget;
  budget_usage?: CollaborationExecutionBudgetUsage;
  quiet_summary?: string;
  user_action_required?: boolean;
  unattended?: boolean;
  members: CollaborationMemberState[];
  messages?: CollaborationRoomMessage[];
  held_drafts?: CollaborationHeldDraft[];
  conflicts?: CollaborationConflict[];
  host_decisions?: CollaborationHostDecision[];
  closure?: CollaborationClosureAssessment | null;
  controls?: {
    can_takeover: boolean;
    can_stop: boolean;
    can_pause: boolean;
  };
}

export interface ToolPresentation {
  visibility: "visible" | "hidden" | "quiet" | "prominent" | string;
  surface: "tool" | "subagent" | "approval" | "service" | "debug" | string;
  risk_level: "low" | "medium" | "high" | string;
  display_name: string;
  summary: string;
}

export interface ChatSpec {
  id: string; // Chat UUID identifier
  session_id: string; // Session identifier (channel:user_id format)
  user_id: string; // User identifier
  channel: string; // Channel name, default: "default"
  name?: string; // Chat display name
  created_at: string | null; // Chat creation timestamp (ISO 8601)
  updated_at: string | null; // Chat last update timestamp (ISO 8601)
  meta?: Record<string, unknown>; // Additional metadata
  status?: ChatStatus; // Conversation status: idle or running
  pinned?: boolean; // Whether the chat is pinned to the top
  archived?: boolean; // Whether the chat is hidden from the active list
}

export interface ChatActivity {
  id: string;
  name: string;
  status: "running";
  kind?: "cron";
}

export interface ChatSearchHit {
  id: string;
  kind: "title" | "message";
  chat_id: string;
  chat_name: string;
  message_id?: string | null;
  role: string;
  excerpt: string;
  timestamp?: string | null;
  archived?: boolean;
  coding?: boolean;
}

export interface Message {
  role: string;
  content: unknown;
  created_at?: string | number | null;
  [key: string]: unknown;
}

export interface ChatHistory {
  messages: Message[];
  ququ_continuations?: QuquContinuationRecord[];
  status?: ChatStatus; // Conversation status: idle or running
  chat_id?: string | null; // Canonical backend chat UUID
  session_id?: string | null; // Runtime session id used by the backend agent
  name?: string | null;
  user_id?: string | null;
  channel?: string | null;
  meta?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
  pinned?: boolean;
  archived?: boolean;
  pending_user_message?: string | null; // Active turn user text not yet in memory
  active_run?: {
    run_key?: string;
    status?: ChatStatus | string;
    generation?: number;
    attempt_id?: string;
    root_session_id?: string | null;
    started_at?: string | null;
    event_count?: number;
    events?: string[];
    [key: string]: unknown;
  } | null;
  turn_event_preview?: ChatTurnEventPreview | null;
  runtime_state?: ChatRuntimeState | null;
}

export interface ChatBranchRequest {
  user_message_id: string;
  name?: string;
}

export interface ChatContextStats {
  total_messages: number;
  compressed_summary_tokens: number;
  messages_tokens: number;
  estimated_tokens: number;
  max_input_length: number;
  context_usage_ratio: number;
  /** Agent setting, calculated provider/runtime cap, and provider-reported cap. */
  requested_window?: number | null;
  model_window?: number | null;
  effective_window?: number | null;
  actual_window?: number | null;
  context_profile_status?: "verified" | "unverified" | string;
  context_profile?: Record<string, unknown>;
  context_policy?: {
    mode: "smart" | "max" | "economy" | string;
    hard_window: number;
    usable_input_tokens: number;
    target_input_tokens: number;
    output_reserve: number;
    safety_margin: number;
    used_unknown_fallback: boolean;
  };
  categories?: Array<{
    key: string;
    estimated_tokens: number;
    upper_bound_tokens: number;
    method: "exact" | "estimated" | string;
    selected: boolean;
    share: number;
  }>;
  count_source?: "provider" | "manifest_estimate" | "memory_estimate" | string;
  compaction?: {
    active: boolean;
    summary_tokens: number;
    budget_discarded: number;
    redundant_discarded: number;
    provider_recovery: boolean;
    reason_codes: string[];
    explanation: string;
  };
}

export interface RuntimeEventUsage {
  input_tokens?: number;
  output_tokens?: number;
  cache_tokens?: number;
  context_tokens?: number;
  cost_points?: number;
  [key: string]: unknown;
}

export type RuntimeEventType =
  | "turn.started"
  | "turn.continue_requested"
  | "message.started"
  | "message.delta"
  | "message.completed"
  | "tool.call.started"
  | "tool.call.delta"
  | "tool.call.completed"
  | "tool.started"
  | "tool.completed"
  | "approval.requested"
  | "approval.resolved"
  | "memory.updated"
  | "plan.updated"
  | "model.routing_decision"
  | "runtime.notification"
  | "runtime.warning"
  | "runtime.error"
  | "turn.completed"
  | "turn.interrupted"
  | "turn.failed"
  | "turn.stalled"
  | "item.completed";

export type RuntimeEventStatus =
  | "queued"
  | "running"
  | "completed"
  | "finished"
  | "interrupted"
  | "failed"
  | "stalled"
  | "info";

export type RuntimeEventSource = "user" | "assistant" | "tool" | "system";

export interface RoutingDecisionPayload {
  route: "local" | "cloud" | string;
  provider?: string;
  model?: string;
  selected_provider?: string;
  selected_model?: string;
  reason?: string;
  reasons?: string[];
  mode?: string;
  complexity?: string;
  previous_route_inherited?: boolean;
  inherited_from_run_id?: string;
  estimated_context_tokens?: number;
  estimated_cost_points?: number;
  fallback_reason?: string;
}

export interface RuntimeFileChange {
  path?: string;
  added_lines?: number;
  deleted_lines?: number;
  summary?: string;
  needs_review?: boolean;
}

export interface RuntimeFailureDiagnosis {
  label: string;
  category: string;
  summary: string;
  next_action: string;
  evidence?: string;
  job_id?: string;
  recovery_action?: string;
  publish_ready?: boolean;
  requires_publish?: boolean;
  requires_human?: boolean;
}

export interface ToolPolicyDecisionPayload {
  decision?: string;
  reason?: string;
  profile?: string;
  matched_rule_id?: string | null;
  matched_rule_ids?: string[];
  matched_rules?: string[];
  risk?: string;
  risk_level?: string;
  requires_approval?: boolean;
  workspace_escape?: boolean;
  suggested_approval_message?: string | null;
  approval_copy?: string | null;
  permission_scope_suggestion?: string | null;
  rememberable?: boolean;
  distribution?: string;
  permission_mode?: string;
  command_category?: string | null;
  command_head?: string | null;
  decision_source?: string | null;
  effective_rule_id?: string | null;
  hard_denied?: boolean;
  command_pattern_suggestion?: string | null;
  path_pattern_suggestion?: string | null;
}

export interface RuntimeEvent {
  schema_version?: 1;
  id: string;
  session_id?: string | null;
  turn_id?: string | null;
  item_id?: string | null;
  seq: number;
  type: RuntimeEventType | string;
  status?: RuntimeEventStatus | string | null;
  source: RuntimeEventSource | string;
  presentation?: ToolPresentation | null;
  payload: Record<string, unknown>;
  usage?: RuntimeEventUsage | null;
  created_at: string;
}

export interface RuntimeEventsResponse {
  chat_id: string;
  workspace_dir?: string | null;
  stream_epoch?: string;
  cursor?: string | null;
  after_seq: number;
  earliest_seq?: number;
  ledger_latest_seq?: number;
  latest_seq: number;
  limit?: number | null;
  next_after_seq?: number | null;
  has_more?: boolean;
  resync_required?: boolean;
  resync_reason?: string | null;
  events: RuntimeEvent[];
}

export interface ChatTurnInterruptResponse {
  stopped: boolean;
  chat_id: string;
  turn_id: string;
}

export interface ChatTurnRetryResponse {
  queued: boolean;
  reason?: string;
  text?: string | null;
  chat_id: string;
  turn_id: string;
}

export interface ChatUpdateRequest {
  name?: string;
  pinned?: boolean;
  archived?: boolean;
}

export interface ChatWorkspaceRequest {
  research_project_id?: string;
  chat_id?: string;
  session_id: string;
  user_id?: string;
  channel?: string;
  name?: string;
  manual_workspace_dir?: string;
  reset_manual_workspace?: boolean;
  coding_mode?: boolean | string;
  coding_workspace_dir?: string;
  workspace_grant_id?: string;
  knowledge_source?: {
    path: string;
    filename: string;
    kind?: string;
  };
  knowledge_sources?: Array<{
    path: string;
    filename: string;
    kind?: string;
  }>;
  knowledge_scope_mode?: "exact" | "expanded";
}

export interface ChatWorkspaceResponse {
  chat: ChatSpec;
  topic_workspace_dir: string;
  manual_workspace_dir?: string | null;
  effective_workspace_dir: string;
  state_workspace_dir: string;
  project_workspace_dir?: string | null;
  workspace_grant_id?: string | null;
  workspace_access_mode?: "read_only" | "read_write" | null;
  workspace_trust_state?: "trusted" | "restricted" | null;
  workspace_reauthorization_required?: boolean;
}

export interface ChatDeleteResponse {
  success: boolean;
  chat_id: string;
}

// Legacy Session type alias for backward compatibility
export type Session = ChatSpec;
