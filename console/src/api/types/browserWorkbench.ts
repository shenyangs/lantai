export type BrowserAccess = "read" | "write" | "sensitive";
export type BrowserRuntime =
  | "electron_bridge"
  | "browser_skill"
  | "playwright_fallback";
export type BrowserRisk = "low" | "medium" | "high" | "critical";
export type BrowserDecision = "allow" | "prompt" | "block";
export type BrowserConfirmPolicy = "never" | "write" | "always";
export type CapabilityMatrixMode =
  | "enabled"
  | "read_only"
  | "interactive_only"
  | "disabled";
export type BrowserTaskStatus =
  | "queued"
  | "running_background"
  | "waiting_for_user_confirmation"
  | "manual_takeover_required"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";
export type BrowserTaskMode = "background_observable" | "foreground_observable";
export type BrowserAgentLoopMode =
  | "off"
  | "fixture"
  | "read_canary"
  | "interactive";
export type BrowserAgentBudgetProfile =
  | "quick"
  | "standard"
  | "long_read"
  | "draft_write";
export type BrowserAgentPhase =
  | "observing"
  | "planning"
  | "checking_policy"
  | "executing"
  | "verifying"
  | "recovering"
  | "waiting_approval"
  | "complete";

export interface BrowserAgentLoopConfig {
  enabled: boolean;
  mode: BrowserAgentLoopMode;
  budget_profile: BrowserAgentBudgetProfile;
  planner_strategy?: string;
  rollback?: string;
  schema_version?: number;
}

export interface BrowserTaskMetadata extends Record<string, unknown> {
  agent_loop?: BrowserAgentLoopConfig;
  agent_phase?: BrowserAgentPhase;
  agent_phase_details?: Record<string, unknown>;
  agent_budget?: Record<string, unknown>;
  agent_result?: Record<string, unknown>;
  budget?: Record<string, unknown>;
  plan?: Record<string, unknown>;
  takeover_count?: number;
}
export type ComputerActionType =
  | "screenshot_screen"
  | "list_windows"
  | "focus_window"
  | "click_at"
  | "type_text"
  | "press_key"
  | "drag";

export interface BrowserWorkbenchConfig {
  agent_control_enabled: boolean;
  agent_loop_enabled?: boolean;
  agent_loop_mode?: BrowserAgentLoopMode;
  agent_loop_budget_profile?: BrowserAgentBudgetProfile;
  default_profile_id: string;
  require_confirmation_for_write: boolean;
  enable_scheduled_browser_tasks: boolean;
  primary_runtime: BrowserRuntime;
  optional_engines_enabled: boolean;
  app_store_distribution?: boolean;
}

export interface BrowserSkillStatus {
  ok: boolean;
  installed: boolean;
  connected: boolean;
  ready: boolean;
  status:
    | "available"
    | "not_installed"
    | "extension_disconnected"
    | "unavailable";
  runtime: "browser_skill";
  message: string;
  error?: string;
  error_code?: string;
  daemon_version?: string;
  protocol_version?: string;
  browsers: Array<{
    instance_id: string;
    label?: string;
    browser_name?: string;
    browser_version?: string;
    extension_version?: string;
    session_count?: number;
    version_skew?: boolean;
  }>;
  sessions: Array<Record<string, unknown>>;
  repository_url: string;
  upstream_commit: string;
}

export interface CapabilityMatrixEntry {
  id: string;
  mode: CapabilityMatrixMode;
  reason_code: string;
  label: string;
  description?: string;
}

export interface BrowserProfile {
  id: string;
  name: string;
  description: string;
  allowed_domains: string[];
  agent_control_enabled: boolean;
  risk_policy: BrowserConfirmPolicy;
  created_at: string;
  updated_at: string;
  last_used_at?: string | null;
  user_data_dir: string;
  metadata: Record<string, unknown>;
}

export interface BrowserPolicyDecisionPayload {
  decision: BrowserDecision;
  reason: string;
  risk: BrowserRisk;
  access: BrowserAccess;
  requires_confirmation: boolean;
  matched_rule_id?: string | null;
}

export interface BrowserObservation {
  ok: boolean;
  action: string;
  runtime: BrowserRuntime;
  profile_id: string;
  page_id: string;
  url: string;
  message: string;
  error?: string | null;
  screenshot_path?: string | null;
  data: Record<string, unknown>;
  decision?: BrowserPolicyDecisionPayload | null;
  replay_event_id: string;
}

export interface BrowserFileGrantRequest {
  path: string;
  profile_id?: string;
  run_id?: string;
  page_id?: string;
  url?: string;
  selector?: string;
  ttl_seconds?: number;
  security_scoped_bookmark?: boolean;
  security_scope_ref?: string;
  security_scope_bookmark_sha256?: string;
}

export interface BrowserFileGrantResponse {
  ok: boolean;
  file_grant_id: string;
  grant_ref?: string;
  security_scope_ref?: string;
  security_scoped_bookmark?: boolean;
  security_scope_bookmark_sha256?: string;
  path_identity_hash?: string;
  path: string;
  profile_id: string;
  run_id: string;
  page_id: string;
  url: string;
  selector: string;
  expires_at: string;
}

export interface BrowserActionPayload {
  action: string;
  profile_id?: string;
  url?: string;
  page_id?: string;
  selector?: string;
  ref?: string;
  text?: string;
  code?: string;
  path?: string;
  access?: BrowserAccess;
  reason?: string;
  run_id?: string;
  confirmed?: boolean;
  runtime?: BrowserRuntime;
  metadata?: Record<string, unknown>;
}

export interface BrowserTaskRun {
  run_id: string;
  profile_id: string;
  target_domains: string[];
  access: BrowserAccess;
  mode: BrowserTaskMode;
  instruction: string;
  status: BrowserTaskStatus;
  created_at: string;
  updated_at: string;
  current_action: string;
  result: Record<string, unknown>;
  error_code: string;
  replay_run_id: string;
  planned_action_count: number;
  completed_action_count: number;
  requires_foreground_confirmation: boolean;
  pending_confirmation?: BrowserActionPayload | null;
  metadata: BrowserTaskMetadata;
}

export interface BrowserTakeoverReceipt {
  ready: boolean;
  run: BrowserTaskRun;
  error_code: string;
  message: string;
}

export interface BrowserTaskEvent {
  id: string;
  ts: string;
  run_id: string;
  type: string;
  status: BrowserTaskStatus;
  message: string;
  action: string;
  observation_id: string;
  metadata: Record<string, unknown>;
}

export interface BrowserTaskArtifacts {
  run_id: string;
  replay_run_id: string;
  event_count: number;
  task_events: BrowserTaskEvent[];
  replay_events: Record<string, unknown>[];
  screenshots: string[];
  observation_summaries: Record<string, unknown>[];
  preflights?: Array<{
    type: string;
    ok: boolean;
    action: string;
    domain_skill_id: string;
    matched_selector_count: number;
    selector_count: number;
    selectors: Array<Record<string, unknown>>;
  }>;
  recovery?: Record<string, unknown> | null;
  recoveries?: Record<string, unknown>[];
  domain_skill_candidate?: DomainSkillDraft | null;
  network: Record<string, unknown>[];
  console: Record<string, unknown>[];
}

export interface ComputerPolicyDecisionPayload {
  decision: BrowserDecision;
  reason: string;
  risk: BrowserRisk;
  requires_confirmation: boolean;
  matched_rule_id?: string | null;
}

export interface ComputerObservation {
  ok: boolean;
  action: ComputerActionType;
  run_id: string;
  message: string;
  error?: string | null;
  data: Record<string, unknown>;
  decision?: ComputerPolicyDecisionPayload | null;
  replay_event_id: string;
}

export interface BrowserReplayExport {
  ok: boolean;
  run_id: string;
  event_count: number;
  copied_evidence_count: number;
  bundle_dir: string;
  manifest_path: string;
  report_path: string;
  archive_path: string;
  export_approval?: BrowserExportApproval & { export_kind?: string };
}

export interface BrowserExportApproval {
  approved: boolean;
  reason: string;
  requested_by?: string;
  scope?: string;
  created_at?: string;
}

export interface DomainSkillDraft {
  domain: string;
  title: string;
  summary: string;
  url_patterns: string[];
  selectors: string[];
  wait_conditions: string[];
  failure_modes: string[];
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface DomainSkillDraftResponse {
  ok: boolean;
  draft: DomainSkillDraft;
  path: string;
}

export interface DomainSkillDraftRecord {
  id: string;
  path: string;
  draft: DomainSkillDraft;
  enabled: boolean;
  updated_at: string;
  source_run_id: string;
  recommended_adapter_id: string;
}

export interface DomainSkillDraftListResponse {
  items: DomainSkillDraftRecord[];
}

export interface LantaiCliAdapterManifest {
  id: string;
  domain: string;
  strategy: string;
  access: BrowserAccess;
  required_profile: boolean;
  inputs: string[];
  outputs: string[];
  confirm_policy: BrowserConfirmPolicy;
  description: string;
  metadata: Record<string, unknown>;
}

export interface LantaiCliAdapterVerifyResult {
  ok: boolean;
  adapter_id: string;
  manifest?: LantaiCliAdapterManifest;
  errors: string[];
  warnings: string[];
  planned_actions: Array<{
    action: string;
    access: BrowserAccess;
    url: string;
    selector: boolean;
    requires_confirmation: boolean;
  }>;
}

export interface ContentSource {
  id?: string;
  source_type:
    | "web_article"
    | "social_post"
    | "video_page"
    | "comment_thread"
    | "search_result"
    | "manual_note";
  title: string;
  url: string;
  domain: string;
  author?: string;
  published_at?: string | null;
  captured_at?: string;
  summary?: string;
  key_points?: string[];
  evidence_paths?: string[];
  metadata?: Record<string, unknown>;
}

export interface ContentSourceDetail {
  ok: boolean;
  source: ContentSource;
  evidence: {
    evidence_paths: string[];
    evidence_count: number;
    assets: {
      index: number;
      path: string;
      file_name: string;
      exists: boolean;
      is_image: boolean;
      preview_url: string;
      reason: string;
    }[];
    actions: string[];
    page_context: Record<string, unknown>;
    adapter_candidate: string;
    source_replay_run_id: string;
  };
  related_dossiers: Record<string, unknown>[];
  related_workflows: Record<string, unknown>[];
  safety: {
    contains_secret_material: boolean;
    source_is_local_artifact: boolean;
    browser_action_required: boolean;
  };
}

export interface WechatSeriesPrepareResult {
  workflow_run: Record<string, unknown>;
  series_plan: Record<string, unknown>;
  dossier: Record<string, unknown>;
  drafts: Record<string, unknown>[];
  queue_items: Record<string, unknown>[];
  export_paths: {
    dossier?: string;
    drafts?: string[];
  };
  safety: {
    platform_write_requires_confirmation: boolean;
    publish_requires_confirmation: boolean;
    silent_publish_enabled: boolean;
    scheduled_browser_write_enabled?: boolean;
  };
}

export interface ReplayResearchPackResult {
  ok: boolean;
  run_id: string;
  visited_urls?: string[];
  observations?: BrowserObservation[];
  ok_observation_count?: number;
  blocked_observation_count?: number;
  sources: ContentSource[];
  dossier?: Record<string, unknown> | null;
  workflow_run?: Record<string, unknown> | null;
  export_paths: {
    dossier?: string;
  };
  safety?: {
    read_only?: boolean;
    broker_enforced?: boolean;
    silent_publish_enabled?: boolean;
  };
  message?: string;
}

export interface WechatDraftWriteResponse {
  ok: boolean;
  run_id: string;
  requires_confirmation: boolean;
  confirmation?: Record<string, unknown> | null;
  requires_manual_takeover?: boolean;
  safety: {
    publish_requires_confirmation: boolean;
    silent_publish_enabled: boolean;
    save_after_fill: boolean;
    manual_takeover_required?: boolean;
  };
  draft: Record<string, unknown>;
  observations: BrowserObservation[];
}

export interface WechatEditorProbeResponse {
  ok: boolean;
  run_id: string;
  requires_manual_takeover: boolean;
  probe: Record<string, unknown>;
  safety: {
    read_only: boolean;
    no_write_actions: boolean;
    publish_requires_confirmation: boolean;
    cover_change_requires_confirmation: boolean;
    original_declaration_requires_confirmation: boolean;
    comment_policy_requires_confirmation: boolean;
    silent_publish_enabled: boolean;
  };
  observation: BrowserObservation;
}

export interface PublishingQueueDetail {
  ok: boolean;
  item: Record<string, unknown>;
  draft?: Record<string, unknown> | null;
  dossier?: Record<string, unknown> | null;
  preview: {
    missing?: boolean;
    title?: string;
    summary?: string;
    body_excerpt?: string;
    body_length?: number;
    body_line_count?: number;
    platform?: string;
    status?: string;
  };
  diff: {
    draft_missing?: boolean;
    changed_fields?: string[];
    title_changed?: boolean;
    summary_changed?: boolean;
    body_changed?: boolean;
    status_changed?: boolean;
    body_length_delta?: number;
    queued_body_length?: number;
    current_body_length?: number;
  };
  history: Record<string, unknown>[];
  schedule?: {
    scheduled_for: string;
    due_status: string;
    is_due: boolean;
    minutes_until?: number | null;
    label: string;
    auto_browser_write_allowed: boolean;
    requires_manual_write_confirmation: boolean;
  };
  safety: {
    queue_confirmation_required: boolean;
    platform_write_requires_confirmation: boolean;
    publish_requires_confirmation: boolean;
    silent_publish_enabled: boolean;
    scheduled_browser_write_enabled?: boolean;
  };
}

export interface BrowserWorkbenchStateTab {
  id: string;
  profile_id: string;
  title: string;
  url: string;
  loading: boolean;
  can_go_back?: boolean;
  can_go_forward?: boolean;
}

export interface BrowserWorkbenchState {
  ok: boolean;
  bridge_url?: string;
  active_tab_id?: string | null;
  profile_id?: string;
  tabs: BrowserWorkbenchStateTab[];
  current_action?: string;
  agent_status?: string;
  message?: string;
  error?: string;
}

export type BrowserBridgeHealthStatus =
  | "available"
  | "unauthorized"
  | "disconnected"
  | "unsupported"
  | "app_store_blocked";

export interface BrowserBridgeHealth {
  status: BrowserBridgeHealthStatus;
  message: string;
  runtime: string;
  error_code: string;
}

export interface BrowserCapabilityFlags {
  can_open: boolean;
  can_read: boolean;
  can_write: boolean;
  can_run_background: boolean;
  can_publish: boolean;
  can_generate_domain_skill: boolean;
  advanced_features_default_open: boolean;
}

export interface BrowserWorkbenchAggregateState {
  config: BrowserWorkbenchConfig;
  profiles: BrowserProfile[];
  bridge_health: BrowserBridgeHealth;
  recent_tasks: BrowserTaskRun[];
  active_task?: BrowserTaskRun | null;
  adapter_count: number;
  domain_skill_draft_count: number;
  content_summary: Record<string, number>;
  capability_flags: BrowserCapabilityFlags;
  capability_matrix?: Record<string, CapabilityMatrixEntry>;
}
