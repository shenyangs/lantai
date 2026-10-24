export interface MdFileInfo {
  filename: string;
  path: string;
  size: number;
  created_time: string;
  modified_time: string;
}

export interface MdFileContent {
  content: string;
}

export interface MarkdownFile extends MdFileInfo {
  updated_at: number;
  enabled?: boolean;
}

export interface WikiPageSummary extends MarkdownFile {
  title: string;
  summary: string;
  links: string[];
  backlinks: string[];
  sources: string[];
  page_type: string;
  category: string;
}

export interface WikiPageDetail extends WikiPageSummary {
  content: string;
}

export interface WikiSourceInfo extends MarkdownFile {
  kind: "markdown" | "data" | "web" | "code" | "text" | "binary" | string;
  extension: string;
  sha256: string;
  original_path: string;
  linked_pages: string[];
  status: string;
  extraction_status?: string;
  extraction_method?: string;
  extracted_chars?: number;
  extraction_message?: string;
  page_count?: number;
  layout_block_count?: number;
  understanding_summary?: string;
  layout_asset?: string;
  semantic_status?: string;
  semantic_method?: string;
  semantic_model?: string;
  semantic_visual_type?: string;
  semantic_structure_kind?: string;
  semantic_summary?: string;
  semantic_message?: string;
  display_title?: string;
  collection_ids?: string[];
}

export interface WikiCollection {
  id: string;
  name: string;
  description: string;
  source_paths: string[];
  created_at: string;
  updated_at: string;
}

export interface WikiSourceContent {
  path: string;
  title: string;
  kind: string;
  content: string;
  content_status: string;
  message: string;
  page_count: number;
  linked_pages: string[];
  source_revision?: string;
  source_text_hash?: string;
  source_excerpt?: string;
  source_start?: number | null;
  source_end?: number | null;
}

export interface WikiArchivedSourceInfo {
  id: string;
  source_path: string;
  display_title: string;
  archived_at: string;
  archived_pages: string[];
}

export interface WikiTrashItem {
  id: string;
  kind: "source" | "page";
  title: string;
  original_path: string;
  archived_at: string;
  related_count: number;
}

export interface WikiSourceImportDetail {
  filename: string;
  path: string;
  kind: string;
  status: "imported" | "skipped" | "failed" | string;
  page?: string | null;
  extraction_status?: string;
  extraction_method?: string;
  extracted_chars?: number;
  message?: string;
  page_count?: number;
  layout_block_count?: number;
  understanding_summary?: string;
  semantic_status?: string;
  semantic_method?: string;
  semantic_model?: string;
  semantic_visual_type?: string;
  semantic_structure_kind?: string;
  semantic_summary?: string;
  semantic_message?: string;
}

export interface WikiGraphNode {
  id: string;
  label: string;
  type: "page" | "source" | string;
  degree?: number;
}

export interface WikiGraphEdge {
  source: string;
  target: string;
  type: "link" | "source" | string;
}

export interface WikiIndexResponse {
  pages: WikiPageSummary[];
  sources: WikiSourceInfo[];
  graph: {
    nodes: WikiGraphNode[];
    edges: WikiGraphEdge[];
  };
  stats: Record<string, number>;
  overview?: WikiOverview | null;
  queue_summary: WikiQueueSummary;
  collections: WikiCollection[];
}

export interface WikiQueueSummary {
  pending: number;
  running: number;
  finished: number;
  failed: number;
  cancelled: number;
  total: number;
  latest: Record<string, unknown>[];
}

export interface WikiOverview {
  path: string;
  content: string;
  updated_at?: string | null;
}

export interface WikiPurposeResponse extends MarkdownFile {
  content: string;
}

export interface WikiSearchHit {
  kind: "page" | "source" | string;
  filename: string;
  title: string;
  path: string;
  snippet: string;
  score: number;
  match_type?: "keyword" | "graph" | "source" | string;
  citations?: string[];
  source_revision?: string;
  source_text_hash?: string;
  source_start?: number | null;
  source_end?: number | null;
  position_unit?: "unicode_codepoint" | string;
}

export interface WikiSearchResponse {
  query: string;
  hits: WikiSearchHit[];
}

export interface WikiLintIssue {
  severity: "error" | "warning" | "info" | string;
  code: string;
  title: string;
  message: string;
  file: string;
}

export interface WikiLintResponse {
  issues: WikiLintIssue[];
  stats: Record<string, number>;
}

export interface WikiInsight {
  kind: string;
  severity: "high" | "medium" | "info" | string;
  title: string;
  message: string;
  targets: string[];
}

export interface WikiInsightsResponse {
  insights: WikiInsight[];
  stats: Record<string, number>;
}

export interface WikiSchemaHeading {
  level: number;
  title: string;
}

export interface WikiSchemaPage {
  filename: string;
  title: string;
  headings: WikiSchemaHeading[];
  frontmatter: Record<string, string>;
  links: string[];
  sources: string[];
}

export interface WikiSchemaField {
  name: string;
  pages: string[];
  examples: string[];
}

export interface WikiSchemaResponse {
  pages: WikiSchemaPage[];
  fields: WikiSchemaField[];
  stats: Record<string, number>;
}

export interface WikiReviewItem {
  id: string;
  status: string;
  action: string;
  title: string;
  message: string;
  targets: string[];
  created_at: string;
  resolved_at?: string | null;
}

export interface WikiReviewResponse {
  items: WikiReviewItem[];
  stats: Record<string, number>;
}

export interface WikiHomeResponse {
  wiki: WikiIndexResponse;
  purpose: WikiPurposeResponse;
  lint: WikiLintResponse;
  insights: WikiInsightsResponse;
  review: WikiReviewResponse;
  structure: WikiSchemaResponse;
}

export interface WikiQueryCitation {
  number: number;
  kind: string;
  title: string;
  path: string;
  snippet: string;
  score: number;
}

export interface WikiQueryStructuredAsset {
  number: number;
  type: string;
  title: string;
  path: string;
  source_path?: string;
  summary?: string;
  headers?: string[];
  rows?: string[][];
  sheet_name?: string;
  header_row?: number;
  row_numbers?: number[];
  cell_ranges?: string[];
  row_count?: number;
  column_count?: number;
  relations?: string[];
  chart_points?: Record<string, unknown>[];
  match_score?: number;
}

export interface WikiQueryResponse {
  query: string;
  citations: WikiQueryCitation[];
  structured_assets?: WikiQueryStructuredAsset[];
  context_summary: string;
}

export interface DailyMemoryFile extends MdFileInfo {
  date: string;
  updated_at: number;
}

export type UserMemoryType = "identity" | "workflow" | "style" | "instruction";

export type UserMemorySource = "auto" | "manual" | "agent";

export type UserMemoryStatus = "active" | "archived" | "conflict";

export type UserMemoryLayer =
  | "trace"
  | "fact"
  | "profile"
  | "workflow"
  | "principle"
  | "intention"
  | string;

export type UserMemoryEvolutionStatus =
  | "current"
  | "superseded"
  | "conflicted"
  | "archived"
  | string;

export type UserMemorySourceKind =
  | "chat"
  | "file"
  | "manual"
  | "agent"
  | "import"
  | "system"
  | string;

export interface UserMemoryEntry {
  id: string;
  type: UserMemoryType;
  content: string;
  source: UserMemorySource;
  status: UserMemoryStatus;
  confidence: number;
  reason: string;
  created_at: string;
  updated_at: string;
  source_session_id?: string;
  source_message_id?: string;
  source_kind?: UserMemorySourceKind;
  source_title?: string;
  related_ids?: string[];
  evidence?: string;
  archived_at?: string;
  memory_layer?: UserMemoryLayer;
  chain_id?: string;
  supersedes_id?: string;
  superseded_by_id?: string;
  evolution_status?: UserMemoryEvolutionStatus;
  evolution_reason?: string;
  valid_from?: string;
  valid_to?: string;
  confirmed?: boolean;
  canonical_fact_id?: string;
  subject?: "user" | "project" | "third_party" | "unknown";
  canonical_atom_ids?: string[];
  scope?: Extract<MemoryFactScope, "thread" | "project" | "global">;
  scope_id?: string;
}

export type MemoryFactStatus =
  | "observed"
  | "pending"
  | "confirmed"
  | "conflict"
  | "rejected"
  | "superseded"
  | "expired"
  | "forgotten";

export type MemoryFactScope = "turn" | "thread" | "project" | "global";

export interface MemoryFact {
  id: string;
  content: string;
  memory_type: UserMemoryType;
  status: MemoryFactStatus;
  event_ids: string[];
  evidence_refs?: MemoryFactSource[];
  support_status?: string;
  support_reason?: string;
  support_model?: string;
  support_reviewed_at?: string;
  confidence: number;
  scope: MemoryFactScope;
  scope_id: string;
  subject: "user" | "assistant" | "project" | "third_party" | "unknown";
  utility_score: number;
  decision_reason: string;
  sensitivity: "normal" | "personal" | "sensitive";
  session_ids: string[];
  occurrence_count: number;
  legacy_entry_ids: string[];
  atom_ids: string[];
  dream_score?: number;
  dream_run_id?: string;
  dream_reason?: string;
  dream_source_event_ids?: string[];
  last_verified_at?: string;
  created_at: string;
  updated_at: string;
}

export interface MemoryFactSource {
  event_id: string;
  source_message_id: string;
  session_id: string;
  source_role: string;
  content_hash: string;
  quote: string;
  start_codepoint: number;
  end_codepoint: number;
  status: "located" | "source_only" | "missing" | "version_mismatch";
  support_status?: string;
  content?: string;
  created_at?: string;
  source_created_at?: string;
  before?: string;
  after?: string;
}

export interface MemoryFactSourcesResponse {
  found: boolean;
  fact_id: string;
  fact?: MemoryFact;
  sources: MemoryFactSource[];
}

export interface MemoryFactSnapshot {
  facts: MemoryFact[];
  stats: Record<string, number>;
}

export interface MemoryRecallTrace {
  query: string;
  updated_at: string;
  status?: "ready" | "empty" | "unavailable" | "error";
  reason?: "" | "no_record" | "read_failed";
  items: Array<{
    source: string;
    citation: string;
    content: string;
    score: number;
  }>;
}

export interface MemoryClearCounts {
  structured_entries: number;
  canonical_facts: number;
  canonical_events: number;
  layered_atoms: number;
  layered_conversations: number;
  scenes: number;
  system2_models: number;
  markdown_files: number;
  import_batches: number;
  dreaming_runs: number;
  user_understanding_atoms: number;
  proactive_opportunities: number;
  backup_files: number;
}

export interface MemoryClearPreview {
  counts: MemoryClearCounts;
  active_items: number;
  backup_files: number;
  total_items: number;
  chat_history_preserved: boolean;
  runtime_cache_preserved: boolean;
  confirmation_text: string;
}

export interface MemoryClearResult {
  cleared: boolean;
  cleared_counts: MemoryClearCounts;
  cleared_items: number;
  backup_created: boolean;
  backup_dir: string;
  chat_history_preserved: boolean;
  runtime_cache_preserved: boolean;
  preferences: {
    runtime_enabled: boolean;
    capture_enabled: boolean;
  };
}

export interface MemoryDreamingCandidate {
  fact_id: string;
  content: string;
  memory_type: UserMemoryType;
  status: MemoryFactStatus;
  scope: MemoryFactScope;
  subject: string;
  sensitivity: "normal" | "personal" | "sensitive";
  score: number;
  signals: Record<string, number>;
  occurrence_count: number;
  session_count: number;
  evidence_count: number;
  grounded_evidence_count: number;
  grounded: boolean;
  eligible: boolean;
  staged?: boolean;
  reason: string;
  source_event_ids: string[];
}

export interface MemoryDreamingRun {
  run_id: string;
  status: "completed" | "skipped" | "error";
  reason?: string;
  trigger: "manual" | "scheduled" | string;
  shadow: boolean;
  started_at?: string;
  completed_at?: string;
  phases?: {
    light: string;
    rem: string;
    deep: string;
  };
  counts?: {
    reviewed: number;
    eligible: number;
    staged: number;
    excluded: number;
  };
  candidates?: MemoryDreamingCandidate[];
  mutations?: Array<Record<string, unknown>>;
  can_rollback?: boolean;
  rolled_back_at?: string;
}

export interface MemoryDreamingStatus {
  enabled: boolean;
  schedule: string;
  rem_enabled: boolean;
  min_score: number;
  scheduled?: boolean;
  latest_run: MemoryDreamingRun | null;
  runs: MemoryDreamingRun[];
  next_run_at: string;
  diary_path: string;
  diary_excerpt: string;
  safety: {
    confirmed_only_injection: boolean;
    assistant_and_tool_evidence_blocked: boolean;
    diary_is_promotion_source: boolean;
  };
}

export type MemoryImportBucket =
  | "recommended"
  | "review"
  | "duplicate"
  | "rejected";

export interface MemoryImportCandidate {
  id: string;
  type: UserMemoryType;
  type_label: string;
  content: string;
  bucket: MemoryImportBucket;
  reason: string;
  selectable: boolean;
  selected_by_default: boolean;
  confidence: number;
  evidence: string;
  scope: MemoryFactScope;
  sensitivity: "normal" | "personal" | "sensitive";
  related_entry_id?: string;
  related_content?: string;
}

export interface MemoryImportStats {
  recommended: number;
  review: number;
  duplicate: number;
  rejected: number;
}

export interface MemoryImportPrompt {
  prompt: string;
  max_characters: number;
  max_candidates: number;
  privacy_note: string;
}

export interface MemoryImportPreview {
  batch_id: string;
  provider: string;
  created_at: string;
  candidates: MemoryImportCandidate[];
  stats: MemoryImportStats;
  raw_stored: boolean;
}

export interface MemoryImportCommitResult {
  batch_id: string;
  provider: string;
  status: "committed";
  imported_count: number;
  skipped_count: number;
  imported: Array<{
    candidate_id: string;
    entry_id: string;
    fact_id: string;
    content: string;
    type: UserMemoryType;
  }>;
  skipped: Array<{ id: string; reason: string }>;
  committed_at: string;
  can_undo: boolean;
}

export interface RecentMemoryImport {
  batch: null | {
    batch_id: string;
    provider: string;
    status: "committed" | "undone";
    imported_count: number;
    committed_at: string;
    undone_at: string;
    can_undo: boolean;
  };
}

export interface UserMemorySnapshot {
  entries: UserMemoryEntry[];
  stats: {
    total: number;
    auto: number;
    manual: number;
    conflict: number;
    evolved?: number;
    by_type: Record<UserMemoryType, number>;
  };
  profile_summary: {
    summary: string;
    updated_at?: string | null;
    by_type?: Record<UserMemoryType, string[]>;
  };
  updated_at?: number | null;
  storage_path?: string;
}

export interface UserMemoryWriteResult {
  decision: "new" | "merge" | "skip" | "conflict" | "evolve";
  reason?: string;
  entry?: UserMemoryEntry | null;
  related?: UserMemoryEntry | null;
  evolution?: {
    chain_id?: string;
    supersedes_id?: string;
    reason?: string;
  };
}

export type UserUnderstandingKind =
  | "fact"
  | "preference"
  | "value"
  | "decision_pattern"
  | "tension"
  | "open_loop"
  | "prospective_intent"
  | "relationship_protocol"
  | "correction";

export type UserUnderstandingStatus =
  | "transient_signal"
  | "observed"
  | "working_hypothesis"
  | "confirmed"
  | "contradicted"
  | "superseded"
  | "expired";

export interface UserUnderstandingAtom {
  id: string;
  kind: UserUnderstandingKind;
  statement: string;
  status: UserUnderstandingStatus;
  confidence: number;
  evidence_ids: string[];
  scope: string;
  scope_id: string;
  sensitivity: string;
  expires_at: string;
  created_at: string;
  updated_at: string;
}

export interface UserUnderstandingSnapshot {
  confirmed: UserUnderstandingAtom[];
  hypotheses: UserUnderstandingAtom[];
  open_loops: UserUnderstandingAtom[];
  stats: {
    confirmed: number;
    hypotheses: number;
    open_loops: number;
    retired: number;
  };
}

export interface ProactiveOpportunity {
  id: string;
  kind: string;
  content: string;
  score: number;
  source_ids: string[];
  status: "queued" | "completed" | "dismissed" | "expired";
  created_at: string;
  updated_at: string;
  expires_at: string;
  cooldown_until: string;
  ignored_hits: number;
  presented_hits: number;
}

export interface ProactiveOpportunitySnapshot {
  opportunities: ProactiveOpportunity[];
  stats: Record<string, number>;
}

export interface LayeredMemoryAtom {
  metadata?: Record<string, unknown>;
  id: string;
  type: string;
  content: string;
  priority: number;
  confidence: number;
  scene_name: string;
  source_message_ids: string[];
  source_record_ids: string[];
  evidence: string;
  created_at: string;
  updated_at: string;
  status: string;
}

export interface LayeredMemoryScene {
  scene_name: string;
  title: string;
  path: string;
  atom_count: number;
  updated_at: string;
  sample: string[];
}

export interface LayeredMemoryOffloadEntry {
  node_id: string;
  tool_call_id: string;
  tool_name: string;
  summary: string;
  result_ref: string;
  bytes: number;
  created_at: string;
}

export interface MemoryEngineStructuredStatus {
  status: string;
  visible_entries: number;
  pending_entries: number;
  total_entries: number;
}

export interface MemoryEngineLayeredStatus {
  l0: number;
  l1: number;
  l2: number;
  system2: number;
}

export interface MemoryEngineVectorStatus {
  enabled: boolean;
  configured?: boolean;
  status: string;
  backend?: string;
  records?: number;
  sources?: number;
  stale_sources?: number;
  dimensions?: number;
  records_source?: string;
  probe?: { write: boolean; query: boolean; delete: boolean };
  error?: string | null;
}

export interface MemoryEngineEmbeddingStatus {
  enabled: boolean;
  configured?: boolean;
  status: string;
  provider?: string;
  model?: string;
  dimensions?: number;
  fingerprint?: string;
  error?: string | null;
}

export interface MemoryEngineLocalWorkerStatus {
  enabled: boolean;
  status: string;
  model?: string;
}

export interface MemoryEngineStatus {
  overall: "ready" | "partial" | "disabled" | "error";
  structured_memory?: MemoryEngineStructuredStatus;
  layered_memory?: MemoryEngineLayeredStatus;
  vector_index?: MemoryEngineVectorStatus;
  embedding?: MemoryEngineEmbeddingStatus;
  local_worker?: MemoryEngineLocalWorkerStatus;
}

export interface LayeredMemorySnapshot {
  stats: {
    l0_conversations: number;
    l1_atoms: number;
    l2_scenes: number;
    offload_entries: number;
    system2_models?: number;
    open_intentions?: number;
  };
  atoms: LayeredMemoryAtom[];
  scenes: LayeredMemoryScene[];
  persona: string;
  system2?: {
    updated_at?: string;
    markdown_path?: string;
    models?: Array<{
      key: string;
      title: string;
      summary: string;
      sample?: string[];
      evidence_count?: number;
      updated_at?: string;
    }>;
    open_intentions?: Array<{
      content: string;
      source_id?: string;
      updated_at?: string;
      confidence?: number;
    }>;
    evolution_chains?: Array<{
      chain_id?: string;
      current: string;
      history?: string[];
      reason?: string;
    }>;
    memory_layers?: Record<string, number>;
    source_counts?: Record<string, number>;
    reason?: string;
    digest?: string;
  };
  offload: LayeredMemoryOffloadEntry[];
  engine_status?: MemoryEngineStatus;
  storage_path: string;
  updated_at: string;
}

export interface MemoryPackReport {
  valid: boolean;
  schema_version: number;
  source_agent_id: string;
  created_at: string;
  categories: string[];
  file_count: number;
  total_bytes: number;
  dry_run: boolean;
  adopted: boolean;
  would_replace: string[];
  would_remove: string[];
  recovery_pack: string;
  warnings: string[];
  contains_original_sources: boolean;
  original_source_categories: string[];
  decision_conflicts: string[];
}
