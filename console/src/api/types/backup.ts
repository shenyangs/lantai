export interface BackupScope {
  include_agents: boolean;
  include_global_config: boolean;
  include_secrets: boolean;
  include_skill_pool: boolean;
  sanitized?: boolean;
}

export interface BackupMeta {
  id: string;
  name: string;
  description: string;
  created_at: string;
  version?: string;
  scope: BackupScope;
  agent_count: number;
}

export interface BackupDetail extends BackupMeta {
  workspace_stats: Record<
    string,
    { files: number; size: number; name?: string }
  >;
  integrity_status?: "verified" | "legacy_unverified";
  integrity_file_count?: number;
  integrity_total_bytes?: number;
}

export interface CreateBackupRequest {
  name: string;
  description?: string;
  scope: BackupScope;
  agents: string[];
}

export interface BackupEstimateComponent {
  label: string;
  files: number;
  bytes: number;
}

export interface BackupEstimate {
  files: number;
  bytes: number;
  components: BackupEstimateComponent[];
  missing_agents: string[];
}

export interface RestoreBackupRequest {
  operation_id?: string;
  include_agents: boolean;
  agent_ids: string[];
  include_global_config: boolean;
  include_secrets: boolean;
  include_skill_pool: boolean;
  default_workspace_dir?: string | null;
  mode?: "full" | "custom";
}

export interface RestoreOperationReceipt {
  operation_id: string;
  backup_id: string;
  state: "running" | "applied" | "rejected" | "unknown";
  code?: string;
  message?: string;
  created_at?: string;
  updated_at?: string;
}

/**
 * Determine if a backup is a full backup.
 * A full backup must have all of:
 * - include_agents is true
 * - include_global_config is true
 * - include_skill_pool is true
 * - include_secrets is true
 */
export function isFullBackup(scope: BackupScope): boolean {
  return (
    scope.include_agents === true &&
    scope.include_global_config === true &&
    scope.include_skill_pool === true &&
    scope.include_secrets === true &&
    scope.sanitized !== true
  );
}

export interface DeleteBackupsResponse {
  deleted: string[];
  failed: { id: string; reason: string }[];
}

export type BackupProgressEvent =
  | { type: "start"; total_agents: number; percent: 0 }
  | {
      type: "agent";
      agent_id: string;
      index: number;
      total: number;
      percent: number;
    }
  | { type: "saving"; percent: number }
  | {
      type: "done";
      meta: BackupMeta;
      integrity?: {
        status: "verified";
        file_count: number;
        total_bytes: number;
      };
      percent: 100;
    }
  | { type: "error"; message: string };

export interface BackupConflictResponse {
  detail: "backup_conflict";
  existing: BackupMeta;
  pending_token: string;
}
