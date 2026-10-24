/**
 * Scope utilities for the Backups feature.
 * Centralises scope construction logic so CreateBackupModal, SilentBackupModal,
 * and any future callers share a single source of truth.
 */
import type { BackupScope } from "@/api/types/backup";

/** Re-export so consumers inside this feature don't need the full api path. */
export { isFullBackup } from "@/api/types/backup";

/** Returns the initial ScopeFormValue shown when the user opens CreateBackupModal. */
export function defaultCreateScope(agentIds: string[]): {
  backupMode: "full" | "partial";
  selectedAgents: string[];
  globalConfig: boolean;
  includeSkillPool: boolean;
  includeSecrets: boolean;
} {
  return {
    backupMode: "partial",
    selectedAgents: agentIds,
    globalConfig: true,
    includeSkillPool: true,
    includeSecrets: false,
  };
}

/**
 * Builds the scope and agents list used for the automatic pre-restore backup.
 * Always creates a complete local disaster backup, including secrets, so a
 * destructive full restore can actually return to the current state.
 *
 * @param allAgentIds - All currently known agent IDs (must be the explicit list).
 */
export function buildPreRestoreScope(allAgentIds: string[]): {
  name: string;
  description: string;
  scope: BackupScope;
  agents: string[];
} {
  const timestamp = new Date()
    .toISOString()
    .replace(/[:.]/g, "-")
    .slice(0, 19)
    .replace("T", " ");
  return {
    name: `[pre-restore] Backup ${timestamp}`,
    description: `backup.preRestoreBackupDesc`, // resolved by caller via t()
    scope: {
      include_agents: true,
      include_global_config: true,
      include_secrets: true,
      include_skill_pool: true,
      sanitized: false,
    },
    agents: allAgentIds,
  };
}

/**
 * Converts the flat ScopeFormValue (from BackupScopeForm) into the
 * BackupScope + agents pair expected by the API. Full-mode overrides individual
 * toggles so everything is always included.
 *
 * Returns `{ scope, agents }` where `agents` is the explicit ID list to send
 * as the top-level `agents` field in CreateBackupRequest.
 * Full backups always use `allAgentIds`, regardless of an earlier partial
 * selection that may still be present in component state.
 */
export function buildScope(
  backupMode: "full" | "partial",
  selectedAgents: string[],
  globalConfig: boolean,
  includeSkillPool: boolean,
  includeSecrets: boolean,
  allAgentIds: string[],
): { scope: BackupScope; agents: string[] } {
  const includeAgents = backupMode === "full" || selectedAgents.length > 0;
  return {
    scope: {
      include_agents: includeAgents,
      include_global_config: backupMode === "full" ? true : globalConfig,
      include_secrets: backupMode === "full" ? true : includeSecrets,
      include_skill_pool: backupMode === "full" ? true : includeSkillPool,
      sanitized: backupMode !== "full",
    },
    agents: includeAgents
      ? backupMode === "full"
        ? allAgentIds
        : selectedAgents
      : [],
  };
}
