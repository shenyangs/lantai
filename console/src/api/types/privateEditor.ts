import type { ChatSpec } from "./chat";

export type PrivateEditorProjectStatus = "active" | "read_only" | "deleted";
export type PrivateEditorRecordKind =
  | "author_intent"
  | "decision"
  | "open_question"
  | "protected_quote"
  | "material_note";
export type PrivateEditorRecordSource =
  | "user_explicit"
  | "model_inferred"
  | "system_observed";
export type PrivateEditorRecordStatus =
  | "proposed"
  | "accepted"
  | "rejected"
  | "superseded"
  | "open"
  | "resolved";

export interface PrivateEditorSettings {
  enabled: boolean;
  research_consent: boolean;
  revocation_epoch: number;
  updated_at: string;
}

export interface PrivateEditorRecordEntry {
  id: string;
  kind: PrivateEditorRecordKind;
  text: string;
  source: PrivateEditorRecordSource;
  source_ref: string;
  scope: "project";
  status: PrivateEditorRecordStatus;
  applicable_work_id?: string | null;
  applicable_digest?: string | null;
  supersedes: string[];
  created_at: string;
}

export interface PrivateEditorChangeGroup {
  id: string;
  target_anchor: string;
  before: string;
  after: string;
  observation: string;
  impact: string;
  rationale: string;
  evidence_refs: string[];
  costs: string[];
  depends_on: string[];
}

export interface PrivateEditorCandidate {
  id: string;
  base_work_id: string;
  base_digest: string;
  record_revision: number;
  revocation_epoch: number;
  change_groups: PrivateEditorChangeGroup[];
  status: "staged" | "adopted" | "revoked";
  current_draft_changed: false;
  created_at: string;
}

export type PrivateEditorAngleProposalStatus =
  | "proposed"
  | "deferred"
  | "rejected"
  | "developed"
  | "deleted"
  | "source_unavailable";

export interface PrivateEditorAngleProposal {
  id: string;
  source_refs: string[];
  key_idea: string;
  why_now: string;
  differs_from: string;
  next_action: string;
  open_questions: string[];
  generated_run_id: string;
  applicable_work_id: string;
  applicable_digest: string;
  status: PrivateEditorAngleProposalStatus;
  resolution_reason?: string | null;
  development_work_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface PrivateEditorAdoptionReceipt {
  operation_id: string;
  request_fingerprint: string;
  candidate_id: string;
  authorization_source: string;
  expected_base_work_id: string;
  expected_base_digest: string;
  expected_record_revision: number;
  revocation_epoch: number;
  selected_change_groups: string[];
  status:
    | "prepared"
    | "artifact_committed"
    | "project_committed"
    | "acknowledged"
    | "cancelled";
  resulting_work_id?: string | null;
  resulting_digest?: string | null;
  created_at: string;
  updated_at: string;
}

export interface PrivateEditorHistoryEntry {
  operation_id: string;
  from_work_id: string;
  from_digest: string;
  to_work_id: string;
  to_digest: string;
  action: "adoption" | "revert";
  created_at: string;
}

export interface PrivateEditorRevertReceipt {
  operation_id: string;
  request_fingerprint: string;
  authorization_source: string;
  from_work_id: string;
  from_digest: string;
  target_work_id: string;
  target_digest: string;
  status: "acknowledged";
  created_at: string;
}

export interface PrivateEditorProject {
  id: string;
  title: string;
  original_work_id: string;
  original_digest: string;
  current_work_id: string;
  current_digest: string;
  record_revision: number;
  revocation_epoch: number;
  status: PrivateEditorProjectStatus;
  chat_id?: string | null;
  working_copy_path?: string | null;
  working_copy_owned_by_experiment: boolean;
  import_scope: string;
  import_warnings: string[];
  invalidated_source_refs: string[];
  record_entries: PrivateEditorRecordEntry[];
  candidates: PrivateEditorCandidate[];
  angle_proposals: PrivateEditorAngleProposal[];
  adoption_receipts: PrivateEditorAdoptionReceipt[];
  revert_receipts: PrivateEditorRevertReceipt[];
  history: PrivateEditorHistoryEntry[];
  created_at: string;
  updated_at: string;
}

export interface PrivateEditorSession {
  project: PrivateEditorProject;
  chat: ChatSpec;
}

export interface UpdatePrivateEditorSettingsRequest {
  enabled?: boolean;
  research_consent?: boolean;
}

export interface AdoptPrivateEditorCandidateRequest {
  operation_id: string;
  candidate_id: string;
  expected_base_work_id: string;
  expected_base_digest: string;
  expected_record_revision: number;
  revocation_epoch: number;
  selected_change_groups: string[];
}

export interface CapturePrivateEditorWorkingCopyRequest {
  expected_base_work_id: string;
  expected_base_digest: string;
  expected_record_revision: number;
  expected_revocation_epoch: number;
}

export interface ResolvePrivateEditorRecordRequest {
  expected_current_work_id: string;
  expected_current_digest: string;
  expected_record_revision: number;
  expected_revocation_epoch: number;
  resolution: "accepted" | "rejected";
}

export interface ResolvePrivateEditorAngleProposalRequest {
  expected_current_work_id: string;
  expected_current_digest: string;
  expected_record_revision: number;
  expected_revocation_epoch: number;
  resolution: "continue" | "later" | "not_suitable" | "delete";
  reason?: string | null;
}

export interface InvalidatePrivateEditorSourceRequest {
  expected_current_work_id: string;
  expected_current_digest: string;
  expected_record_revision: number;
  expected_revocation_epoch: number;
  source_ref: string;
}

export interface RevertPrivateEditorProjectRequest {
  operation_id: string;
  expected_current_work_id: string;
  expected_current_digest: string;
  expected_record_revision: number;
  revocation_epoch: number;
  target_work_id: string;
}

export interface PrivateEditorCurrentBody {
  work_id: string;
  digest: string;
  body: string;
  import_scope: string;
  warnings: string[];
}

export interface PrivateEditorDeletionReceipt {
  deleted: true;
  saved_works_preserved: true;
  managed_working_copy_deleted: boolean;
  chat_transcript_preserved: true;
  provider_copies_out_of_scope: true;
  local_backup_copies_out_of_scope: true;
}
