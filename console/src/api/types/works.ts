export type WorkKind =
  | "file"
  | "image"
  | "html"
  | "markdown"
  | "pdf"
  | "presentation"
  | "document"
  | "spreadsheet"
  | "audio"
  | "video";

export interface WorkValidationCheck {
  id: string;
  outcome: "passed" | "failed" | "not_run" | "error" | "waived";
  required: boolean;
  detail: string;
  evidence_refs: string[];
}

export interface WorkValidation {
  schema_version: 1;
  record_id: string;
  status: "passed" | "failed" | "partial" | "not_run" | "error";
  source_run_id: string;
  source_attempt_id: string;
  work_id: string;
  artifact_id: string;
  artifact_version: number;
  artifact_digest: string | null;
  validator_id: string;
  validator_version: string;
  ruleset_id: string;
  ruleset_version: string;
  checked_at: string;
  required_scope: string[];
  observed_scope: string[];
  checks: WorkValidationCheck[];
  invalidation_rules: string[];
}

export interface WorkDerivation {
  schema_version: 1;
  operation: "office_conversion";
  target_format: "pdf";
  source_work_id: string;
  source_artifact_id: string;
  source_version: number;
  source_digest: string;
  output_digest: string;
  converter_output_digest: string;
  converter: string;
}

export interface WorkItem {
  id: string;
  artifact_id: string;
  title: string;
  kind: WorkKind;
  path: string;
  source_path: string;
  mime_type?: string | null;
  size?: number | null;
  source_chat_id?: string | null;
  source_run_id?: string | null;
  source_digest?: string | null;
  /** Canonical Runtime artifact role; absent means the role is not proven. */
  artifact_role?: "candidate" | "primary" | "delivery" | null;
  /** Exact Work id appears in canonical Runtime completion evidence. */
  completion_bound?: boolean;
  /** Delivery status applies only when completion_bound is true. */
  delivery_status?: string | null;
  /** Exact, versioned completion-gate facts for these immutable bytes. */
  validation?: WorkValidation | null;
  parent_work_id?: string | null;
  parent_digest?: string | null;
  lineage_root_id?: string | null;
  version: number;
  /** Explicit different-format provenance; never a same-Work revision edge. */
  derivation?: WorkDerivation | null;
  cover_path?: string | null;
  cover_mime_type?: string | null;
  cover_source?: "ai" | null;
  availability_status?:
    "available" | "missing" | "permission_denied" | "unknown";
  integrity_status?: "intact" | "modified" | "unknown";
  created_at: string;
  updated_at: string;
}

export interface AddWorkRequest {
  path: string;
  title?: string | null;
  source_chat_id?: string | null;
  source_run_id?: string | null;
}

export interface WorkDerivationRequest {
  target_format: "pdf";
  chat_id?: string | null;
}

export interface WorkPreviewPage {
  number: number;
  width: number;
  height: number;
}

export interface WorkPreview {
  work_id: string;
  source_digest: string;
  kind: "pdf" | "presentation";
  renderer: string;
  renderer_version: string;
  page_count: number;
  pages: WorkPreviewPage[];
}

export interface WorkTextPreview {
  work_id: string;
  source_digest: string;
  kind: "document" | "spreadsheet";
  method: string;
  content_type: string;
  content: string;
  truncated: boolean;
  message: string;
}

export interface WorkEditCopy {
  work_id: string;
  asset_id: string;
  edit_path: string;
  base_version: number;
  base_digest: string;
  lineage_root_id: string;
}

export interface WorksIndexBackup {
  digest: string;
  schema_version: number;
  work_count: number;
  created_at: string;
}

export interface WorksIndexInspection {
  status: "ready" | "uninitialized" | "recovery_required" | "unavailable";
  code?: string | null;
  diagnostic_id?: string | null;
  current_digest?: string | null;
  schema_version?: number | null;
  work_count: number;
  invalid_record_indexes: number[];
  managed_work_ids: string[];
  orphaned_managed_ids: string[];
  backups: WorksIndexBackup[];
}

export interface WorksIndexRecoveryPreview {
  current_digest: string;
  snapshot_digest: string;
  snapshot_schema_version: number;
  snapshot_works: WorkItem[];
  orphaned_managed_ids: string[];
  confirmation_required: boolean;
}

export interface WorksIndexRestoreResult {
  status: "restored" | "already_restored";
  restored_digest: string;
  quarantine_digest?: string | null;
  work_count: number;
}
