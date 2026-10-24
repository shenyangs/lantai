import { request } from "../request";

export interface ToolGuardRule {
  id: string;
  tools: string[];
  params: string[];
  category: string;
  severity: string;
  patterns: string[];
  exclude_patterns: string[];
  description: string;
  remediation: string;
}

export type ToolPermissionMode =
  | "default"
  | "plan"
  | "accept_edits"
  | "quiet"
  | "full_auto";

export interface ToolGuardCommandRule {
  id: string;
  enabled: boolean;
  effect: "allow" | "ask" | "deny";
  tool_name: string;
  command_pattern: string;
  path_pattern: string;
  description: string;
  source?: string;
}

export interface RememberedApproval {
  id: string;
  enabled: boolean;
  scope: "workspace";
  workspace_dir: string;
  tool_name: string;
  permission_profile: string;
  matched_rule_ids: string[];
  command_head: string;
  command_pattern?: string;
  path_pattern?: string;
  risk_level: string;
  approval_copy: string;
  policy_decision: Record<string, unknown>;
  source_agent_id: string;
  created_at: string;
  updated_at: string;
}

export interface ToolGuardConfig {
  enabled: boolean;
  permission_mode?: ToolPermissionMode;
  default_workspace_dir?: string;
  guarded_tools: string[] | null;
  denied_tools: string[];
  custom_rules: ToolGuardRule[];
  command_rules?: ToolGuardCommandRule[];
  disabled_rules: string[];
  auto_denied_rules: string[];
  remembered_approvals?: RememberedApproval[];
  shell_evasion_checks: Record<string, boolean>;
}

export interface RememberedApprovalsResponse {
  remembered_approvals: RememberedApproval[];
  count: number;
}

export interface RememberedApprovalsMutationResponse {
  removed: number;
  count: number;
}

export interface RememberedApprovalDeleteResponse {
  removed: boolean;
  rule_id: string;
  count: number;
}

// ── File Guard types ──────────────────────────────────────────────

export interface FileGuardResponse {
  enabled: boolean;
  paths: string[];
}

export interface FileGuardUpdateBody {
  enabled?: boolean;
  paths?: string[];
}

// ── Skill Scanner types ────────────────────────────────────────────

export interface SkillScannerWhitelistEntry {
  skill_name: string;
  content_hash: string;
  added_at: string;
}

export type SkillScannerMode = "block" | "warn" | "off";

export interface SkillScannerConfig {
  mode: SkillScannerMode;
  timeout: number;
  whitelist: SkillScannerWhitelistEntry[];
}

export interface BlockedSkillFinding {
  severity: string;
  title: string;
  description: string;
  file_path: string;
  line_number: number | null;
  rule_id: string;
}

export interface BlockedSkillRecord {
  skill_name: string;
  blocked_at: string;
  max_severity: string;
  findings: BlockedSkillFinding[];
  content_hash: string;
  action: "blocked" | "warned";
}

export interface SecurityScanErrorResponse {
  type: "security_scan_failed";
  detail: string;
  skill_name: string;
  max_severity: string;
  findings: BlockedSkillFinding[];
}

// ── Allow No Auth Hosts types ──────────────────────────────────────

export interface AllowNoAuthHostsResponse {
  hosts: string[];
}

export interface AllowNoAuthHostsUpdateBody {
  hosts: string[];
}

export const securityApi = {
  // ── Tool Guard ──────────────────────────────────────────────────

  getToolGuard: () => request<ToolGuardConfig>("/config/security/tool-guard"),

  updateToolGuard: (body: ToolGuardConfig) =>
    request<ToolGuardConfig>("/config/security/tool-guard", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  getBuiltinRules: () =>
    request<ToolGuardRule[]>("/config/security/tool-guard/builtin-rules"),

  getRememberedApprovals: (workspaceDir?: string) => {
    const query = workspaceDir
      ? `?workspace_dir=${encodeURIComponent(workspaceDir)}`
      : "";
    return request<RememberedApprovalsResponse>(
      `/config/security/tool-guard/remembered-approvals${query}`,
    );
  },

  deleteRememberedApproval: (ruleId: string) =>
    request<RememberedApprovalDeleteResponse>(
      `/config/security/tool-guard/remembered-approvals/${encodeURIComponent(
        ruleId,
      )}`,
      { method: "DELETE" },
    ),

  clearRememberedApprovals: (workspaceDir?: string) => {
    const query = workspaceDir
      ? `?workspace_dir=${encodeURIComponent(workspaceDir)}`
      : "";
    return request<RememberedApprovalsMutationResponse>(
      `/config/security/tool-guard/remembered-approvals${query}`,
      { method: "DELETE" },
    );
  },

  // ── File Guard ─────────────────────────────────────────────────

  getFileGuard: () => request<FileGuardResponse>("/config/security/file-guard"),

  updateFileGuard: (body: FileGuardUpdateBody) =>
    request<FileGuardResponse>("/config/security/file-guard", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // ── Skill Scanner ───────────────────────────────────────────────

  getSkillScanner: () =>
    request<SkillScannerConfig>("/config/security/skill-scanner"),

  updateSkillScanner: (body: SkillScannerConfig) =>
    request<SkillScannerConfig>("/config/security/skill-scanner", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  getBlockedHistory: () =>
    request<BlockedSkillRecord[]>(
      "/config/security/skill-scanner/blocked-history",
    ),

  clearBlockedHistory: () =>
    request<{ cleared: boolean }>(
      "/config/security/skill-scanner/blocked-history",
      { method: "DELETE" },
    ),

  removeBlockedEntry: (index: number) =>
    request<{ removed: boolean }>(
      `/config/security/skill-scanner/blocked-history/${index}`,
      { method: "DELETE" },
    ),

  addToWhitelist: (skillName: string, contentHash: string = "") =>
    request<{ whitelisted: boolean; skill_name: string }>(
      "/config/security/skill-scanner/whitelist",
      {
        method: "POST",
        body: JSON.stringify({
          skill_name: skillName,
          content_hash: contentHash,
        }),
      },
    ),

  removeFromWhitelist: (skillName: string) =>
    request<{ removed: boolean; skill_name: string }>(
      `/config/security/skill-scanner/whitelist/${encodeURIComponent(
        skillName,
      )}`,
      { method: "DELETE" },
    ),

  // ── Allow No Auth Hosts ─────────────────────────────────────────

  getAllowNoAuthHosts: () =>
    request<AllowNoAuthHostsResponse>("/config/security/allow-no-auth-hosts"),

  updateAllowNoAuthHosts: (body: AllowNoAuthHostsUpdateBody) =>
    request<AllowNoAuthHostsResponse>("/config/security/allow-no-auth-hosts", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};
