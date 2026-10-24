export type DailyBriefHomeState =
  | "not_configured"
  | "declined"
  | "generating"
  | "ready"
  | "no_update"
  | "partial"
  | "failed"
  | "not_run"
  | "paused"
  | "stale"
  | "hidden";

export interface DailyBriefHomeCard {
  title: "澜台简报" | string;
  status_line: string;
  hint: string;
  route: string;
  edition_id: string | null;
  edition_revision: number | null;
  edition_date: string | null;
  item_count: number | null;
  has_update: boolean;
  is_stale: boolean;
}

/** Metadata only. Full brief content must never be added to this shape. */
export interface DailyBriefHomeSnapshot {
  state: DailyBriefHomeState;
  visible: boolean;
  card: DailyBriefHomeCard;
}

export interface BriefBudget {
  max_searches: number;
  max_source_reads: number;
  max_model_tokens: number;
  max_cost_usd: number;
  max_cost_cny?: number;
}

export interface BriefGrantCreateRequest {
  explicit_consent: boolean;
  one_time: boolean;
  topic_ids: string[];
  public_source_discovery: boolean;
  public_source_read: boolean;
  cloud_processing: "off" | "public_only" | "approved_context";
  allowed_context_refs: string[];
  search_provider: string;
  model_provider: string;
  budget: BriefBudget;
  expires_at: string | null;
  action_id: string;
}

export interface BriefGrant {
  id: string;
  revision: number;
  status: "active" | "revoked" | "expired" | "consumed";
  one_time: boolean;
  topic_ids: string[];
  public_source_discovery: boolean;
  public_source_read: boolean;
  cloud_processing: "off" | "public_only" | "approved_context";
  allowed_context_refs: string[];
  search_provider: string;
  model_provider: string;
  model_policy?: "free_only" | "configured";
  budget: BriefBudget;
  granted_at: string;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface BriefActivationRequest {
  explicit_consent: boolean;
  configured_model_consent?: boolean;
  mode: "preview" | "daily";
  topic_ids: string[];
  use_memory_clues: boolean;
  memory_context_processing_consent: boolean;
  timezone: string;
  local_time: string;
  home_visible: boolean;
  native_notifications: boolean;
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  action_id: string;
}

export interface BriefActivationResult {
  grant: BriefGrant;
  topic_ids: string[];
  discovered_topic_ids: string[];
  subscription: BriefSubscription | null;
  cycle: BriefCycle | null;
  execution_state: "queued" | "scheduled";
}

export interface BriefSubscription {
  id: string;
  revision: number;
  status: "active" | "paused" | "archived";
  grant_id: string;
  grant_revision: number;
  topic_revisions: Record<string, number>;
  timezone: string;
  local_time: string;
  schedule_revision: number;
  home_visible: boolean;
  native_notifications: boolean;
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  last_successful_check_at: string | null;
  last_successful_local_date: string | null;
  created_at: string;
  updated_at: string;
}

export interface BriefSubscriptionCreateRequest {
  grant_id: string;
  topic_ids: string[];
  timezone: string;
  local_time: string;
  home_visible: boolean;
  native_notifications: boolean;
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  action_id: string;
}

export interface BriefSubscriptionUpdateRequest {
  expected_revision: number;
  status?: "active" | "paused" | "archived";
  timezone?: string;
  local_time?: string;
  home_visible?: boolean;
  native_notifications?: boolean;
  quiet_hours_start?: string | null;
  quiet_hours_end?: string | null;
  action_id: string;
}

export interface BriefCycle {
  id: string;
  logical_key: string;
  subscription_id: string | null;
  grant_id: string;
  grant_revision: number;
  topic_revisions: Record<string, number>;
  occurrence_id: string;
  local_date: string;
  schedule_revision: number;
  status:
    | "queued"
    | "running"
    | "succeeded"
    | "partial"
    | "failed"
    | "cancelled"
    | "invalidated";
  attempt_count: number;
  manual: boolean;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  failure_reason: string;
  edition_id: string | null;
}

export interface BriefCoverage {
  topic_id: string;
  status: "complete" | "partial" | "failed" | "skipped";
  checked_at: string | null;
  query_count: number;
  source_read_count: number;
  note: string;
}

export interface BriefEvidence {
  id: string;
  canonical_url: string;
  original_url: string;
  origin_group: string;
  source_kind: "primary" | "republished" | "secondary";
  publisher: string;
  title: string;
  published_at: string | null;
  event_at: string | null;
  fetched_at: string;
  read_level: "search_snippet" | "source_page" | "primary_document";
  evidence_text: string;
  content_hash: string;
  retracted: boolean;
}

export interface BriefItem {
  id: string;
  topic_id: string;
  title: string;
  fact: string;
  relevance: string;
  analysis: string;
  limitations: string;
  optional_action: string;
  evidence_ids: string[];
  private_dependency_refs: string[];
  related_work_refs: string[];
  explicit_work_association: boolean;
}

export interface BriefTrend {
  id: string;
  topic_id: string;
  revision: number;
  statement: string;
  support_evidence_ids: string[];
  counter_evidence_ids: string[];
  falsifiers: string[];
  uncertainty: string;
  supersedes_id: string | null;
  observed_at: string;
}

export interface BriefEdition {
  id: string;
  revision: number;
  cycle_id: string;
  subscription_id: string | null;
  grant_id: string;
  grant_revision: number;
  topic_revisions: Record<string, number>;
  status: "draft" | "published" | "withdrawn" | "superseded";
  outcome: "ready" | "no_material_update" | "partial";
  local_date: string;
  timezone: string;
  title: string;
  summary: string;
  coverage: BriefCoverage[];
  evidence: BriefEvidence[];
  items: BriefItem[];
  trends: BriefTrend[];
  private_dependency_refs: string[];
  checked_started_at: string;
  checked_finished_at: string;
  published_at: string | null;
  supersedes_id: string | null;
  correction_note: string;
  content_hash: string;
}

export interface BriefTopicProjection {
  id: string;
  title: string;
  description: string;
  enabled: boolean;
  revision: number;
  brief_status:
    | "unconfigured"
    | "proposed"
    | "confirmed"
    | "paused"
    | "archived";
  brief_purpose: string;
  brief_scope_refs: string[];
  brief_evidence_refs: string[];
  brief_exclusions: string[];
  brief_valid_until: string | null;
}

export interface BriefFeatureFlags {
  homepage_entry: boolean;
  research: boolean;
  native_notification: boolean;
}

export interface BriefWorkspaceSnapshot {
  grants: BriefGrant[];
  subscriptions: BriefSubscription[];
  latest_edition: BriefEdition | null;
  history: BriefEdition[];
  topics: BriefTopicProjection[];
  feature_flags: BriefFeatureFlags;
  invitation_hidden: boolean;
  cycles?: BriefCycleView[];
  runtime?: BriefRuntimeStatus;
  activation_state?:
    | "not_configured"
    | "topics_only"
    | "authorized"
    | "preview"
    | "scheduled"
    | "paused";
  history_total?: number;
  saved_items?: BriefSavedItem[];
  subscription_issues?: Record<string, string>;
}

export interface BriefCycleView {
  id: string;
  subscription_id: string | null;
  status: BriefCycle["status"];
  stage: string;
  local_date: string;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  supersedes_cycle_id: string | null;
  failure_code: string;
  message: string;
  retryable: boolean;
  usage: BriefBudget;
  usage_uncertain: boolean;
  cost_cny: number;
}

export interface BriefRuntimeStatus {
  enabled: boolean;
  running: boolean;
  message: string;
  last_tick_at: string | null;
}

export interface BriefModelCapability {
  ready: boolean;
  code: string;
  message: string;
  model_label?: string;
}

export interface BriefSavedItem {
  id: string;
  edition_id: string;
  edition_revision: number;
  item_id: string;
  saved_at: string;
}

export interface BriefFeedbackCreateRequest {
  kind: "useful" | "not_useful" | "incorrect" | "too_much" | "save";
  note?: string;
  action_id: string;
}

export interface BriefFeedback {
  id: string;
  edition_id: string;
  item_id: string | null;
  kind: BriefFeedbackCreateRequest["kind"];
  note: string;
  created_at: string;
}

export interface BriefDiscussionDraft {
  id: string;
  status: "draft" | "sent" | "invalidated";
  context_ref: {
    edition_id: string;
    edition_revision: number;
    item_id: string | null;
    evidence_ids: string[];
  };
  text: string;
  private_dependency_refs: string[];
  command_id: string | null;
  created_at: string;
  invalidated_at: string | null;
}

export interface BriefDiscussionDraftView extends BriefDiscussionDraft {
  composer_text: string;
}

export interface NativeBriefNotification {
  receipt_id: string;
  edition_id: string;
  edition_revision: number;
  title: string;
  body: string;
  open_route: "/daily-brief" | string;
}

export interface BriefDeliveryReceipt {
  id: string;
  edition_id: string;
  edition_revision: number;
  subscription_id: string;
  channel: "home" | "native";
  state:
    | "pending"
    | "submitted"
    | "unknown"
    | "acknowledged"
    | "read"
    | "failed"
    | "suppressed";
  attempt_count: number;
  adapter_message_id: string | null;
  last_attempt_at: string | null;
  acknowledged_at: string | null;
  read_at: string | null;
  error: string;
}
